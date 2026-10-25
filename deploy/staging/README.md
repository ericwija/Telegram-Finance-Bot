# V2 staging runbook: PostgreSQL 18, migrations and the Telegram bot

Compose project **`finbot-staging`** (D18). Services: `postgres` (Step 3A, running), `migrate`
(one-shot, operator-run only) and `bot` (Step 5A). The `backup` service follows in Phase 6
(doc 04 §8.1). Staging holds **synthetic data only**. Production data never enters it (doc 04
§8.3).

> §1–§5 describe the running database (Step 3A). §6–§11 (image, migrations, admin bootstrap,
> bot, onboarding test) have **not been executed yet**; each needs owner approval. D24
> (approved 2026-10-24): staging runs on the existing Contabo VPS.

**Who runs what** (owner decision, D24):

| Account | Role | Never |
|---|---|---|
| `claude` | development user: repository, tests, `make check`, `make staging-config` | has no Docker access and no sudo; does not read secret files; never runs §6–§11 |
| `deploy` | the only Docker operator: every `docker` / `docker compose` command below | does not edit the repository on the server outside a reviewed checkout |
| root (sudo) | §2 only: host group and secret files | — |
| owner | approves each of §6–§11, provides the staging bot token and username, is the first platform admin | does not share the bot token with V1 or production |

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
| App image | `finbot-v2:staging` (plus `finbot-v2:staging-<git sha>`), built locally from `deploy/docker/Dockerfile` with a digest-pinned Python 3.13 base (§6); `pull_policy: never`, so compose never pulls or builds it |
| `migrate` | one-shot `finbot-migrate`, profile `ops`: **never** started by `up`; run only as `run --rm migrate …` (§7, §8). uid:gid `2997:2999`; db network only; holds only `db_owner_password` |
| `bot` | container `finbot-staging-bot`, `finbot bot --health-file /run/finbot/heartbeat.json`, **one replica**, uid:gid `2998:2999`, `init: true`, `restart: unless-stopped`, SIGTERM with a 75 s grace period; networks `finbot-staging_db` + `finbot-staging_egress` (`172.31.250.32/27`, the only route out); holds only `db_app_password` and `telegram_token`; read-only root FS, `cap_drop: ALL`, 0.5 CPU, 256 MiB |
| Bot health | `finbot healthcheck` on the heartbeat file: healthy only while the bot holds the single-poller lock and polls, with a heartbeat younger than 60 s (doc 04 §5.8) |
| Bot start-up guards | G-ENV-1/5 (configuration), G-ENV-2 (`current_database()` = `finbot_staging`), G-ENV-3 (`platform_meta` marker = `staging`), G-ENV-6 (poller lock), G-ENV-4 (`getMe` = expected username). Any failure: exit 3, nothing polled |

Coexistence: V1 uses the root `docker-compose.yml` (container `finance-bot`, volume
`<project>_bot-data`). No V2 name overlaps, so no V2 command can address V1 objects. Always pass
`-f deploy/staging/compose.yml`; never run bare `docker compose` from the repository root (that
targets V1).

## 2. One-time host preparation (root)

### 2.1 Database secrets

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

The application containers run as uid **2997** (`migrate`) and **2998** (`bot`) with gid 2999,
so they read the same `root:finbot-staging 0440` files. Each container sees only the secrets
compose mounts into it. Check that the uids are unused on the host (`getent passwd 2997 2998`
prints nothing).

### 2.2 Staging Telegram bot token (root, once per token)

The owner creates a **separate** staging bot with @BotFather (for example
`<name>_staging_bot`). Never use V1's token or the production token (D18; G-ENV-4 refuses the
wrong bot anyway). Enter the token without echoing it and without putting it on a command line
or in shell history:

