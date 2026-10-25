# 10 — Database Migrations & Database Testing

| | |
|---|---|
| **Status** | Phase 1 Step 3B.2 (tooling: migration environment, credential separation, test harness, CI workflow) **Step 3B.3** (revision `0001`, the Phase 1 baseline; §9) **Step 4A** (revision `0002`, identity and onboarding; §10) **Step 4B** (revision `0003`, platform-admin bootstrap and invites; §11) and **Step 6** (revision `0004`, ledger core; §12). Step 6 awaits owner review |
| **Date** | 2026-10-25 |
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

The staging network is isolated (doc 04 §8.2), so nothing on the host can reach the database. Migrations run in a **dedicated one-shot `migrate` container** on `finbot-staging_db`, with only the owner secret (Step 5A: service `migrate` in `deploy/staging/compose.yml`, profile `ops`). They are **operator-controlled**: `up` never starts the service and the bot does not depend on it. The procedure (preflight, SQL review, mandatory backup, `upgrade 0003`, verification, restore instead of downgrade) is `deploy/staging/README.md` §7; the admin bootstrap is §8. The approved fallback is **offline SQL**: `finbot-migrate sql upgrade <range>` is reviewed, then applied by the `deploy` operator through `docker exec … psql -U finbot_owner -h 127.0.0.1 -d finbot_staging`, inside the container, with the password read from the mounted secret. Each use of the fallback needs explicit owner approval. Nothing in Step 3B.2 touches staging.

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
| `security` (Step 6B) | gitleaks (whole history), pip-audit (locked set), Trivy fs (lockfile, Dockerfile), V2 image build + Trivy image | Pinned, checksum-verified scanners; any finding fails (Trivy: High/Critical). §13.6 |

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
7. **Not implemented yet** (invite creation, listing and revocation followed in Step 4B, §11): the `/start` rate limit (T17, AT-TOK-04), `telegram_username` (the documented signature has no parameter for it), `user_preferences.active_workspace_id` (the personal workspace is shown as the active one), and the main menu (FR-IDN-07; no features exist yet). The Telegram framework is not wired in: `python-telegram-bot` is not a dependency, and adding it needs approval (Phase 1 Step 4).

Tests: `tests/integration/test_onboarding_db.py` (first and repeat onboarding, refusals and their audit, concurrent redeems for one account and for one invite, rollback inside the function and by the caller, argument validation, tenant isolation, no direct table access, function privileges), `tests/migration/test_identity_migration_db.py` (0001 ↔ 0002 lifecycle, head → base), `tests/unit/test_identity_revision.py` (offline SQL), `tests/unit/test_identity_domain.py`, `tests/unit/test_onboarding_service.py`. The Phase 1 tests now pin revision `0001` where they test the baseline alone, and AT-RLS-COVERAGE, AT-RLS-05 and AT-RLS-06 include the identity tables and functions.

## 11. Platform-admin bootstrap and invite management (revision `0003`, Step 4B)

**Owner decision (2026-10-25):** the first platform admin is created **once**, by the operator, as `finbot_owner`, through `finbot-migrate bootstrap-admin`. That subcommand is separate from upgrade and downgrade: it runs no Alembic code, is never run by a migration or at application start-up, and the bot's credentials can never perform it.

| Function (revision `0003`) | Security | Behaviour |
|---|---|---|
| `bootstrap_platform_admin(tg_user_id bigint, display_name text)` | **Not** `SECURITY DEFINER`; **no `EXECUTE` grant** (only its owner `finbot_owner` can run it); also refuses any `session_user` other than `finbot_owner`; fixed `search_path` | First claims the permanent `platform_meta` key `platform_admin_bootstrapped` (`ON CONFLICT DO NOTHING`; a concurrent bootstrap waits on it and is then refused). Refuses an already-linked Telegram account. Creates the admin user (`is_platform_admin`), identity, personal workspace and owner membership; audits `platform_admin.bootstrapped`, `user.onboarded`, `identity.linked`, `workspace.created` (`actor_kind = 'system'`, channel `cli`). The marker is never removed, so the bootstrap stays closed even if the admin is later suspended, demoted or unlinked, and also after a `0003` downgrade and re-upgrade |
| `create_onboarding_invite(token_hash bytea, note text, channel text)` | `SECURITY DEFINER`, `EXECUTE` for `finbot_app` only | Requires `app_user_id()` to be an active platform admin. Stores only the 32-byte digest; fixed 7-day expiry, single use (FR-IDN-01); optional note of up to 200 printable characters; audits `onboarding_invite.created` (`actor_kind = 'platform_admin'`) |
| `list_onboarding_invites(include_inactive boolean)` | same | Admin check; returns `(invite_id, status, created_at, expires_at, note, created_by, used_by)`, **never `token_hash`**; status is `active`, `used`, `revoked` or `expired` |
| `revoke_onboarding_invite(invite_id uuid, channel text)` | same | Admin check; locks the invite; returns `revoked`, or `not_found`, `used`, `already_revoked` or `expired` without changing anything. Audits `onboarding_invite.revoked` only when it revokes. A revoke racing a redeem has exactly one effect |

The application layer (`services/platform_invites.py`) authorizes first: the `--as` Telegram account must resolve to an active platform admin (docs/08 §3.3), and the database checks it again. Tokens are 128-bit (`secrets.token_urlsafe(16)`); only their SHA-256 leaves the process.

### 11.1 Operator instructions

All commands run where the respective credentials are mounted (Step 5: the one-shot `migrate` container for owner commands, the `bot` container for `finbot-admin`). Nothing here is run against staging until Step 5.

1. **Migrate:** `finbot-migrate upgrade` (to `0003` or later).
2. **Bootstrap the first admin, once:**
   `finbot-migrate bootstrap-admin --telegram-user-id <your numeric Telegram ID> --display-name "<name>"`
   Both arguments are required; there is no default. It checks the database's environment marker against `FINBOT_ENV` (G-ENV-3). Exit 0 prints only the new user ID; exit 3 means refused (already bootstrapped, account already linked, wrong environment, schema too old, invalid input). The output never contains credentials.
