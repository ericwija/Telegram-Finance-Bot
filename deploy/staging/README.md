# V2 staging: PostgreSQL 18 runbook

Compose project **`finbot-staging`** (D18). This step (Phase 1 Step 3A) contains the database
service only; `migrate`, `bot` and `backup` are added in later steps (doc 04 §8.1). Staging holds
**synthetic data only**. Production data never enters it (doc 04 §8.3).

> Nothing in this file has been executed yet. Bring-up needs owner approval. D24 (approved
> 2026-10-24): staging runs on the existing Contabo VPS.

**Who runs what** (owner decision, D24):

| Account | Role | Never |
|---|---|---|
| `claude` | development user: repository, tests, `make check`, `make staging-config` | has no Docker access and no sudo; does not read secret files |
| `deploy` | the only Docker operator: every `docker` / `docker compose` command below | does not edit the repository on the server outside a reviewed checkout |
| root (sudo) | §2 only: host group and secret files | — |

The VPS also runs the unrelated `services` stack (`services-postgres`, `services-redis`, network
`services_default`, volumes `services_*`). Nothing here touches it; never pass `services_*`
names to any command in this runbook.

## 1. What runs

| Item | Value |
|---|---|
| Compose file | `deploy/staging/compose.yml` (project name fixed inside the file) |
| Image | `postgres:18.6-trixie@sha256:edb0c08d…1ddb6` (pinned version + multi-arch index digest) |
| Container | `finbot-staging-postgres`, uid:gid `2999:2999`, read-only root FS, `cap_drop: ALL`, `no-new-privileges` |
| Data | named volume `finbot-staging_pgdata` → `/var/lib/postgresql`; `PGDATA=/var/lib/postgresql/18/docker` (PG 18 layout) |
| Network | `finbot-staging_db`, `172.31.250.0/27`, `internal: true` + gateway mode `isolated`: no egress, no bridge address on the host, so neither host processes nor other projects' containers can reach it, and the container cannot reach host services |
| Published ports | **none** (AT-DEP-01). There is no host access; use `docker exec` (§5) |
| Database / roles | `finbot_staging` owned by `finbot_owner`; `finbot_app`, `finbot_backup`, `finbot_readonly` (NOLOGIN) |
| Limits | 1 CPU, 512 MiB memory (no swap on this host), 256 PIDs, `shm_size` 128 MiB; logs json-file 10 MiB × 5 |
| Auth | SCRAM-SHA-256 only, `pg_hba.conf` from `deploy/staging/postgres/`: superuser in-container only; app roles only to `finbot_staging` from `172.31.250.0/27` or in-container loopback; gateway and everything else rejected |
| Health | healthy only when `/var/lib/postgresql/18/finbot-init.done` exists (written last by `00-roles.sh`) **and** `pg_isready` succeeds over TCP |

Coexistence: V1 uses the root `docker-compose.yml` (container `finance-bot`, volume
`<project>_bot-data`). No V2 name overlaps, so no V2 command can address V1 objects. Always pass
`-f deploy/staging/compose.yml`; never run bare `docker compose` from the repository root (that
targets V1).

## 2. One-time host preparation (root)

The postgres container runs as uid/gid **2999**, which must stay unused on the host. Compose
"secrets" from files are bind mounts that keep host ownership and mode (Compose ignores
`uid`/`gid`/`mode` for file secrets), so the files must be readable by gid 2999 and nobody
else. **Do not** `chown 999`: on this VPS uid 999 is the `caddy` user.

```bash
sudo groupadd --system --gid 2999 finbot-staging   # reserves the gid; no members
sudo install -d -m 0750 -o root -g finbot-staging deploy/secrets/staging
for name in db_superuser_password db_owner_password db_app_password db_backup_password; do
  # 32 random bytes, URL-safe, single line, never printed to the terminal.
  sudo sh -c "umask 0137 && openssl rand -base64 32 | tr -d '\n=' | tr '+/' '-_' > deploy/secrets/staging/$name"
  sudo chown root:finbot-staging "deploy/secrets/staging/$name"   # mode 0440 from umask
done
sudo ls -l deploy/secrets/staging    # expect -r--r----- root finbot-staging, never cat the files
```

`deploy/secrets/` is git-ignored; `tests/deploy` fails if anything under it is tracked. Store
copies of the staging passwords in the password manager (staging vault, separate from
production).

## 3. Start

```bash
F=deploy/staging/compose.yml
docker compose -f $F config --quiet               # syntax + secrets resolution, starts nothing
docker compose -f $F pull postgres                # fetches exactly the pinned digest
docker compose -f $F up -d postgres
docker compose -f $F ps                           # wait for "healthy" (init ≈ 10–30 s)
docker compose -f $F logs --tail 50 postgres      # expect "00-roles.sh: roles and database finbot_staging created"
```

First start on an empty volume runs `initdb` (data checksums on, builtin `C.UTF-8` locale,
SCRAM) and `deploy/postgres/initdb/00-roles.sh`. The entrypoint never runs init scripts again
once the cluster exists, so later edits to the script have no effect on an existing cluster.

**If the first initialisation fails half-way** (for example, an unreadable secret file), the
container exits, restarts, finds an existing cluster and skips the init scripts. It then runs but
stays **unhealthy** because the marker file is missing. Fix the cause, then either:

