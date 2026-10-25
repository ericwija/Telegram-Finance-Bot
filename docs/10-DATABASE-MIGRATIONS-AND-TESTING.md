# 10 — Database Migrations & Database Testing

| | |
|---|---|
| **Status** | Phase 1 Step 3B.2 (tooling: migration environment, credential separation, test harness, CI workflow) **Step 3B.3** (revision `0001`, the Phase 1 baseline; §9) and **Step 4A** (revision `0002`, identity and onboarding; §10). Step 4A awaits owner review |
| **Date** | 2026-10-24 |
| **Decisions** | S1–S11 approved 2026-10-24 (§8); D9, D18, D24 (doc 01 §8) |
| **Related** | [04 Architecture](04-LOGICAL-ARCHITECTURE.md) §8 · [05 Data Model](05-DATA-MODEL.md) §8, §11 · [07 Tests](07-TEST-AND-ACCEPTANCE-PLAN.md) · [09 Security](09-TENANT-ISOLATION-AND-SECURITY.md) |

## 1. Overview

| Piece | Where | Purpose |
|---|---|---|
| Alembic config | `alembic.ini` | Script location and file template only. **No URL, no credentials** |
| Environment | `migrations/env.py` → `finbot.infra.db.migration_runtime.run_env` | All policy (below), typed and unit-tested |
| Revisions | `migrations/versions/NNNN_<slug>.py` | Hand-written SQL; empty until Step 3B.3 |
| Entry point | `finbot-migrate` (`finbot.adapters.cli.migrate`) | The only V2 program that reads `finbot_owner` credentials |
| Settings | `finbot.migration_settings.MigrationSettings` | Owner role, password file only, downgrade policy |
| Connections | `finbot.infra.db.connection` | SQLAlchemy 2.x async + asyncpg; the password never appears in a URL |
| Test harness | `tests/support/pg.py`, `tests/plugins/db.py` | Disposable PostgreSQL 18.6 clusters, per-test databases, skip detection |
| CI | `.github/workflows/ci.yml` | Lint/type/unit job plus a database job on the pinned staging image |

## 2. Configuration and credentials (S10)

The application and the migration entry point read **disjoint** sets of `FINBOT_*` variables. Each one refuses the other's secrets, so a misconfigured container fails at start instead of running with too much privilege.

| Variable | `finbot` (bot) | `finbot-migrate` | Notes |
|---|---|---|---|
| `FINBOT_ENV` | required | required | `dev`, `test`, `staging`, `production`, `drill` |
| `FINBOT_LOG_LEVEL` | ✓ | ✓ | |
| `FINBOT_DB_HOST`, `FINBOT_DB_PORT` | ✓ | ✓ | `finbot-migrate` also accepts an absolute Unix-socket directory as the host, in `dev`/`test` only |
| `FINBOT_DB_NAME` | ✓ | ✓ | Must equal `finbot_<env>` (`finbot_prod`). In `test`, migrations also accept `finbot_test_<suffix>` (per-test databases) |
| `FINBOT_DB_APP_USER`, `FINBOT_DB_APP_PASSWORD_FILE` | ✓ | **refused** | The app role never runs migrations |
| `FINBOT_TELEGRAM_*`, `FINBOT_GEMINI_*` | ✓ | **refused** | |
| `FINBOT_DB_OWNER_PASSWORD_FILE` | **refused** | **required to connect** | Absolute path, regular file, not world-readable, ≤ 64 KiB, in **every** environment |
| `FINBOT_DB_OWNER_PASSWORD` (plaintext) | **refused** | **refused** | Never accepted anywhere |
| `FINBOT_DB_OWNER_USER` | **refused** | optional | Must be `finbot_owner` |
| `FINBOT_MIGRATION_ALLOW_DOWNGRADE` | **refused** | optional | Allowed only in `dev`/`test`/`drill`; an error elsewhere (S9) |
| `FINBOT_DB_BACKUP_*`, `FINBOT_DB_SUPERUSER_*` | **refused** | **refused** | Backup job / database container only |
| Unknown `FINBOT_*` | refused | refused | Typos fail loudly |

Error messages name the variable and the file path, never a value. `finbot-migrate check-config [--json]` prints a redacted summary (the password appears only as "set (file: …)").