3. **Create an invite** (app role, as the admin):
   `finbot-admin invite create --as <admin Telegram ID> [--note "<who it is for>"]`
   The `t.me/<bot>?start=onb_…` link is printed **once, on stdout only**. Deliver it privately to one person. It is not stored (only its hash is) and cannot be shown again; if it is lost, revoke the invite and create a new one. Do not paste it into tickets or chats that others can read.
4. **List invites:** `finbot-admin invite list --as <admin Telegram ID> [--all]` (metadata only).
5. **Revoke an unused invite:** `finbot-admin invite revoke --as <admin Telegram ID> <invite_id>`. Exit 3 tells you why nothing changed.

`--as` attributes the action to an admin and is checked against the database. It is not authentication: whoever can run `finbot-admin` already holds the bot's database credentials, so access to the bot container must stay limited to the `deploy` operator (doc 04 §8.3).

**Interpretations and limits** (owner review requested):

1. **Expiry is fixed at 7 days** (FR-IDN-01). There is no option to change it.
2. **Unauthorized attempts are refused but not audited:** the refusal rolls back the function's transaction. The service logs `security.authz_denied` (no identifiers beyond the event) instead.
3. **Invite notes are free text chosen by the operator**, stored in the database and shown by `list`. They are never logged.
4. **`FINBOT_INVITES_ENABLED` does not gate onboarding invites.** Its scope is undocumented; gating onboarding on it would block all new users. It is left for workspace invitations.
5. **Not implemented:** the bot-DM channel for invites (docs/08 §3.3 "+ bot DM"; the functions already accept channel `telegram`), suspend/reactivate (FR-IDN-06), and a full `authz/policy.py` (the platform check lives in the service for now).

Tests: `tests/integration/test_platform_admin_db.py`, `tests/migration/test_platform_admin_migration_db.py` (CLI end to end, `0003` lifecycle, marker kept across downgrade), `tests/unit/test_admin_revision.py`, `tests/unit/test_platform_invites.py`. AT-RLS-06 includes the three invite functions.

## 12. Ledger core (revision `0004`, Step 6)

Revision `0004_ledger_core` implements the finance part of the P3 row of doc 05 §11: `accounts`, `categories`, `transactions` (header), `transaction_entries` (D1), `transaction_revisions`, `drafts`, `pending_actions`, the `current_entries` view, the doc 05 §5.2 triggers, and `workspace_memberships.default_account_id` with its composite FK. It is **additive**: new tables, one new nullable column, grants. No existing row is read or rewritten, so it is safe on a database that already has users and workspaces. Workspaces created before `0004` get their finance defaults the first time they use a finance feature (§12.2).

### 12.1 Schema, as implemented

| Object | Notes |
|---|---|
| `accounts` | As doc 05 §5.1. `UNIQUE (workspace_id, id)`; active names unique case-insensitively (`lower(name)`). Only the seeded "Kas" is created in this step |
| `categories` | As doc 05 §5.1, plus `created_by`, `version` and two CHECKs: the name is trimmed (`name = btrim(name)`), and the default (`Lainnya`) category cannot be archived. `UNIQUE (workspace_id, id, kind)` is the target of the kind-matching FK from `transactions`. Active names unique per kind with `lower(name)`; one default per kind |
| `transactions` | As doc 05 §5.2, plus **`currency text NOT NULL DEFAULT 'IDR' CHECK (currency = 'IDR')`** (reports group by it, so currencies can never be summed together), `CHECK (txn_date >= '2000-01-01')`, `description` 1–500 characters (empty is stored as NULL), `version >= 1`. **Not yet created** (added by later expand-only revisions): `receipt_id` and the `receipts` table (Phase 4), `import_ref` (Phase 7), `replaces_transaction_id` (D21 type change, not built yet). Indexes: `ix_txn_ws_date`, `ix_txn_ws_category` (posted only) and `ix_txn_ws_history (workspace_id, status, txn_date DESC, id DESC)` for paging |
| `transaction_entries` | As doc 05 §5.2 (signed `bigint`, leg/sign CHECK, composite FKs to the header *with its type* and to the account, one leg per revision, two distinct accounts per revision) |
| `transaction_revisions` | As doc 05 §5.2. `snapshot` holds structured fields only (`has_description`, never the text) |
| `drafts` | As doc 05 §5.3 without `batch_id`, `receipt_id` and `prompt_version` (AI and receipts are Phase 4), plus `purpose` (`create`/`edit`), `target_transaction_id` (edit drafts; composite FK), `updated_at`, and **`uq_drafts_one_pending (workspace_id, user_id) WHERE status = 'pending'`**: one conversation per user and workspace |
| `pending_actions` | As doc 05 §5.3 plus `status` (`pending`/`completed`/`cancelled`/`expired`) and a `kind` CHECK (`category_create`, `category_rename`); one pending action per user and workspace |
| `current_entries` | `security_invoker` view as doc 05 §5.2, plus `t.currency` |
| `workspace_memberships.default_account_id` | Nullable, FK `(workspace_id, default_account_id)` → `accounts` (FR-ACC-05) |

Triggers (functions owned by `finbot_owner`, `SET search_path = pg_catalog, public`, `EXECUTE` revoked from PUBLIC and granted to nobody):

