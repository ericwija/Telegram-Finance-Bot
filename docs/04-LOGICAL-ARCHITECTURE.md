# 04 — Logical Architecture

| | |
|---|---|
| **Status** | Draft; reconciled with approved D1, D5, D9, D18 (rev. 2, 2026-10-23) |
| **Date** | 2026-10-23 |
| **Related** | [05 Data Model](05-DATA-MODEL.md) · [08 RBAC](08-RBAC-MATRIX.md) · [09 Security](09-TENANT-ISOLATION-AND-SECURITY.md) |

## 1. Architectural style

A **modular monolith** with hexagonal (ports and adapters) layering. There is one deployable Python package and one PostgreSQL database. Channels (Telegram now, web later) are thin adapters over a shared, channel-agnostic application layer.

Why not microservices: the scale is a few hundred users, there is one operator, and strong transactional consistency matters (ownership transfer, batch confirm). A monolith with strict module boundaries keeps the option to split later.

## 2. Context diagram

```
          ┌────────────┐                    ┌──────────────────────────────┐
 Users ──►│  Telegram  │◄── long polling ──►│                              │
          │  Bot API   │                    │   finbot (Python, asyncio)    │
          └────────────┘                    │                              │
                                            │  ┌────────────────────────┐  │      ┌──────────────┐
 (future) Browser ── HTTPS ──► reverse ────►│  │ Web adapter (FastAPI)  │  │─────►│ Gemini API   │
                               proxy        │  └────────────────────────┘  │      └──────────────┘
                                            │                              │
 Operator ── SSH ──► CLI (finbot-admin) ───►│                              │
                                            └──────────────┬───────────────┘
                                                           │ asyncpg (internal Docker network)
                                                  ┌────────▼────────┐        ┌──────────────────┐
                                                  │ PostgreSQL 18   │──dump─►│ backup job ──►   │
                                                  │ (RLS enabled)   │        │ S3-compatible    │
                                                  └─────────────────┘        └──────────────────┘
```

## 3. Layers and dependency rule

```
adapters/  (telegram, web[future], cli, jobs)      ── depends on ──►  services/
services/  (use cases; transaction boundary)        ── depends on ──►  authz/, domain/, ports
authz/     (policy: role → permission; rules)       ── depends on ──►  domain/
domain/    (entities, value objects, parsing)       ── depends on ──►  (nothing external)
infra/     (SQLAlchemy repos, Gemini client, storage, clock) ── implements ──► ports
```

- Dependencies point **inward only** (enforced by import-linter, NFR-MNT-01).
- `domain/` is pure Python: `Money` (int rupiah), `TransactionType`, the date-expression resolver, and the Indonesian amount parser.
- `services/` is the **only** place that opens a Unit of Work, checks authorization and writes audit events.
- Adapters translate channel input into service calls and service results into channel output. They contain **no business rules**.

## 4. Proposed package layout

V2 lives alongside the V1 `main.py` until cutover. V1 is not modified by V2 work.