**Containers (Step 5).** The `migrate` service receives only the `db_owner_password` secret and the variables above; the `bot` service never receives it. `tests/deploy/test_staging_compose.py::test_owner_and_application_credentials_never_share_a_service` enforces this as soon as those services are added to `deploy/staging/compose.yml`.

## 3. Migration workflow

### 3.1 Rules

1. **Manual revisions only.** Create one with a deterministic id; `--autogenerate` is refused by `env.py`:
   ```bash
   uv run alembic revision --rev-id 0002 -m "short description"   # → migrations/versions/0002_short_description.py
   ```
2. **Policy, checked before every run.** Ids are `0001`, `0002`, … with no gaps; file names are `NNNN_<lowercase_slug>.py`; one linear chain (each `down_revision` is the previous id; no branches, merges or `depends_on`).
3. **Hand-written SQL** through `op.execute()`. No ORM metadata, no autogenerate. The template (`migrations/script.py.mako`) restates the tenant-table checklist (CLAUDE.md rule 12).
4. **Never in a migration:** creating roles, setting passwords, or copying data from a real environment. Roles come from `deploy/postgres/initdb/00-roles.sh` (doc 04 §8.2); Alembic creates schema objects and **grants** as `finbot_owner`.
5. **One transaction.** All pending revisions run in a single transaction (`transaction_per_migration=False`), so any failure rolls back the whole run.

### 3.2 What runs first: the guard

Inside the migration transaction, before any revision, `env.py` executes:

1. `SET LOCAL lock_timeout = '10s'`, so a migration never queues indefinitely behind traffic;
2. `pg_advisory_xact_lock(…)`, one migrator at a time;
3. a `DO` block that raises `finbot-migrate guard: …` unless:
   - `current_database()` is the configured database;
   - `session_user` **and** `current_user` are `finbot_owner`;
   - that role is not a superuser and owns the database;
   - if `platform_meta` exists (Step 3B.3 onward), its `environment` marker equals `FINBOT_ENV` (guard G-ENV-3 for migrations).

The same statements open every offline (`--sql`) script, so SQL that is reviewed and applied by hand is guarded identically. The guard SQL contains no `%` and no placeholders, so it runs unchanged under `psql`.

### 3.3 Downgrades (S9)

| Environment | `upgrade` | `downgrade` / `stamp` |
|---|---|---|
| `dev`, `test`, `drill` | ✓ | Only with `FINBOT_MIGRATION_ALLOW_DOWNGRADE=true` |
| `staging`, `production` | ✓ | **Refused.** Use a forward-fix revision. Setting the flag is a configuration error |

Enforcement layers, each tested:
1. `finbot-migrate downgrade` / `sql downgrade` refuse before loading Alembic or connecting.
2. The `alembic downgrade|stamp` CLI is refused in `env.py` by command name.
3. The planned steps are inspected **before the first one runs**, online and offline, in every invocation path.
4. A per-step callback acts as a backstop; because the run is one transaction, a refusal rolls back.

Layer 3 wraps Alembic's private `MigrationContext._migrations_fn` (Alembic is pinned `<2` in `uv.lock`). `tests/unit/test_migration_env.py` fails if an Alembic upgrade changes that behaviour.

### 3.4 Commands

```bash
# Development (dev/test values; owner password from a private 0600 file)
export FINBOT_ENV=dev FINBOT_DB_HOST=… FINBOT_DB_OWNER_PASSWORD_FILE=/abs/path/owner_password
uv run finbot-migrate check-config          # offline: validates settings, prints redacted summary
uv run finbot-migrate upgrade               # to head
uv run finbot-migrate current
uv run finbot-migrate history               # offline
FINBOT_MIGRATION_ALLOW_DOWNGRADE=true uv run finbot-migrate downgrade base   # dev/test/drill only

# Review: print the guarded SQL without connecting (no password needed)
FINBOT_ENV=staging uv run finbot-migrate sql upgrade head
make migrate-sql                            # same, using the current FINBOT_ENV
```

Exit codes: `0` success, `1` unexpected error, `2` invalid configuration, `3` refused by policy or guard.

### 3.5 Staging and production (S4)

