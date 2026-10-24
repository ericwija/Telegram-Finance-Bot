# 01 — Project Vision: Telegram-Finance-Bot V2

| | |
|---|---|
| **Status** | Approved in part: D1, D5, D9, D11, D18 and D23 approved 2026-10-23; D24 (staging placement) approved 2026-10-24; other decisions open (§8) |
| **Date** | 2026-10-23 (rev. 2: Phase 1 Step 1) |
| **Supersedes** | V1 single-file bot (`main.py`, Google Sheets storage) |
| **Inputs** | `PROJECT_AUDIT.md`, `README.md`, the approved product vision brief |

## Document set

| # | Document | Purpose |
|---|---|---|
| 01 | Project Vision (this file) | Product intent, scope boundaries, principles, **open decisions register** |
| 02 | [Functional Requirements](02-FUNCTIONAL-REQUIREMENTS.md) | What the system does (FR-IDs) |
| 03 | [Non-Functional Requirements](03-NON-FUNCTIONAL-REQUIREMENTS.md) | Quality attributes (NFR-IDs) |
| 04 | [Logical Architecture](04-LOGICAL-ARCHITECTURE.md) | Components, layers, request flows |
| 05 | [Data Model](05-DATA-MODEL.md) | PostgreSQL schema, constraints, Sheets migration mapping |
| 06 | [Implementation Roadmap](06-IMPLEMENTATION-ROADMAP.md) | Phases, acceptance criteria, rollback plans, cutover |
| 07 | [Test & Acceptance Plan](07-TEST-AND-ACCEPTANCE-PLAN.md) | Test strategy, authorization tests, UAT |
| 08 | [RBAC Matrix](08-RBAC-MATRIX.md) | Roles, permissions, precise rules |
| 09 | [Tenant Isolation & Security](09-TENANT-ISOLATION-AND-SECURITY.md) | Isolation layers, RLS, threat model, backup security |
| 10 | [Database Migrations & Testing](10-DATABASE-MIGRATIONS-AND-TESTING.md) | Alembic workflow, credential separation, database test harness, CI, Step 3B decisions S1–S11 |

---

## 1. Vision statement

A **Telegram-first finance assistant for individuals and small businesses in Indonesia**. It records money in and money out from plain Indonesian messages and receipt photos, keeps personal and business money strictly apart, and lets a small team share a business's books with clear roles.

V2 starts as a **private/internal MVP**, used by the owner and invited users only. It is designed from day one so that **public website registration and a web dashboard** can be added later without re-architecting identity, tenancy or data.

## 2. Problem statement

The V1 bot showed that the conversational capture model works (natural-language entry, receipt OCR, confirmation step). It cannot grow, for these reasons (from `PROJECT_AUDIT.md`):

- Google Sheets is the database. There are no stable row IDs, deletes can remove the wrong row, and all users share one sheet that every user can see.
- There is no concept of workspaces, roles or accounts (cash, bank, e-wallet), and no internal transfers.
- Money is stored as `float`, dates use UTC, there are no tests, and everything is in one 3,300-line module with deprecated SDKs.

## 3. Target users (personas)

| Persona | Description | Primary needs |
|---|---|---|
| **P1 Individual** | Salaried or freelance person tracking daily spending | Fast capture, balances per wallet, monthly cash flow |
| **P2 Micro-business owner** | Warung, online seller or service provider; usually has a personal workspace too | Separate business books, multiple accounts, a staff member who records sales |
| **P3 Business staff (Editor)** | Cashier or assistant | Record and correct transactions; cannot delete or see admin functions |
| **P4 Accountant / partner (Viewer)** | Advisor or co-founder | Read-only balances and reports |
| **P5 Platform operator** | The project owner running the VPS | Onboard users, back up and restore, monitor, no casual access to tenant data |

## 4. Product principles

