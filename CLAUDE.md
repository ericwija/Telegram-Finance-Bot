# CLAUDE.md — Telegram-Finance-Bot

Guidance for Claude Code (and humans) working in this repository.

## Project state (as of 2026-10-23)

- **V1 (legacy, in production):** `main.py`, a single-file python-telegram-bot app with Google Sheets as storage. Known issues are catalogued in `PROJECT_AUDIT.md`.
- **V2 (Phase 1 in progress):** a multi-tenant, Telegram-first personal and business finance app on PostgreSQL 18. Step 1 (decision reconciliation) is done; Step 2 (package skeleton, config, testing foundation; checklist in doc 06 §6) awaits owner approval and D23. **Approved decisions (2026-10-23): D1** (transfers = header + 2 balancing entries), **D5** (RLS from Phase 1), **D9** (PostgreSQL 18 in Docker, pinned, no public port), **D11** (Sheets is not a system of record; optional export later), **D18** (separate staging bot and DB). Its full specification is in `docs/` (read these before any V2 work):

| Doc | Read when |
|---|---|
| `docs/01-PROJECT-VISION.md` | Always first: scope, principles, **open decisions D1–D20** |
| `docs/02-FUNCTIONAL-REQUIREMENTS.md` | Implementing any feature (FR-IDs, global rules G-1…G-8) |
| `docs/03-NON-FUNCTIONAL-REQUIREMENTS.md` | Performance, logging, privacy, quality gates |
| `docs/04-LOGICAL-ARCHITECTURE.md` | Package layout, layers, request flows |
| `docs/05-DATA-MODEL.md` | Any schema or migration work |
| `docs/06-IMPLEMENTATION-ROADMAP.md` | Which phase is current; acceptance criteria; rollback |
| `docs/07-TEST-AND-ACCEPTANCE-PLAN.md` | Writing tests; test IDs (AT-*) |
| `docs/08-RBAC-MATRIX.md` | Anything touching permissions |
| `docs/09-TENANT-ISOLATION-AND-SECURITY.md` | Anything touching data access, RLS, secrets, backups |

If code and docs disagree, **stop and ask**. Do not silently pick one. Decisions still marked open in doc 01 §8 must not be assumed beyond their documented default without saying so.

## Non-negotiable rules for V2 code

1. **Tenant isolation.** Every tenant-owned table has `workspace_id`, composite FKs `(workspace_id, id)`, and RLS `ENABLE` + `FORCE` with a policy. Every repository method takes a `WorkspaceContext` and filters by it. The workspace comes from server-side state, never from callback data or other client input.
2. **Authorization.** Every public service method calls `authorize()` / `@requires(...)` first, using the *current* membership (no caching of roles across requests). A resource in another workspace raises **NotFound**, never Forbidden. When a permission changes, update `authz/policy.py`, `docs/08-RBAC-MATRIX.md` **and** `tests/authz/expected_matrix.py` together.
3. **Money is `int` rupiah.** No `float` or `Decimal` in money paths. The transaction header `amount` is positive; **balances come only from signed `transaction_entries`** (D1): income has 1 entry (+), expense 1 entry (−), transfer exactly 2 (source −, destination +, summing to zero). Build entries only through `domain/ledger.py`. Never UPDATE or DELETE entries; edits write a new entry revision, and cancellation is a **void** of the whole transaction. Transfers never count as income or expense.
4. **Dates** are computed in the workspace timezone (default `Asia/Jakarta`); instants are stored as UTC `timestamptz`.
5. **Confirm before commit.** AI and parsers only create drafts. Nothing reaches `transactions` without explicit user confirmation.
6. **Audit** every state change in the same DB transaction (event catalogue in doc 09 §6).
7. **No PII or financial text in logs**: no descriptions, raw messages, prompts, AI responses or amounts. Use the structured logger only, never `print`.
8. **Secrets** come only from `*_FILE` paths and Docker secrets. Never hard-code, log or commit them, and never echo `.env` contents.
9. **Schema changes only through Alembic**, expand/contract style, each with a downgrade or a documented forward-fix.
10. **Layering:** `adapters → services → authz/domain`; `infra` implements `ports`. No imports from `adapters` into `services` or `domain`, and no I/O at import time.
11. **Rendering:** Telegram `parse_mode=HTML` with `html.escape()` on every interpolated user or AI string.
12. **New tenant table checklist:** `workspace_id`, a composite FK, RLS + FORCE + policy, indexes, an AT-RLS-COVERAGE pass, and isolation tests.

## V1 rules

- **Do not refactor `main.py`.** V1 is frozen except for owner-approved Phase 0 keep-alive fixes (doc 06).
- Do not add features to V1. New work goes into V2 (`src/finbot/`).

## Planned layout (V2; created in Phase 1)

```
src/finbot/{domain,authz,services,ports,infra,adapters}/   # see doc 04 §4
migrations/        # Alembic
tests/{unit,integration,authz,adapter,migration}/
deploy/            # compose files, backup scripts, runbooks (secrets/ is git-ignored)
docs/              # specifications (this set)
```

## Commands

V2 tooling does not exist yet. Phase 1 will add these, and this section should be updated when it does:

```
uv sync / pip install -r requirements.lock      # install (pinned, hashed)
pytest                                           # all tests (needs Docker for testcontainers)
pytest tests/authz                               # authorization matrix + rules
ruff check . && ruff format --check . && mypy src
lint-imports                                     # layering contract
docker compose -f deploy/compose.staging.yml up -d
```

V1: `python main.py` (needs `.env`) or `docker compose up -d` (root `docker-compose.yml`).

## Conventions

- Python 3.13 with `uv` (D23, pending approval), type hints throughout, `ruff` formatting.
- Environments: `dev`, `test`, `staging`, `production`, `drill`. Never point staging config at production resources, never put production data in staging, and never publish the Postgres port on a VPS (doc 04 §8.2–8.3).
- User-facing text is in **Indonesian**, kept in the message catalog (`adapters/telegram/messages_id.py`). Code, comments and docs are in English.
- IDs are UUIDv7 generated in the application.
- Tests run against real PostgreSQL as the `finbot_app` role. Do not mock the database for tenant-data tests.
- Test names reference doc 07 IDs where one exists (e.g. `test_AT_ISO_01_cross_ws_account_rejected`).

## Working agreements

- Git: the integration branch is `develop`; `main` is production. Do not commit, push, deploy or install dependencies unless the user asks in the current conversation.
- When a task changes behavior covered by an FR, NFR or RBAC row, update the corresponding doc in the same change.
- Any isolation or authorization defect is Sev-1: report it immediately and do not work around it.