The staging network is isolated (doc 04 §8.2), so nothing on the host can reach the database. Migrations run in a **dedicated one-shot `migrate` container** on `finbot-staging_db`, added in Step 5, with only the owner secret. The approved fallback is **offline SQL**: `finbot-migrate sql upgrade <range>` is reviewed, then applied by the `deploy` operator through `docker exec … psql -U finbot_owner -h 127.0.0.1 -d finbot_staging`, inside the container, with the password read from the mounted secret. Each use of the fallback needs explicit owner approval. Nothing in Step 3B.2 touches staging.

## 4. Database tests

### 4.1 Architecture

```
pytest ── tests/plugins/db.py (hooks + fixtures)
            │  FINBOT_TEST_PG_BACKEND = container | userspace
            ▼
         tests/support/pg.py ── one disposable cluster per session
            │   container : pinned staging image (testcontainers), CI
            │   userspace : PostgreSQL 18.6 binaries in ~/.local, Unix socket only, development
            │   both      : random role passwords in a private temp dir (0700/0600)
            │               initialised by the REAL deploy/postgres/initdb/00-roles.sh
            │               initdb args + server settings identical to staging (test-enforced)
            ▼
         per test: CREATE DATABASE finbot_test_<12 hex> OWNER finbot_owner   (superuser, harness only)
                   owner_engine (finbot_owner) · app_engine (finbot_app) · alembic_config
                   DROP DATABASE … WITH (FORCE)                              (that name only)
```

| Property | How |
|---|---|
| Exact image in CI | `ContainerCluster` reads the `image:` (version + digest) from `deploy/staging/compose.yml` |
| PostgreSQL 18.6 in development | `UserspaceCluster` refuses binaries whose `postgres --version` is not 18.6 |
| Same roles as staging | The real `00-roles.sh` runs in both backends. In `dev`/`test` only, `FINBOT_SECRETS_DIR` points it at the temporary secrets; other environments refuse that variable (§4.3) |
| Same server behaviour | `INITDB_ARGS` and `SERVER_SETTINGS` mirror the compose file; `tests/unit/test_pg_support.py` fails on drift. Test-only additions (`fsync=off` etc.) are asserted absent from the compose file |
| Separate connections | `owner_engine` for setup and assertions, `app_engine` (`finbot_app`) for anything that touches tenant data, as RLS tests require |
| Isolation between runs | A new cluster per session, a new database per test, random passwords per session |
| Cleanup of own resources only | Cluster directories are deleted only if they contain the sentinel `.finbot-test-cluster` written at creation; databases are dropped only if they match `^finbot_test_[0-9a-f]{12}$`; the container is stopped by the fixture. Nothing else is touched |
| No silent skips | See §4.2 |
| Never staging | No code path reads staging configuration or secrets; the userspace cluster has no TCP listener, and the container cluster is a fresh container with a random port |

### 4.2 Skip policy

| Situation | Result |
|---|---|
| No backend, local run (`make check`) | Database tests are skipped with the summary line `NOTE: N database test(s) SKIPPED … They have NOT run` |
| No backend and `FINBOT_REQUIRE_DB_TESTS=1` (`make test-db`) or `CI=true` | **Usage error** (exit 4) |
| `FINBOT_REQUIRE_DB_TESTS=1` but no database test selected | **Usage error** |
| Backend set (or required, or CI) and any database test skips | **Run fails** (exit 1), listing the skipped tests |
| `-m "not db"` (CI `checks` job) | Database tests are deselected, which is not a skip |

The harness variables `FINBOT_TEST_PG_BACKEND`, `FINBOT_TEST_PG_BIN`, `FINBOT_TEST_PG_LIBDIR` and `FINBOT_REQUIRE_DB_TESTS` are captured at session start. The autouse `clean_finbot_env` fixture still removes every `FINBOT_*` variable from each test, so application-config tests stay hermetic.

### 4.3 `00-roles.sh` and test secrets

Reviewed for Step 3B.2. The change is a **`dev`/`test`-only** `FINBOT_SECRETS_DIR` override:
- it is refused (before any SQL) for `staging`, `production` and `drill`, and must be an absolute directory;
- psql reads the files through `` `cat :'secrets_dir'/…` ``, where psql shell-quotes the variable (psql manual, *Interpolation*);
- staging behaviour is unchanged (still `/run/secrets`). The running staging cluster is unaffected anyway, because the script runs only on an empty volume or as an explicit operator repair.

