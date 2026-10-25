"""docs/08 §3.1, transcribed independently of ``authz/policy.py`` (AT-RBAC-SYNC-01).

Columns: owner, admin, editor, viewer, personal owner. ``Y`` allowed (allowed or allowed subject
to resource rules, which are tested separately), ``N`` denied, ``-`` not applicable (always
denied). Change this file, ``authz/policy.py`` and docs/08 together.
"""

EXPECTED_MATRIX: dict[str, str] = {
    "workspace.view": "YYYYY",
    "workspace.update": "YYNNY",
    "workspace.delete": "YNNN-",
    "workspace.cancel_deletion": "YNNN-",
    "workspace.transfer_ownership": "YNNN-",
    "member.view": "YYYY-",
    "member.invite": "YYNN-",
    "member.revoke_invitation": "YYNN-",
    "member.change_role": "YYNN-",
    "member.remove": "YYNN-",
    "member.leave": "NYYY-",
    "account.view": "YYYYY",
    "account.manage": "YYNNY",
    "category.view": "YYYYY",
    "category.manage": "YYNNY",
    "transaction.view": "YYYYY",
    "transaction.view_deleted": "YYNNY",
    "transaction.create": "YYYNY",
    "transaction.update": "YYYNY",
    "transaction.delete": "YYNNY",
    "transaction.restore": "YYNNY",
    "receipt.scan": "YYYNY",
    "report.view": "YYYYY",
    "report.export": "YYNNY",
    "audit.view": "YYNNY",
    "preference.default_account": "YYYYY",
}
COLUMNS = ("owner", "admin", "editor", "viewer", "personal")

#: docs/08 §5, transcribed independently: what a role may still do while its workspace is
#: ``pending_deletion`` (the role must also hold the permission in EXPECTED_MATRIX). Every
#: ``*.view`` permission (``audit.view`` included), ``transaction.view_deleted`` and
#: ``member.leave``; the Owner additionally ``workspace.cancel_deletion`` and ``report.export``.
PENDING_DELETION_ALLOWED: frozenset[str] = frozenset(
    {
        "workspace.view",
        "member.view",
        "account.view",
        "category.view",
        "transaction.view",
        "transaction.view_deleted",
        "report.view",
        "audit.view",
        "member.leave",
    }
)
PENDING_DELETION_OWNER_EXTRA: frozenset[str] = frozenset(
    {"workspace.cancel_deletion", "report.export"}
)