```
src/finbot/
├── __main__.py                 # composition root: build container, start bot / jobs
├── config.py                   # pydantic-settings; secrets from /run/secrets
├── logging.py                  # JSON logs + PII redaction filter
├── domain/
│   ├── money.py                # Money = NewType(int); parse/format IDR
│   ├── models.py               # dataclasses: Transaction, Entry, Account, Category, Workspace, Membership…
│   ├── ledger.py               # build/validate entry sets (D1 sign & sum rules); pure functions
│   ├── enums.py                # Role, TxnType, AccountType, WorkspaceKind, Source
│   ├── dates.py                # Indonesian relative-date resolution in a given tz
│   └── parsing/
│       ├── amount.py           # 50rb / 1,5jt / 1.000.000 (fixed per audit B-2)
│       └── local_parser.py     # keyword fallback, word-boundary matching
├── authz/
│   ├── permissions.py          # Permission enum
│   ├── policy.py               # ROLE_PERMISSIONS table + rule functions (doc 08)
│   └── context.py              # Principal, WorkspaceContext
├── services/
│   ├── uow.py                  # UnitOfWork: DB txn + RLS context + audit buffer
│   ├── onboarding.py           # invites, user + personal workspace creation
│   ├── workspaces.py           # create/rename/delete/transfer/switch
│   ├── members.py              # invite/accept/change role/remove/leave
│   ├── accounts.py, categories.py
│   ├── drafts.py               # propose → edit → confirm/cancel/expire
│   ├── transactions.py         # update/delete/restore/history/revisions
│   ├── capture.py              # text & receipt → drafts (AI + fallback)
│   ├── reports.py              # cash flow, balances, export
│   └── audit.py
├── ports/                      # Protocols: repositories, AIParser, Clock, Notifier, BlobStore
├── infra/
│   ├── db/
│   │   ├── engine.py           # async engine, session factory, set_config helpers
│   │   ├── tables.py           # SQLAlchemy Core/ORM mappings
│   │   └── repositories/       # one per aggregate; every method takes WorkspaceContext
│   ├── ai/gemini.py            # google-genai async client, schema output, retries
│   └── telegram_notifier.py
├── adapters/
│   ├── telegram/
│   │   ├── app.py              # PTB Application builder, handler registration
│   │   ├── middleware.py       # update → Principal resolution, rate limit, request_id
│   │   ├── callbacks.py        # callback-data codec (v1:<action>:<ref>)
│   │   ├── handlers/           # start, workspace, capture, drafts, history, reports, members…
│   │   ├── render.py           # HTML rendering + escaping + pagination
│   │   └── messages_id.py      # Indonesian message catalog
│   ├── jobs/                   # draft expiry, invite expiry, purge, quota reset, backup-age check
│   └── cli/                    # finbot-admin: invites, suspend, import-v1, restore-verify
migrations/                     # Alembic (owner role)
tests/                          # unit / integration / authz / e2e (doc 07)
deploy/
├── compose.base.yml            # shared service definitions (pinned postgres:18 image, healthchecks)
├── compose.dev.yml             # local only: 127.0.0.1 port binding, never on a VPS
├── compose.staging.yml         # project finbot-staging
├── compose.prod.yml            # project finbot-prod (Phase 7)
├── postgres/initdb/00-roles.sh # login roles from secrets (empty volume only)
├── secrets/{staging,prod}/     # git-ignored, 0400
├── backup/                     # dump/encrypt/upload scripts
└── runbooks/                   # restore, cutover, break-glass, minor-upgrade
```

## 5. Key components

### 5.1 Principal resolution (Telegram middleware)

1. Every update gets a `request_id` (UUIDv7).
2. `telegram_user_id` → `telegram_identities` → `users`. If there is no identity, only onboarding deep links are allowed (FR-IDN-02). If the user is suspended, the update is refused.
3. The **active workspace** is loaded from `user_preferences.active_workspace_id`, and the active membership is verified. If it fails, the active workspace falls back to personal (FR-WSP-07).
4. The result is `Principal(user_id, telegram_user_id, is_platform_admin)` plus a `WorkspaceContext(workspace_id, kind, role, timezone)`. Both are **built server-side**. Nothing in the update payload chooses the workspace except an explicit `/ruang` switch, which is itself membership-checked.

### 5.2 Authorization service

- `authorize(ctx, Permission, resource=None)` raises `NotFound` or `Forbidden` according to G-3 and G-4.
- It is a pure function of (role, workspace kind, workspace status, permission, resource attributes). The rules and the matrix are in doc 08.
- **Every service method's first line** calls it. A test introspects all public service methods and checks that each is decorated or calls `authorize` (doc 07 AT-STRUCT-01).

### 5.3 Unit of Work and RLS context

```python
async with uow.begin(ctx) as tx:           # BEGIN
    # SELECT set_config('app.user_id',      :uid, true),
    #        set_config('app.workspace_id', :wid, true);   -- transaction-local
    ...repository calls (always filter by ctx.workspace_id as well)...
    tx.audit("transaction.create", entity=..., meta=...)
# COMMIT  (audit rows inserted in the same transaction)
```

- `set_config(..., true)` is **transaction-local**, so pooled connections cannot leak context between requests.
- Repositories add `WHERE workspace_id = :wid` explicitly. RLS is the backstop (defense in depth), not the primary filter.
- User-scoped operations (list my workspaces, accept an invitation) run with `app.workspace_id` unset and use the policies keyed on `app.user_id` (doc 09 §4).

### 5.4 Drafts and the Telegram callback protocol

- Conversation state is stored in **PostgreSQL** (`drafts`, `user_preferences`, `pending_actions`), not in PTB `PicklePersistence`. This fixes audit S-8 and is restart-safe.
- Callback data format: `v1:<action>:<opaque_ref>`, at most 64 bytes. `opaque_ref` is a draft or pending-action UUID (base64url, 22 chars), never a raw workspace ID.
- When a callback arrives: load the referenced row, check that `row.user_id == principal.user_id`, check that the row's workspace membership is still active with the required permission (G-2), check expiry and status, then act. A stale or foreign reference → "Tombol ini sudah kedaluwarsa."
- Pending multi-step flows (rename, invite role selection, deletion confirmation) use `pending_actions` rows with a 15-minute TTL.