Behaviour is tested with a fake `psql` (`tests/deploy/test_init_roles_script.py`): the override is refused outside dev/test, staging never falls back to another directory, weak or identical secrets are refused, no secret value reaches output or psql's arguments, and the success marker is written only after psql succeeds.

### 4.4 Running

```bash
make check                                   # everything; database tests skipped (and reported) without a backend
# Database tests only; fails if any is skipped. `env -u` drops libpq variables and LD_LIBRARY_PATH
# so nothing in the caller's shell can steer psql or asyncpg away from the disposable cluster.
env -u PGHOST -u PGPORT -u PGDATABASE -u PGUSER -u PGPASSWORD \
    -u PGHOSTADDR -u PGSERVICE -u PGSERVICEFILE \
    -u PGOPTIONS -u PGCONNECT_TIMEOUT \
    -u LD_LIBRARY_PATH \
    FINBOT_TEST_PG_BACKEND=userspace \
    FINBOT_TEST_PG_BIN="$HOME/.local/opt/postgresql-18.6/usr/lib/postgresql/18/bin" \
    FINBOT_TEST_PG_LIBDIR="$HOME/.local/opt/postgresql-18.6/usr/lib/x86_64-linux-gnu" \
    make test-db
```

## 5. CI (`.github/workflows/ci.yml`)

| Job | Runs | Notes |
|---|---|---|
| `checks` | `uv lock --check`, ruff, mypy, import-linter, `pytest -m "not db" --cov` (includes the AT-DEP-01 compose checks, which need only the Docker CLI) | |
| `database` | Pulls the pinned image digest parsed from `deploy/staging/compose.yml`, then `make test-db` with `FINBOT_TEST_PG_BACKEND=container` | Fails if any database test is skipped. The testcontainers reaper is disabled (it would pull an extra, unpinned image); the fixture stops the container and the runner is ephemeral |

Supply chain:
- `actions/checkout` is pinned to commit `08c6903c…` (v5.0.0), with `persist-credentials: false`;
- uv 0.12.23 is downloaded and verified against SHA-256 `9167d72b…66d6` (the same binary used locally);
- `uv sync --frozen` installs only what `uv.lock` pins;
- permissions are `contents: read`.

The workflow uses **no secrets**, deploys nothing, and pushes nothing. D25 (CI platform) is still formally open (doc 01 §8). The workflow is written for GitHub Actions and has **not run yet**, because nothing has been pushed.

## 6. PostgreSQL 18.6 user-space installation (executed and verified 2026-10-24, Step 3B.2A)

**Goal:** `claude` can run `make test-db` locally without Docker, sudo or system packages.

**Source:** the official PGDG repository for Ubuntu 24.04 (noble). The build is **`18.6-1.pgdg24.04+2`**, the only 18.6 build in the `noble-pgdg` index (`InRelease` dated 2026-10-19). The one extra runtime dependency, `liburing2`, comes from the official Ubuntu noble archive (see below).

| Package | File | Bytes | SHA-256 |
|---|---|---|---|
| Server | `postgresql-18_18.6-1.pgdg24.04+2_amd64.deb` | 7576708 | `c0773357df8769abb70e92722196d9b53422d71c0f29a4f39c834b94276cfa3d` |
| Client (`psql`, `pg_isready`, used by `00-roles.sh`) | `postgresql-client-18_18.6-1.pgdg24.04+2_amd64.deb` | 2114236 | `b9d10d99a73bf7aa375be2fe36626c40499cfdf553d0ad174674a88a1b256b43` |
| libpq | `libpq5_18.6-1.pgdg24.04+2_amd64.deb` | 264072 | `b487c5ed2ceb9244c6a9d6ae65818ed6707c3c44a2e14488394ae4194c52c53b` |
| io_uring library (Ubuntu noble archive) | `liburing2_2.5-1build1_amd64.deb` | 21092 | `c2aef62accee92a06263c3ad4ef46132c13e409b54296c44792a20491830a7b0` |
| *(not installed)* JIT | `postgresql-18-jit_…` | — | skipped: test clusters run with `jit=off` |

