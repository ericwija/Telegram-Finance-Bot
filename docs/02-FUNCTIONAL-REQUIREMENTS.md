# 02 — Functional Requirements

| | |
|---|---|
| **Status** | Draft for owner approval; reconciled with approved D1 and D11 (rev. 2, 2026-10-23) |
| **Date** | 2026-10-23 |
| **Related** | [01 Vision](01-PROJECT-VISION.md) · [05 Data Model](05-DATA-MODEL.md) · [08 RBAC](08-RBAC-MATRIX.md) |

## Conventions

- **ID**: `FR-<AREA>-<NN>`.
- **Release**: `MVP` = must ship in V2 MVP · `FUT` = future public SaaS (designed for, not built) · `MIG` = V1 migration.
- **Permission** names refer to [08 RBAC Matrix](08-RBAC-MATRIX.md). Every FR that reads or changes workspace data is subject to the **global rules G-1…G-8** below. They are not repeated in each FR.
- Bot UI text is in Indonesian. The command names shown are proposals.

## Global rules (apply to every requirement)

| ID | Rule |
|---|---|
| **G-1** | Every operation runs in exactly one **workspace context** (or an explicit user-only context for listing one's own workspaces). The context comes from server-side state, **never** from client-supplied IDs alone. |
| **G-2** | Authorization is evaluated **at execution time**, not when a button or link was created. If a role was downgraded or a member removed after a button was shown, pressing it fails. |
| **G-3** | A resource that exists in another workspace is reported as **not found**, never as "forbidden", so its existence is not leaked. |
| **G-4** | A permission denial inside the user's own workspace shows *"Anda tidak memiliki izin untuk tindakan ini."* and records a `security.authz_denied` audit event. |
| **G-5** | Every state-changing operation writes an audit event (doc 09 §6) in the same database transaction. |
| **G-6** | Amounts are integers in whole rupiah, `1 ≤ amount ≤ 1,000,000,000,000`. A rupiah has no decimal unit; any decimal input is rounded half-up and the user is told. |
| **G-7** | Dates are calendar dates in the **workspace timezone** (default `Asia/Jakarta`). "Today" is computed in that timezone. |
| **G-8** | Every confirmation prompt shows the **active workspace name and type** (🏠 Pribadi / 🏢 Bisnis) so the user cannot record into the wrong workspace by accident. |

---

## 1. Identity & onboarding (`IDN`)

| ID | Release | Requirement | Acceptance (summary) |
|---|---|---|---|
| FR-IDN-01 | MVP | Onboarding is **invite-only** (D6). A platform admin creates single-use invite codes with a 7-day expiry via CLI. A new user opens `t.me/<bot>?start=onb_<code>`. | A valid code creates a User and a Telegram identity, then marks the code used. An expired, used or unknown code is refused with a generic message. |
| FR-IDN-02 | MVP | Messages from Telegram users with no linked identity get only an onboarding explanation. No other feature is reachable. | Every command or callback from an unknown Telegram ID is refused, and no workspace data is touched. |
| FR-IDN-03 | MVP | Each User has an internal UUID. A Telegram `user_id` maps to at most one active User, and a User has at most one active Telegram identity. | Database uniqueness enforced (doc 05). |
| FR-IDN-04 | MVP | On first onboarding the system creates the user's **personal workspace**, a default "Kas" cash account and default categories in one transaction. | The user has exactly 1 personal workspace with 1 owner membership. |
| FR-IDN-05 | MVP | The user can set a display name (defaults to their Telegram first name) and view their profile (`/profil`). | |
| FR-IDN-06 | MVP | A platform admin can **suspend** or **reactivate** a user via CLI. A suspended user is refused everywhere, and their pending drafts expire. | Covered by authorization tests in doc 07. |
| FR-IDN-07 | MVP | `/start` for an onboarded user shows the main menu and the active workspace. | |
| FR-IDN-08 | FUT | Website registration with email and password, email verification, login, logout, password reset, and session management. | Reserved columns and tables in doc 05 §8. |
| FR-IDN-09 | FUT | **Telegram linking**: a logged-in web user generates a single-use token (expires in 10 min, stored hashed) and opens `t.me/<bot>?start=lnk_<token>` to bind their Telegram identity. | The token can be used only once and only before expiry; a reused or expired token is refused and audit-logged. |
| FR-IDN-10 | FUT | Unlink and relink Telegram. An unlinked Telegram ID can then be linked to a different User. | |

## 2. Workspaces (`WSP`)

| ID | Release | Requirement | Permission |
|---|---|---|---|
| FR-WSP-01 | MVP | Each user has exactly **one personal workspace**. It cannot be shared, have members added, be transferred or be deleted on its own. | — |
| FR-WSP-02 | MVP | Any active user can **create a business workspace** (`/ruang baru`), becoming its Owner. A default account and categories are seeded. | `workspace.create_business` (user-level) |
| FR-WSP-03 | MVP | `/ruang` lists the user's workspaces (personal first, then businesses with the user's role) and switches the **active workspace**. The choice persists per user. | membership |
| FR-WSP-04 | MVP | Rename a business workspace and change its timezone. | `workspace.update` |
| FR-WSP-05 | MVP | **Delete a business workspace** (Owner only). It takes two steps: the Owner confirms, then types the workspace name. The workspace enters `pending_deletion` for 30 days (D13), during which it is read-only for everyone, and the Owner can cancel. After 30 days a purge job hard-deletes its data. | `workspace.delete` |
| FR-WSP-06 | MVP | **Ownership transfer** (Owner only) to an existing active member. The target must accept within 24 h. On acceptance the target becomes Owner and the previous Owner becomes Admin, atomically. | `workspace.transfer_ownership` |
| FR-WSP-07 | MVP | If the active workspace becomes inaccessible (the user was removed, or the workspace is deleted or pending deletion), the active workspace falls back to the personal workspace on the next interaction and the user is told. | — |
| FR-WSP-08 | FUT | Web workspace management with the same rules. | |

