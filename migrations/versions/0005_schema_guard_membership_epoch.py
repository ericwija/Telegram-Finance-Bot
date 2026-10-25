"""Schema guard and membership epochs: the bot can learn its schema revision, and every role or
status change of a membership bumps its version.

Revision ID: 0005
Revises: 0004

Specification: docs/10 §13 (Step 6B: F05 schema compatibility, R03 removal and rejoin).

* ``public.app_schema_revision()`` returns the revision id from ``alembic_version`` (NULL unless
  there is exactly one row). It is ``SECURITY DEFINER`` with a pinned ``search_path`` and
  EXECUTE for ``finbot_app`` only, so the app role still has no privilege on the migration
  bookkeeping table itself (AT-RLS-05). The bot refuses to start unless the revision is one it
  supports (``finbot.infra.db.guards``), so a release never runs against a schema it was not
  written for. Migrations stay operator-run.
* ``trg_membership_version`` makes ``workspace_memberships.version`` change with every role or
  status change, whoever performs it. Conversations (drafts, category prompts) record the version
  they were started under and are refused after a downgrade, a removal, or a removal and rejoin.

Additive only: two functions and one trigger; no row is rewritten. Rules for this file
(docs/10 §3): one SQL statement per ``op.execute``; no ``%`` and no ``:name`` in the SQL; the
downgrade removes everything the upgrade created (dev/test/drill only).
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_UPGRADE = (
    # Definer: finbot_app has no privilege on alembic_version; this returns one value only.
    """CREATE FUNCTION public.app_schema_revision() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
  SELECT CASE WHEN count(*) = 1 THEN min(version_num) END FROM public.alembic_version
$$""",
    "REVOKE ALL ON FUNCTION public.app_schema_revision() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.app_schema_revision() TO finbot_app",
    # Not SECURITY DEFINER: it only adjusts the row being updated.
    """CREATE FUNCTION public.membership_version_bump() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$$
BEGIN
  IF NEW.role IS DISTINCT FROM OLD.role OR NEW.status IS DISTINCT FROM OLD.status THEN
    NEW.version := GREATEST(NEW.version, OLD.version + 1);
  END IF;
  RETURN NEW;
END
$$""",
    "REVOKE ALL ON FUNCTION public.membership_version_bump() FROM PUBLIC",
    "CREATE TRIGGER trg_membership_version BEFORE UPDATE OF role, status "
    "ON public.workspace_memberships FOR EACH ROW "
    "EXECUTE FUNCTION public.membership_version_bump()",
)


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        "DROP TRIGGER trg_membership_version ON public.workspace_memberships",
        "DROP FUNCTION public.membership_version_bump()",
        "DROP FUNCTION public.app_schema_revision()",
    )
    for statement in statements:
        op.execute(statement)