The three PGDG packages are verified through the signed chain: `InRelease` signature (`gpgv`, key fingerprint below), then the `Packages.gz` hash from `InRelease`, then each package's SHA-256 from `Packages.gz`.

`liburing2` is needed because `postgresql-18` declares `Depends: liburing2 (>= 2.5)` and the host does not have it. It was downloaded over HTTPS from the official Ubuntu noble archive, and its SHA-256 (above) was compared and matched. **Ubuntu's `InRelease`/`Packages` signature chain was not verified for this package**; that remains an open verification gap. It is unpacked only under the private prefix and is not installed system-wide (no dpkg database entry).

**Steps** (all as `claude`, user space only):

```bash
D=$HOME/.cache/finbot-pgdg; mkdir -p "$D" && cd "$D"
BASE=https://apt.postgresql.org/pub/repos/apt
# 1. Signed repository metadata and the PGDG signing key (fingerprint B97B 0AFC AA1A 47F0 44F2 44A0 7FCC 7D46 ACCC 4CF8)
curl -fsSLO "$BASE/dists/noble-pgdg/InRelease"
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor > pgdg.gpg
gpg --show-keys --with-fingerprint pgdg.gpg            # compare the fingerprint above BEFORE continuing
gpgv --keyring ./pgdg.gpg InRelease                    # signature must be valid
# 2. Package index, hash-checked against InRelease
curl -fsSLO "$BASE/dists/noble-pgdg/main/binary-amd64/Packages.gz"
want=$(awk '/^SHA256:/{s=1;next} /^[A-Za-z0-9]+:/{s=0} s&&$3=="main/binary-amd64/Packages.gz"{print $1}' InRelease)
echo "$want  Packages.gz" | sha256sum --check --strict      # must print OK
# 3. The three .deb files, each hash-checked against Packages.gz
for p in postgresql-18 postgresql-client-18 libpq5; do
  f=$(zcat Packages.gz | awk -v p="$p" '$1=="Package:"{x=($2==p)} x&&$1=="Version:"&&$2=="18.6-1.pgdg24.04+2"{v=1} x&&v&&$1=="Filename:"{print $2; exit}')
  s=$(zcat Packages.gz | awk -v f="$f" '$1=="Filename:"{x=($2==f)} x&&$1=="SHA256:"{print $2; exit}')
  curl -fsSLO "$BASE/$f" && echo "$s  $(basename "$f")" | sha256sum --check --strict
done
# 3b. liburing2 from the official Ubuntu noble archive over HTTPS; SHA-256 compared against the
#     value recorded above. (Ubuntu's InRelease/Packages signature chain was NOT verified.)
echo "c2aef62accee92a06263c3ad4ef46132c13e409b54296c44792a20491830a7b0  liburing2_2.5-1build1_amd64.deb" \
  | sha256sum --check --strict
# 4. Unpack without dpkg/root, then smoke-check (no cluster, no server start)
P=$HOME/.local/opt/postgresql-18.6; mkdir -p "$P"
for f in *.deb; do dpkg-deb -x "$f" "$P"; done
L="$P/usr/lib/x86_64-linux-gnu"                     # bundled libpq.so.5 and liburing.so.2
LD_LIBRARY_PATH="$L" "$P/usr/lib/postgresql/18/bin/postgres" --version              # → 18.6
LD_LIBRARY_PATH="$L" "$P/usr/lib/postgresql/18/bin/psql" --version
LD_LIBRARY_PATH="$L" ldd "$P/usr/lib/postgresql/18/bin/postgres" | grep 'not found' || echo "all libraries present"
```

Without `LD_LIBRARY_PATH`, `ldd` reports `liburing.so.2 => not found`. That is expected: the library exists only under the private prefix, and the test harness supplies it through `FINBOT_TEST_PG_LIBDIR`.