```bash
sudo bash -c 'umask 0137
  IFS= read -rs -p "staging bot token: " t; echo
  printf "%s" "$t" > deploy/secrets/staging/telegram_token; unset t'
sudo chown root:finbot-staging deploy/secrets/staging/telegram_token
sudo ls -l deploy/secrets/staging/telegram_token     # -r--r----- root finbot-staging
```

Store it in the staging vault. Never `cat` the file, paste it in a chat or ticket, or pass it
as `-e` to `docker run`.

### 2.3 Staging bot username (owner, reviewed commit)

`FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME` in `deploy/staging/compose.yml` (service `bot`) is
**`CHANGE_ME_staging_bot`** until the owner names the staging bot. With the placeholder, the
bot refuses to start at G-ENV-4 (exit 3, "the bot token belongs to another bot"), so nothing is
served by mistake. Replace it with the real username (without `@`) in a reviewed commit on
`develop`; it is not a secret. The deployed checkout must contain that commit.

### 2.4 Secrets per service

| Secret file (`deploy/secrets/staging/`) | postgres | migrate | bot |
|---|---|---|---|
| `db_superuser_password` | ✓ | | |
| `db_owner_password` | ✓ | ✓ | |
| `db_app_password` | ✓ | | ✓ |
| `db_backup_password` | ✓ | | |
| `telegram_token` | | | ✓ |

`tests/deploy` enforces this table (S10). The bot never receives owner credentials, and
`finbot` refuses any `FINBOT_DB_OWNER_*` variable.

## 3. Start

```bash
F=deploy/staging/compose.yml
docker compose -f $F config --quiet               # syntax + secrets resolution, starts nothing
                                                  # (create every §2 secret file first, telegram_token included)
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
docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' finbot-staging_db   # finbot-staging-postgres (+ finbot-staging-bot once §9 ran)
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

There is no host port. Connect from inside the container (superuser, break-glass only; or
`finbot_owner` for the read-only checks in §7), or from containers attached to
`finbot-staging_db` as the application roles (`migrate`, `bot`).

## 6. Build the V2 image (deploy, from a reviewed checkout)

The image is built on the VPS from the reviewed `develop` commit; compose never builds or pulls
it (`pull_policy: never`). The Python base has **no default** in the Dockerfile: the build fails
unless an exact tag **and** digest are passed.

```bash
cd ~/Telegram-Finance-Bot                         # the deploy checkout (never edited by hand)
git fetch origin && git checkout --detach <reviewed commit on develop>
test -z "$(git status --porcelain)" || { echo "checkout not clean"; exit 1; }
REV=$(git rev-parse --short=12 HEAD)

# Resolve the base once per Python update and record it in the deployment log. Use the newest
# 3.13.x slim-trixie tag; copy the "Digest:" of the multi-arch index, not a per-platform one.
docker buildx imagetools inspect python:3.13.<x>-slim-trixie | head -n 5
BASE=python:3.13.<x>-slim-trixie@sha256:<64 hex from the line above>

docker build --pull -f deploy/docker/Dockerfile \
  --build-arg PYTHON_BASE="$BASE" \
  --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
  --label org.finbot.python-base="$BASE" \
  -t "finbot-v2:staging-$REV" -t finbot-v2:staging .

# Offline smoke tests: no network, read-only, no secrets.
docker run --rm --network none --read-only finbot-v2:staging finbot version
docker run --rm --network none --read-only -e FINBOT_ENV=dev finbot-v2:staging finbot bot --dry-run
docker image inspect -f '{{json .Config.Labels}} {{.Config.User}}' finbot-v2:staging
```

The build context is limited by `deploy/docker/Dockerfile.dockerignore` (an allow-list:
`pyproject.toml`, `uv.lock`, `README.md`, `alembic.ini`, `migrations/`, `src/`), so V1 files,
`.env` and `deploy/secrets/` never enter it. Dependencies come only from `uv.lock`
(`uv sync --frozen --no-dev`); uv itself is verified against the CI checksum. Keep the previous
`finbot-v2:staging-<sha>` tag until the new one is verified (§12.3).

## 7. Migrations 0001–0003 (operator-controlled, owner approval)

Migrations never run implicitly: `migrate` is in the `ops` profile, so `up` never starts it, and
the bot does not depend on it. The bot simply refuses to start (G-ENV-3, exit 3) while the
schema is missing. Downgrades are refused in staging (S9); recovery is a restore (§12.2).

Helper used below. It runs read-only checks as `finbot_owner` inside the postgres container;
the password is read there and reaches `psql` only through its environment:

```bash
F=deploy/staging/compose.yml
owner_psql() { docker exec -i finbot-staging-postgres sh -c \
  'PGPASSWORD="$(cat /run/secrets/db_owner_password)" exec psql -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -U finbot_owner -d finbot_staging'; }