### 5.5 Capture pipeline

```
text/photo ─► rate-limit ─► quota check ─► AIParser (Gemini, 8 s timeout, schema output)
                                   │                │ invalid/timeout/quota
                                   │                ▼
                                   │          LocalParser
                                   ▼
                     ProposalResolver (workspace-scoped: names → account/category IDs,
                                       date expr → date in ws tz, amount → int)
                                   ▼
                         drafts (status=pending) ─► render confirmation (G-8)
```

- The AI client is async (`google-genai`). It retries **only** on 429 and 5xx, with exponential backoff and jitter, at most 2 retries, all within the overall timeout.
- The model ID comes from config (D8). Prompts are versioned (`prompt_version` is stored on the draft for debugging).

### 5.6 Background jobs

The MVP runs jobs **in-process** with the PTB JobQueue (APScheduler), protected by advisory locks so they are safe if a second process ever starts.

| Job | Schedule | Scope handling |
|---|---|---|
| Expire drafts and pending actions | every 5 min | Iterates per workspace, setting the RLS context for each |
| Expire invitations and link tokens | hourly | — |
| Purge workspaces past their grace period | daily 02:00 WIB | Per workspace, owner role connection (doc 09 §4.4) |
| Prune receipt JSON > 90 d, ai_usage > 13 mo | daily | Per workspace |
| Backup-age watchdog | hourly | Platform |
| Future: scheduled reports | per member preference | Per workspace + membership re-check |

Rule: **every job that touches tenant data runs through the same service layer with a `WorkspaceContext` whose principal is `system:<job>`**. Jobs re-check the target members' status before notifying them.

The **backup** itself runs in a separate container (doc 06 §Deployment), not in the bot process.

### 5.7 Future web adapter (D16)

- FastAPI app in `adapters/web/`, sharing `services/` and `authz/`.
- The web session resolves to the same `Principal`, and the workspace is selected per request through a URL path segment that is verified against membership.
- Telegram linking (FR-IDN-09) is a service in `onboarding.py`. The web creates the token, and the Telegram `/start lnk_…` handler consumes it.
- Runs as a separate container, behind Caddy or Traefik with TLS. The bot container stays unchanged.

## 6. Request flows

### 6.1 Text capture → confirm

```
User: "makan siang 35rb pakai gopay"
 └► middleware: principal U, ws W (role Editor) ─► authorize(transaction.create)
 └► capture.propose_from_text(ctx, text)
      AI → {type: expense, amount: 35000, account_hint: "gopay", category_hint: "makanan"}
      resolver → account=GoPay(W), category=Makanan(W), date=today(W.tz)
      INSERT drafts(..., status=pending)  + audit(draft.create)
 └► bot: "🏢 Toko Sinar · Pengeluaran Rp 35.000 · GoPay · Makanan · 23 Okt 2026  [✅][✏️][❌]"
User taps ✅  (callback v1:dc:<draft_ref>)
 └► load draft FOR UPDATE; draft.user_id==U; membership(U,W) active & role has transaction.create
 └► INSERT transactions (header, type=expense, amount=35000, entry_revision=1)
    INSERT transaction_entries (leg=single, account=GoPay, amount=-35000, revision=1)
    INSERT transaction_revisions(v1) ; UPDATE drafts SET status=confirmed
    audit(transaction.create)  — single DB transaction; deferred INV check at COMMIT
 └► bot: "✅ Tersimpan" + today's summary for W
```

### 6.1a Transfer confirm (D1)

```
User: "transfer dari BCA ke GoPay 500rb"  → draft proposal legs:
      [{leg: source, account: BCA, amount: -500000}, {leg: destination, account: GoPay, amount: +500000}]
✅ → one DB transaction:
      INSERT transactions (type=transfer, amount=500000, category_id=NULL, entry_revision=1)
      INSERT transaction_entries (source, BCA, -500000, rev 1)
      INSERT transaction_entries (destination, GoPay, +500000, rev 1)
      COMMIT → trg_entries_balanced verifies: exactly 2 legs, sum = 0, accounts differ, both in W
      (if any INSERT fails or a leg is missing → whole transaction rolls back; nothing persisted)
```

The service builds legs only through `domain.ledger.build_entries(type, amount, accounts)`, which is the single place that encodes the sign rules (mirroring the DB CHECKs), so the application and database cannot disagree.

### 6.2 Role downgrade race (G-2)

