# 08 — RBAC Matrix

| | |
|---|---|
| **Status** | Draft for owner approval (see D2, D3, D4, D19, D20, D21); reconciled with approved D1 (rev. 2, 2026-10-23) |
| **Date** | 2026-10-23 |
| **Related** | [02 FR](02-FUNCTIONAL-REQUIREMENTS.md) · [07 Tests](07-TEST-AND-ACCEPTANCE-PLAN.md) · [09 Security](09-TENANT-ISOLATION-AND-SECURITY.md) |

## 1. Model

- **Roles exist only within a workspace**, through `workspace_memberships.role`. There are no global roles that grant tenant data access.
- `is_platform_admin` grants **platform operations only**: onboarding invites, suspending users, backups, `/status`. It grants **no** permission inside any workspace (§7).
- The decision is `allow(principal, workspace, permission, resource?)`. It is evaluated **at execution time** with the *current* membership, role and workspace status (G-2).
- A single code table, `authz/policy.py::ROLE_PERMISSIONS`, implements §3 (Step 6; with `PERSONAL_PERMISSIONS` and the §5 overlay; D2 default (a)). `tests/authz/expected_matrix.py` encodes this table independently (AT-RBAC-SYNC-01). The tests in doc 07 assert the code table equals the expected matrix encoded in the test suite. Changing either side without the other fails CI.

Evaluation order (first failing step decides):

```
1. principal.user.status == 'active'                         else DENY (Forbidden: suspended)
2. resource (if any) belongs to ctx.workspace_id              else DENY as NOT FOUND (G-3)
3. active membership(user, workspace) exists                  else DENY as NOT FOUND
4. permission applicable to workspace.kind (§3, "Personal")   else DENY (Forbidden)
5. workspace.status overlay (§5)                              else DENY (Forbidden: read-only)
6. ROLE_PERMISSIONS[role] contains permission                 else DENY (Forbidden)
7. resource rules (§4: role-assignment limits, owner protection, draft ownership, deleted state)
                                                              else DENY (Forbidden)
→ ALLOW
```

## 2. Roles

| Role | Intent (from approved brief) |
|---|---|
| **Owner** | Full control, including ownership transfer and workspace deletion. Exactly one per workspace. |
| **Admin** | Manage transactions, accounts, categories and members, excluding owner-only actions. |
| **Editor** | View, create and edit transactions. Cannot delete transactions or manage members. |
| **Viewer** | Read-only access to balances, transactions and reports. |

In a **personal workspace** the only membership is its owner. Every collaboration permission is not applicable there.

## 3. Permission matrix

Legend: ✅ allowed · ❌ denied · ➖ not applicable (always denied) · ⚙️ allowed subject to the §4 rules.

### 3.1 Workspace-scoped permissions

| # | Permission | Owner | Admin | Editor | Viewer | Personal (owner) | Notes |
|---|---|:-:|:-:|:-:|:-:|:-:|---|
| 1 | `workspace.view` | ✅ | ✅ | ✅ | ✅ | ✅ | name, kind, timezone |
| 2 | `workspace.update` | ✅ | ✅ | ❌ | ❌ | ✅ | rename, timezone (D20) |
| 3 | `workspace.delete` | ✅ | ❌ | ❌ | ❌ | ➖ | owner-only; 30-day grace (D13) |
| 4 | `workspace.cancel_deletion` | ✅ | ❌ | ❌ | ❌ | ➖ | |
| 5 | `workspace.transfer_ownership` | ⚙️ | ❌ | ❌ | ❌ | ➖ | target must be an active member (R-6) |
| 6 | `member.view` | ✅ | ✅ | ✅ | ✅ | ➖ | display name and role only (D19) |
| 7 | `member.invite` | ⚙️ | ⚙️ | ❌ | ❌ | ➖ | R-1 assignable roles |
| 8 | `member.revoke_invitation` | ⚙️ | ⚙️ | ❌ | ❌ | ➖ | same limits as invite (R-1) |
| 9 | `member.change_role` | ⚙️ | ⚙️ | ❌ | ❌ | ➖ | R-1, R-2, R-3 |
| 10 | `member.remove` | ⚙️ | ⚙️ | ❌ | ❌ | ➖ | R-2, R-3 |
| 11 | `member.leave` | ❌ | ✅ | ✅ | ✅ | ➖ | the Owner must transfer first (R-4) |
| 12 | `account.view` | ✅ | ✅ | ✅ | ✅ | ✅ | includes balances |
| 13 | `account.manage` | ✅ | ✅ | ❌ | ❌ | ✅ | create, rename, opening balance, archive, aliases |
| 14 | `category.view` | ✅ | ✅ | ✅ | ✅ | ✅ | |
| 15 | `category.manage` | ✅ | ✅ | ❌ | ❌ | ✅ | create, rename, archive |
| 16 | `transaction.view` | ✅ | ✅ | ✅ | ✅ | ✅ | posted transactions, plus revision history |
| 17 | `transaction.view_deleted` | ✅ | ✅ | ❌ | ❌ | ✅ | "Dihapus" list (voided) |
| 18 | `transaction.create` | ✅ | ✅ | ✅ | ❌ | ✅ | includes drafts, confirmation, transfers |
| 19 | `transaction.update` | ✅ | ✅ | ⚙️ | ❌ | ✅ | R-7; Editor scope D2 |
| 20 | `transaction.delete` | ✅ | ✅ | ❌ | ❌ | ✅ | void (cancel); entries unchanged; whole transaction only (R-13) |
| 21 | `transaction.restore` | ✅ | ✅ | ❌ | ❌ | ✅ | |
| 22 | `receipt.scan` | ✅ | ✅ | ✅ | ❌ | ✅ | produces drafts; consumes AI quota |
| 23 | `report.view` | ✅ | ✅ | ✅ | ✅ | ✅ | cash flow, balances, comparisons |
| 24 | `report.export` | ✅ | ✅ | ❌ | ❌ | ✅ | CSV (D4) |
| 25 | `audit.view` | ✅ | ✅ | ❌ | ❌ | ✅ | workspace audit trail |
| 26 | `preference.default_account` | ✅ | ✅ | ✅ | ✅ | ✅ | the caller's own preference only |