1. **Telegram-first, web-ready.** Every capability is a service-layer use case. Telegram is one adapter, and the web is a future adapter that reuses the same services.
2. **Confirm before commit.** AI and the parsers only *propose*. A human confirms every transaction before it is stored.
3. **Isolation by construction.** Every record belongs to exactly one workspace. Database constraints and row-level security make cross-workspace leaks structurally hard, not just "checked in code".
4. **Simple cash-basis accounting.** Money moves when cash moves. Income, expense and transfer are the only transaction types, and amounts are integer rupiah.
5. **Nothing is silently lost.** Corrections are versioned, deletes are soft, and important actions are audit-logged. Backups are tested by actually restoring them.
6. **Privacy by default.** Logs contain no financial text, and platform operators do not browse tenant data. Data sent to AI is the minimum needed.
7. **Indonesian first.** UI strings are in Indonesian. Amounts are parsed in Indonesian formats (`50rb`, `1,5jt`). The default timezone is `Asia/Jakarta`.

## 5. Scope

### 5.1 MVP (in scope)

| Area | Included |
|---|---|
| Identity | Private onboarding by invite; internal user IDs; Telegram identity mapping |
| Tenancy | One personal workspace per user (owner-only); multiple business workspaces per user |
| Collaboration | Business invitations; Owner / Admin / Editor / Viewer roles; ownership transfer; leaving and removing members |
| Ledger | Accounts (cash, bank, e-wallet); categories; income, expense and internal transfer; integer rupiah |
| Capture | Gemini Indonesian text parsing, receipt OCR, local fallback parser, mandatory confirmation |
| Lifecycle | Correction with revision history; soft delete and restore |
| Reporting | Cash flow by period, category and account; account balances; CSV export |
| Operations | Audit log; automated encrypted backups; tested restore; Docker Compose on an Ubuntu VPS |
| Migration | One-time import of V1 Google Sheets data into personal workspaces |

### 5.2 Future public SaaS (designed for, not built in MVP)

- Website registration, email verification, secure login, password reset.
- Secure Telegram linking with expiring, single-use tokens (web account → Telegram).
- Public self-service onboarding and workspace management on the web.
- Web financial dashboard.
- Subscription plans, quotas and billing.

MVP design obligations that make this possible later: identity is separate from Telegram; the `users` table has nullable email and credential columns reserved; the service layer is independent of any channel; the authorization policy is channel-agnostic; quota counters (`ai_usage`) exist from the MVP.

### 5.3 Out of scope (MVP)

Inventory; COGS; accrual accounting; a full general ledger or double-entry journals with a chart of accounts (the D1 `transaction_entries` model records movements of money between *money accounts* only; it is not a GL); balance sheet; receivables and payables; automatic bank or e-wallet synchronization; subscriptions and billing; multi-currency (IDR only); Telegram group-chat operation (private chats only); a native mobile app.

## 6. Success criteria for the MVP

| Metric | Target |
|---|---|
| Cross-workspace data exposure | **Zero** (enforced by tests in doc 07 and RLS) |
| V1 data migrated | 100% of valid rows imported, with per-user count and sum reconciliation |
| Capture speed | Text entry → confirmation prompt in p95 ≤ 6 s (AI) or ≤ 1.5 s (local parser) |
| Parser accuracy | ≥ 95% correct amounts on the golden Indonesian corpus (doc 07) |
| Restore | Monthly restore drill passes; RTO ≤ 4 h, RPO ≤ 24 h |
| Adoption (internal) | All V1 users moved to V2 and V1 decommissioned |

## 7. Assumptions

- A1. MVP users number in the tens and grow to a few hundred, all invited.
- A2. One Ubuntu VPS (2 vCPU / 4 GB RAM or better) runs everything for the MVP.
- A3. IDR is the only currency, and amounts fit in `BIGINT` whole rupiah.
- A4. Gemini remains the AI provider. The choice of model and pricing tier is decision D8.
- A5. V1 stays in service (with only minimal stabilization) until V2 cutover.

---

## 8. Open decisions register

Each document cites these decisions by ID. **Status** is either ✅ *Approved* (binding; date of owner approval) or ⏳ *Open* (the recommended default is assumed, but not binding until approved). Approved decisions may change only through a new owner decision recorded here.