## 3. Members & invitations (`MEM`)

| ID | Release | Requirement | Permission |
|---|---|---|---|
| FR-MEM-01 | MVP | List members with display name and role (D19). | `member.view` |
| FR-MEM-02 | MVP | Create an **invitation** for a role. Invitations are single-use, expire after 72 h by default, and are delivered as a deep link `t.me/<bot>?start=inv_<token>`. Only the token's SHA-256 hash is stored. | `member.invite` + role-assignment limits (doc 08 §4) |
| FR-MEM-03 | MVP | Accepting an invitation needs an onboarded user. If an unknown Telegram user opens an invitation link, the bot explains that they need a platform invite first (MVP). The user sees the workspace name and role before accepting. | — |
| FR-MEM-04 | MVP | Revoke a pending invitation. | `member.revoke_invitation` (same limits as invite) |
| FR-MEM-05 | MVP | Change a member's role, within the role-assignment limits. | `member.change_role` |
| FR-MEM-06 | MVP | Remove a member. Access ends immediately, and their drafts in that workspace are expired. | `member.remove` |
| FR-MEM-07 | MVP | Leave a workspace (any non-owner role). | `member.leave` |
| FR-MEM-08 | MVP | A workspace always has **exactly one Owner**. The Owner cannot leave, be removed or be demoted except through FR-WSP-06. | DB constraint + policy |
| FR-MEM-09 | MVP | Members are notified by Telegram DM when they are added, change role or are removed. | — |

## 4. Accounts (`ACC`)

| ID | Release | Requirement | Permission |
|---|---|---|---|
| FR-ACC-01 | MVP | Create an account with a name (unique within the workspace among active accounts), a type (`cash`, `bank`, `ewallet`), an opening balance (integer, may be 0 or negative) and an opening date. | `account.manage` |
| FR-ACC-02 | MVP | Rename an account, or change its opening balance (versioned in the audit log). | `account.manage` |
| FR-ACC-03 | MVP | **Archive** an account: no new transactions can use it, but its history and balance remain. Hard delete is allowed only if no transactions ever referenced it. | `account.manage` |
| FR-ACC-04 | MVP | List accounts with their current balances (`/saldo`). | `account.view` |
| FR-ACC-05 | MVP | Each member has a **default account** per workspace, used when the parser cannot tell which account was meant. | own preference |
| FR-ACC-06 | MVP | Negative balances are allowed and show a ⚠️ warning (D15). | — |

