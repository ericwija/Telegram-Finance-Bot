"""Role policy (docs/08 §3.1, §5) and ``authorize()`` with the documented evaluation order.

``ROLE_PERMISSIONS`` is the single code table of the workspace matrix. ``tests/authz/
expected_matrix.py`` encodes the same matrix independently (AT-RBAC-SYNC-01): change this
table, docs/08 and that file together.

Evaluation order (docs/08 §1; the first failing step decides):

1. the user is active, else Forbidden;
2. the resource belongs to the context workspace: callers look resources up *inside* the
   context, so a foreign resource is NotFound before ``authorize`` runs (G-3);
3. an active membership exists, else NotFound (a context only exists for an active member);
4. the permission applies to the workspace kind (personal workspaces have no collaboration
   permissions), else Forbidden;
5. the workspace status overlay (``pending_deletion`` is read-only), else Forbidden;
6. the role holds the permission, else Forbidden;
7. resource rules (R-7..R-10) are checked by the services.

The platform admin flag is never consulted: it grants nothing inside a workspace (docs/08 §7).
"""

from typing import Final

from finbot.authz.context import WorkspaceContext
from finbot.authz.permissions import Permission
from finbot.domain.identity import UserStatus

P = Permission

_VIEW: Final = frozenset(
    {
        P.WORKSPACE_VIEW,
        P.MEMBER_VIEW,
        P.ACCOUNT_VIEW,
        P.CATEGORY_VIEW,
        P.TRANSACTION_VIEW,
        P.REPORT_VIEW,
        P.PREFERENCE_DEFAULT_ACCOUNT,
    }
)

ROLE_PERMISSIONS: Final[dict[str, frozenset[Permission]]] = {
    "owner": frozenset(
        {
            *_VIEW,
            P.WORKSPACE_UPDATE,
            P.WORKSPACE_DELETE,
            P.WORKSPACE_CANCEL_DELETION,
            P.WORKSPACE_TRANSFER_OWNERSHIP,
            P.MEMBER_INVITE,
            P.MEMBER_REVOKE_INVITATION,
            P.MEMBER_CHANGE_ROLE,
            P.MEMBER_REMOVE,
            P.ACCOUNT_MANAGE,
            P.CATEGORY_MANAGE,
            P.TRANSACTION_VIEW_DELETED,
            P.TRANSACTION_CREATE,
            P.TRANSACTION_UPDATE,
            P.TRANSACTION_DELETE,
            P.TRANSACTION_RESTORE,
            P.RECEIPT_SCAN,
            P.REPORT_EXPORT,
            P.AUDIT_VIEW,
        }
    ),
    "admin": frozenset(
        {
            *_VIEW,
            P.WORKSPACE_UPDATE,
            P.MEMBER_INVITE,
            P.MEMBER_REVOKE_INVITATION,
            P.MEMBER_CHANGE_ROLE,
            P.MEMBER_REMOVE,
            P.MEMBER_LEAVE,
            P.ACCOUNT_MANAGE,
            P.CATEGORY_MANAGE,
            P.TRANSACTION_VIEW_DELETED,
            P.TRANSACTION_CREATE,
            P.TRANSACTION_UPDATE,
            P.TRANSACTION_DELETE,
            P.TRANSACTION_RESTORE,
            P.RECEIPT_SCAN,
            P.REPORT_EXPORT,
            P.AUDIT_VIEW,
        }
    ),
    # D2 (open; documented default (a)): an Editor may update any non-voided transaction.
    "editor": frozenset(
        {
            *_VIEW,
            P.MEMBER_LEAVE,
            P.TRANSACTION_CREATE,
            P.TRANSACTION_UPDATE,
            P.RECEIPT_SCAN,
        }
    ),
    "viewer": frozenset({*_VIEW, P.MEMBER_LEAVE}),
}

#: Permissions that exist in a personal workspace (its owner holds all of them; the others are
#: "not applicable" in docs/08).
PERSONAL_PERMISSIONS: Final = frozenset(
    {
        *_VIEW - {P.MEMBER_VIEW},
        P.WORKSPACE_UPDATE,
        P.ACCOUNT_MANAGE,
        P.CATEGORY_MANAGE,
        P.TRANSACTION_VIEW_DELETED,
        P.TRANSACTION_CREATE,
        P.TRANSACTION_UPDATE,
        P.TRANSACTION_DELETE,
        P.TRANSACTION_RESTORE,
        P.RECEIPT_SCAN,
        P.REPORT_EXPORT,
        P.AUDIT_VIEW,
    }
)

#: docs/08 §5: what remains allowed while a workspace is pending deletion: every ``*.view``
#: permission (``audit.view`` and ``transaction.view_deleted`` included) and ``member.leave``.
#: The role must still hold the permission (step 6). Setting a default account is a write.
_READ_ONLY_ALLOWED: Final = frozenset(
    {
        *(p for p in Permission if p.value.endswith(".view")),
        P.TRANSACTION_VIEW_DELETED,
        P.MEMBER_LEAVE,
    }
)
_READ_ONLY_OWNER_EXTRA: Final = frozenset({P.WORKSPACE_CANCEL_DELETION, P.REPORT_EXPORT})


class ForbiddenError(PermissionError):
    """Denied inside the caller's own workspace (G-4). ``reason`` is for logs and audit only."""

    def __init__(self, permission: Permission, reason: str, role: str | None = None) -> None:
        super().__init__(f"{permission.value}: {reason}")
        self.permission = permission
        self.reason = reason
        self.role = role


def allowed(ctx: WorkspaceContext, permission: Permission) -> bool:
    try:
        authorize(ctx, permission)
    except ForbiddenError:
        return False
    return True


def authorize(ctx: WorkspaceContext, permission: Permission) -> None:
    """Raise :class:`ForbiddenError` unless ``ctx`` may use ``permission`` (steps 1, 4 to 6)."""
    if ctx.user_status is not UserStatus.ACTIVE:
        raise ForbiddenError(permission, "user_inactive", ctx.role)
    if ctx.workspace_kind == "personal" and (
        permission not in PERSONAL_PERMISSIONS or ctx.role != "owner"
    ):
        raise ForbiddenError(permission, "not_applicable_personal", ctx.role)
    if ctx.workspace_status == "pending_deletion":
        extra = _READ_ONLY_OWNER_EXTRA if ctx.role == "owner" else frozenset()
        if permission not in _READ_ONLY_ALLOWED | extra:
            raise ForbiddenError(permission, "workspace_read_only", ctx.role)
    if ctx.workspace_kind == "personal":
        return
    if permission not in ROLE_PERMISSIONS.get(ctx.role, frozenset()):
        raise ForbiddenError(permission, "role", ctx.role)