An Admin opens a transaction detail, which shows a 🗑️ button. The Owner then demotes that Admin to Editor. When the former Admin taps 🗑️, the callback handler re-evaluates `transaction.delete` with the *current* role (Editor) → Forbidden → message plus an `authz_denied` audit event.

### 6.3 Ownership transfer

1. The Owner runs `/anggota` → member → "Jadikan Pemilik" → confirm. This creates a `pending_actions(kind=ownership_transfer, target=user B, expires=24h)`, and B gets a DM with Accept and Decline buttons.
2. B accepts. In one transaction: lock both membership rows → verify that A is still Owner and B is still an active member → set A to admin and B to owner (the partial unique index guarantees one owner) → audit `workspace.ownership_transferred`.

## 7. Technology choices (to be pinned in Phase 1)

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.13 (D23, pending) | Fixes the audit finding: 3.10 is EOL |
| Packaging | `uv` + committed `uv.lock` (D23, pending) | Hashed, reproducible; `uv sync --frozen` in images and CI |
| Telegram | python-telegram-bot (current stable major) | async; JobQueue |
| Database | **PostgreSQL 18** (D9, approved) in Docker, pinned minor + digest | RLS, partial indexes, `NULLS NOT DISTINCT`, native `uuidv7()`, `security_invoker` views |
| DB access | SQLAlchemy 2.x async + asyncpg | Core for reports, ORM optional |
| Migrations | Alembic | runs as the owner role in a one-shot container |
| Validation / config | Pydantic v2, pydantic-settings | |
| AI | `google-genai` SDK | replaces deprecated `google-generativeai` |
| Images | Pillow (patched) | downscale before upload |
| IDs | UUIDv7 generated in the application | time-ordered, non-enumerable |
| Testing | pytest, pytest-asyncio, testcontainers (Postgres), hypothesis | doc 07 |
| Quality | ruff, mypy, import-linter, gitleaks, pip-audit, Trivy | CI |
| Backup | `pg_dump -Fc` + `age` encryption + `rclone` to S3-compatible storage | doc 09 §9 |

## 8. Deployment topology (MVP)

### 8.1 Topology per environment

```
Ubuntu 24.04 LTS VPS  (ufw: allow 22/tcp key-only; deny all other inbound)
├── V1 (unchanged until cutover): root docker-compose.yml, container "finance-bot"
├── docker compose project "finbot-staging"   (D18; host placement per D24)
│     postgres · migrate · bot · backup        volumes: finbot-staging_pgdata
└── docker compose project "finbot-prod"      (created in Phase 7)
      postgres · migrate · bot · backup        volumes: finbot-prod_pgdata

Within each project:
    ├── postgres  (PostgreSQL 18, pinned; network "db" only; NO published ports)
    ├── migrate   (one-shot: alembic upgrade head, as finbot_owner)
    ├── bot       (as finbot_app; depends_on postgres healthy + migrate completed_successfully)
    └── backup    (pg_dump → age → rclone, as finbot_backup)
networks: db (internal: true; no egress) + egress (bot: Telegram, Gemini; backup: object storage)
```

### 8.2 PostgreSQL 18 container specification (D9, approved)