### 3.2 User-scoped permissions (no workspace context)

| Permission | Who | Notes |
|---|---|---|
| `workspace.list_own` | any active user | returns only workspaces with an active membership |
| `workspace.switch_active` | any active user | target must have an active membership and not be purged |
| `workspace.create_business` | any active user | future: subject to plan quota |
| `invitation.accept` | any active user holding a valid token | R-5 |
| `ownership.accept` | the designated target user only | R-6 |
| `profile.manage` | the user themself | display name |

### 3.3 Platform permissions (`is_platform_admin`)

| Permission | Channel | Notes |
|---|---|---|
| `platform.onboarding_invite.create/revoke` | CLI (+ bot DM for the admin) | `finbot-admin invite create/list/revoke` (doc 10 §11). The first platform admin is created once by the operator with `finbot-migrate bootstrap-admin` (owner role), never through the bot |
| `platform.user.suspend/reactivate/anonymize` | CLI | audit-logged |
| `platform.status` | bot `/status` and CLI | no tenant data |
| `platform.backup.run/verify/restore` | CLI on the host | |
| `platform.import_v1` | CLI | migration only |
| `platform.break_glass` | CLI, two-step, time-boxed | doc 09 §8; never routine |

## 4. Resource rules

| ID | Rule |
|---|---|
| **R-1 Assignable roles** | **Owner** may invite or assign `admin`, `editor` or `viewer`. **Admin** may invite or assign `editor` or `viewer` only (D3). Nobody can invite or assign `owner`; ownership changes only through R-6. |
| **R-2 Target protection** | Admin may change or remove only members whose **current** role is `editor` or `viewer` (D3). Owner may change or remove any non-owner member. The Owner membership cannot be the target of `change_role` or `remove`. |
| **R-3 No self-modification** | `change_role` and `remove` cannot target the caller. Use `member.leave` instead. |
| **R-4 Owner cannot leave** | The Owner must complete a transfer (R-6) before leaving. Deleting the workspace is the alternative. |
| **R-5 Invitation acceptance** | The token's hash matches; the invitation is not expired, revoked or accepted; the workspace is `active`; the accepting user is active and **not already an active member**. Acceptance sets `accepted_*` and creates the membership in one transaction. Re-joining after removal is allowed with a new invitation. |
| **R-6 Ownership transfer** | Initiated only by the current Owner, targeting an **active member** (any role) other than themself. The target accepts within 24 h. At acceptance: the initiator is still Owner, the target is still an active member, and the workspace is active. Then, atomically, target → owner and initiator → admin. Initiating while another transfer is pending replaces the earlier one. |
| **R-7 Editor update scope** | With D2(a) (default), an Editor may update **any non-deleted** transaction in the workspace. With D2(b), only those where `created_by = caller`. Either way an Editor cannot change `deleted_*` fields. |
| **R-8 Voided transactions** | `transaction.update` is denied on a voided transaction. It must be restored first, which requires `transaction.restore`. |
| **R-9 Drafts** | Viewing, editing, confirming or cancelling a draft requires `draft.user_id = caller` **and** the caller currently holding `transaction.create` in the draft's workspace. Drafts of removed or downgraded members are expired by the service immediately (FR-MEM-06). |
| **R-10 Archived references** | New or updated transactions cannot reference archived accounts or categories. Only roles holding `account.manage` or `category.manage` can unarchive. |
| **R-11 Pending actions** | Only `pending_actions.user_id` can complete one, and the underlying permission is re-checked at completion. |
| **R-13 Entries are not separate resources** (D1) | No permission acts on an individual `transaction_entries` row. View, update, void and restore always apply to the **whole** transaction, so both legs of a transfer change together. Entries are only created by services, inside the same database transaction as their header. Both transfer accounts must be in the active workspace, and an account in another workspace is NotFound (G-3). |
| **R-14 Type change** (D21) | Changing a transaction's type requires `transaction.update` (not `transaction.delete`). It is implemented as: void the original with `void_reason='replaced'`, then create a replacement with `replaces_transaction_id`. The audit trail shows it as a type change, not a user deletion. |
| **R-12 Audit of denials** | Every Forbidden decision (not NotFound) emits `security.authz_denied` with permission, role and entity type. Every NotFound on a syntactically valid foreign reference emits `security.cross_tenant_probe`. |

