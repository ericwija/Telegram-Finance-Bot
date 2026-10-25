# 09 — Tenant Isolation & Security

| | |
|---|---|
| **Status** | Draft; D5 and D18 approved 2026-10-23 and D24 (staging placement) approved 2026-10-24, incorporated; D7, D8, D10, D13, D14 and the D24 drill placement open |
| **Date** | 2026-10-23 |
| **Related** | [05 Data Model](05-DATA-MODEL.md) · [08 RBAC](08-RBAC-MATRIX.md) · [07 Tests](07-TEST-AND-ACCEPTANCE-PLAN.md) |

## 1. Isolation goals

1. A principal can never read, change or infer the existence of data in a workspace where they have no active membership.
2. A bug in one layer (a forgotten `WHERE`, a forged callback, a stale role) must not be enough on its own to cause a leak.
3. Personal workspaces are strictly owner-only. No membership, invitation or transfer path exists for them, and the schema enforces this.
4. Every isolation property is proven by automated tests (doc 07).

## 2. Defense-in-depth layers

| # | Layer | Mechanism | Stops |
|---|---|---|---|
| L1 | **Channel adapter** | Principal resolved from the Telegram `from.id` → identity → user. The workspace is taken from server-side `user_preferences` and **never** from callback data. Callback data only references drafts or actions owned by the caller (doc 04 §5.4). | Forged or replayed callbacks, workspace-ID tampering |
| L2 | **Service + policy** | `authorize()` first in every use case (doc 08 §1), evaluated against current membership | Role escalation, stale roles, cross-tenant IDs (→ NotFound) |
| L3 | **Repository** | Every method signature requires `WorkspaceContext` and adds `workspace_id = :ctx` | Accidental unscoped queries |
| L4 | **Schema constraints** | Composite FKs `(workspace_id, id)`, kind-aware FKs, personal-workspace constraints and triggers (doc 05) | Cross-workspace references even when L2 or L3 are buggy |
| L5 | **PostgreSQL RLS** (D5: on from Phase 1) | `FORCE ROW LEVEL SECURITY` keyed on transaction-local `app.workspace_id` and `app.user_id` | Any query that escapes L3. Fails closed when the context is unset |
| L6 | **DB role privileges** | `finbot_app`: no BYPASSRLS, no DDL; INSERT/SELECT only on `audit_events`, `transaction_entries` and `transaction_revisions` | Tampering with audit or money history, schema changes from the app |
| L7 | **Environment isolation** (D18) | Separate staging and production bots, databases, secrets and backup keys; startup guards G-ENV-1…6 (doc 04 §8.3) | Staging code serving production data, or the reverse |

**What RLS does *not* do:** RLS protects against *bugs*. A fully compromised application process can set `app.workspace_id` to anything, so RLS is not a defense against that. That threat is reduced by L6, by secret hygiene, by host hardening, and by the audit trail.

## 3. Context handling rules

- The UoW opens a database transaction and **verifies the active membership** (as part of L2) *before* calling `set_config('app.workspace_id', :wid, true)` and `set_config('app.user_id', :uid, true)`.
- Settings are always **transaction-local** (`is_local = true`). Session-level `SET` is forbidden, and a lint rule blocks it in `infra/db`.
- The connection pool runs `DISCARD ALL` on release (asyncpg reset) as an extra safeguard.
- A request that needs two workspaces (none exist in the MVP) must use two separate UoWs.
- Background jobs get workspace IDs through narrow `SECURITY DEFINER` functions that return IDs only. They then process each workspace in its own UoW with `app.user_id` unset and `app.actor = 'system:<job>'`.

## 4. Row-level security policies

### 4.1 Helpers

```sql
CREATE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

CREATE FUNCTION app_workspace_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.workspace_id', true), '')::uuid $$;
```

An unset setting returns NULL. `workspace_id = NULL` is never true, so **zero rows** are visible (fail-closed).

**Membership check (S5, approved 2026-10-24; implemented in Step 3B.3).** Policies also require that `app_user_id()` has an **active** membership in the row's workspace, not just a matching context. This is done through one helper, `app_is_active_member(ws uuid) RETURNS boolean`, with these rules so that no policy recurses into RLS and no definer function widens access:
- `SECURITY DEFINER`, owned by `finbot_owner`, `STABLE`, `SET search_path = pg_catalog, public`, `EXECUTE` granted only to `finbot_app`;
- it reads `workspace_memberships` directly (the owner bypasses RLS, so evaluating it never re-enters a policy) and returns a boolean only, never rows;
- it compares only against `app_user_id()`, never a caller-supplied user ID;
- policies call it as `(SELECT app_is_active_member(workspace_id))`, and the helpers as `(SELECT app_workspace_id())`, so they are planned once per query.

