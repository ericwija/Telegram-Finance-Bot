"""AT-RBAC-SYNC-01 and the evaluation order of ``authorize()`` (docs/08 §1, §5, §7)."""

import uuid

import pytest

from finbot.authz.context import WorkspaceContext
from finbot.authz.permissions import Permission
from finbot.authz.policy import ForbiddenError, allowed, authorize
from finbot.domain.identity import UserStatus
from tests.authz.expected_matrix import (
    COLUMNS,
    EXPECTED_MATRIX,
    PENDING_DELETION_ALLOWED,
    PENDING_DELETION_OWNER_EXTRA,
)


def ctx(
    role: str = "owner",
    *,
    kind: str = "business",
    status: str = "active",
    user_status: UserStatus = UserStatus.ACTIVE,
) -> WorkspaceContext:
    return WorkspaceContext(
        user_id=uuid.uuid4(),
        user_status=user_status,
        workspace_id=uuid.uuid4(),
        workspace_kind=kind,
        workspace_name="W",
        workspace_status=status,
        timezone="Asia/Jakarta",
        currency="IDR",
        role=role,
        default_account_id=None,
    )


def _context_for(column: str) -> WorkspaceContext:
    return ctx("owner", kind="personal") if column == "personal" else ctx(column)


def test_AT_RBAC_SYNC_01_matrix_covers_every_permission() -> None:
    assert set(EXPECTED_MATRIX) == {p.value for p in Permission}
    assert len(Permission) == 26


@pytest.mark.parametrize("permission", sorted(EXPECTED_MATRIX))
@pytest.mark.parametrize("column", COLUMNS)
def test_AT_RBAC_SYNC_01_policy_equals_expected_matrix(permission: str, column: str) -> None:
    expected = EXPECTED_MATRIX[permission][COLUMNS.index(column)] == "Y"
    assert allowed(_context_for(column), Permission(permission)) is expected


def test_suspended_user_is_denied_everything() -> None:
    suspended = ctx(user_status=UserStatus.SUSPENDED)
    for permission in Permission:
        with pytest.raises(ForbiddenError) as caught:
            authorize(suspended, permission)
        assert caught.value.reason == "user_inactive"


def test_pending_deletion_is_read_only() -> None:
    owner = ctx("owner", status="pending_deletion")
    admin = ctx("admin", status="pending_deletion")
    for permission in (
        Permission.TRANSACTION_VIEW,
        Permission.REPORT_VIEW,
        Permission.CATEGORY_VIEW,
    ):
        authorize(owner, permission)
    for permission in (
        Permission.TRANSACTION_CREATE,
        Permission.TRANSACTION_UPDATE,
        Permission.CATEGORY_MANAGE,
        Permission.WORKSPACE_UPDATE,
    ):
        with pytest.raises(ForbiddenError, match="workspace_read_only"):
            authorize(owner, permission)
    authorize(owner, Permission.WORKSPACE_CANCEL_DELETION)
    authorize(owner, Permission.REPORT_EXPORT)
    with pytest.raises(ForbiddenError):
        authorize(admin, Permission.REPORT_EXPORT)


def test_unknown_role_and_personal_non_owner_are_denied() -> None:
    assert not allowed(ctx("guest"), Permission.TRANSACTION_VIEW)
    assert not allowed(ctx("viewer", kind="personal"), Permission.TRANSACTION_VIEW)


def _expected(column: str, permission: str, workspace_status: str, user: UserStatus) -> bool:
    """The independent expectation for one cell of role x permission x status."""
    if user is not UserStatus.ACTIVE:
        return False
    allowed_by_role = EXPECTED_MATRIX[permission][COLUMNS.index(column)] == "Y"
    if workspace_status == "pending_deletion":
        extra = PENDING_DELETION_OWNER_EXTRA if column in ("owner", "personal") else frozenset()
        return allowed_by_role and permission in PENDING_DELETION_ALLOWED | extra
    return allowed_by_role


@pytest.mark.parametrize("user_status", list(UserStatus))
@pytest.mark.parametrize("workspace_status", ["active", "pending_deletion"])
@pytest.mark.parametrize("column", COLUMNS)
def test_F11_role_permission_status_matrix(
    column: str, workspace_status: str, user_status: UserStatus
) -> None:
    kind = "personal" if column == "personal" else "business"
    role = "owner" if column == "personal" else column
    context = ctx(role, kind=kind, status=workspace_status, user_status=user_status)
    for permission in EXPECTED_MATRIX:
        expected = _expected(column, permission, workspace_status, user_status)
        assert allowed(context, Permission(permission)) is expected, (permission, expected)


def test_F11_audit_view_survives_pending_deletion_for_roles_that_hold_it() -> None:
    for role in ("owner", "admin"):
        authorize(ctx(role, status="pending_deletion"), Permission.AUDIT_VIEW)
    for role in ("editor", "viewer"):
        assert not allowed(ctx(role, status="pending_deletion"), Permission.AUDIT_VIEW)
    assert not allowed(
        ctx("owner", status="pending_deletion"), Permission.PREFERENCE_DEFAULT_ACCOUNT
    )  # a write, even for the Owner