| Trigger | Function | Enforces |
|---|---|---|
| `trg_entries_balanced` (2 constraint triggers, **DEFERRABLE INITIALLY DEFERRED**: on entry INSERT and on header INSERT/UPDATE OF `amount, entry_revision, type`) | `ledger_entries_balanced()`, `SECURITY DEFINER` (sees every entry of one transaction whatever the caller's RLS context; reads by transaction ID only) | INV-2…INV-4 at COMMIT; no entry set beyond `entry_revision`. A header without entries, a one-legged or unbalanced transfer, or an entry that disagrees with the header never commits |
| `trg_entries_immutable`, `trg_entries_no_truncate`, `trg_revisions_immutable`, `trg_revisions_no_truncate` | `ledger_append_only()` (invoker) | INV-6: UPDATE, DELETE and TRUNCATE refused unless `session_user = 'finbot_owner'` (S6; purge only) |
| `trg_txn_type_immutable` | `ledger_txn_type_immutable()` (invoker) | D21(a): `type` never changes, for the owner too |
| `trg_entries_active_account` | `ledger_entry_active_account()`, definer (reads one account by key) | R-10: no new entry on an archived account |
| `trg_txn_active_category` | `ledger_txn_active_category()`, definer (reads one category by key) | R-10, FR-CAT-04: an archived category is never newly set; existing rows keep it |

RLS (D5): every new table is `ENABLE` + `FORCE` with policy `tenant_isolation`: `workspace_id = app_workspace_id() AND app_is_active_member(workspace_id)`, for `USING` and `WITH CHECK`. `drafts` and `pending_actions` add `AND user_id = app_user_id()` (R-9: a draft is visible only to its creator). A `pending_actions` row with `workspace_id IS NULL` is invisible to the app (no user-level kinds exist yet).

Grants to `finbot_app` (S7): `SELECT, INSERT` on all seven tables; column-level `UPDATE` only on mutable columns (never `id`, `workspace_id`, `type`, `currency`, `created_*`, `kind`); **no UPDATE on `transaction_entries` and `transaction_revisions`**, and no DELETE or TRUNCATE anywhere; `UPDATE (default_account_id)` on memberships; `SELECT` on `current_entries`. `finbot_backup` reads everything through the default privileges of revision `0001`.

### 12.2 Seeding (FR-IDN-04, FR-CAT-01, FR-ACC-05)

The defaults live in application code (`domain/finance.py`: `DEFAULT_CATEGORIES`, `DEFAULT_ACCOUNT_*`), not in a migration or in the onboarding function. The first finance request of a member whose `default_account_id` is NULL, in an **active** workspace, runs `services/finance.py::ensure_finance_defaults` in its own short transaction, before the request's own one (Step 6B: `SessionMode.SEED`, membership locked `FOR NO KEY UPDATE` first, then the workspace's category-name lock; retried on deadlock; §13.3):

1. if the workspace has no category at all (archived ones count), insert the documented FR-CAT-01 lists: income Gaji, Bonus, Penjualan, Investasi, Hadiah, Lainnya; expense Makanan, Transportasi, Belanja, Tagihan, Kesehatan, Pendidikan, Hiburan, Iuran, Lainnya (`Lainnya` is the default of each kind);
2. if it has no active account, insert "Kas" (`cash`, opening balance 0, opening date 2000-01-01 so that back-dated entries count);
3. set the member's default account to the oldest active account.

Every insert is also `ON CONFLICT DO NOTHING` against the unique indexes, so concurrent first requests seed exactly once (tested with 20 concurrent reads and writes of five members; the Step 6 version deadlocked on concurrent first writes, F02). Each inserted row is audited (`category.created` / `account.created`, `actor_kind = 'system'`, `seeded: true`). This is why the migration needs no data step. The Step 6 request listed slightly different default names. The approved document wins, and only the code list needs changing if the owner wants otherwise.

### 12.3 Money, dates and currency

- **Representation:** whole rupiah as `bigint` in the database and `int` in Python (G-6, CLAUDE.md rule 3; IDR has scale 0). No `float` or `Decimal` exists in any money path.
- **Input:** `domain/money.py::parse_amount` accepts `25000`, `25.000`, `25,000`, `Rp 1.250.000`, `25rb`, `1,5jt`, `2 juta` and similar. A fraction is rounded **half-up** with integer arithmetic, and the user is told. Zero, negative and out-of-range (above 10^12) inputs are refused; the transaction type alone decides the direction.
- **Aggregation:** sums are SQL `SUM(bigint)`, which PostgreSQL computes exactly as `numeric`, converted back to `int`.
- **Currency:** each header carries `currency` (IDR only, enforced by a CHECK). Reports and balances are grouped by currency and never added across currencies.
- **Dates:** a transaction date is a business date in the workspace timezone (G-7), between 2000-01-01 and today. Back-dating is allowed and flagged in the audit (`back_dated`; D17). Technical instants are UTC `timestamptz`.

### 12.4 Transaction lifecycle

```
draft (pending) ──✅ confirm──► transaction (posted, v1, entries rev 1)
   │  ❌ cancel / new conversation → cancelled          │ edit amount → v+1, entries rev+1 (old set kept)
   │  24 h → expired                                    │ edit category/description/date → v+1
   │  member downgraded/removed → expired / refused     ▼
   └────────────────────────────────────────── void ("hapus") ⇄ restore (v+1 each; entries untouched)
```

- **Confirm** locks the draft row (`FOR UPDATE`), writes the header, its entries, a revision and the audit event, and sets `drafts.status = 'confirmed'` in one transaction. A second confirm (double tap, Telegram retry, a concurrent press) finds the draft already confirmed and reports the same transaction: exactly one is stored (AT-TXN-IDEMP-01). Permission is checked at that moment (G-2, R-9); a replay is re-authorized and shows only a still-posted transaction under the same membership (Step 6B, §13.4).
- **Edit** and **void/restore** use optimistic locking (`version`). A stale button or draft is refused and the user reopens the latest version (AT-TXN-OCC-01).
- **Void** is the application's reversible delete: the transaction stops counting in balances, history and reports, and stays restorable from `/riwayat dihapus`. **A migration downgrade is a different thing:** dropping `0004` drops every finance table and its data. Downgrades are refused in staging and production (S9), and recovering from a bad schema change means restoring a backup (§12.6).