```

### 7.1 Preflight (all must pass; stop at the first failure)

```bash
docker compose -f $F ps postgres                   # healthy
docker compose -f $F ps bot                        # not running (first deployment)
df -h /var/lib/docker | tail -1                    # ≥ 5 GiB free
docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' finbot-v2:staging
                                                   # = the reviewed commit
docker compose -f $F run --rm migrate check-config # "configuration OK"; password shown only as "set (file: …)"
docker compose -f $F run --rm migrate history      # exactly 0001 → 0002 → 0003 (plus later ones, if any)
docker compose -f $F run --rm migrate current      # first deployment: no revision
owner_psql <<'SQL'
SELECT current_database(), current_user, version();
SELECT count(*) AS tables_in_public FROM pg_tables WHERE schemaname = 'public';  -- first deployment: 0
SQL
```

### 7.2 Review the SQL (no connection)

```bash
umask 077; mkdir -p ~/finbot-staging-migrations
docker compose -f $F run --rm --no-deps migrate sql upgrade 0003 \
  > ~/finbot-staging-migrations/upgrade_to_0003_$REV.sql
grep -c 'finbot-migrate guard' ~/finbot-staging-migrations/upgrade_to_0003_$REV.sql   # ≥ 1
```

The script starts with the guard (lock timeout, advisory lock, database/role/marker checks;
doc 10 §3.2). Compare it with the reviewed revisions. It contains no credentials.

### 7.3 Backup, then apply

A backup is **required** immediately before every migration, even on an empty database (§12.1).
Note the file name in the deployment log. Then apply exactly up to `0003`:

```bash
docker compose -f $F run --rm migrate upgrade 0003; echo "exit $?"
```

Exit codes: `0` applied; `2` invalid configuration; `3` refused by policy or a guard (wrong
database, role, environment marker); `1` unexpected error. All pending revisions run in **one
transaction**, so any non-zero exit leaves the database exactly as it was: check with
`current`, fix the cause, and run again.

### 7.4 Verify

```bash
docker compose -f $F run --rm migrate current      # 0003
owner_psql <<'SQL'
SELECT version_num FROM public.alembic_version;                                   -- 0003
SELECT key, value FROM public.platform_meta ORDER BY key;                         -- environment | staging
-- Every table with workspace_id has RLS ENABLE + FORCE: expect 0 rows.
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r'
   AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
               AND a.attname = 'workspace_id' AND NOT a.attisdropped)
   AND NOT (c.relrowsecurity AND c.relforcerowsecurity);
-- The bot role: no direct access to identities/invites, no bootstrap; redeem only. Expect f f f t.
SELECT has_table_privilege('finbot_app', 'public.telegram_identities', 'SELECT'),
       has_table_privilege('finbot_app', 'public.onboarding_invites', 'SELECT'),
       has_function_privilege('finbot_app', 'public.bootstrap_platform_admin(bigint,text)', 'EXECUTE'),
       has_function_privilege('finbot_app', 'public.redeem_onboarding_invite(bytea,bigint,text)', 'EXECUTE');