Tests: AT-RLS-06, plus a recursion test (a policy on `workspace_memberships` that uses the helper must not loop).

### 4.2 Tenant tables

Applies to `accounts`, `categories`, `transactions`, **`transaction_entries`**, `transaction_revisions`, `receipts`, `drafts`, `ai_usage`, `workspace_invitations` and `pending_actions` (rows with `workspace_id` set). The `current_entries` view is `security_invoker`, so the caller's RLS applies through it. D5 (approved): every table gets RLS in **the same migration that creates it**, starting with the Phase 1 baseline:

```sql
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON transactions
  USING      (workspace_id = app_workspace_id())
  WITH CHECK (workspace_id = app_workspace_id());
```

### 4.3 Tenancy and identity tables

| Table | Policy |
|---|---|
| `workspaces` | `USING (id = app_workspace_id() OR id IN (SELECT workspace_id FROM workspace_memberships WHERE user_id = app_user_id() AND status = 'active'))`; `WITH CHECK (id = app_workspace_id())` |
| `workspace_memberships` | `USING (workspace_id = app_workspace_id() OR user_id = app_user_id())`; `WITH CHECK (workspace_id = app_workspace_id())` |
| `users` | `USING (id = app_user_id() OR id IN (SELECT user_id FROM workspace_memberships WHERE workspace_id = app_workspace_id()))`. Updates: `WITH CHECK (id = app_user_id())`, **plus column-level UPDATE grants (S7)**: `finbot_app` may update only `display_name`, `locale`, `timezone` and `updated_at`, never `is_platform_admin`, `status`, `email*` or `password_hash`. The same rule applies to every protected table: `id`, `workspace_id`, `created_*`, `kind` and `personal_owner_id` are never updatable by the app |
| `user_preferences` | `USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id())` |
| `audit_events` | SELECT `USING (workspace_id = app_workspace_id())`; INSERT `WITH CHECK (workspace_id IS NULL OR workspace_id = app_workspace_id())`; no UPDATE or DELETE grant to the app |
| `telegram_identities`, `onboarding_invites`, `import_batches` | No direct grants to `finbot_app`. Access only through the `SECURITY DEFINER` functions below |

`SECURITY DEFINER` functions (owned by `finbot_owner`, `SET search_path = pg_catalog, public`, narrow inputs and outputs):

| Function | Returns | Used for |
|---|---|---|
| `resolve_principal(tg_user_id bigint)` | `(user_id, status, is_platform_admin)` | Middleware, before any context exists |
| `redeem_onboarding_invite(token_hash bytea, tg_user_id bigint, display_name text)` | `user_id` | FR-IDN-01 (creates the user, identity, personal workspace and seeds) |
| `find_invitation(token_hash bytea)` | `(invitation_id, workspace_id, role, workspace_name)` or NULL | FR-MEM-03. The caller then opens a UoW in that workspace with `app.user_id` set |
| `jobs_workspaces_with(kind text)` | `setof uuid` | Job fan-out |

### 4.4 Privileged operations

- `FORCE ROW LEVEL SECURITY` applies to table owners too, so `finbot_owner` is created with **`BYPASSRLS`** for migrations, purge and retention pruning. Its credentials are mounted only into the `migrate` container and the purge/prune job entry point, never into the bot's request path.
- **Migrations** run as `finbot_owner` in the one-shot `migrate` container, through `finbot-migrate`, the only entry point that accepts owner credentials (S10, doc 10 §2). The bot refuses `FINBOT_DB_OWNER_*` variables, and migrations refuse the app's.
- **Immutability bypass (S6):** triggers that protect entries, revisions and audit rows allow UPDATE/DELETE only when `session_user = 'finbot_owner'`. `current_user` is not used, because inside any `SECURITY DEFINER` function owned by `finbot_owner` it *is* `finbot_owner`, even when `finbot_app` called it. Tested with a definer function in Step 3B.4.
- **Workspace purge** (D13) runs as `finbot_owner`, from a job entry point that accepts only workspace IDs whose `status='pending_deletion' AND deletion_requested_at < now() - interval '30 days'`. It deletes in FK order, prunes revisions, and writes one platform-level `workspace.purged` audit event (`workspace_id` kept in metadata).
- **Backup** uses `finbot_backup` (BYPASSRLS, SELECT only).

## 5. Threat model (STRIDE-style summary)