## 5. Workspace status overlay

| Workspace status | Allowed | Everything else |
|---|---|---|
| `active` | as per matrix | — |
| `pending_deletion` | Every `*.view` permission, `report.view`, `member.view`, `member.leave`; Owner additionally `workspace.cancel_deletion` and `report.export` (to take data out) | **Forbidden** (read-only) |
| purged | nothing; the workspace no longer exists → NotFound | — |

User status overlay: `suspended` or `anonymized` → every permission is denied, including user-scoped ones (only `/start` returns a "suspended" notice).

## 6. Role change effects

| Event | Immediate effect |
|---|---|
| Downgrade (e.g. Admin → Editor) | The next action is evaluated with the new role. Already-rendered buttons fail on press (G-2). Pending actions that need the old permission are invalidated. |
| Downgrade to Viewer | The member's pending drafts in that workspace are expired. |
| Removal or leave | Membership `status=removed/left`. Drafts and pending actions are expired. If this was their active workspace, it falls back to personal. The member is notified. |
| Ownership transfer | Both parties are notified, and all members see the new owner in `/anggota`. |
| User suspended | All sessions refused. Drafts expired. Pending ownership transfers *to* that user are cancelled. If they own business workspaces, those workspaces continue read-write for the other members; owner-only actions are unavailable until reactivation or a platform-assisted transfer (open item O-RBAC-1). |

## 7. Platform admin and tenant data

The platform admin has **no** implicit workspace access. To see a workspace, the platform admin needs a membership like anyone else, or must use break-glass (doc 09 §8). This keeps NFR-PRV-03 enforceable and is tested (AT-RBAC-P-01).

## 8. Authorization test catalogue (summary; full plan in doc 07)

| Test ID | What it proves |
|---|---|
| AT-RBAC-M-* | **Matrix tests**, generated: for each of the 26 workspace permissions × {owner, admin, editor, viewer, non-member} in a business workspace, plus {owner, other user} in a personal workspace, the outcome equals §3 (≈ 26 × 7 = 182 cases). Each runs through the **service layer** against real Postgres. |
| AT-RBAC-R1-* | Admin cannot invite or assign `admin`; nobody can assign `owner`; Owner can assign each non-owner role. |
| AT-RBAC-R2-* | Admin cannot change or remove an Admin or the Owner; Owner can change or remove an Admin. |
| AT-RBAC-R3-* | Self change-role and self-remove are denied. |
| AT-RBAC-R4-01 | The Owner's `leave` is denied. |
| AT-RBAC-R5-* | Invitation: reused, expired, revoked, wrong-workspace and already-member tokens are refused; a valid token creates the membership with exactly the invited role. |
| AT-RBAC-R6-* | Transfer: a non-owner initiator, self as target, a non-member target, acceptance by a third party, acceptance after 24 h, and acceptance after the initiator lost ownership are all refused. The happy path leaves exactly one owner and the previous owner as admin. |
| AT-RBAC-R7-* | Editor updates someone else's transaction: allowed under D2(a), denied under D2(b) (both parametrized). |
| AT-RBAC-R8-01 | Update of a voided transaction is denied for all roles. |
| AT-RBAC-R13-* | Voiding or restoring a transfer affects both legs. No service operation targets a single entry. A transfer whose destination account is in another workspace → NotFound. |
| AT-RBAC-R14-* | Editor changes the type (allowed, and the original is shown as replaced, not deleted); Viewer changes the type (Forbidden). |
| AT-RBAC-R9-* | Confirming another user's draft is denied; a draft confirmed after its creator was downgraded to Viewer is denied; a double confirm creates one transaction. |
| AT-RBAC-S-* | `pending_deletion` makes every write Forbidden for all roles; the Owner can cancel and export. |
| AT-RBAC-U-* | A suspended user is denied everywhere. |
| AT-RBAC-G2-* | Stale-button tests: render with role A, change the role, press → evaluated against the new role. |
| AT-RBAC-P-01 | A platform admin without membership gets NotFound on workspace resources. |
| AT-RBAC-STRUCT-01 | Every public service method calls `authorize()` (introspection test). |
| AT-RBAC-SYNC-01 | `ROLE_PERMISSIONS` in code equals the expected matrix in the tests. |

## 9. Open items

- **O-RBAC-1**: Should a platform-assisted ownership transfer be allowed when the Owner is suspended or unreachable? It needs a documented verification procedure. Proposed: yes, via break-glass, with a two-person rule once there is a second operator.
- **O-RBAC-2**: Should there be a per-workspace setting that lets Editors use `report.export`? Proposed: defer.