### 12.5 Verification after `upgrade 0004` (read-only, as `finbot_owner`)

These use the `owner_psql` helper of `deploy/staging/README.md` §7:

```sql
SELECT version_num FROM public.alembic_version;                                   -- 0004
-- Every ledger table has RLS ENABLE + FORCE and its policy: expect 7 rows, all t / t / 1.
SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relname IN ('accounts', 'categories', 'transactions',
  'transaction_entries', 'transaction_revisions', 'drafts', 'pending_actions') ORDER BY 1;
-- The app can never rewrite money movements or the type: expect f f f f f.
SELECT has_table_privilege('finbot_app', 'public.transaction_entries', 'UPDATE'),
       has_table_privilege('finbot_app', 'public.transaction_entries', 'DELETE'),
       has_table_privilege('finbot_app', 'public.transaction_revisions', 'UPDATE'),
       has_column_privilege('finbot_app', 'public.transactions', 'type', 'UPDATE'),
       has_table_privilege('finbot_app', 'public.transactions', 'DELETE');
-- The deferred balance check and the immutability triggers exist: expect 9 rows.
SELECT tgrelid::regclass, tgname, tgdeferrable FROM pg_trigger
WHERE tgname LIKE 'trg_entries%' OR tgname LIKE 'trg_revisions%' OR tgname LIKE 'trg_txn%'
ORDER BY 1, 2;
-- No ledger function is executable by PUBLIC or the app: expect 0 rows.
SELECT p.proname FROM pg_proc p
WHERE p.proname LIKE 'ledger\_%' AND (has_function_privilege('finbot_app', p.oid, 'EXECUTE')
   OR has_function_privilege('public', p.oid, 'EXECUTE'));
-- Existing data untouched; nothing seeded yet: expect the old counts, 0 and 0.
SELECT (SELECT count(*) FROM public.users) AS users,
       (SELECT count(*) FROM public.workspace_memberships WHERE default_account_id IS NOT NULL) AS defaults,
       (SELECT count(*) FROM public.transactions) AS transactions;
```

After the first finance command of a test account, its workspace has 15 categories and one "Kas" account, and its membership has a default account.

### 12.6 Proposed staging deployment of Step 6 (superseded by §13.8)

**Superseded:** the Step 6B code requires revision `0005` (G-ENV-7); deploy `0004` and `0005` together with the consolidated procedure in §13.8. The text below is kept for the record.

Each step needs owner approval, and is run by `deploy` per `deploy/staging/README.md`.

1. **Backup** (README §12.1). Record the file name. This is the rollback point for the schema.
2. **Build** the image from the reviewed commit (README §6.2) and run the offline smoke tests. `finbot bot --dry-run` should now list `/catat … /riwayat … /saldo … /laporan … /kategori` and `callback_query`.
3. **Stop the bot** (README §11: `stop bot`). The migration takes a brief `ACCESS EXCLUSIVE` lock on `workspace_memberships` (ADD COLUMN, ADD CONSTRAINT) under `lock_timeout = 10s`. Stopping the bot avoids waiting behind it. The Step 5 code would keep working on the new schema (additive), but the Step 6 code needs `0004`.
4. **Review the SQL**: `docker compose -f $F run --rm --no-deps migrate sql upgrade 0003:0004` (README §7.2), then **apply**: `docker compose -f $F run --rm migrate upgrade 0004` (one transaction; a non-zero exit leaves the database unchanged).
5. **Verify** with §12.5.
6. **Start the new image**: `up -d bot` (README §9.2); wait for the healthcheck, then confirm the second-instance refusal (README §9.3).
7. **Smoke test** with a synthetic test account: `/bantuan`; `/pengeluaran 25rb makan` → category → ✅; press ✅ again (expect "sudah disimpan sebelumnya"); `/riwayat` → detail → 🗑️ → confirm; `/riwayat dihapus` → ♻️; `/saldo`; `/laporan`; `/kategori` → add and deactivate one; a viewer or editor account in a business workspace when those exist. Check that the logs show no message text, amounts or tokens (README §10).
8. **Rollback:**
   - Application problem: `stop bot`, retag the previous image, `up -d bot`. The Step 5 code ignores the new tables.
   - Schema problem before any real use: README §12.2, restoring the step-1 backup (the downgrade is refused in staging).
   - After testers have recorded data: prefer a forward-fix revision. A restore loses everything entered since the backup.

### 12.7 Tests

| Area | Tests |
|---|---|
| Unit (no database) | `test_money.py` (parsing, half-up, bounds, AT-ENT-20, AT-PARSE-PROP), `test_finance_domain.py` (names, dates and months in the timezone, capture/edit state machines, entry rules), `tests/authz/test_policy_matrix.py` (AT-RBAC-SYNC-01 against `tests/authz/expected_matrix.py`, evaluation order), `test_finance_adapter.py` (callback codec and forgery, AT-RENDER-01, argument parsing), `test_ledger_revision.py` (offline SQL: RLS, grants, triggers, psql safety), `test_finance_services.py` and `test_telegram_finance.py` (services and handlers over an in-memory store) |
| PostgreSQL | `test_ledger_schema_db.py` (AT-ENT-04…10, 16, 17; AT-ISO-01/03; AT-RLS-00/01/02 for the ledger tables), `test_ledger_service_db.py` (seeding under concurrency, category rules, capture, AT-TXN-IDEMP-01, concurrent conversation starts, AT-ENT-11/12/19, AT-TXN-OCC-01, void/restore, AT-RPT-01 month boundaries, paging, AT-ISO-05, AT-RBAC-P-01, finance AT-RBAC-M rows, AT-RBAC-R9-02, removal mid-conversation, AT-LOG-01), `test_telegram_finance_db.py` (every command, button flows, duplicate ✅, stale/forged/foreign buttons, unknown accounts, `/start`), `test_ledger_migration_db.py` (populated 0003 → 0004 → 0003 → 0004). The Phase 1 RLS coverage, privilege and definer-function inventories include the new objects |