| Aspect | Detail |
|---|---|
| Disk | ≈ 11 MB downloads (in `~/.cache/finbot-pgdg`) + ≈ 48 MB unpacked (in `~/.local/opt/postgresql-18.6`); each test session's cluster uses ≈ 40–60 MB under `/tmp`, removed at session end |
| System impact | None: no root, no apt/dpkg database changes, no service, no systemd unit, no TCP listener (test clusters use a private Unix socket) |
| Runtime libraries | `libpq.so.5` and `liburing.so.2` come from the private prefix (`FINBOT_TEST_PG_LIBDIR`); everything else from the host (Ubuntu 24.04: ICU 74, OpenSSL 3, libxml2, LZ4, Zstd, LDAP, GSSAPI). Step 4's `ldd` check confirms they are present; if any other library is missing, **stop** and report, and do not install system packages |
| Differences from CI | Same PostgreSQL version; Ubuntu instead of Debian build, host glibc. Collation does not depend on glibc (builtin `C.UTF-8` provider). CI on the pinned image stays the authoritative gate |
| Rollback | `rm -rf ~/.local/opt/postgresql-18.6 ~/.cache/finbot-pgdg` (both directories are created by this plan only). Any leftover test cluster lives in pytest's temp directory under `/tmp/pytest-of-claude/` |

## 7. Known limitations and pending approvals (Steps 3B.2–3B.3)

| Item | Status |
|---|---|
| Database tests (`tests/integration/test_db_harness.py`, `tests/migration/test_migration_env_db.py`) | **10/10 passed** on the userspace backend (PostgreSQL 18.6, `18.6-1.pgdg24.04+2`), 2026-10-24 (Step 3B.2A), after the strict-mode `pytest_asyncio.fixture` fix in `tests/plugins/db.py`. The container backend has not run |
| Online migration path (`finbot-migrate upgrade/current`, guard execution in PostgreSQL) | Covered by the passing database tests above (guard refusals, environment-marker check, downgrade refusal and rollback). Since Step 3B.3 they run the real chain (revision `0001`) |
| Phase 1 baseline (Step 3B.3, §9) | **49/49 database tests passed** on the userspace backend, 2026-10-24: RLS, privileges, integrity triggers, and the upgrade → downgrade → upgrade lifecycle. Not yet applied to staging (Step 5, `migrate` container) |
| `liburing2` provenance (§6) | SHA-256 compared and matched; Ubuntu's `InRelease`/`Packages` signature chain **not verified** |
| CI workflow | Written, **never run** (nothing pushed; D25 open) |
| Container backend | Unexecuted on this host by design (`claude` has no Docker access) |
| Role timeouts (S8) | Not implemented; planned for the idempotent `00-roles.sh` plus an app startup check (Step 4). No role change applied |
| `compose.dev.yml` (doc 06 Step 3 originally) | Deferred: `claude` cannot run Docker; the user-space backend replaces it for development |
| Alembic log lines | Alembic's own `%s` arguments (revision ids) are redacted by the JSON log formatter; use `finbot-migrate current`/`history` to see revisions |

## 8. Step 3B decisions (approved 2026-10-24)

| ID | Decision |
|---|---|
| S1 | Step 3B covers Phase 1 schema only (doc 05 §11, P1 row). No Phase 2 identity/onboarding or Phase 3 ledger tables |
| S2 | PostgreSQL 18.6 user-space testing for `claude` plus GitHub Actions CI on the pinned image (architecture approved; the install in §6 needs separate approval) |
| S3 | SQLAlchemy 2.x (asyncio), asyncpg, Alembic, pytest-asyncio, testcontainers[postgres], pinned through `uv.lock` |
| S4 | Staging migrations through a dedicated `migrate` container in Step 5; offline SQL is an explicitly approved fallback only |
| S5 | RLS validates active workspace membership as well as tenant context, without recursive RLS or unsafe `SECURITY DEFINER` behaviour (doc 09 §4.1) |
| S6 | Immutability and append-only bypass checks use `session_user`, not `current_user` (doc 09 §4.4, §6.2) |
| S7 | Column-level UPDATE privileges for protected entities (doc 09 §4.3) |
| S8 | Role timeouts are configured through the idempotent initialisation script, with application startup checks; not applied yet |
| S9 | Downgrades only in dev/test/drill; staging and production use forward fixes (§3.3) |
| S10 | Application and migration-owner credentials strictly separated (§2) |
| S11 | The `public` schema is used |

## 9. Phase 1 baseline (revision `0001`, Step 3B.3)

