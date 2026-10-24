# 06 — Implementation Roadmap

| | |
|---|---|
| **Status** | Phase 1 in progress (Step 1: decision reconciliation); D1, D5, D9, D11, D18 approved 2026-10-23 |
| **Date** | 2026-10-23 |
| **Related** | [07 Tests](07-TEST-AND-ACCEPTANCE-PLAN.md) · [01 Open decisions](01-PROJECT-VISION.md#8-open-decisions-register) |

## 1. Strategy

- **Rewrite alongside V1, not in place.** V2 is built in `src/finbot/` with its own compose stack and staging bot. V1 (`main.py`) keeps running unchanged, apart from Phase 0 keep-alive fixes, until cutover (Phase 7).
- **Vertical slices.** Each phase ends with something demonstrable on the staging bot and a passing acceptance suite.
- **Isolation first.** Identity, tenancy, RBAC and RLS (Phases 1–2) land *before* any finance feature, so every later feature is built on the enforced model rather than retrofitted.
- **Gates.** A phase is complete only when every acceptance criterion (AC) passes, the owner signs off, and the decisions listed under "Needs before start" for the next phase are resolved.

Estimates are rough, for one developer working with AI assistance, and exclude review waiting time.

```
P0 V1 keep-alive ─┐ (parallel, optional)
P1 Foundations ──► P2 Identity/Tenancy/RBAC ──► P3 Ledger core ──► P4 AI capture ──► P5 Reports
                                                                                       │
                                     P6 Ops hardening (backup/restore/monitoring) ◄────┘
                                                        │
                                                 P7 Migration & cutover ──► MVP DONE
                                                        │
                                       P8 Public SaaS (8a auth+linking, 8b web dashboard, 8c quotas/billing)
```

## 2. Phases

---

### Phase 0: V1 keep-alive (optional; ~1–2 days)

**Goal:** stop V1 from breaking or losing data while V2 is built. These are the minimal audit fixes and are **not** a V1 refactor.

| Scope | Pin V1 dependencies to the versions currently running (taken from the live container's `pip freeze`); set `TZ=Asia/Jakarta`; take a manual export of the Google Sheet as a baseline backup. |
|---|---|
| **AC-P0-01** | V1 image rebuilds reproducibly and the bot answers `/start` and records a test transaction on staging. |
| **AC-P0-02** | A dated Sheets export is stored off-site. |
| **Rollback** | Redeploy the previous V1 image tag (keep it tagged before rebuilding). |
| **Needs before start** | Owner confirms V1 changes are allowed (V1 is otherwise frozen). |

---

### Phase 1: Foundations (~1–1.5 weeks)

**Goal:** a skeleton that enforces the architecture from the first commit, with **RLS on from the first tenant table (D5)**, **PostgreSQL 18 in Docker (D9)** and an **isolated staging environment (D18)**.

Phase 1 runs as **five owner-reviewed steps**. Each step stops for owner review before the next one starts.

| Step | Content | Exit (owner review) |
|---|---|---|
| **1. Decision reconciliation** (this revision) | Docs updated for D1, D5, D9, D11 and D18; blockers listed; Step 2 checklist (§6) | Owner approves the docs and D23 |
| **2. Package skeleton, configuration, testing foundation** | `pyproject.toml`, `uv.lock`, `src/finbot` layer packages (empty but importable), `config.py` (typed, `*_FILE` secrets, env guards that need no I/O), `logging.py` (JSON + redaction), test tree, ruff, mypy, import-linter, pytest config, `finbot check-config` CLI. **No DB, no Docker, no Telegram.** Checklist: §6 | AC-P1-01, AC-P1-05, AC-P1-06 |
| **3. Database foundation** | **3A** (infrastructure audit and staging design): `deploy/staging/compose.yml` with **pinned PostgreSQL 18** (doc 04 §8.2), `pg_hba.conf`, `deploy/postgres/initdb/00-roles.sh`, runbook, AT-DEP-01 static tests; nothing started. **3B** (S1–S11, doc 10): **3B.2** tooling: dependencies, Alembic env (owner role, guard SQL, downgrade policy, manual revisions), `finbot-migrate`, credential separation, database test harness (userspace + pinned-image container), CI workflow; **3B.3** baseline revisions per doc 05 §11 (P1 row) with RLS ENABLE + FORCE and policies; **3B.4** RLS/privilege/isolation tests; **3B.5** migration lifecycle tests and checkpoint. `compose.dev.yml` is deferred: `claude` has no Docker access, and the user-space backend replaces it (doc 10 §4) | AC-P1-02, AC-P1-03 |
| **4. Runtime plumbing** | Async engine (app role); UoW with transaction-local `set_config`; guards G-ENV-2…G-ENV-6 at startup; advisory-lock poller guard; minimal PTB app answering `/start` with "V2 staging"; heartbeat healthcheck | AC-P1-04, AC-P1-07 |
| **5. CI and staging bring-up** | CI workflow (D25): lint, type, unit + db tests, gitleaks, pip-audit, image build, Trivy, AT-DEP-01; `migrate`/`bot` services added to `deploy/staging/compose.yml`; staging bring-up on the existing Contabo VPS (D24 ✅) by the `deploy` operator, with the **staging bot token** | AC-P1-02 (on the VM), AC-P1-08 |

| Criterion | Requirement |
|---|---|
| **AC-P1-01** | `uv run ruff check`, `ruff format --check`, `mypy` (strict on domain, authz, services and config) and `pytest` pass on a clean checkout. A deliberate layering violation fails import-linter. |
| **AC-P1-02** | `docker compose -p finbot-staging … up` on a fresh Ubuntu VM brings up PostgreSQL 18 (healthy), `migrate` (exit 0) and `bot`. `migrate` runs `upgrade head` and `downgrade base` cleanly in CI. |
| **AC-P1-03** | AT-RLS-00, 04, 05, 07 and AT-RLS-COVERAGE pass for every Phase 1 table. `finbot_app` lacks BYPASSRLS and DDL. |
| **AC-P1-04** | Starting a second bot process against the same DB exits with a "poller lock held" log. |
| **AC-P1-05** | A log-redaction unit test proves a message body never appears in log output (AT-LOG-01 skeleton). |
| **AC-P1-06** | AT-CFG-01..05 pass; importing every `finbot` module performs no network, file or database I/O. |
| **AC-P1-07** | AT-ENV-01..06 pass. A staging bot pointed at a database marked `production` refuses to start. |
| **AC-P1-08** | AT-DEP-01 passes: no published Postgres port; pinned digest; healthchecks present. The staging bot username differs from production and from V1. V1 is still running and unaffected (checked by `docker ps` and a V1 `/start`). |
| **Rollback** | No production impact. Steps 2–4 are code-only: revert the branch. Staging: follow `deploy/staging/README.md` §7. Removing the data volume (`docker volume rm finbot-staging_pgdata`) is a **destructive, owner-approved** action for a failed first initialisation only; **never** `down -v` or `docker volume prune` on the VPS (they would also hit other projects' volumes). Schema problems on staging are fixed forward (S9). **V1 is never touched.** |
| **Approved inputs** | D5, D9, D18 (approved 2026-10-23); D1 and D11 approved (affect later phases). |
| **Needs before Step 2** | **D23** (Python version and uv). |
| **Needs before Step 5 / Phase 1 exit** | **D24** drill environment (staging host ✅ approved 2026-10-24), **D25** (CI platform), a staging bot created via BotFather by the owner. |

---

### Phase 2: Identity, tenancy & RBAC (~2 weeks)

**Goal:** users, personal and business workspaces, members and roles, fully enforced and tested, with no finance features yet.

| Scope | Onboarding invites (CLI + `/start onb_…`); `resolve_principal`; personal workspace auto-create; `/ruang` list, switch and create business; rename; deletion request, cancel and purge job; invitations (create, accept, revoke); change role, remove, leave; ownership transfer with acceptance; `pending_actions`; `authz/policy.py` with the full matrix; notifications; audit events for all of the above; `/profil`; platform suspend and reactivate. |
|---|---|
| **AC-P2-01** | AT-RBAC-SYNC-01 and the full AT-RBAC-M-* matrix pass (all 26 permissions × all principals, *including finance permissions*, tested against service stubs that call `authorize`). |
| **AC-P2-02** | AT-RBAC-R1…R6, R11, S, U, G2 and P pass. |
| **AC-P2-03** | Schema tests: a second membership in a personal workspace fails; a second active owner fails; committing a business workspace with zero owners fails; a personal-workspace invitation fails. |
| **AC-P2-04** | Manual UAT on staging with 3 Telegram accounts: create business → invite Editor and Viewer → change roles → transfer ownership → remove → leave. All notifications are correct, and `/audit` (once available) or a DB query shows every event. |
| **AC-P2-05** | Pressing a stale button after a role change behaves as G-2. |
| **Rollback** | Staging only. Migrations have `downgrade` steps, and any failed acceptance means fix-forward. |
| **Needs before start** | D3, D6, D13, D19, D20 approved. |

---

### Phase 3: Ledger core (~2–2.5 weeks)

**Goal:** accounts, categories and transactions (income, expense, transfer) with drafts, confirmation, correction, void and restore, without AI.

| Scope | Tables `accounts`, `categories`, `transactions` (header), **`transaction_entries`** (D1: 1 entry for income/expense, exactly 2 summing to zero for transfers), `transaction_revisions`, `drafts`, `receipts` (empty for now), the `current_entries` view and the doc 05 §5.2 triggers, all with composite FKs and RLS; `domain/ledger.py` entry builder. Seeding; `/akun`, `/kategori`; the fixed **local parser** (amount, dates, keywords) with the golden corpus; free-text → drafts via the local parser; `/catat` and `/transfer` guided flows; confirmation (idempotent); history `/riwayat` with filters and pagination; edit with versioning, entry revisions and optimistic lock; type change as void + replace (D21); void, restore and "Dihapus" view; undo button; default account preference; HTML rendering with escaping. |
|---|---|
| **AC-P3-01** | AT-ISO-01..06 pass: cross-workspace account, category, transaction, draft and receipt references are rejected at the service (NotFound) **and** the DB (FK violation) levels. |
| **AC-P3-02** | AT-RBAC-M-* finance rows pass against the real services (stubs replaced); AT-RBAC-R7…R10 pass. |
| **AC-P3-03** | Local parser ≥ 95% on the golden corpus, and the V1 regressions (audit B-2, B-3) pass. |
| **AC-P3-04** | Double-tapping ✅ creates exactly one transaction (AT-TXN-IDEMP-01). Concurrent edits: the second one is refused with the latest version shown (AT-TXN-OCC-01). |
| **AC-P3-05** | The balance property test (hypothesis) holds: random sequences of income, expense, transfer, edit, void, restore and type change keep Σ balances = Σ opening + Σ income − Σ expense, and Σ current transfer entries = 0. |
| **AC-P3-07** | AT-ENT-01..21 pass at both the service and raw-SQL levels (a one-legged or unbalanced transfer can never be committed; entries are immutable for `finbot_app`). |
| **AC-P3-06** | Every user-supplied string renders safely (AT-RENDER-*). |
| **Rollback** | Staging only. |
| **Needs before start** | D2, D15, D17, **D21, D22** approved; O-1 (business category seed). (D1 approved.) |

---

### Phase 4: AI capture (~1.5–2 weeks)

**Goal:** Gemini text parsing and receipt OCR behind the same draft pipeline.

| Scope | `google-genai` async client; model ID from config; structured output schemas; prompts with versioning; proposal resolver (server-side, scoped to the workspace); timeouts, retries only on 429 and 5xx; fallback to the local parser; receipt flow (downscale, extract, reconcile, three modes); `ai_usage` and per-user and global quotas; prompt-injection containment tests; recorded fixtures for CI (no live calls in CI); an opt-in live smoke test on staging. |
|---|---|
| **AC-P4-01** | AI text corpus (recorded): ≥ 95% correct amount and type, ≥ 90% correct category, 100% of dates resolved in the workspace timezone. |
| **AC-P4-02** | AT-AI-INJ-*: injected instructions never change the workspace or the target account outside the active workspace, never skip confirmation, and never produce non-schema fields. |
| **AC-P4-03** | AI outage simulation (timeout, 429, 500, invalid JSON): a draft is still produced by the local parser within 10 s, and the outcome is recorded in `ai_usage`. |
| **AC-P4-04** | Quota exceeded → local parser for text and a friendly refusal for receipts, both tested. |
| **AC-P4-05** | The receipt fixture set (≥ 20 real Indonesian receipts, anonymized) reconciles the total within Rp 1 for ≥ 85%. The rest show the mismatch warning. |
| **AC-P4-06** | No receipt image is written to disk or the DB (AT-PRIV-RCPT-01). Logs contain no prompt or response text (AT-LOG-01). |
| **Rollback** | Feature flag `FINBOT_AI_ENABLED=false` → the local parser only. |
| **Needs before start** | D7, D8 approved; the Gemini paid-tier account is set up. |

---

### Phase 5: Reporting & export (~1 week)

| Scope | `/laporan` periods, by category and account; `/saldo` as of a date; 6-month comparison; pagination; CSV export with formula-injection protection; `/audit` view. |
|---|---|
| **AC-P5-01** | Report numbers equal a reference computation over fixtures, including transfers being neutral, deleted rows being excluded, and WIB period boundaries around midnight. |
| **AC-P5-02** | 100k-transaction workspace: report ≤ 2 s on staging hardware (NFR-PRF-03). |
| **AC-P5-03** | `report.export` and `audit.view` matrix rows hold; the export contains only the active workspace's rows (AT-ISO-EXPORT-01); a formula cell is escaped (AT-EXPORT-02). |
| **Rollback** | Staging only. |
| **Needs before start** | D4 approved. |

---

### Phase 6: Operations hardening (~1–1.5 weeks)

| Scope | `backup` container (`pg_dump` → `age` → `rclone`), lifecycle retention, manifest; restore script and verification SQL; restore runbook; Telegram alerts (backup age, error rate, AI failure, disk); `/status`; host hardening (ufw, unattended-upgrades, SSH key-only, fail2ban); compose hardening (read-only root filesystem, `cap_drop`, `no-new-privileges`, memory limits); load test; internal security review against doc 09 §11; privacy notice text. |
|---|---|
| **AC-P6-01** | **Restore drill**: restore last night's backup (of staging; production does not exist yet) into a fresh **`finbot-drill`** environment on a fresh VM within 4 h → verification SQL passes → the bot starts with `FINBOT_ENV=drill` against a test token (AT-DR-01). After cutover, monthly drills of **production** backups run only in `finbot-drill`, never in staging (D18/D24), and the drill environment is destroyed afterwards. |
| **AC-P6-02** | A deliberately broken backup (wrong credentials) raises a Telegram alert within 1 h. |
| **AC-P6-03** | Load test: 50 simulated users at 1 message per 10 s for 30 min, with p95 within NFR-PRF-01/02 (AI mocked with realistic latency) and no event-loop stalls > 100 ms. |
| **AC-P6-04** | Security checklist (doc 09 §11) complete; no open High findings. |
| **Rollback** | Staging only. |
| **Needs before start** | D10 (bucket provider) and D14 approved. |

---

### Phase 7: V1 migration & production cutover (~3–5 days + a 14-day rollback window)

**Goal:** move all V1 users and data to V2 and decommission V1.

**Preparation**

1. Provision the production VPS and compose stack (doc 04 §8), and generate production secrets.
2. Onboard every V1 user to production V2 with an invite **before** cutover, so that their personal workspaces exist (they may explore V2 with no data).
3. **Rehearsal in the ephemeral `finbot-drill` environment** (not staging: staging holds synthetic data only, D18) with a copy of the real sheet: run `finbot-admin import-v1 --dry-run`, then `--apply`, then reconcile. Fix the mapping and repeat until the reconciliation is clean, then destroy the drill environment and its volume.

**Cutover runbook (production)**

| Step | Action | Owner check |
|---|---|---|
| C-1 | Announce a maintenance window (≈ 1 h) to V1 users | ✔ |
| C-2 | `docker compose stop` V1 (**the V1 bot token is no longer polled**). Export the sheet to XLSX/CSV and store it off-site as a frozen snapshot | snapshot hash recorded |
| C-3 | Take a manual production V2 backup (pre-import) | ✔ |
| C-4 | `import-v1 --dry-run` against the frozen sheet → review the report | per-user counts and sums OK |
| C-5 | `import-v1 --apply` → automatic reconciliation (FR-MIG-05) | **GO/NO-GO** |
| C-6 | Switch the bot token: **(a)** reuse the V1 token for V2 (users keep the same bot), or **(b)** a new bot username (users must open a new chat). Proposed: **(a)** | ✔ |
| C-7 | Start the V2 bot. Smoke test with 2 users: `/saldo`, `/laporan` this month equals the V1 sheet totals for the same users | ✔ |
| C-8 | Announce go-live with a short guide (workspace concept, `/ruang`, accounts) | ✔ |

**Acceptance**

| **AC-P7-01** | The reconciliation for every user is exact (count, Σ income, Σ expense). Rejected rows are listed and accepted by the owner. |
|---|---|
| **AC-P7-02** | Smoke tests pass for every migrated user (their totals match their V1 `/laporan` all-time figures, excluding rejected rows). |
| **AC-P7-03** | After 14 days of operation with no Sev-1 incident, V1 is decommissioned: the container is removed, the service-account key is revoked, the sheet is archived read-only. |

**Rollback plan (Phase 7)**

| When | Trigger | Action | Data consequence |
|---|---|---|---|
| Before C-6 | Reconciliation fails | Truncate the import (`DELETE … WHERE source='import' AND import_ref LIKE 'v1:%'` per batch, as owner) or restore the C-3 backup; restart V1 | None (V1 untouched) |
| C-6 … +14 days | Sev-1 defect, data error, users blocked | Stop V2 → restart the V1 container on the same token → users continue in V1 | Transactions entered in V2 since cutover must be carried back: `finbot-admin export-v1-format --since <cutover>` produces rows in the V1 sheet layout for manual append. **Accepted risk:** V2-only concepts (transfers, business workspaces) have no V1 representation; transfers are exported as an expense/income pair, and business data is listed separately |
| After +14 days | — | No rollback to V1; fix forward on V2 | — |

**Needs before start:** D11, D12, the C-6 token choice, and owner sign-off on the P6 drill.

---

### Phase 8: Future public SaaS (post-MVP; planned, not scheduled)

| Sub-phase | Scope | Key acceptance |
|---|---|---|
| **8a Web identity** | FastAPI web adapter; registration; email verification; Argon2id login; password reset; sessions; CSRF; rate limiting; **Telegram linking tokens** (FR-IDN-09/10); switch onboarding from invite-only to public (feature flag) | ASVS L2 checklist; link token single-use and expiry tests; account enumeration resistance |
| **8b Web dashboard** | Workspace management and reports on the web, reusing services and authz | The web RBAC matrix tests reuse AT-RBAC-M-* through the HTTP adapter |
| **8c Plans & quotas** | `plan_quotas`, metering (`ai_usage`, workspace and member counts), then subscriptions and billing | Quota enforcement tests; billing provider sandbox |
| **8e Optional Sheets export** (D11) | One-way, per-workspace export to Google Sheets (FR-RPT-08); never read back; not a system of record | Export contains only the active workspace; formula escaping; revocable credentials |
| **8d Scale-out** | Managed Postgres, WAL archiving (RPO ≤ 15 min), webhooks instead of polling, a separate worker for jobs | DR drill with point-in-time recovery |

## 3. General rollback principles (all phases)

| Change type | Rollback approach |
|---|---|
| Application code | Images tagged by git SHA; `docker compose up -d` with the previous tag. Keep the last 5 images. |
| Schema migration | Expand/contract: additive migrations first; destructive steps only in a later release once the code no longer uses the column. Every migration has a tested `downgrade` **or** a documented forward-fix. A backup is taken automatically before `migrate` runs in production. |
| Data correction | Use void and restore plus revisions and entry revisions; never ad-hoc SQL on production without a backup and an audit entry (`ops.manual_data_fix`). |
| Catastrophic | Whole-DB restore (doc 09 §9), with owner approval; all tenants roll back to the backup point. |
| Feature | Flags (`FINBOT_AI_ENABLED`, `FINBOT_RECEIPTS_ENABLED`, `FINBOT_EXPORT_ENABLED`, `FINBOT_INVITES_ENABLED`) can be switched off without a deploy (env + restart). |

## 4. Branching & release

- `main` = production. `develop` = integration (the current default working branch). Feature branches go to `develop` through a PR with CI green and review.
- Release: tag `v2.<phase>.<n>` on `main`; staging deploys from `develop`; production deploys from tags.
- V1 hotfixes (Phase 0 only) go on a `v1-maintenance` branch.

## 5. Decision gates summary

| Gate | Decisions required | Status |
|---|---|---|
| Start P1 | D5, D9, D18 | ✅ approved 2026-10-23 |
| P1 Step 2 | **D23** | ✅ approved 2026-10-23 |
| P1 Step 5 / exit | D24 drill placement (staging placement ✅ 2026-10-24), D25; staging bot created | ⏳ open |
| Start P2 | D3, D6, D13, D19, D20 | ⏳ open |
| Start P3 | D2, D15, D17, D21, D22, O-1 (D1 ✅) | ⏳ open |
| Start P4 | D7, D8 | ⏳ open |
| Start P5 | D4 | ⏳ open |
| Start P6 | D10, D14 | ⏳ open |
| Start P7 | D12, C-6 token choice (D11 ✅) | ⏳ open |

## 6. Phase 1 Step 2 checklist: package skeleton, configuration, testing foundation

**Scope limits for Step 2:** no database, no Docker, no Telegram network calls, no migrations, and no change to V1 (`main.py`, root `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `.env.example`). Installing packages with `uv sync` is part of Step 2 and needs owner permission at that time.

### 6.1 Files to create

| # | Path | Content |
|---|---|---|
| 1 | `pyproject.toml` | Project `finbot` (version `2.0.0.dev0`), `requires-python = ">=3.13,<3.14"` (D23); `src` layout; runtime deps **only those used in Step 2**: `pydantic>=2`, `pydantic-settings>=2`; dev group: `pytest`, `pytest-asyncio`, `hypothesis`, `ruff`, `mypy`, `import-linter`, `pytest-cov`; `[project.scripts] finbot = "finbot.__main__:main"`; tool config for ruff (target py313; rules E, F, I, B, UP, S, ASYNC, DTZ, T20 (no `print`), PL subset), mypy (strict for `finbot.domain`, `finbot.authz`, `finbot.services`, `finbot.config`), pytest (`testpaths`, markers `db` and `slow`, `--strict-markers`, `-p no:cacheprovider` optional), coverage (`fail_under` 80; per-package targets enforced later) |
| 2 | `uv.lock` | Generated by `uv lock` (committed; hashes included) |
| 3 | `.python-version` | `3.13` |
| 4 | `.importlinter` (or a `[tool.importlinter]` section) | Contracts: **layers** `finbot.adapters` > `finbot.services` > `finbot.authz` > `finbot.domain`; **forbidden** `finbot.domain` → `finbot.infra`, `finbot.adapters`, `pydantic_settings`, `sqlalchemy`, `telegram`; **independence** among `finbot.adapters.*` |
| 5 | `src/finbot/__init__.py` | `__version__` only; no side effects |
| 6 | `src/finbot/__main__.py` | `main()` with argparse subcommands: `check-config` (load and validate settings, print a **masked** summary, exit 0/2) and `version`. No network, DB or Telegram in Step 2 |
| 7 | `src/finbot/config.py` | `Settings(BaseSettings)` reading **only `FINBOT_`-prefixed** environment variables (no implicit `.env`; unknown `FINBOT_*` names are rejected): `FINBOT_ENV` ∈ `dev`/`test`/`staging`/`production`/`drill` (required, G-ENV-1); `FINBOT_LOG_LEVEL`; `FINBOT_DEFAULT_TIMEZONE=Asia/Jakarta` (validated with `zoneinfo`); `FINBOT_DB_HOST`, `FINBOT_DB_PORT=5432`, `FINBOT_DB_NAME`, `FINBOT_DB_APP_USER=finbot_app`, `FINBOT_DB_APP_PASSWORD_FILE` (→ `SecretStr`); `FINBOT_TELEGRAM_TOKEN_FILE`; `FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME`; `FINBOT_GEMINI_API_KEY_FILE` (optional; required only when `FINBOT_AI_ENABLED=true`); feature flags `FINBOT_AI_ENABLED`, `FINBOT_RECEIPTS_ENABLED`, `FINBOT_EXPORT_ENABLED`, `FINBOT_INVITES_ENABLED` (all default `false`). Each secret may be given as `FINBOT_<NAME>` or `FINBOT_<NAME>_FILE`, never both. **Validators:** `FINBOT_DB_NAME == expected_db_name(env)` (G-ENV-2, static part); in `staging`/`production`/`drill` plaintext secret variables are rejected (G-ENV-5); `*_FILE` must be a regular, non-empty file (≤ 64 KiB), never world-writable, and in `staging`/`production`/`drill` an absolute path that is not world-readable (group-read is allowed for the container's non-root uid); `repr` and `model_dump` never reveal secrets. `load_settings()` is called explicitly, **never at import** |
| 8 | `src/finbot/logging.py` | `configure_logging(settings)`; JSON formatter (stdlib); **allow-listed** extra fields (`request_id`, `user_id`, `workspace_id`, `event`, `duration_ms`, `outcome`, `env`); a `RedactionFilter` that drops non-allow-listed extras and masks token-like strings (Telegram token pattern, Google API-key pattern, `postgresql://user:pass@`) |
| 9 | `src/finbot/{domain,authz,services,ports,infra,adapters}/__init__.py` | Empty packages with a module docstring stating the layer rule (doc 04 §3). `adapters/{telegram,cli,jobs}/__init__.py`, `infra/{db,ai}/__init__.py` likewise |
| 10 | `src/finbot/py.typed` | PEP 561 marker |
| 11 | `tests/conftest.py` | Isolated env fixture (clears `FINBOT_*` env vars); `tmp_secret_file` factory (mode 0400); the `db` marker auto-skips when Docker is unavailable (prepared for Step 3; no DB tests yet) |
| 12 | `tests/unit/test_config.py` | AT-CFG-01..05: missing env → error naming the field; unknown env rejected; `db_name` mismatch rejected; inline secret rejected in staging; secrets loaded from file and masked in `repr`, `str` and `model_dump_json` |
| 13 | `tests/unit/test_logging_redaction.py` | AT-LOG-01 skeleton: logging a record with `description='ZZSECRETZZ'` extra and a token-like string → neither appears in the output; allow-listed fields do |
| 14 | `tests/unit/test_import_side_effects.py` | Imports every `finbot.*` module with `socket.socket`, `open` (outside `site-packages`) and `os.environ` mutation patched to fail (NFR-MNT-06) |
| 15 | `tests/unit/test_cli.py` | `finbot check-config` exits 0 with valid test settings and 2 with invalid ones; output contains no secret values |
| 16 | `tests/architecture/test_layers.py` | Runs the import-linter contracts from pytest, and a test that a deliberately bad import (a fixture module) is detected |
| 17 | `tests/{integration,authz,adapter,migration}/__init__.py` | Placeholders with a README line pointing to doc 07 (no tests yet) |
| 18 | `deploy/env/finbot.env.example` | Non-secret settings for staging (`FINBOT_ENV=staging`, `FINBOT_DB_NAME=finbot_staging`, `FINBOT_*_FILE=/run/secrets/...`) with placeholder values only |
| 19 | `Makefile` (optional) | `lint`, `type`, `test`, `check` wrappers around `uv run …` |

### 6.2 Files to modify (non-application, owner-visible)

| Path | Change |
|---|---|
| `.gitignore` | Append the V2 entries `deploy/secrets/`, `.ruff_cache/`, `.mypy_cache/`, `.coverage*`; the existing V1 entries are kept unchanged |
| `CLAUDE.md` | Replace the "Commands" section with the real Step 2 commands once they work |

### 6.3 Explicitly not in Step 2

SQLAlchemy, Alembic, asyncpg, python-telegram-bot and google-genai dependencies; any Docker or compose file; CI workflow (Step 5); `domain/ledger.py` and money types (Phase 3, though `domain/money.py` may be pulled forward if the owner wants AT-PARSE scaffolding early).

### 6.4 Step 2 verification commands (to be run in Step 2, not now)

```bash
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run lint-imports
uv run pytest -q
uv run finbot check-config   # with test env vars → exit 0, masked output
git status --short           # only the files listed in §6.1/6.2 changed; main.py etc. untouched
```

### 6.5 Step 2 acceptance

AC-P1-01 (without the DB parts), AC-P1-05 and AC-P1-06 pass. `git diff --stat` shows no V1 files. The owner reviews the result before Step 3.