SQL
```

If any check differs, do not start the bot. Record the outcome in the deployment log.

## 8. Bootstrap the first platform admin (once, owner approval)

Run only after §7.4 passed. This is **not** a migration and never runs automatically. The
Telegram user ID is the owner's numeric ID (not the @username); there is no default.

```bash
docker compose -f $F run --rm migrate bootstrap-admin \
  --telegram-user-id <owner numeric Telegram ID> --display-name "<display name>"; echo "exit $?"
```

Exit 0 prints only the new user ID. Exit 3 means refused: already bootstrapped, account already
linked, wrong environment, schema older than `0003`, or invalid input. The bootstrap is
permanent: a second run is always refused (doc 10 §11). Verify:

```bash
owner_psql <<'SQL'
SELECT key FROM public.platform_meta WHERE key = 'platform_admin_bootstrapped';   -- 1 row
SELECT action, count(*) FROM public.audit_events GROUP BY action ORDER BY action;
-- platform_admin.bootstrapped 1, user.onboarded 1, identity.linked 1, workspace.created 1
SQL
```

## 9. Start the bot (owner approval)

Preconditions: §2.2 token file, §2.3 username committed in the deployed checkout, §6 image,
§7.4 verified. §8 is needed before any invite can be created.

```bash
docker compose -f $F up -d bot                     # starts only bot (postgres is already up); never migrate
docker compose -f $F ps bot                        # "healthy" within ~60 s
docker compose -f $F logs --tail 50 bot            # poller_lock.acquired, bot.polling_started; no "refused:"
docker exec finbot-staging-bot finbot healthcheck --health-file /run/finbot/heartbeat.json --json
docker inspect -f 'exit={{.State.ExitCode}} restarts={{.RestartCount}}' finbot-staging-bot   # restarts=0
docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' finbot-staging_egress     # finbot-staging-bot only
```

Start-up order: G-ENV-2 (database name) → G-ENV-3 (marker) → G-ENV-6 (poller lock) → G-ENV-4
(`getMe`) → polling. A refusal prints `refused: <reason>` and exits 3; `restart:
unless-stopped` then retries with Docker's backoff, so fix the cause and `up -d bot` again:

| `refused:` message mentions | Cause | Fix |
|---|---|---|
| `G-ENV-2` | connected to another database | `FINBOT_DB_NAME` / `FINBOT_DB_HOST` in compose |
| `G-ENV-3` or `database environment check failed` | schema missing (§7 not done) or wrong marker | run §7; never edit the marker |
| `poller lock held` | another bot process for staging is running | find and stop it (`docker ps`); never work around the lock |
| `G-ENV-4` | token of another bot, or the `CHANGE_ME_staging_bot` placeholder | §2.2 / §2.3 |
| `Telegram getMe failed (InvalidToken)` | token revoked or mistyped | §2.2 |
| `configuration INVALID` (exit 2) | missing secret file, wrong permissions, unknown variable | the message names the variable and path, never a value |

Exit 4 (`stopped: poller lock lost`) means the bot lost its database session while polling and
stopped polling; Docker restarts it and it must win the lock again. Repeated exit 4 points at
database or network trouble (§4).

**Exactly one poller (AC-P1-04).** Prove that a second instance is refused before it contacts
Telegram:

```bash
docker compose -f $F run --rm --no-deps bot; echo "exit $?"
# refused: poller lock held by another process for environment 'staging' (G-ENV-6)   exit 3
```

Never `--scale bot=N`, never a second checkout or compose project with this database, and never
a local `finbot bot` against staging.

**Graceful stop and restart check:**

```bash
docker compose -f $F stop bot                       # SIGTERM; finishes within the 75 s grace period
docker compose -f $F logs --tail 5 bot              # bot.stopped outcome=stopped, poller_lock.released
docker inspect -f 'exit={{.State.ExitCode}}' finbot-staging-bot     # exit=0
docker compose -f $F up -d bot && docker compose -f $F ps bot       # healthy again (lock re-acquired)
```

## 10. Test invite-only onboarding (synthetic accounts only)

Use the owner's account (the platform admin from §8) and two **test** Telegram accounts, A and
B, that are not onboarded. Never invite real users to staging.

| # | Who | Action | Expected |
|---|---|---|---|
| 1 | B | DM the staging bot `/start` | "Bot ini hanya untuk pengguna yang diundang…"; nothing is created |
| 2 | admin | `/start` | "Selamat datang kembali." with workspace **Pribadi** |
| 3 | admin | `/undangan_baru tester A` | A one-time `t.me/<bot>?start=onb_…` link, the invite ID and the expiry (7 days). Forward it privately to A only |
| 4 | A | open the link (Start) | "Selamat datang! Akun Anda sudah aktif." with workspace **Pribadi** |
| 5 | A | `/start` again | "Selamat datang kembali." (no second account) |
| 6 | B | open the same link | "Tautan undangan tidak valid atau sudah tidak berlaku…" (single use) |
| 7 | admin | `/undangan_daftar semua` | the invite with status `terpakai` (used) |
| 8 | admin | `/undangan_baru tester B`, then `/undangan_cabut <ID>` | revoked; B opening that link gets the step-6 refusal |
| 9 | A | `/undangan_baru` | "Anda tidak memiliki izin untuk tindakan ini." |
| 10 | admin | add the bot to a test group | the bot leaves the group at once and answers nothing there |

The CLI equivalent of step 3, if Telegram is unavailable to the admin, prints the link **once**
on your terminal; never paste it into tickets or shared chats:
`docker exec -it finbot-staging-bot finbot-admin invite create --as <admin Telegram ID> --note "tester A"`.

Verify the audit trail, then check the logs for leaks (both must print nothing):

```bash
owner_psql <<'SQL'
SELECT action, count(*) FROM public.audit_events GROUP BY action ORDER BY action;
-- expect onboarding_invite.created 2, onboarding_invite.redeemed 1, onboarding_invite.revoked 1,
-- onboarding_invite.failed ≥ 2, user.onboarded 2, identity.linked 2, workspace.created 2
SQL
docker compose -f $F logs bot | grep -E 'onb_[A-Za-z0-9_-]{8,}|[0-9]{8,10}:[A-Za-z0-9_-]{30,}'
docker compose -f $F logs bot | grep -ci 'tester'    # 0: notes and message texts are never logged
```

## 11. Stop and remove

```bash
docker compose -f deploy/staging/compose.yml stop bot        # SIGTERM = graceful, 75 s grace; releases the poller lock
docker compose -f deploy/staging/compose.yml stop postgres   # SIGINT = fast shutdown, 60 s grace
docker compose -f deploy/staging/compose.yml down            # removes containers + networks, KEEPS the volume
```

`up -d` without a service name starts `postgres` and `bot`, never `migrate`. Stopping the bot
does not affect V1 (container `finance-bot`, a different token).

**Never** use `down -v` or `docker volume prune` on this host: they delete data volumes (and
`prune` would also hit the other project's volumes). `restart: unless-stopped` brings the
container back after a reboot unless it was stopped explicitly.

## 12. Backup, restore and rollback

### 12.1 Backup

Until the Phase 6 backup service exists, staging backups are manual and contain synthetic data
only. Take one before any image change, migration or admin bootstrap:

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

### 12.2 Restore (or reset) the database (owner approval)

There are no downgrades in staging (S9). To undo a migration or a bootstrap, recreate the
database and either restore the backup taken just before it, or re-run §7 and §8 from an empty
database:

```bash
F=deploy/staging/compose.yml
docker compose -f $F stop bot
docker exec -i finbot-staging-postgres sh -c \
  'PGPASSWORD="$(cat /run/secrets/db_superuser_password)" exec psql -X -v ON_ERROR_STOP=1 -h 127.0.0.1 -U postgres -d postgres' <<'SQL'