`migrations/versions/0001_phase1_baseline.py` creates exactly the P1 row of doc 05 §11 (S1). Each SQL statement is a separate `op.execute`, and the SQL contains no `%` and no `:name`, so the offline output (`finbot-migrate sql upgrade head`) runs unchanged under psql. The environment marker comes from the validated `FINBOT_ENV` through `migration_runtime.environment_literal()`.

| Area | What `0001` does |
|---|---|
| Database and schema | `REVOKE CONNECT, TEMPORARY … FROM PUBLIC`; `CONNECT` for `finbot_app` and `finbot_backup`. `REVOKE ALL ON SCHEMA public FROM PUBLIC`; `USAGE` for the same two roles |
| Default privileges (owner) | `EXECUTE` on new functions is not granted to `PUBLIC`; `finbot_backup` gets `SELECT` on new tables and sequences. `alembic_version` (created before `0001`) gets an explicit backup grant |
| Tables | `platform_meta` (+ marker row), `users`, `workspaces`, `workspace_memberships` (no `default_account_id`), `audit_events`, with doc 05's constraints and indexes; `uuidv7()` defaults; `citext` for `users.email` |
| RLS (D5, S5) | ENABLE + FORCE on the four tenancy tables, policies per doc 09 §4.3, with every context check also requiring an active membership through `app_is_active_member()` (SECURITY DEFINER, boolean only, `EXECUTE` for `finbot_app` only) |
| App privileges (S7) | `users`: `SELECT`, `UPDATE (display_name, locale, timezone, updated_at)`. `workspaces`: `SELECT, INSERT`, `UPDATE (name, timezone, currency, status, deletion_requested_at, deletion_requested_by, updated_at, version)`. `workspace_memberships`: `SELECT, INSERT`, `UPDATE (role, status, ended_at, version)`. `audit_events`: `SELECT, INSERT`. `platform_meta`: `SELECT`. No `DELETE` or `TRUNCATE` anywhere |
| Triggers | `trg_personal_membership_is_owner`; `trg_business_has_owner` (deferred constraint trigger); audit append-only on UPDATE, DELETE **and TRUNCATE**, using `session_user` (S6) |

**Interpretations made where the documents leave a choice** (owner review requested):

1. **S5 applies to every context check, including `WITH CHECK`.** As a result, `finbot_app` alone cannot create a business workspace together with its first owner membership: no membership exists yet to satisfy the policy. Phase 2 needs a decision: a narrow `SECURITY DEFINER` creation function (like `redeem_onboarding_invite`) or a documented bootstrap policy.
2. **Audit inserts also require an active membership** (S5). A `member.left` event must therefore be written before the membership is ended, in the same transaction.
3. **`finbot_readonly` gets no grants yet.** Break-glass (doc 09 §8) sets only `app.workspace_id`, which S5 policies reject without an active member in `app.user_id`. This conflict must be resolved before break-glass is built.
4. **`users` has no `INSERT` grant** for the app: users are created by the Phase 2 onboarding definer function.
5. **Membership columns `invited_by` and `joined_at` are not updatable**, treated as creation metadata like `created_*` (S7).
6. **`trg_business_has_owner` also fires on `workspaces` INSERT**, so a business workspace cannot be committed without an owner, and it checks every existing business workspace regardless of `status`.
7. **The two membership trigger functions are `SECURITY DEFINER`** (fixed `search_path`, no `EXECUTE` grants), so their checks see the rows they need whatever the caller's RLS context. The audit trigger function runs as the invoker.

Tests: `tests/integration/test_rls_phase1_db.py` (AT-RLS-00…07, AT-RLS-COVERAGE, S5–S7, integrity triggers), `tests/migration/test_baseline_migration_db.py` (lifecycle), `tests/unit/test_baseline_revision.py` (offline SQL, no database).

## 10. Identity and onboarding (revision `0002`, Step 4A)

`migrations/versions/0002_identity_onboarding.py` implements the identity part of the P2 row of doc 05 §11 and the two identity functions of doc 09 §4.3. It is the narrow bootstrap that §9 items 1 and 4 call for, for onboarding only: business-workspace creation is still open.

