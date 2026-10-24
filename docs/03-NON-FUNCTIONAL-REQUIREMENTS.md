# 03 — Non-Functional Requirements

| | |
|---|---|
| **Status** | Draft; reconciled with approved D1, D9, D18 (rev. 2, 2026-10-23) |
| **Date** | 2026-10-23 |
| **Related** | [04 Architecture](04-LOGICAL-ARCHITECTURE.md) · [07 Tests](07-TEST-AND-ACCEPTANCE-PLAN.md) · [09 Security](09-TENANT-ISOLATION-AND-SECURITY.md) |

IDs follow `NFR-<AREA>-<NN>`. **Verification** says how doc 07 proves each requirement.

## 1. Security & tenant isolation (`SEC`)

| ID | Requirement | Verification |
|---|---|---|
| NFR-SEC-01 | Every query and mutation on tenant data is scoped by `workspace_id` in three layers: (1) the service-layer policy check, (2) repository methods that require a `WorkspaceContext`, and (3) PostgreSQL RLS (D5). | Authz matrix tests, cross-tenant tests, RLS tests |
| NFR-SEC-02 | Cross-workspace references are impossible at the schema level, using composite foreign keys on `(workspace_id, id)`. | Constraint tests |
| NFR-SEC-03 | The application connects as a database role **without** `BYPASSRLS`, superuser, DDL rights, or UPDATE/DELETE on `audit_events`. | Role-grant test in CI |
| NFR-SEC-04 | Secrets (Telegram token, Gemini key, DB passwords, backup encryption key) are loaded from Docker secrets or files readable only by root and the consuming container's dedicated group (mode 0400 or 0440; doc 09 §7). They are never logged, never committed, and never put in image layers. | Secret scan in CI (gitleaks), log-redaction test |
| NFR-SEC-05 | Tokens (onboarding, invitation, Telegram link, password reset) are ≥ 128-bit random, stored as SHA-256 hashes, single-use, expiring, and compared in constant time. | Unit tests |
| NFR-SEC-06 | Dependencies are pinned with hashes. `pip-audit` and image scanning (Trivy) run in CI, and High or Critical findings block release. | CI gate |
| NFR-SEC-07 | Rate limiting per Telegram user: 30 updates/min and 5 receipts/min. Updates over the limit are dropped with a single notice. | Unit test |
| NFR-SEC-08 | All user- and AI-supplied text is escaped before rendering (`parse_mode=HTML` + `html.escape`). | Unit and property tests |
| NFR-SEC-09 | Future web: OWASP ASVS Level 2 baseline, Argon2id passwords, secure session cookies, CSRF protection, login rate-limit and lockout. | Future phase |

## 2. Privacy & compliance (`PRV`)