### 12.8 Interpretations and limits (owner review requested)

1. **Active workspace = personal.** There is no `/ruang` yet (FR-WSP-03, `user_preferences`). The services accept an explicit workspace, and the tests exercise business workspaces seeded by the owner role, but the bot always uses the personal workspace.
2. **Accounts:** only the seeded "Kas" exists, and every income or expense goes to the member's default account. `/akun`, account management, opening balances and transfers by text or `/transfer` (FR-ACC-01…03, FR-TXN-07) are not built. The schema and `domain/ledger.py` already support two-leg transfers, so `/saldo` sums every account.
3. **Not built:** free-text capture and the local parser (FR-TXN-03, AT-PARSE-GOLDEN), the undo button (FR-TXN-13), type change (D21), revision history view (FR-TXN-12), the 6-month comparison (FR-RPT-03), CSV export, `/audit`, draft-expiry and prune jobs (expiry is enforced lazily on use), and `security.cross_tenant_probe` (R-12; NotFound is not audited).
4. **Command names:** the documented names (docs/02 §12) plus the Step 6 aliases `/tambah`, `/transaksi`, `/ringkasan` and the shortcuts `/pemasukan`, `/pengeluaran`.
5. **Open decisions applied with their documented defaults:** D2 (a) Editors edit any transaction, D15 negative balances allowed with ⚠️, D17 no period locking, D21 (a) the type is immutable, D22 (a) signed entries with leg names.

## 13. Step 6B remediation (revision `0005`; independent review of Step 6)

An independent review of `0411db8` concluded "not approved for staging" with twelve findings (F01–F12) and three risks to investigate (R01–R03). This section records what changed, the operating model that results, and the **consolidated operator procedure for deploying `0004` and `0005`** (§13.8), which replaces §12.6. Nothing in this section has been run against staging.

### 13.1 Findings and resolutions

| ID | Severity | Finding | Resolution | Regression tests |
|---|---|---|---|---|
| F01 | High | `confirm()` replay, `current()` and `cancel()` skipped authorization; a replay could show a later-edited or voided transaction to a downgraded or suspended user | Every finance session is authorized: with the method's permission, or at least `workspace.view` (denied to inactive users). A replay re-authorizes the draft's permission and `transaction.view`, requires the same membership epoch (§13.4), and never shows a voided transaction. `current()` offers only conversations the caller may still use. Voided rows are NotFound for roles without `transaction.view_deleted` everywhere (`services/finance.py::visible`) | `test_F01_*` (PostgreSQL), unit tests |
| F02 | Medium | Two concurrent first requests deadlocked: `FOR SHARE` on the membership, then `UPDATE … default_account_id` (a lock upgrade). Reproduced on `0411db8`: 16 deadlocks in 24 concurrent first writes | Seeding moved to its **own short transaction** (`SessionMode.SEED`) that locks the membership `FOR NO KEY UPDATE` first (no upgrade), then the workspace's category-name lock; it is idempotent and posts nothing, so it alone is retried (3 attempts) on deadlock/serialization failure. Request transactions are never re-run automatically: a transient failure stores nothing and is answered "coba lagi" (`RetryLaterError`) | `test_F02_*`: 20 concurrent writes and reads by 5 members (the same user several times) on an unseeded workspace: 0 errors, seeded once |
| F03 | Medium | `/saldo` read balances and totals in separate `READ COMMITTED` statements | Every read-only request is one `REPEATABLE READ READ ONLY` transaction (`SessionMode.READ`): all figures of a report come from one snapshot | `test_F03_a_report_reads_one_snapshot` (deterministic: a commit between two queries is not seen), `test_F03_reports_reconcile_under_concurrent_create_edit_and_void` |
| F04 | Medium | `/pengeluaran 2 juta` recorded Rp 2 (`juta` became the description) | `domain/money.py::split_leading_amount` uses the `parse_amount` grammar for the leading amount, separated units included; a unit-like word after a bare number (`2 jutaan`) is refused, never guessed | `test_F04_*` (domain, adapter, Telegram, PostgreSQL end to end) |
| F05 | Medium | The bot started on a database at `0003` | G-ENV-7 (`infra/db/guards.py::require_schema_revision`): before the poller lock, Telegram and the "ready" heartbeat, the revision read through `public.app_schema_revision()` must be in `SUPPORTED_SCHEMA_REVISIONS` (`{"0005"}`). Behind (`0003`, `0004`), never migrated, ahead or ambiguous → refused, exit 3. The bot never migrates | `test_schema_guard_migration_db.py` (AT-ENV-07), `test_schema_guard_revision.py` |
| F06 | Medium | Category lists were capped (90 buttons) and could hide `Lainnya`; no size protection | Category management and the draft's category step are **paged** (20 per page, ◀️/▶️). Every reply is measured as Telegram does (UTF-16 units after entity parsing; ≤ 100 buttons) before sending; an oversized one is replaced by a short explanation. The fake Bot API now refuses oversized messages like Telegram | `test_F06_*` (45 and 30 extra categories, worst-case escaping and astral characters) |
| F07 | Medium | Deactivated categories were not listed, so they could not be reactivated | `/kategori [pemasukan\|pengeluaran] nonaktif` and a "🗃️ Kategori nonaktif" button list them; detail → ✅ Aktifkan. Reactivation refuses an active namesake | `test_F06_F07_*`, `test_F07_deactivate_and_reactivate_end_to_end` |
| F08 | Medium | A stale ❌ cancelled a newer category prompt | `cancel(ref)` cancels only the conversation whose reference is exactly `ref`; category prompts have their own reference and ❌ button; `/batal` cancels whatever is in progress | `test_F08_*` |
| F09 | Medium | Confirming an expired draft rolled the `expired` status back (exception inside the transaction) | Expiry is persisted, the session commits, and the refusal is raised afterwards (confirm, buttons, text, prompts) | `test_F09_an_expired_draft_is_persisted_as_expired_on_confirm` |
| F10 | Medium | The confirmation did not show the account; a later default-account change redirected the draft; no pre-save correction | The account is resolved when the draft starts, stored in `drafts.proposal.account_id`, shown on every summary, and used at confirm (an archived account is refused, not replaced). ✅ Simpan · ✏️ Ubah · ❌ Batal (FR-TXN-04): Ubah corrects type, amount, category, description or date; every correction returns to the complete confirmation; "↩️ Kembali ke ringkasan" leaves it unchanged. **Choosing another account is not offered** (only "Kas" exists; deferred with account management) | `test_F10_*` |
| F11 | Low | `audit.view` was denied while a workspace was pending deletion | The read-only overlay allows every `*.view` permission (docs/08 §5) | `test_F11_role_permission_status_matrix`: 5 roles × 26 permissions × 2 workspace states × 3 user states against an independent expectation |
| F12 | Low | Audit events lacked a correlation id; denials lost their entity type | Every Telegram update gets a request id; every audit event of its transactions (including seeding and denials) stores it in `audit_events.request_id`; denials record the entity type (`draft`, `transaction`, `category`) | `test_F12_*` |