## 5. Categories (`CAT`)

| ID | Release | Requirement | Permission |
|---|---|---|---|
| FR-CAT-01 | MVP | New workspaces are seeded with default Indonesian categories. Income: Gaji, Bonus, Penjualan, Investasi, Hadiah, Lainnya. Expense: Makanan, Transportasi, Belanja, Tagihan, Kesehatan, Pendidikan, Hiburan, Iuran, Lainnya. Business workspaces may get a business-oriented set instead (open item O-1, below). | — |
| FR-CAT-02 | MVP | Create, rename and archive categories. Each has a `kind` (income or expense), and names are unique per workspace and kind. | `category.manage` |
| FR-CAT-03 | MVP | A transaction's category must exist in the same workspace and have the same kind as the transaction (doc 05 constraint). | — |
| FR-CAT-04 | MVP | Archived categories stay on historical transactions but are not offered for new ones. | — |

## 6. Transactions (`TXN`)

| ID | Release | Requirement | Permission |
|---|---|---|---|
| FR-TXN-01 | MVP | Three types, each one logical transaction made of a header plus **transaction entries** (D1, doc 05 §5.2). **Income** has 1 entry (+amount) on one account. **Expense** has 1 entry (−amount) on one account. **Transfer** has **exactly 2 entries**: −amount on the source account and +amount on a *different* destination account in the **same workspace**. The entries sum to zero. A transfer is never counted as income or expense. | — |
| FR-TXN-02 | MVP | Header fields: type, amount (absolute, G-6), date (G-7), category (income and expense only; required, falling back to "Lainnya"), description (≤ 500 chars), source (`text`, `ai_text`, `receipt`, `manual`, `import`). The account (income or expense), or the source and destination accounts (transfer), are stored as entries. The user sees one transaction; entries are not exposed as separate items. | — |
| FR-TXN-02a | MVP | **Atomicity:** a transaction and all its entries are written in one database transaction. A partially written transfer (one leg) can never be committed (deferred invariant check, doc 05 INV-2/INV-4). | — |
| FR-TXN-03 | MVP | **Free-text capture**: any non-command message in a private chat is parsed into one or more **drafts**. One line gives one draft; several lines give a batch of up to 20 lines. | `transaction.create` |
| FR-TXN-04 | MVP | **Confirmation is mandatory.** A draft shows workspace, type, amount, date, account, category and description, with buttons ✅ Simpan · ✏️ Ubah · ❌ Batal. Nothing is stored until ✅. | `transaction.create` at confirm time |
| FR-TXN-05 | MVP | **Editing a draft before saving**: change amount, date, account, category, type or description via buttons or a reply. | creator of the draft |
| FR-TXN-06 | MVP | Drafts expire after 24 h. A draft can be confirmed only by its creator, only in its own workspace, and only once (idempotent: pressing twice stores one transaction). | — |
| FR-TXN-07 | MVP | **Transfers by text**, e.g. "transfer dari BCA ke GoPay 500rb" or "tarik tunai 200rb" (bank → Kas). Accounts are matched by name or alias. If the match is ambiguous, the user is asked to choose. | `transaction.create` |
| FR-TXN-08 | MVP | **Manual guided entry** (`/catat`): a button flow for type → account → amount → category → date → description, with no AI involved. | `transaction.create` |
| FR-TXN-09 | MVP | **History** (`/riwayat`): a paginated list (10 per page) with filters for period, account, category and type. Each item opens a detail view with actions allowed by the user's role. | `transaction.view` |
| FR-TXN-10 | MVP | **Correction**: edit the date, description, category, amount or account(s) of a posted transaction. Each edit increments `version` and stores a revision snapshot. Changing the amount or an account writes a **new complete entry set**; earlier sets are kept as immutable history and never edited. **Changing the type** (e.g. expense → transfer) voids the original and creates a linked replacement in one step (D21). Concurrent edits are detected with optimistic locking; if the stale editor is refused, they are shown the latest version. | `transaction.update` (D2) |
| FR-TXN-11 | MVP | **Cancellation (void)**, labelled "hapus" in the UI, with an optional reason. The transaction's status becomes `voided`. Its entries are **not** modified or deleted, but they stop counting in balances and reports. Voided transactions appear in a "Dihapus" view and can be **restored** (status back to `posted`). A transfer is always voided or restored as a whole, never one leg. | `transaction.delete`, `transaction.restore` |
| FR-TXN-12 | MVP | Revision history of a transaction can be viewed (who changed what, and when). | `transaction.view` (same workspace) |
| FR-TXN-13 | MVP | Undo: a "↩️ Batalkan" button on the save confirmation voids the transaction within 5 minutes, but only for the creator and only if they hold `transaction.delete`. Editors instead get "✏️ Ubah" to correct the entry. | as stated |
| FR-TXN-14 | MVP | No period locking in the MVP (D17). Back-dated entries are allowed and flagged in the audit log. | — |