| ID | Requirement |
|---|---|
| NFR-PRV-01 | Logs never contain transaction descriptions, raw messages, amounts, AI prompts or responses, or images. They may contain internal IDs, event names, durations and outcome codes. |
| NFR-PRV-02 | The minimum data is sent to Gemini (FR-AI-02). The paid data tier is used so prompts are not used for training (D8, to be confirmed against Google's current terms). |
| NFR-PRV-03 | Platform operators have **no routine access** to tenant data. Support access uses an audited break-glass procedure (doc 09 §8). |
| NFR-PRV-04 | Retention: transactions are kept until the workspace is purged. Receipt AI JSON is kept 90 days. `ai_usage` 13 months. Audit events 24 months (configurable). Backups up to 6 months. |
| NFR-PRV-05 | The design supports data-subject requests under Indonesia's UU PDP (Law 27/2022): export of a user's data, and erasure by anonymization (D14). Legal review is needed before public launch. |
| NFR-PRV-06 | A privacy notice is shown during onboarding (MVP: short text with a link to a document). |

## 3. Data integrity (`INT`)

| ID | Requirement |
|---|---|
| NFR-INT-01 | Money is `BIGINT` whole rupiah everywhere: database, domain and API. No floating point appears in any money path, and lint rules ban `float` in the money modules. Balances are derived only from signed `transaction_entries` (D1). |
| NFR-INT-01a | Ledger invariants INV-1…INV-8 (doc 05 §5.2) are enforced by the database (FKs, CHECKs, deferred triggers, grants), not only by application code. A transfer is committed with exactly two balancing legs or not at all. |
| NFR-INT-02 | Every multi-row change (transfer, batch confirm, ownership transfer, onboarding) is a single database transaction. |
| NFR-INT-03 | Confirmation is idempotent: a draft is confirmed at most once (status transition with a row lock). |
| NFR-INT-04 | Edits use optimistic concurrency (`version` column). Lost updates are impossible. |
| NFR-INT-05 | Balances are computed from transactions. Any cached or materialized balance must be verifiable with a recompute job that runs in CI against fixtures. |
| NFR-INT-06 | Timezone correctness: all `timestamptz` values are stored in UTC, and all business dates are `date` values in the workspace timezone. |

## 4. Performance & capacity (`PRF`)

Targets for the MVP VPS (2 vCPU / 4 GB):

| ID | Requirement |
|---|---|
| NFR-PRF-01 | Non-AI interactions (menus, confirmation, history page): p95 ≤ 1.5 s end-to-end, as measured by the bot. |
| NFR-PRF-02 | AI text parse → draft shown: p95 ≤ 6 s. Receipt OCR → summary: p95 ≤ 15 s, with a "processing" message within 1 s. |
| NFR-PRF-03 | Report for one workspace with 100k transactions: ≤ 2 s. Indexes are defined in doc 05. |
| NFR-PRF-04 | Capacity target: 500 users, 1,000 workspaces, 5M transactions, with no schema change. |
| NFR-PRF-05 | Nothing in the asyncio event loop blocks. All I/O is async or offloaded, and a CI test fails if a handler blocks the loop for more than 100 ms (using asyncio debug-mode slow-callback detection). |

## 5. Availability & recoverability (`AVL`)

| ID | Requirement |
|---|---|
| NFR-AVL-01 | Monthly availability target for the MVP: 99.0% (single VPS; maintenance windows announced). |
| NFR-AVL-02 | **RPO ≤ 24 h** (nightly backup) in the MVP. The upgrade path to RPO ≤ 15 min is WAL archiving (pgBackRest or wal-g) before the public launch. |
| NFR-AVL-03 | **RTO ≤ 4 h** to a fresh VPS, following the runbook. |
| NFR-AVL-04 | Graceful degradation: if AI is unavailable, the local parser is used; if the database is unavailable, the bot replies "Layanan sedang gangguan" and does not lose drafts already stored. |
| NFR-AVL-05 | Restart safety: drafts and conversation state live in PostgreSQL, so a container restart loses no confirmed or pending data. |
| NFR-AVL-06 | Only one bot poller runs per token. A startup lock (`pg_try_advisory_lock`) prevents a second instance. |

## 6. Observability (`OBS`)

| ID | Requirement |
|---|---|
| NFR-OBS-01 | Structured JSON logs to stdout with `request_id`, `user_id` (internal UUID), `workspace_id`, `event`, `duration_ms` and `outcome`, filtered by the PII rules (NFR-PRV-01). |
| NFR-OBS-02 | Metrics (counts and latencies) for updates, AI calls (by outcome), drafts confirmed or expired, authz denials, and backup age. MVP: logged counters plus `/status`. Later: a Prometheus endpoint. |
| NFR-OBS-03 | Alerts to the platform admin via Telegram DM for: backup failed or older than 26 h, error rate > 5% over 15 min, AI failure rate > 50% over 15 min, disk > 80%, and a burst of authz denials (> 20 per user per hour). |

## 7. Maintainability (`MNT`)

| ID | Requirement |
|---|---|
| NFR-MNT-01 | Layered architecture (doc 04): there are no imports from `adapters/` into `domain/` or `services/` (enforced with import-linter). |
| NFR-MNT-02 | Python 3.13 (D23, pending), type hints throughout, `mypy --strict` on `domain`, `services` and `authz`. `ruff` for lint and format. |
| NFR-MNT-03 | Test coverage: ≥ 90% lines on `domain`, `authz`, `services` and parsers; ≥ 80% overall. The authz matrix must be 100% covered (every role × permission). |
| NFR-MNT-04 | Schema changes only through Alembic migrations, reviewed, and following the expand/contract pattern for zero-downtime-friendly changes. |
| NFR-MNT-05 | Configuration is typed and validated at startup (pydantic-settings). The process refuses to start on invalid config. |
| NFR-MNT-06 | No import-time side effects (no network or database access at import time). |

## 8. Usability & localization (`USE`)

| ID | Requirement |
|---|---|
| NFR-USE-01 | All user-facing strings are in Indonesian and live in a message catalog (not inline), so English can be added later. |
| NFR-USE-02 | Amounts are displayed as `Rp 1.250.000`. Dates as `23 Okt 2026` or `23/10/2026`. |
| NFR-USE-03 | Every multi-step flow has a cancel path, and a stale button gives a clear "sudah kedaluwarsa" message. |
| NFR-USE-04 | Every confirmation shows the active workspace (G-8). |

## 9. Cost (`CST`)

| ID | Requirement |
|---|---|
| NFR-CST-01 | AI spend is capped by per-user quotas (FR-AI-07) and a global daily budget. Above the budget, the bot falls back to the local parser and alerts the operator. |
| NFR-CST-02 | Receipt images are downscaled to ≤ 1600 px on the long edge and JPEG quality 85 before upload. |
| NFR-CST-03 | The MVP's infrastructure runs on one VPS plus object storage for backups. |

## 10. Portability & deployment (`DEP`)

| ID | Requirement |
|---|---|
| NFR-DEP-01 | The whole stack runs with `docker compose up -d` on Ubuntu 24.04 LTS (or the then-current LTS). |
| NFR-DEP-02 | Images are reproducible: pinned base by digest, hashed lock file, non-root user, read-only root filesystem. |
| NFR-DEP-03 | PostgreSQL is not exposed on any public interface. The VPS firewall allows only SSH (key-only) inbound, because the bot uses polling. |
| NFR-DEP-04 | Separate **staging** environment (D18, approved): its own Telegram bot token, PostgreSQL container, database, secrets and backup keys. It holds synthetic data only, and startup guards refuse cross-environment configuration (doc 04 §8.3). |
| NFR-DEP-05 | PostgreSQL **18** in Docker (D9, approved): the official image pinned to an exact minor version and digest, a persistent named volume, a healthcheck, and **no published port** on any VPS (doc 04 §8.2). |