```bash
# (a) repair in place: the script is idempotent (creates what is missing, re-applies attributes
#     and passwords from the secret files, verifies, then writes the marker)
docker exec finbot-staging-postgres /docker-entrypoint-initdb.d/00-roles.sh
# (b) or, while no data exists yet, start from scratch (owner approval; this volume only)
docker compose -f deploy/staging/compose.yml down && docker volume rm finbot-staging_pgdata
```

## 4. Verify (after every start or change)

```bash
docker port finbot-staging-postgres                                      # must print nothing
docker inspect -f '{{json .NetworkSettings.Ports}}' finbot-staging-postgres # {} or only "5432/tcp":null
ss -tlnp | grep -c ':5432 '        # unchanged vs. before bring-up (the other stack's 127.0.0.1:5432)
docker network inspect -f '{{.Internal}} {{index .Options "com.docker.network.bridge.gateway_mode_ipv4"}}' finbot-staging_db   # true isolated
ip -4 addr show | grep -c '172\.31\.250\.'    # 0: the host has no address on the staging network
IP=$(docker inspect -f '{{(index .NetworkSettings.Networks "finbot-staging_db").IPAddress}}' finbot-staging-postgres)
timeout 3 bash -c "</dev/tcp/$IP/5432" && echo "UNEXPECTED: reachable from host" || echo "ok: not reachable from host"
docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' finbot-staging_db   # only finbot-staging-postgres
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'finance-bot|finbot-staging'
```

Role check, inside the container (prompts for the superuser password; nothing is echoed):

```bash
docker exec -it finbot-staging-postgres psql -h 127.0.0.1 -U postgres -d finbot_staging -c "
  SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin
  FROM pg_roles WHERE rolname LIKE 'finbot\_%' ORDER BY 1;
  SELECT datname, pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname='finbot_staging';
  SHOW data_checksums; SHOW server_version;"
```

Expected: `finbot_app` has every flag `f` except `rolcanlogin`; owner is `finbot_owner`;
`data_checksums = on`; `server_version = 18.6…`.

## 5. Day-to-day access

There is no host port. Connect from inside the container (superuser, break-glass only) or, from
Step 4 on, from containers attached to `finbot-staging_db` as the application roles.

## 6. Stop

```bash
docker compose -f deploy/staging/compose.yml stop postgres   # SIGINT = fast shutdown, 60 s grace
docker compose -f deploy/staging/compose.yml down            # removes container + network, KEEPS the volume
```

**Never** use `down -v` or `docker volume prune` on this host: they delete data volumes (and
`prune` would also hit the other project's volumes). `restart: unless-stopped` brings the
container back after a reboot unless it was stopped explicitly.

## 7. Backup and rollback

Until the Phase 6 backup service exists, staging backups are manual and contain synthetic data
only. Take one before any image change or migration:

```bash
umask 077; mkdir -p ~/finbot-staging-backups
docker exec finbot-staging-postgres sh -c 'PGPASSWORD="$(cat /run/secrets/db_backup_password)" \
  pg_dump -h 127.0.0.1 -U finbot_backup -d finbot_staging -Fc' \
  > ~/finbot-staging-backups/finbot_staging_$(date -u +%Y%m%dT%H%M%SZ).dump
```

The password is read inside the container and passed through the environment of `pg_dump`
only (never argv). `pg_hba.conf` allows the application roles on the container's loopback
address, so `finbot_backup` can log in here. The dump file is plaintext synthetic data: keep it in the
0700 directory and delete it when no longer needed.

| Situation | Rollback |
|---|---|
| Bring-up fails before any data exists | `down`, then `docker volume rm finbot-staging_pgdata` (this volume only, owner approval), fix, start again |
| Bad config change (`command`, limits, hba) | Revert the commit, `up -d postgres` (the volume is untouched) |
| Minor image update (18.x → 18.y) | Backup first; on failure, revert the digest in the PR and `up -d`. Same major only: the data format is compatible |
| Major upgrade (19+) | Never by changing the tag. Separate written plan (dump/restore or `pg_upgrade`); the PG 18 entrypoint refuses to start on data from another layout |
| Staging data corrupted | Restore the last dump into a fresh volume, or recreate from scratch (synthetic data) |

V1 is unaffected by every row above.

## 8. Password rotation

`log_statement=ddl` logs DDL text, so a rotation must disable statement logging in the same
session, and the new value must not appear on a command line:

```sql
SET log_statement = 'none';
\password finbot_app      -- psql prompts and sends only the SCRAM verifier
```

Then replace the secret file (same owner and mode) and restart the consumers.

## 9. Resource estimate (this VPS: 4 vCPU, 7.8 GiB RAM, no swap, 62 GiB free disk)

| Resource | Staging PostgreSQL |
|---|---|
| Memory | idle ≈ 60–120 MiB; hard cap 512 MiB (shared_buffers 128 MiB, max_connections 30) |
| CPU | idle ≈ 0; cap 1 vCPU |
| Disk | image ≈ 450 MB; empty cluster ≈ 50 MB; WAL up to `max_wal_size` (1 GB default); logs ≤ 50 MiB |