## 7. AI parsing & receipt OCR (`AI`)

| ID | Release | Requirement |
|---|---|---|
| FR-AI-01 | MVP | Indonesian text is parsed by Gemini using **structured JSON output** validated against a schema. The output fields are type, amount, date expression or resolved date, account hint, to-account hint, category hint and description. |
| FR-AI-02 | MVP | The AI receives only the message text, today's date in the workspace timezone, and the **names** of the workspace's active categories and accounts. It never receives balances, other transactions or other workspaces' data. |
| FR-AI-03 | MVP | Hints are resolved to real IDs **server-side**, against the active workspace only. An unknown category falls back to "Lainnya"; an unknown account falls back to the user's default account. |
| FR-AI-04 | MVP | **Local fallback parser** when the AI is unavailable, times out (8 s), is over quota, or returns invalid output. It must pass the golden corpus (doc 07), including the V1 regressions: "beli 2 kopi 30000" → 30,000, and "busana" is not classified as Transportasi. |
| FR-AI-05 | MVP | **Receipt OCR**: a photo leads to an extracted store, date, items, tax, discount and total. The user picks *total only*, *per item* or *per category*, and each choice produces drafts for confirmation. If the item sum does not reconcile with the total within Rp 1, a warning is shown. |
| FR-AI-06 | MVP | Receipt images are processed in memory and **not stored** (D7). Only the Telegram `file_unique_id` and the extracted JSON (kept 90 days) are stored. |
| FR-AI-07 | MVP | **Per-user AI quota** (default 200 text calls and 50 receipts per day, configurable). Every call is recorded in `ai_usage`. Once the quota is exceeded, the local parser is used and receipts are refused with an explanation. |
| FR-AI-08 | MVP | AI output can never cause an action by itself: no tool calls, no direct writes. It only fills drafts (prompt-injection containment). |

## 8. Reporting (`RPT`)

| ID | Release | Requirement | Permission |
|---|---|---|---|
| FR-RPT-01 | MVP | **Cash-flow report** (`/laporan`) for today, this week, this month, last month or a custom range. It shows total income, total expense and net, plus breakdowns by category and by account. Transfers are excluded from income and expense. | `report.view` |
| FR-RPT-02 | MVP | **Account balances** (`/saldo`) as of today or a chosen date. Balance = opening balance + Σ signed current entries on that account, over posted (non-voided) transactions dated on or before that date. This equals income − expense − transfers out + transfers in. | `report.view` |
| FR-RPT-03 | MVP | Month-over-month comparison of the last 6 months (income, expense, net). | `report.view` |
| FR-RPT-04 | MVP | **CSV export** of transactions for a period, sent as a Telegram document. | `report.export` (D4) |
| FR-RPT-05 | MVP | Reports are computed with SQL aggregates and respect G-7 period boundaries. Messages over 4,096 chars are paginated. | — |
| FR-RPT-06 | FUT | Scheduled daily, weekly and monthly summaries (opt-in per member). | `report.view` |
| FR-RPT-07 | FUT | Web dashboard with charts. | `report.view` |
| FR-RPT-08 | FUT | Optional, per-workspace **Google Sheets export** (one-way, V2 → Sheets; never read back). Not scheduled (D11). | `report.export` |