DROP DATABASE finbot_staging WITH (FORCE);
SQL
# Recreates the empty database owned by finbot_owner, re-verifies the roles, rewrites the marker.
docker exec finbot-staging-postgres /docker-entrypoint-initdb.d/00-roles.sh
docker compose -f $F ps postgres                   # healthy again
# Either restore the pre-change backup ...
docker exec -i finbot-staging-postgres sh -c \
  'PGPASSWORD="$(cat /run/secrets/db_owner_password)" exec pg_restore -h 127.0.0.1 -U finbot_owner -d finbot_staging --no-owner --exit-on-error --single-transaction' \
  < ~/finbot-staging-backups/<file>.dump
# ... or leave it empty and repeat §7 (and §8).
```

Then repeat the §7.4 checks before `up -d bot`. `DROP DATABASE` touches only `finbot_staging`;
the volume, the roles and every other database stay.

### 12.3 Rollback table

| Situation | Rollback |
|---|---|
| Bot misbehaves | `stop bot` (V1 unaffected). If the token may have leaked, revoke it in @BotFather and rotate it (§13) |
| Bad app image | `docker tag finbot-v2:staging-<previous sha> finbot-v2:staging && docker compose -f $F up -d bot`. Only if that code supports the current schema (expand/contract) |
| `migrate upgrade` exited non-zero | Nothing was applied (one transaction). `current` confirms; fix and re-run §7.3 |
| A migration applied but is wrong | Forward-fix revision (reviewed), or §12.2 with the pre-migration backup. Never `downgrade` or `stamp` (refused in staging) |
| Wrong admin bootstrapped | The marker is permanent: §12.2 with the pre-bootstrap backup (or empty + §7), then §8 again |
| Bring-up fails before any data exists | `down`, then `docker volume rm finbot-staging_pgdata` (this volume only, owner approval), fix, start again |
| Bad config change (`command`, limits, hba) | Revert the commit, `up -d postgres` (the volume is untouched) |
| Minor image update (18.x → 18.y) | Backup first; on failure, revert the digest in the PR and `up -d`. Same major only: the data format is compatible |
| Major upgrade (19+) | Never by changing the tag. Separate written plan (dump/restore or `pg_upgrade`); the PG 18 entrypoint refuses to start on data from another layout |
| Staging data corrupted | Restore the last dump into a fresh volume, or recreate from scratch (synthetic data) |

V1 is unaffected by every row above.

## 13. Password and token rotation

`log_statement=ddl` logs DDL text, so a rotation must disable statement logging in the same
session, and the new value must not appear on a command line:

```sql
SET log_statement = 'none';
\password finbot_app      -- psql prompts and sends only the SCRAM verifier
```

Then replace the secret file (same owner and mode) and restart the consumers
(`docker compose -f deploy/staging/compose.yml restart bot` for `db_app_password`).

**Telegram token:** in @BotFather, `/revoke` the staging bot's token (the old one stops working
at once), write the new one with §2.2, then `docker compose -f deploy/staging/compose.yml
restart bot`. The username stays the same, so G-ENV-4 still passes.

## 14. Resource estimate (this VPS: 4 vCPU, 7.8 GiB RAM, no swap, 62 GiB free disk)

| Resource | Staging PostgreSQL | Staging bot (estimate, not measured yet) |
|---|---|---|
| Memory | idle ≈ 60–120 MiB; hard cap 512 MiB (shared_buffers 128 MiB, max_connections 30) | hard cap 256 MiB |
| CPU | idle ≈ 0; cap 1 vCPU | cap 0.5 vCPU |
| Disk | image ≈ 450 MB; empty cluster ≈ 50 MB; WAL up to `max_wal_size` (1 GB default); logs ≤ 50 MiB | image (slim base + venv); logs ≤ 50 MiB |
| Connections | `max_connections` 30 | ≤ 10 as `finbot_app` (1 poller lock + ≤ 8 concurrent updates; role limit 20) |