| ID | Status | Decision | Options | Approved outcome / recommended default | Affects |
|---|---|---|---|---|---|
| **D1** | ✅ Approved 2026-10-23 | How internal transfers are modelled | (a) one row with `account_id` + `to_account_id`; (b) a transaction header with entry lines | **(b)**: one logical transaction (header) with **exactly two `transaction_entries`**: a negative leg on the source account and a positive leg on the destination account. The legs sum to zero, and transfers are never counted as income or expense. Income and expense use the same model with exactly one entry (doc 05 §5.2) | 02, 04, 05, 07, 08 |
| **D2** | ⏳ Open | Editor edit scope | (a) edit any transaction; (b) edit only their own | **(a)**, as the brief states; keep (b) as a per-workspace setting later | 08 |
| **D3** | ⏳ Open | Can Admins manage other Admins? | (a) yes; (b) only the Owner grants, revokes or removes Admin | **(b)** limits the damage from a hostile Admin | 08 |
| **D4** | ⏳ Open | Who may export CSV | (a) all roles; (b) Owner and Admin; (c) Owner, Admin and Editor | **(b)**: export is bulk exfiltration; viewing reports stays open to all | 08 |
| **D5** | ✅ Approved 2026-10-23 | When RLS is enabled | (a) from Phase 1; (b) at hardening (Phase 6) | **(a)**: RLS `ENABLE` + `FORCE` on every tenant table from the first migration that creates it, starting in Phase 1 | 06, 07, 09 |
| **D6** | ⏳ Open | Private MVP onboarding | (a) single-use invite codes made by the platform admin; (b) static Telegram ID allow-list | **(a)** reuses the token machinery the future web linking will need | 02, 05 |
| **D7** | ⏳ Open | Receipt image retention | (a) never store, only extracted data; (b) store encrypted for N days | **(a)** for the MVP; store the Telegram `file_unique_id` only | 09, 05 |
| **D8** | ⏳ Open | Gemini model and data tier | Paid tier (no training on prompts) vs free tier; model ID set by config | **Paid tier**, model chosen at Phase 4 from Google's then-current list | 03, 09 |
| **D9** | ✅ Approved 2026-10-23 | PostgreSQL hosting and version | (a) container on the same VPS; (b) managed Postgres | **(a) PostgreSQL 18 in Docker**, with an explicitly pinned supported image (exact minor version + digest), a persistent named volume, healthchecks, and **no published database port**. Managed Postgres may be reconsidered for the public SaaS (Phase 8d). Container plan: doc 04 §8 | 03, 04, 05, 06, 07 |
| **D10** | ⏳ Open | Off-site backup target | S3-compatible object storage (vendor TBD) vs a second VPS | **S3-compatible bucket** with object lock or versioning | 09, 06 |
| **D11** | ✅ Approved 2026-10-23 | Google Sheets after cutover | (a) drop; (b) optional per-workspace export | **Google Sheets is not a system of record after cutover.** V2 never reads from or writes to Sheets in normal operation (the one-time V1 import is the only read). An optional Sheets **export** may return in a future phase (listed in Phase 8, unscheduled). CSV export covers the MVP need | 02, 06 |
| **D12** | ⏳ Open | Where V1 history goes | (a) each user's personal workspace and a default "Kas" account; (b) a user-chosen workspace | **(a)**, with a post-import "move transactions" tool deferred | 05, 06 |
| **D13** | ⏳ Open | Workspace deletion | Grace period and purge policy | **30-day** pending-deletion (read-only), then a hard purge job; audit rows kept | 02, 09 |
| **D14** | ⏳ Open | User erasure vs audit retention | Delete vs anonymise | **Anonymise** the user's profile; keep audit rows with a pseudonymous actor ID | 09 |
| **D15** | ⏳ Open | Negative balances | Allow or block | **Allow** and show a warning (cash-basis reality: overdrafts and data-entry lag) | 02 |
| **D16** | ⏳ Open | Future web stack | FastAPI + server-rendered vs FastAPI + SPA | **FastAPI** sharing the service layer; front-end choice deferred | 04 |
| **D17** | ⏳ Open | Period locking | None vs lock months older than N | **None** in the MVP; every edit is versioned and audited | 02 |
| **D18** | ✅ Approved 2026-10-23 | Environments | One bot token vs separate staging bot | **Separate staging Telegram bot token and separate staging PostgreSQL database**, isolated from production (separate compose project, volume, network, secrets, and backup destination; startup guards refuse cross-environment configuration). Plan: doc 04 §8.3 | 03, 04, 06, 09 |
| **D19** | ⏳ Open | Membership visibility | Can all members see the member list? | **Yes**: display name and role only | 08 |
| **D20** | ⏳ Open | Who can rename a business workspace and change its settings | Owner only vs Owner + Admin | **Owner + Admin** (not owner-only per the brief); deletion and transfer stay owner-only | 08 |
| **D21** | ⏳ Open (new) | Can a transaction's **type** be changed after it is posted? | (a) type is immutable; "change type" voids the original and creates a linked replacement; (b) mutable type | **(a)**: the type is part of the entry sign rules and FKs (doc 05 §5.2). The UI still offers "ubah jenis", implemented as void + replace in one DB transaction | 02, 05, 08 |
| **D22** | ⏳ Open (new) | Sign and terminology for entries | (a) signed `amount` (outflow < 0, inflow > 0) with leg names `source`/`destination`/`single`; (b) separate debit/credit columns | **(a)**. The owner's D1 wording "debit from the source, credit to the destination" maps to *source leg negative, destination leg positive*. We avoid the words debit/credit in code because classical double-entry uses the opposite sign for asset accounts | 05 |
| **D23** | ⏳ Open (new; **blocks Step 2**) | Python version and dependency tooling | Python 3.12 / 3.13 / 3.14; `uv` (uv.lock) vs pip-tools (hashed requirements) | **Python 3.13** + **uv** with a committed `uv.lock`; image builds use `uv sync --frozen` | 03, 04, 06 |
| **D24** | ✅ Approved 2026-10-24 (staging placement); drill placement ⏳ open | Where staging runs, and where backup drills and migration rehearsals run | (a) staging on an existing VPS as a separate compose project; (b) a separate staging VPS. Drills and rehearsals: (i) in staging; (ii) in a separate ephemeral "drill" environment | **Approved: staging runs on the existing Contabo VPS**, isolated from everything else on it by its own Docker compose project (`finbot-staging`), internal network (`finbot-staging_db`, gateway mode isolated), volume (`finbot-staging_pgdata`) and secrets (`deploy/secrets/staging/`). The VPS also hosts an unrelated `services` stack (`services-postgres`, `services-redis`, network `services_default`), which V2 never touches. **Operators:** `claude` is the development user and has **no Docker access**; `deploy` is the Docker operator; root (sudo) prepares host groups and secret files (doc 04 §8.3). Drills and rehearsals: recommended default **(ii)**, because staging must never hold production personal data (doc 09 §9); not yet approved | 04, 06, 09 |
| **D25** | ⏳ Open (new; non-blocking) | CI platform | GitHub Actions vs other | **GitHub Actions** (the README and remote suggest GitHub) | 06 |

## 9. Glossary

| Term | Meaning |
|---|---|
| **User** | A person with an internal UUID. Independent of any channel. |
| **Telegram identity** | The binding between a Telegram `user_id` and a User. |
| **Workspace** | The tenant boundary. Either *personal* (exactly one owner, no other members) or *business* (members with roles). |
| **Active workspace** | The workspace a user's Telegram chat currently records into. It is always shown in prompts. |
| **Account** | A place money sits: cash, bank or e-wallet. Belongs to one workspace. |
| **Transaction** | A confirmed income, expense or transfer: a header row plus its entries. Integer rupiah, voidable (soft delete), versioned. |
| **Transaction entry** | One signed movement of integer rupiah on one account. Income and expense have exactly 1 entry; a transfer has exactly 2, summing to zero (D1). |
| **Void** | Cancelling a posted transaction (UI: "hapus"). Its entries stay unchanged but stop counting. Reversible ("pulihkan"). |
| **Draft** | An unconfirmed transaction proposal (from AI, a parser or a receipt) awaiting user confirmation. |
| **Cash-basis** | Recorded when money actually moves. No accruals, receivables or payables. |