| Area | What `0002` does |
|---|---|
| Tables | `telegram_identities` (one active binding per Telegram account and per user; `telegram_user_id > 0`) and `onboarding_invites` (`token_hash` = 32-byte SHA-256, unique; `used_at`/`used_by` consistent), as in doc 05 §3 with `uuidv7()` defaults |
| Access | **No grants to `finbot_app`** on either table (doc 09 §4.3). RLS ENABLE + FORCE and a deny-all `no_direct_access` policy, so an accidental future grant still exposes nothing. `finbot_backup` reads them through the default privileges |
| `resolve_principal(tg_user_id bigint)` | `SECURITY DEFINER`, `STABLE`, owner `finbot_owner`, `search_path = pg_catalog, public`; returns only `(user_id, status, is_platform_admin)` for an active (not unlinked) binding. `EXECUTE` for `finbot_app` only |
| `redeem_onboarding_invite(token_hash bytea, tg_user_id bigint, display_name text)` | `SECURITY DEFINER`, same owner and `search_path`, `EXECUTE` for `finbot_app` only. Validates its arguments (raises on a bad digest, ID or name, before any write). Takes a transaction-scoped advisory lock per Telegram account, then locks the invite `FOR UPDATE`. An account that is already linked gets its existing user ID back and the invite is not touched. Otherwise a refused invite (unknown, used, revoked or expired) writes one `onboarding_invite.failed` audit row with the reason and returns NULL. A valid invite creates the user, the identity, the personal workspace `Pribadi` and its owner membership, marks the invite used, and writes `onboarding_invite.redeemed`, `user.onboarded`, `identity.linked` (platform-level) and `workspace.created` (in the new workspace), all in the caller's transaction. It accepts no workspace, role or user ID |

Application side: `domain/identity.py` (token parsing, SHA-256, display-name normalisation), `ports/identity.py`, `infra/db/identity.py` (calls only the two functions; reads the personal workspace with `app.user_id` set, under RLS), `services/onboarding.py` (`/start` outcomes), and `adapters/telegram/{start.py,messages_id.py}` (framework-independent `/start` handler and Indonesian messages).

**Interpretations and limits** (owner review requested):

1. **D6 is still open in doc 01 §8.** This implements its documented default (a), single-use invite codes, because FR-IDN-01, doc 05 and doc 09 are all written for it.
2. **FR-IDN-04 seeds are deferred.** The "Kas" account and default categories need Phase 3 tables (`accounts`, `categories`). Phase 3 must add them to `redeem_onboarding_invite` in the revision that creates those tables.
3. **The personal workspace is named `Pribadi`.** No document fixes the name; it is the only literal default in the function.
4. **The display name falls back to `Pengguna`** when the Telegram first name has nothing printable. Control and Unicode format characters (including bidi overrides) are removed, whitespace is collapsed, and the result is cut to 64 characters.
5. **Malformed `/start` payloads** (not `onb_` followed by 22 to 60 URL-safe characters) are treated as "no invite". They are not sent to the database and not audited; only well-formed tokens that fail are audited.
6. **A failed-invite audit row records only the reason**, not the Telegram user ID, to keep identifiers of people who were never onboarded out of the audit log.
7. **Not implemented yet:** invite creation and revocation (FR-IDN-01 CLI; `created_by` needs an existing platform-admin user, so bootstrapping the first admin is an open decision), the `/start` rate limit (T17, AT-TOK-04), `telegram_username` (the documented signature has no parameter for it), `user_preferences.active_workspace_id` (the personal workspace is shown as the active one), and the main menu (FR-IDN-07; no features exist yet). The Telegram framework is not wired in: `python-telegram-bot` is not a dependency, and adding it needs approval (Phase 1 Step 4).

Tests: `tests/integration/test_onboarding_db.py` (first and repeat onboarding, refusals and their audit, concurrent redeems for one account and for one invite, rollback inside the function and by the caller, argument validation, tenant isolation, no direct table access, function privileges), `tests/migration/test_identity_migration_db.py` (0001 ↔ 0002 lifecycle, head → base), `tests/unit/test_identity_revision.py` (offline SQL), `tests/unit/test_identity_domain.py`, `tests/unit/test_onboarding_service.py`. The Phase 1 tests now pin revision `0001` where they test the baseline alone, and AT-RLS-COVERAGE, AT-RLS-05 and AT-RLS-06 include the identity tables and functions.