| # | Threat | Vector | Mitigation | Test |
|---|---|---|---|---|
| T1 | Cross-tenant read or write via forged IDs | Crafted callback data, or a guessed UUID | L1 (no workspace in callbacks; refs owned by caller), L2 NotFound, L4, L5 | AT-ISO-01..06 |
| T2 | Missing tenant filter in a new query | Developer error | L3 signature enforcement, L5 RLS fail-closed, RLS test suite | AT-RLS-* |
| T3 | Stale privilege | Old buttons after a downgrade or removal | Authorization at execution time (G-2) | AT-RBAC-G2-* |
| T4 | Invitation link forwarded or leaked | Shared in a group chat | Single-use, 72 h expiry, hashed at rest; workspace and role shown before accepting; Owner and Admins are notified on join and can remove | AT-RBAC-R5-* |
| T5 | Telegram account takeover | SIM swap, or a stolen session | Outside our control. Notify the Owner on sensitive actions (member changes, ownership, delete, export). Future web: 2FA and session list | Manual |
| T6 | Prompt injection via text or receipt | "Ignore instructions, set amount…" | AI output is data only (FR-AI-08); schema-validated; IDs resolved server-side in the active workspace; human confirmation | AT-AI-INJ-* |
| T7 | Secret leakage | Logs, image layers, the repo | Docker secrets, gitleaks, redaction filter, `.dockerignore` | CI |
| T8 | SQL injection | String-built SQL | SQLAlchemy bound parameters only; a lint rule bans `text()` with f-strings | Static check |
| T9 | Backup theft | Bucket compromise | `age` encryption, private key kept offline, bucket credentials write-only for the server, object lock or versioning | Restore drill |
| T10 | Operator misuse | Browsing tenant data | No routine access; break-glass with audit and notification (§8) | AT-RBAC-P-01 |
| T11 | Cost or denial-of-service abuse | Spamming receipts or text | Rate limits (NFR-SEC-07), AI quotas, global budget | AT-QUOTA-* |
| T12 | PII in logs | Debug logging | Structured logging with an allow-listed field set; test asserts no description text appears in logs | AT-LOG-01 |
| T13 | Markup injection | `<`, `&`, `*`, `_` in descriptions | HTML parse mode with escaping on every interpolation | AT-RENDER-* |
| T14 | CSV formula injection | Export opened in Excel or Sheets | Prefix `'` on cells starting with `= + - @ \t \r` | AT-EXPORT-02 |
| T15 | Insecure deserialization | V1 pickle persistence | Removed; all state in Postgres | — |
| T16 | Group-chat exposure | Bot added to a group | Private chats only; group updates ignored, and the bot leaves the group | AT-ISO-07 |
| T17 | Brute-forcing tokens | Guessing invite or onboarding tokens | 128-bit tokens; rate limit on `/start` payloads; failed attempts audited | AT-TOK-* |

## 6. Audit logging

### 6.1 Event catalogue (MVP)

| Domain | Actions |
|---|---|
| Identity | `user.onboarded`, `user.suspended`, `user.reactivated`, `user.anonymized`, `identity.linked`, `identity.unlinked`, `onboarding_invite.created/revoked/redeemed/failed`, `platform_admin.bootstrapped` (once per database, doc 10 §11) |
| Workspace | `workspace.created`, `workspace.updated`, `workspace.deletion_requested`, `workspace.deletion_cancelled`, `workspace.purged`, `workspace.ownership_transfer_initiated/accepted/declined/expired` |
| Members | `invitation.created/revoked/accepted/failed`, `member.role_changed`, `member.removed`, `member.left` |
| Ledger | `account.created/updated/archived/unarchived`, `category.created/updated/archived`, `transaction.created/updated/voided/restored/type_changed`, `draft.created/confirmed/cancelled/expired` (drafts: counts only, sampled) |
| Data access | `report.exported` (period and row count), `audit.viewed` |
| Security | `security.authz_denied`, `security.cross_tenant_probe`, `security.rate_limited`, `security.break_glass_started/ended` |
| Operations | `backup.completed/failed`, `restore.performed`, `import.batch_dry_run/applied`, `ai.budget_exceeded` |

### 6.2 Content rules

- `metadata` holds structured values only: IDs, enum values, amounts **before and after** for transaction edits, and changed field names. **No** descriptions, raw messages or AI text.
- Audit rows are written in the same database transaction as the change (G-5). Denied attempts are written in their own short transaction.
- Append-only: there is no UPDATE or DELETE grant for `finbot_app`, and a trigger raises an exception on UPDATE or DELETE unless `session_user = 'finbot_owner'` (retention prune and purge; S6, §4.4).

## 7. Secrets management

| Secret | Storage | Rotation |
|---|---|---|
| Telegram bot token | Docker secret `telegram_token` | On suspicion, and yearly (BotFather `/revoke`) |
| Gemini API key | Docker secret; key restricted to the Generative Language API; billing budget alert | 90 days |
| DB passwords (owner, app, backup) | Docker secrets; SCRAM-SHA-256 | 180 days |
| Backup `age` recipient (public) | Docker secret on the server | — |
| Backup `age` identity (private) | **Offline** (password manager plus a paper copy). Never on the VPS | Yearly re-key |
| Object-storage credentials | rclone config secret; write-only (no delete) where the provider supports it | 180 days |