| Aspect | Specification |
|---|---|
| **Image** | Official `postgres` image, **PostgreSQL 18**, Debian variant, pinned as `postgres:18.<minor>-<debian-codename>@sha256:<digest>`. The exact minor version and digest are chosen in P1 Step 3 from the then-current Docker Official Image and recorded in `deploy/compose.base.yml`. **Never** `latest`, `18` or an unpinned tag. Minor updates go through a reviewed PR (Renovate or Dependabot), are applied after a fresh backup, and are noted in the changelog. A major upgrade (19+) needs a separate written plan (dump/restore or `pg_upgrade`); it never happens by changing the tag. |
| **Persistent storage** | A named volume per environment (`finbot-<env>_pgdata`). **Verify in Step 3:** starting with PostgreSQL 18, the official image changed its default data layout (`PGDATA` under a version-specific directory beneath `/var/lib/postgresql`). The volume must be mounted where the pinned image's documentation says, and `PGDATA` set explicitly in compose so a future image default change cannot silently create a new, empty cluster. Bind mounts to arbitrary host paths are not used. |
| **No public port** | The `postgres` service has **no `ports:` key** in the staging or production compose files. It is attached only to the `db` network (`internal: true`). A CI check (AT-DEP-01) fails if `ports:` appears under `postgres` in `compose.staging.yml` or `compose.prod.yml`. Developer machines may use `compose.dev.yml`, which binds `127.0.0.1:55432` only, and that file is never used on a VPS. |
| **Healthcheck** | `pg_isready -h 127.0.0.1 -p 5432 -d finbot_<env> -U postgres`; interval 10 s, timeout 5 s, retries 6, start_period 30 s. `migrate` runs `depends_on: postgres: condition: service_healthy`; `bot` runs `depends_on: migrate: condition: service_completed_successfully`. The bot also has its own healthcheck: a heartbeat file updated by a job tick and checked by `finbot healthcheck`. |
| **Authentication** | `POSTGRES_PASSWORD_FILE=/run/secrets/db_superuser_password`; `POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256`; `password_encryption=scram-sha-256`. The superuser is used only by the init script and break-glass. |
| **Initialization** | `/docker-entrypoint-initdb.d/00-roles.sh` (runs only on an empty volume) creates the login roles `finbot_owner`, `finbot_app` and `finbot_backup`, with passwords read from Docker secrets, plus `finbot_readonly NOLOGIN`, and the database `finbot_<env>` owned by `finbot_owner`. **All grants, RLS and schema** are created by Alembic as `finbot_owner`, not by the init script. |
| **Environment marker** | The first Alembic revision creates `platform_meta(key text primary key, value text)` and inserts `('environment', '<env>')` from migration config. The bot refuses to start if the marker differs from `FINBOT_ENV` (guard G-ENV-3, §8.3). |
| **Server settings** | Passed through `command: -c ...` and kept in version control: `shared_buffers=256MB`, `max_connections=50`, `log_min_duration_statement=500ms`, `log_connections=on`, `log_disconnections=on`, `log_statement=ddl`. Statement logging does not include parameters (PII). Data checksums: enabled. **Verify in Step 3** that PG 18 `initdb` enables them by default; otherwise pass `--data-checksums` in `POSTGRES_INITDB_ARGS`. |
| **Resources** | `mem_limit: 1g` on a 4 GB VPS (staging: 512 MB), `shm_size: 256m`, `restart: unless-stopped`, `stop_grace_period: 60s`. |
| **Hardening** | `security_opt: [no-new-privileges:true]`; no Docker socket mounts; the volume is never shared with other services; the `backup` service connects over the `db` network, not by sharing the volume. |

### 8.3 Staging isolation (D18, approved)

| Aspect | Staging | Production |
|---|---|---|
| Compose project / file | `finbot-staging` / `deploy/compose.staging.yml` (extends `compose.base.yml`) | `finbot-prod` / `deploy/compose.prod.yml` |
| Telegram bot | **Separate bot**, e.g. `@<name>_staging_bot`, with its own token | The production bot (V1's token after cutover C-6, or a new one) |
| PostgreSQL | Separate container, volume, database `finbot_staging`, and distinct role passwords | Database `finbot_prod` |
| Networks | Compose-scoped networks (`finbot-staging_db`); never shared | `finbot-prod_db` |
| Secrets | `deploy/secrets/staging/*` | `deploy/secrets/prod/*` (git-ignored; mode 0400) |
| Gemini key | A separate key (separate Google Cloud project), low quota and budget | Production key (paid tier, D8) |
| Backups | A separate bucket or prefix **and a separate `age` key pair** | Production bucket and offline key |
| Data | **Synthetic only.** Production personal data never enters staging. Backup-restore drills and the V1-import rehearsal run in an **ephemeral drill environment** (`finbot-drill`) that is destroyed afterwards (D24) | Real |
| Users | Owner plus test Telegram accounts | Invited users |

**Startup guards** (the bot refuses to start, with a clear log message, if any of these fail):

| Guard | Check |
|---|---|
| G-ENV-1 | `FINBOT_ENV` ∈ {`dev`, `test`, `staging`, `production`, `drill`} and is set explicitly (no default) |
| G-ENV-2 | `SELECT current_database()` = `finbot_${FINBOT_ENV}` (`finbot_prod` for production) |
| G-ENV-3 | `platform_meta.environment` = `FINBOT_ENV`. This stops a production dump restored into staging from being served by the staging bot |
| G-ENV-4 | Telegram `getMe().username` = `TELEGRAM_EXPECTED_BOT_USERNAME`. This stops the production token from being used with the staging config, or the reverse |
| G-ENV-5 | In `production` and `staging`, every secret comes from a `*_FILE` path. Inline secret environment variables are rejected |
| G-ENV-6 | Single-poller advisory lock acquired (NFR-AVL-06) |

**Coexistence with V1:** V1 keeps its root `docker-compose.yml`, container name `finance-bot` and token. V2 compose projects use different project names, container names, networks and volumes, so `docker compose` commands for V2 cannot affect V1. V2 must not be configured with the V1 token before cutover step C-6 (doc 06).