## 9. Audit (`AUD`)

| ID | Release | Requirement | Permission |
|---|---|---|---|
| FR-AUD-01 | MVP | Every event listed in doc 09 §6 is recorded with actor, workspace, action, entity, timestamp, channel and request ID. Records contain **no free-text financial descriptions**; changes record field names plus before and after values for structured fields. | — |
| FR-AUD-02 | MVP | Workspace audit view (`/audit`): the last 50 events with filters. | `audit.view` |
| FR-AUD-03 | MVP | Audit records are append-only at the database level. | — |

## 10. Backup & restore (`OPS`)

| ID | Release | Requirement |
|---|---|---|
| FR-OPS-01 | MVP | A nightly logical backup (`pg_dump -Fc`) is encrypted and uploaded off-site (D10). Retention is 7 daily, 4 weekly and 6 monthly. |
| FR-OPS-02 | MVP | A documented, scripted **restore** into a fresh database, with verification queries (row counts, balance checksums). |
| FR-OPS-03 | MVP | A **monthly restore drill** into an ephemeral drill environment (never staging, D18/D24), with results recorded. |
| FR-OPS-04 | MVP | Backup failure alerts the operator via Telegram DM to the platform admin. |
| FR-OPS-05 | MVP | Restores are whole-database only. Recovering from a single tenant's mistake uses void/restore and revisions, not a database restore. |
| FR-OPS-06 | MVP | Health status: `/status` for the platform admin only (database reachable, last backup age, AI availability). |

## 11. V1 migration (`MIG`)

| ID | Release | Requirement |
|---|---|---|
| FR-MIG-01 | MIG | An import tool reads the V1 sheet **read-only** and maps each distinct `User ID` to a V2 user (pre-created via invites, or created during import). Rows go into that user's **personal workspace** and a default "Kas" account (D12). |
| FR-MIG-02 | MIG | Mapping: sign of `Amount` → type (> 0 income, < 0 expense, 0 rejected); `abs(round(Amount))` → amount; `Category` → a category matched case-insensitively or created; `Date` → `txn_date`; `Timestamp` → `created_at`; source = `import`; `import_ref` = sheet row number plus a content hash. |
| FR-MIG-03 | MIG | **Dry-run mode** produces a report of rows OK, rows rejected (with reason), duplicates, rounding adjustments, and per-user count and sum, without writing anything. |
| FR-MIG-04 | MIG | Re-running the import is idempotent (`UNIQUE(workspace_id, import_ref)`). |
| FR-MIG-05 | MIG | **Reconciliation**: per user, the imported count and net sum must equal the dry-run figures. Any mismatch blocks cutover. |
| FR-MIG-06 | MIG | **Google Sheets is not a system of record after cutover (D11, approved).** The one-time import is the only V2 read of the sheet, and no V2 data is written back to it. An optional Sheets export may be specified in a future phase (see FR-RPT-08). |

---

## 12. Command map (proposed)