| ID | Investigation | Result |
|---|---|---|
| R01 | Can financial text reach the PostgreSQL server log? | Bound parameters are not logged (`log_parameter_max_length_on_error = 0`). **But a constraint violation logs its DETAIL**: a unique violation shows the key (a category name), a CHECK violation shows the failing row (a draft's proposal, with the description). Verified on a disposable 18.6 cluster with synthetic markers. Fixed in the application: name changes and seeding run under a per-workspace lock after a duplicate query, so the unique indexes never fire in normal operation (test: no SQL error at all during duplicate create/rename/reactivate and 4 concurrent creates). Residual risk: CHECK violations only on a bug. **Proposed (not applied)**: §13.7 |
| R02 | Authorization and archive changes concurrent with mutations | WRITE sessions lock the user, membership and workspace rows `FOR SHARE`; postings lock their category and account `FOR SHARE`. A suspension, role change, removal, workspace status change, category archive or account archive therefore waits for an in-flight authorized write, or is seen by it. Proven with `lock_timeout` on a competing connection, plus concurrent archive/confirm and three-way concurrent edit/void (one wins, the others are stale or not found) |
| R03 | Removal and rejoin | Revision `0005` bumps `workspace_memberships.version` on every role or status change (trigger), and every draft and category prompt stores the version it was started under. After a removal and rejoin (or any role change) the old conversation is expired, never revived; a downgrade is refused (and audited) first. Same-workspace privacy between two members: a member cannot see, confirm, correct or cancel another member's draft even with its reference |

Reproduction on the baseline `0411db8` (scratch worktree, same cluster): F02 16 deadlocks/24, F09 status stayed `pending`, F01 a suspended user received the amount, F08 the stale ❌ returned `True`, F04 `('2', 'juta sewa')`, F11 `False`. The same script passes on the remediated code.

### 13.2 Revision `0005_schema_guard_membership_epoch`

Additive: two functions and one trigger; no table, column or row changes.

| Object | Notes |
|---|---|
| `public.app_schema_revision()` | `LANGUAGE sql STABLE SECURITY DEFINER`, `SET search_path = pg_catalog, public`, owned by `finbot_owner`, `EXECUTE` for `finbot_app` only. Returns `alembic_version.version_num` if there is exactly one row, else NULL. `finbot_app` still has **no** privilege on `alembic_version` (AT-RLS-05 unchanged) |
| `public.membership_version_bump()` + `trg_membership_version` | `BEFORE UPDATE OF role, status ON workspace_memberships`: `version := GREATEST(NEW.version, OLD.version + 1)` when the role or status changes (an updater that bumps it itself is not bumped twice). Invoker, `EXECUTE` granted to nobody. Setting `default_account_id` does not bump |

Downgrade (dev/test/drill only) drops the three objects. Tested: populated `0004` → `0005` (rows unchanged) → `0004` (state identical) → `0005`; and failure injection (a conflicting function created first): the upgrade fails and the database is exactly at `0004`, including the statements that ran before the failure.

**Bot/schema compatibility:** the Step 6B bot requires `0005` and refuses `0004`. The Step 5/6 bots run on `0005` too (additive), so a rollback of the image does not need a schema rollback.

### 13.3 Session modes and lock order

| Mode | Isolation | Row locks at start | Used by |
|---|---|---|---|
| `READ` | `REPEATABLE READ READ ONLY` | none | every read-only request (history, detail, categories, reports, `current`) |
| `WRITE` | `READ COMMITTED` | `users` (own row) `FOR SHARE`, then `workspace_memberships` and `workspaces` `FOR SHARE` | every mutation, conversation step and denial-expiry |
| `SEED` | `READ COMMITTED` | `users` `FOR SHARE`, then the membership `FOR NO KEY UPDATE` and the workspace `FOR SHARE` | first-use seeding only |

Global order (every code path takes a subset of it, in this order, so no cycle exists): user → membership/workspace → conversation advisory lock (`finbot.conversation:<user>:<ws>`) or category-name advisory lock (`finbot.category-names:<ws>`) → category/account rows (`FOR SHARE` to post, `FOR UPDATE` to change) → transaction row (`FOR UPDATE`) → draft/pending-action row (`FOR UPDATE`). A deadlock or serialization failure (SQLSTATE 40P01/40001) rolls the whole request back and surfaces as `RetryLaterError`; only seeding is retried automatically.

### 13.4 Conversations

- A button carries an 8-hex reference of its draft or category prompt. The conversation is always the caller's own pending one (RLS `user_id = app_user_id()`); a mismatch is stale.
- `proposal.membership_version` / `payload.membership_version` = the membership version at start. On use: expired → `expired`; else the draft's permission is checked (Forbidden, audited, the conversation expires: R-9); else a different version → `expired`. Expiry is committed before the refusal.
- `proposal.account_id` = the account resolved at start (FR-ACC-05).
- Replay (✅ on a confirmed draft): same permission, `transaction.view`, same membership version, transaction still posted → "sudah disimpan sebelumnya" with the **current** version; otherwise answered as an expired button.

### 13.5 Start-up order

G-ENV-2 (database name) → G-ENV-3 (environment marker) → **G-ENV-7 (schema revision)** → G-ENV-6 (poller lock) → G-ENV-4 (`getMe`) → polling → heartbeat `ready`. A G-ENV-7 refusal prints `refused: database schema revision …` and exits 3 without taking the lock or contacting Telegram; the heartbeat says `failed`, so the healthcheck reports unhealthy.

### 13.6 Security gates (CI job `security`)

| Gate | Tool and pin | Scope | Fails on |
|---|---|---|---|
| Secret detection | gitleaks 8.30.1 (linux x64 tarball, SHA-256 `551f6fc8…70eb` from the release checksums) | whole git history (`fetch-depth: 0`), values redacted in output | any finding |
| Dependency audit | pip-audit 2.10.1 via `uvx` (PyPI version pin) | `uv export --frozen --all-groups` (hash-pinned requirements; `--require-hashes --disable-pip`, nothing installed) | any known vulnerability (`--strict`) |
| Lockfile and Dockerfile | Trivy 0.75.0 (tarball SHA-256 `c6e65abd…3f3f`) | `trivy fs` vuln, secret and misconfig, V1 files skipped | High or Critical |
| Image | Trivy 0.75.0 | the V2 image built in the job from the pinned base (not pushed) | High or Critical, fixed or not (NFR-SEC-06 as approved). An exception needs an owner-approved `.trivyignore` entry with reason and expiry |

`make security` runs the first three locally (tool paths via `GITLEAKS=`/`TRIVY=`). None of them needs a secret.

Local results on 2026-10-25 (same pinned versions, binaries checksum-verified in a scratch directory, nothing installed): gitleaks: no leaks in the V2 working tree and in 17 commits of V2-path history (V1 paths were not scanned: V1 is out of bounds for this work; **the CI job scans the whole history, V1 included, and may report findings there that need triage**); pip-audit: no known vulnerabilities in the 485-line locked set; Trivy fs: 0 High/Critical in `uv.lock`, 0 Dockerfile misconfigurations. **The image scan has not run anywhere** (the image has never been built; `claude` has no Docker). The workflow has not run on GitHub (nothing pushed).

### 13.7 Proposed: PostgreSQL error detail for the app role (R01; not applied)

The application no longer triggers name-revealing unique violations, but any CHECK violation (a bug) would log the failing row. Proposed for staging (and production later), run once by root/`deploy` as the PostgreSQL superuser, owner approval required:

```sql
ALTER ROLE finbot_app IN DATABASE finbot_staging SET log_error_verbosity = 'terse';
```

Verified on a disposable cluster: `finbot_app` sessions then log errors without DETAIL (the failing row and key values disappear), while `finbot_owner` keeps full detail for migrations and operator work. It takes effect for new connections (restart the bot). Trade-off: less detail when debugging app errors, which the structured application log covers by error type. Alternative (not recommended): `-c log_error_verbosity=terse` for the whole server in `deploy/staging/compose.yml`, which also hides detail from migrations.

### 13.8 Consolidated staging procedure for `0004` + `0005` (operator-only; nothing here has been run)

Every step needs owner approval and is run by `deploy` (Docker) or root (secrets) per `deploy/staging/README.md`. `F=deploy/staging/compose.yml`; `owner_psql` is the README §7 helper. Stop at the first unexpected result.

1. **Predeployment checks.** `git log -1` on the reviewed checkout = the approved commit; `docker compose -f $F ps` (postgres healthy, bot stopped or the Step 5 bot running); `df -h /var/lib/docker` ≥ 5 GiB; `docker compose -f $F run --rm migrate current` → `0003` (if `0001`–`0003` are not applied yet, do README §7 first, up to `0003`); `migrate history` → `0001 … 0005`; README §9.1 preflight (token file present, `bot-identity.env` valid, `check-config` exit 0).
2. **Image provenance and revision.** Build per README §6.2 from the reviewed commit; `docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' finbot-v2:staging` = that commit; base digest as pinned in `deploy/docker/Dockerfile`; `finbot bot --dry-run` lists `/catat … /kategori` and `callback_query`. Keep the previous image tagged `finbot-v2:staging-<previous sha>` for rollback.
3. **Backup.** README §12.1; record the file name and `sha256sum` in the deployment log.
4. **Backup validation.** `pg_restore --list <file> | head` succeeds and lists `alembic_version`, `workspace_memberships`; the file is non-empty and in the 0700 directory.
5. **Restore rehearsal (isolated).** Restore the dump into a **separate, disposable** database or cluster (never `finbot_staging`) with the README §12.2 `pg_restore` flags and compare row counts. Rehearsed locally with synthetic data on 2026-10-25 (`pg_dump` as `finbot_backup`, `pg_restore --no-owner --exit-on-error --single-transaction` as `finbot_owner`: counts identical, 13 RLS-forced tables, definer ownership kept, G-ENV-2/3/7 pass on the copy, the app reads the correct balance). **Recovery of a real staging backup has not been proven**; do this step before relying on it.
6. **Migration SQL review.** `docker compose -f $F run --rm --no-deps migrate sql upgrade 0003:0005 > ~/finbot-staging-migrations/upgrade_0003_0005_$REV.sql`; check the guard header, compare with `migrations/versions/0004_*.py` and `0005_*.py`, no credentials. `0004`: new tables, one nullable column with an FK on `workspace_memberships` (brief `ACCESS EXCLUSIVE`, `lock_timeout 10s`). `0005`: two functions and a trigger.
7. **Controlled bot shutdown.** `docker compose -f $F stop bot` (graceful: README §9.3); confirm `ps bot` shows it stopped and its last log lines show `bot.stopped` and `poller_lock.released` (README §9.3).
8. **Migration by the operator.** `docker compose -f $F run --rm migrate upgrade 0005; echo "exit $?"`: one transaction; non-zero leaves `0003` (check with `current`).
9. **Post-migration verification.** §12.5 queries (expect `0005` instead of `0004` for the version) and §13.9.
10. **Application start and health.** `docker compose -f $F up -d bot`; `ps bot` healthy within ~60 s; logs show `poller_lock.acquired`, `bot.polling_started` and no `refused:`; `finbot healthcheck --json`; restarts=0; second-instance refusal (README §9.3). A `refused: database schema revision` means step 8 did not reach `0005`.
11. **Telegram smoke tests** (synthetic test account, staging bot only): `/bantuan`; `/pengeluaran 2 juta sewa` → shows Rp 2.000.000 and "Akun: Kas" → ✏️ Ubah → Jumlah → `1,5 juta` → ✅ → press ✅ again ("sudah disimpan sebelumnya"); `/catat` → ❌ on an older message (stale, nothing cancelled); `/riwayat` → detail → 🗑️ → `/riwayat dihapus` → ♻️; `/saldo`; `/laporan`; `/kategori tambah pengeluaran Uji` → deactivate → `/kategori nonaktif` → ✅ Aktifkan. Logs: no message text, amounts or tokens (README §10).
12. **Rollback and forward-fix decisions.**
    - Application problem, schema fine: `stop bot`; retag the previous image. A Step 5/6 image runs on `0005` (additive). `up -d bot`.
    - `migrate upgrade` failed: nothing applied (one transaction); fix and repeat step 8.
    - Schema wrong, no tester data yet: README §12.2 with the step-3 backup (downgrades are refused in staging, S9).
    - Testers have recorded data: prefer a reviewed forward-fix revision; a restore loses everything since step 3.
    - Optional, separately approved: §13.7 (`log_error_verbosity` for `finbot_app`).

### 13.9 Verification after `upgrade 0005` (read-only, as `finbot_owner`)

```sql
SELECT version_num FROM public.alembic_version;                                     -- 0005
-- The guard function: owner-owned definer, executable by the app only; the app has no table access.
SELECT p.prosecdef, pg_get_userbyid(p.proowner),
       has_function_privilege('finbot_app', p.oid, 'EXECUTE'),
       has_function_privilege('public', p.oid, 'EXECUTE'),
       has_table_privilege('finbot_app', 'public.alembic_version', 'SELECT')
FROM pg_proc p WHERE p.proname = 'app_schema_revision';                             -- t | finbot_owner | t | f | f
-- The membership-epoch trigger exists and is enabled.
SELECT tgname, tgenabled FROM pg_trigger WHERE tgname = 'trg_membership_version';    -- trg_membership_version | O
SELECT has_function_privilege('finbot_app', 'public.membership_version_bump()', 'EXECUTE');  -- f
```

### 13.10 Tests added in Step 6B

| File | Covers |
|---|---|
| `tests/integration/test_finance_remediation_db.py` | F01 (replay after edit, downgrade, promotion, void, suspension, removal; `current`), F02 (concurrent first use, read-only reads), F03 (snapshot, concurrent reconciliation), F08, F09, F10 (default-account change, archived account, every correction), F06/F07, F12, R01 (no SQL error on duplicates), R02 (lock waits for suspension, role change, removal, workspace status, category and account archive; concurrent archive/confirm; concurrent edit/void), R03 (removal and rejoin, version trigger), two-member draft privacy |
| `tests/migration/test_schema_guard_migration_db.py` | AT-ENV-07 (head passes; `0003`, `0004`, never migrated, unexpected, two rows, no rows refused; the bot never polls or reports ready on `0004`), `0005` up/down/up on populated data, failure injection |
| `tests/integration/test_telegram_finance_db.py` (3 more) | separated units + correction + replay, stale ❌, deactivate/reactivate, end to end |
| `tests/unit/test_schema_guard_revision.py` | offline `0005` SQL (one narrow grant, no table privilege, additive), supported revision = head |
| `tests/unit/test_telegram_finance.py`, `test_finance_adapter.py`, `test_finance_domain.py`, `tests/authz/test_policy_matrix.py` | every command and alias, separated units (12 cases) and ambiguous ones, corrections, restart replay, stale buttons, category pages and reactivation, expiry, suspension, oversized replies, `RetryLaterError`, the role × permission × status matrix |

### 13.11 Scope and owner decisions

Implemented scope is §12 plus §13.1. Still deferred (unchanged from §12.8 unless stated): workspace switching (`/ruang`), account management and transfers (so the draft's account cannot be changed in ✏️ Ubah yet), free-text capture, undo, type change of posted transactions (D21), revision view, 6-month comparison, CSV export, `/audit`, expiry/prune jobs, `security.cross_tenant_probe`.

Decisions for the owner (not treated as approved):

1. **§13.7** `log_error_verbosity = terse` for `finbot_app` (R01).
2. **Trivy strictness:** unfixed High/Critical base-image CVEs fail the image gate as NFR-SEC-06 is written; if that blocks releases, approve either `--ignore-unfixed` or a `.trivyignore` process.
3. **pip-audit pin:** the tool is pinned by version (PyPI), not by hash; approve, or ask for a hash-pinned tool requirements file.
4. **A role change expires the member's open conversation** (R03 design), including a promotion; accept, or ask for promotions to keep it.
5. **D25** (CI platform), and the open defaults applied in §12.8 (D2, D15, D17, D21, D22), remain open.