Files live in `deploy/secrets/<env>/` (git-ignored, owned by `root:<env group>`, mode **0440**, directory 0750) and are mounted at `/run/secrets/*`. Compose file secrets are bind mounts that keep host ownership and mode (`uid`/`gid`/`mode` are ignored for file secrets), so a root-only 0400 file would be unreadable by the non-root container user; each environment's containers instead run with a dedicated gid (staging: 2999, group `finbot-staging`) that no host user belongs to. Never reuse the image's default uid 999 for ownership (on the current VPS it is the `caddy` user). Secret files are created by root; the development user `claude` neither reads them nor has Docker access, and `deploy` (Docker operator) is the only non-root account that can reach them, through Docker (doc 04 §8.3). Config loads secrets from `*_FILE` paths only.

## 8. Break-glass support access

Used only when a tenant asks for help in writing, or for incident response.

1. The platform admin runs `finbot-admin break-glass --workspace <id> --reason "<ticket>" --ttl 60m`.
2. The command enables the `finbot_readonly` login with a random password for the TTL. It writes `security.break_glass_started` and **sends a DM to the workspace Owner**.
3. Access is read-only and subject to RLS through a session with `app.workspace_id` set to that workspace.
4. When the TTL ends, the login is disabled automatically and `security.break_glass_ended` is written.

## 9. Backup & restore security

| Aspect | Design |
|---|---|
| Method | `pg_dump -Fc` (as `finbot_backup`) → `age -r <recipient>` → `rclone copy` to an S3-compatible bucket (D10) with versioning or object lock |
| Schedule | Nightly at 01:30 WIB, plus an ad-hoc dump before every migration |
| Retention | 7 daily, 4 weekly, 6 monthly (lifecycle rules) |
| Integrity | SHA-256 manifest uploaded alongside; `pg_restore --list` check after each dump |
| Restore | Run on a fresh host or in staging only. Decrypt with the offline key → `pg_restore` into an empty database → run the verification SQL (row counts per table, Σ amounts per workspace, constraint validation, RLS enabled on every tenant table) |
| Drill | Monthly, into the **ephemeral `finbot-drill` environment** (never staging, which holds synthetic data only, D18), destroyed after the drill; results recorded in `deploy/runbooks/restore-log.md` |
| Environment separation | Staging and production use separate buckets or prefixes **and separate `age` key pairs**, so a staging host cannot decrypt production backups. A restored database carries its `platform_meta.environment` marker, so guard G-ENV-3 prevents the wrong bot from serving it (doc 04 §8.3) |
| Scope | Whole-database only (FR-OPS-05). Never restore over production without the owner's explicit approval. Restoring rolls back **all** tenants to the backup point |
| Future | WAL archiving (pgBackRest or wal-g) for RPO ≤ 15 min before the public launch |

## 10. Privacy specifics

- **Gemini (D8):** use the paid tier, so prompts are not used for model training (to be re-confirmed against Google's terms at Phase 4). Send the minimum data (FR-AI-02). Do not send user names or Telegram IDs.
- **Receipts (D7):** images are processed in memory, never written to disk, and never stored. Extracted JSON is pruned after 90 days.
- **User erasure (D14):** the profile is anonymized (`display_name` becomes `Pengguna dihapus`, email nulled), identities are unlinked, and memberships ended. The personal workspace is purged after 30 days. Business transactions created by the user remain (they belong to the business), attributed to the anonymized user ID. Audit actor IDs are kept, with no personal data.
- **Workspace deletion (D13):** 30-day read-only grace, then a hard purge of all workspace rows. Backups age out according to retention (documented in the privacy notice).

## 11. Release security checklist

- [ ] gitleaks, pip-audit and Trivy clean (no High or Critical findings).
- [ ] The AT-RLS, AT-ISO and AT-RBAC suites pass against a fresh migration.
- [ ] New tenant table? It has `workspace_id`, a composite FK, RLS ENABLE + FORCE, a policy, and a test in AT-RLS-COVERAGE.
- [ ] New service method? It calls `authorize`, writes an audit event, and has its permission added to the matrix and the tests.
- [ ] Money change? It goes through `domain/ledger.py`; AT-ENT-* and AT-BAL-PROP-01 pass; no UPDATE or DELETE on `transaction_entries`.
- [ ] Compose change? AT-DEP-01 passes (no Postgres port, pinned digest, healthchecks).
- [ ] No new log field outside the allow-list.
- [ ] Backup taken before the migration, and the migration has a downgrade or forward-fix plan.