| Command | Purpose | V1 equivalent |
|---|---|---|
| `/start` | Menu, onboarding and invitation deep links | `/start` |
| `/bantuan` (`/help`) | Help | `/help` |
| `/batal` | Cancel the conversation in progress (Step 6) | — |
| `/ruang` | List and switch workspaces; `/ruang baru` creates a business | — |
| `/catat` (`/tambah`) | Guided entry (Step 6). `/pemasukan` and `/pengeluaran [jumlah] [keterangan]` start it with the type (and amount) given | `/catat` |
| `/transfer` | Guided transfer | — |
| `/saldo` | Account balances (Step 6: recorded balances, plus all-time income, expense and net) | — |
| `/laporan` (`/ringkasan`) | Cash-flow report (Step 6: one calendar month, `/laporan [bulan]`) | `/laporan` |
| `/riwayat` (`/transaksi`) | History; edit, delete and restore (Step 6: `[pemasukan\|pengeluaran] [dihapus] [bulan \| tanggal tanggal]`) | `/hapus` (replaced) |
| `/akun`, `/kategori` | Manage accounts and categories (Step 6: `/kategori [pemasukan\|pengeluaran] [nonaktif]` and `/kategori tambah <jenis> <nama>`, paged; `nonaktif` lists deactivated categories to reactivate them, Step 6B) | — |
| `/anggota`, `/undang` | Members and invitations | — |
| `/ekspor` | CSV export | `/sheet` (replaced) |
| `/audit` | Audit view | — |
| `/profil` | Profile and default account | `/me` |
| `/status` | Platform admin health | — |
| `/undangan_baru`, `/undangan_daftar`, `/undangan_cabut` | Platform admin: create, list and revoke onboarding invites (FR-IDN-01; private chat only; doc 04 §5.8) | — |

**Step 6 status (finance core, doc 10 §12).**
- *Implemented:* FR-IDN-04 (lazily, on first use), FR-CAT-01…04, FR-TXN-01/02/02a (income and expense, through the D1 entry model), FR-TXN-04…06, FR-TXN-08, FR-TXN-09, FR-TXN-10 (without type change), FR-TXN-11, FR-ACC-04/05/06 (seeded "Kas" only), FR-RPT-01 (monthly), FR-RPT-02 (as of today) and FR-RPT-05.
- *Not built yet:* FR-ACC-01…03, FR-TXN-03, FR-TXN-07, FR-TXN-12/13, FR-RPT-03/04 and FR-IDN-07's full menu (`/start` points to `/bantuan`).

**Step 6B status (remediation, doc 10 §13).**
- FR-TXN-04: the confirmation shows workspace, type, amount, date, **account**, category and description, with ✅ Simpan · ✏️ Ubah · ❌ Batal. The account is resolved when the draft starts and kept in the draft, so changing the default account later does not redirect it.
- FR-TXN-05: **partly implemented.** ✏️ Ubah corrects type, amount, category, description and date, by buttons or a reply, and every correction returns to the complete confirmation. *Changing the account is not offered* until account management (FR-ACC-01) exists; only "Kas" can be chosen today.
- FR-TXN-06: an expired draft is persisted as expired; a replay of a confirmed draft is re-authorized and never shows a voided transaction or one seen under an earlier membership.
- FR-CAT-02/04: category lists are paged, so every category (including `Lainnya`) is reachable; deactivated categories are listed with `nonaktif` and can be reactivated.
- Amounts in commands keep separated units: `/pengeluaran 2 juta sewa` is Rp 2.000.000 with the description "sewa"; an ambiguous amount (`2 jutaan`) is asked again.

**Step 6C status (release candidate, doc 10 §14).**
- FR-TXN-05: ✏️ Ubah opens a field menu; ↩️ Kembali ke ringkasan returns to the confirmation from the menu or from any field left unchanged; ✅ stays valid. The account is still not changeable (deferred with FR-ACC-01).
- FR-CAT-02: at most **200 categories per workspace and kind**, archived ones included (owner decision pending on the number); the 201st is refused ("Batas 200 kategori …"). Every category page is reachable and `Lainnya` is always listed.
- ❌ and `/batal` are refused (and the conversation invalidated) when the member no longer holds the conversation's permission.

## 13. Open items specific to this document

- **O-1**: Should business workspaces be seeded with business categories (Penjualan, Bahan Baku, Gaji Karyawan, Sewa, Operasional, Marketing)? Proposed: yes.
- **O-2**: Should the batch limit stay at 20 lines?
- **O-3**: Should the invitation expiry stay at 72 h?
