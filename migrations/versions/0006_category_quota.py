"""Category quota: at most 200 categories per workspace and kind, archived ones included.

Revision ID: 0006
Revises: 0005

Specification: docs/10 §14 (Step 6C, F06). The service refuses the 201st category of a kind
under the workspace's category-name lock; this trigger enforces the same limit in the database,
so category listings never have more pages than the bot can link to (``CATEGORY_MAX_PAGES``)
and the fallback ``Lainnya`` is always reachable.

* ``ledger_category_quota()`` is ``SECURITY DEFINER`` (it counts every row of the workspace,
  whatever the caller's RLS context) with a pinned ``search_path``; EXECUTE is granted to
  nobody. It takes the same transaction-level advisory lock as the application
  (``finbot.category-names:<workspace>``), so concurrent inserts of one workspace are serialized
  and the count is exact. Its error message contains no name.
* Only INSERT is checked: archiving and reactivating do not change the count, and rows that
  already exist are never touched (no workspace can exceed the quota today: the bot was never
  deployed with ``0004``).

Rules for this file (docs/10 §3): one SQL statement per ``op.execute``; no ``%`` and no ``:name``
in the SQL; the downgrade removes everything the upgrade created (dev/test/drill only).
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

QUOTA = 200  # domain/finance.py::CATEGORY_QUOTA_PER_KIND (kept equal by a unit test)

_UPGRADE = (
    f"""CREATE FUNCTION public.ledger_category_quota() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
BEGIN
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('finbot.category-names' || chr(58) || NEW.workspace_id::text, 0));
  IF (SELECT count(*) FROM public.categories c
      WHERE c.workspace_id = NEW.workspace_id AND c.kind = NEW.kind) >= {QUOTA} THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation',
      MESSAGE = 'category quota of {QUOTA} per kind reached';
  END IF;
  RETURN NEW;
END
$$""",  # noqa: S608 - a constant, no input
    "REVOKE ALL ON FUNCTION public.ledger_category_quota() FROM PUBLIC",
    "CREATE TRIGGER trg_categories_quota BEFORE INSERT ON public.categories "
    "FOR EACH ROW EXECUTE FUNCTION public.ledger_category_quota()",
)


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TRIGGER trg_categories_quota ON public.categories",
        "DROP FUNCTION public.ledger_category_quota()",
    ):
        op.execute(statement)
