"""Phase 1 baseline: privileges, platform_meta, users, workspaces, memberships, audit_events.

Revision ID: 0001
Revises: (none)

Specification: docs/05 §3, §4, §6, §8 and the P1 row of §11; docs/09 §4 and §6.2; decisions
D5 (RLS from Phase 1), S5 (active-membership check), S6 (session_user), S7 (column UPDATE
grants), S11 (public schema). Roles are not created here (deploy/postgres/initdb/00-roles.sh).

Rules for this file (docs/10 §3):
* one SQL statement per ``op.execute`` (asyncpg prepares each statement);
* no ``%`` and no ``:name`` anywhere in the SQL, so offline (``--sql``) output runs under psql;
* every table gets RLS ENABLE + FORCE and its policies in this same revision (D5);
* the downgrade removes everything the upgrade created, in reverse order (dev/test/drill only).
"""

from alembic import op

from finbot.infra.db.migration_runtime import environment_literal

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Shorthands for the RLS helpers, called through scalar subqueries (docs/09 §4.1).
_WS = "(SELECT public.app_workspace_id())"
_USER = "(SELECT public.app_user_id())"


def _member(column: str) -> str:
    return f"(SELECT public.app_is_active_member({column}))"


# --------------------------------------------------------------------- database and schema ACL

_DATABASE_ACL = (
    # CONNECT and TEMP only where needed. The database name differs per environment.
    """DO $finbot$
BEGIN
  EXECUTE 'REVOKE CONNECT, TEMPORARY ON DATABASE '
    || pg_catalog.quote_ident(pg_catalog.current_database()) || ' FROM PUBLIC';
  EXECUTE 'GRANT CONNECT ON DATABASE '
    || pg_catalog.quote_ident(pg_catalog.current_database()) || ' TO finbot_app, finbot_backup';
END
$finbot$""",
    "REVOKE ALL ON SCHEMA public FROM PUBLIC",
    "GRANT USAGE ON SCHEMA public TO finbot_app, finbot_backup",
    # Functions are not executable by PUBLIC unless granted; the backup role reads every table.
    "ALTER DEFAULT PRIVILEGES FOR ROLE finbot_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
    "ALTER DEFAULT PRIVILEGES FOR ROLE finbot_owner IN SCHEMA public "
    "GRANT SELECT ON TABLES TO finbot_backup",
    "ALTER DEFAULT PRIVILEGES FOR ROLE finbot_owner IN SCHEMA public "
    "GRANT SELECT ON SEQUENCES TO finbot_backup",
    # Created by Alembic before this revision, so default privileges do not cover it.
    "GRANT SELECT ON public.alembic_version TO finbot_backup",
)

_EXTENSIONS = ("CREATE EXTENSION citext WITH SCHEMA public",)

# ------------------------------------------------------------------------------ platform_meta

_PLATFORM_META = (
    """CREATE TABLE public.platform_meta (
  key    text PRIMARY KEY,
  value  text NOT NULL
)""",
    "GRANT SELECT ON public.platform_meta TO finbot_app",
)


def _environment_marker() -> str:
    # environment_literal() is a validated literal (identifier characters only).
    return (
        "INSERT INTO public.platform_meta (key, value) "  # noqa: S608
        f"VALUES ('environment', {environment_literal()})"
    )


# ---------------------------------------------------------------------------------- helpers

_CONTEXT_HELPERS = (
    """CREATE FUNCTION public.app_user_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(pg_catalog.current_setting('app.user_id', true), '')::uuid $$""",
    """CREATE FUNCTION public.app_workspace_id() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(pg_catalog.current_setting('app.workspace_id', true), '')::uuid $$""",
    "REVOKE ALL ON FUNCTION public.app_user_id() FROM PUBLIC",
    "REVOKE ALL ON FUNCTION public.app_workspace_id() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.app_user_id() TO finbot_app",
    "GRANT EXECUTE ON FUNCTION public.app_workspace_id() TO finbot_app",
)

# ----------------------------------------------------------------------------------- tables

_TABLES = (
    """CREATE TABLE public.users (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  display_name       text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 64),
  status             text NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'suspended', 'anonymized')),
  is_platform_admin  boolean NOT NULL DEFAULT false,
  locale             text NOT NULL DEFAULT 'id-ID',
  timezone           text NOT NULL DEFAULT 'Asia/Jakarta',
  email              public.citext UNIQUE,
  email_verified_at  timestamptz,
  password_hash      text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
)""",
    """CREATE TABLE public.workspaces (
  id                     uuid PRIMARY KEY DEFAULT uuidv7(),
  kind                   text NOT NULL CHECK (kind IN ('personal', 'business')),
  name                   text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
  personal_owner_id      uuid REFERENCES public.users (id),
  timezone               text NOT NULL DEFAULT 'Asia/Jakarta',
  currency               text NOT NULL DEFAULT 'IDR' CHECK (currency = 'IDR'),
  status                 text NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'pending_deletion')),
  deletion_requested_at  timestamptz,
  deletion_requested_by  uuid REFERENCES public.users (id),
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  version                int NOT NULL DEFAULT 1,
  CONSTRAINT workspaces_id_kind_key UNIQUE (id, kind),
  CONSTRAINT workspaces_personal_has_owner
    CHECK ((kind = 'personal') = (personal_owner_id IS NOT NULL)),
  CONSTRAINT workspaces_personal_always_active CHECK (kind = 'business' OR status = 'active'),
  CONSTRAINT workspaces_deletion_consistent
    CHECK ((status = 'pending_deletion') = (deletion_requested_at IS NOT NULL))
)""",
    "CREATE UNIQUE INDEX uq_one_personal_ws_per_user ON public.workspaces (personal_owner_id) "
    "WHERE kind = 'personal'",
    """CREATE TABLE public.workspace_memberships (
  workspace_id    uuid NOT NULL,
  workspace_kind  text NOT NULL,
  user_id         uuid NOT NULL REFERENCES public.users (id),
  role            text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'viewer')),
  status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed', 'left')),
  invited_by      uuid REFERENCES public.users (id),
  joined_at       timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  version         int NOT NULL DEFAULT 1,
  PRIMARY KEY (workspace_id, user_id),
  CONSTRAINT workspace_memberships_workspace_fkey FOREIGN KEY (workspace_id, workspace_kind)
    REFERENCES public.workspaces (id, kind) ON DELETE CASCADE,
  CONSTRAINT workspace_memberships_personal_owner_only
    CHECK (workspace_kind = 'business' OR role = 'owner'),
  CONSTRAINT workspace_memberships_ended_consistent CHECK ((status = 'active') = (ended_at IS NULL))
)""",
    "CREATE UNIQUE INDEX uq_one_owner ON public.workspace_memberships (workspace_id) "
    "WHERE role = 'owner' AND status = 'active'",
    "CREATE UNIQUE INDEX uq_personal_single_member ON public.workspace_memberships (workspace_id) "
    "WHERE workspace_kind = 'personal'",
    "CREATE INDEX ix_membership_user ON public.workspace_memberships (user_id) "
    "WHERE status = 'active'",
    """CREATE TABLE public.audit_events (
  id             uuid PRIMARY KEY DEFAULT uuidv7(),
  occurred_at    timestamptz NOT NULL DEFAULT now(),
  workspace_id   uuid,
  actor_user_id  uuid,
  actor_kind     text NOT NULL CHECK (actor_kind IN ('user', 'system', 'platform_admin')),
  channel        text NOT NULL CHECK (channel IN ('telegram', 'web', 'cli', 'job')),
  action         text NOT NULL,
  entity_type    text,
  entity_id      uuid,
  request_id     uuid,
  outcome        text NOT NULL DEFAULT 'success' CHECK (outcome IN ('success', 'denied', 'failed')),
  metadata       jsonb NOT NULL DEFAULT '{}'
)""",
    "CREATE INDEX ix_audit_ws_time ON public.audit_events (workspace_id, occurred_at DESC)",
)

# ---------------------------------------------------- membership helper (S5) and triggers

_FUNCTIONS = (
    # S5: SECURITY DEFINER owned by finbot_owner (BYPASSRLS), so evaluating it never re-enters a
    # policy; boolean only; compares against app_user_id(), never a caller-supplied user.
    """CREATE FUNCTION public.app_is_active_member(ws uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS
$$ SELECT EXISTS (
     SELECT 1 FROM public.workspace_memberships m
     WHERE m.workspace_id = ws AND m.user_id = public.app_user_id() AND m.status = 'active') $$""",
    "REVOKE ALL ON FUNCTION public.app_is_active_member(uuid) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.app_is_active_member(uuid) TO finbot_app",
    # Definer so the check sees the workspace row whatever the caller's RLS context is.
    """CREATE FUNCTION public.personal_membership_is_owner() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
BEGIN
  IF NEW.workspace_kind = 'personal' AND NEW.user_id IS DISTINCT FROM (
       SELECT w.personal_owner_id FROM public.workspaces w WHERE w.id = NEW.workspace_id) THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation',
      MESSAGE = 'a personal workspace membership must belong to the workspace owner';
  END IF;
  RETURN NEW;
END
$$""",
    "REVOKE ALL ON FUNCTION public.personal_membership_is_owner() FROM PUBLIC",
    # Deferred to commit, so an ownership transfer inside one transaction is safe (docs/05 §4).
    """CREATE FUNCTION public.business_has_owner() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
DECLARE
  ids uuid[];
  ws uuid;
BEGIN
  IF TG_TABLE_NAME = 'workspaces' THEN
    ids := ARRAY[NEW.id];
  ELSIF TG_OP = 'INSERT' THEN
    ids := ARRAY[NEW.workspace_id];
  ELSIF TG_OP = 'DELETE' THEN
    ids := ARRAY[OLD.workspace_id];
  ELSE
    ids := ARRAY[OLD.workspace_id, NEW.workspace_id];
  END IF;
  FOREACH ws IN ARRAY ids LOOP
    IF EXISTS (SELECT 1 FROM public.workspaces w WHERE w.id = ws AND w.kind = 'business')
       AND NOT EXISTS (SELECT 1 FROM public.workspace_memberships m
                       WHERE m.workspace_id = ws AND m.role = 'owner' AND m.status = 'active') THEN
      RAISE EXCEPTION USING ERRCODE = 'integrity_constraint_violation',
        MESSAGE = 'business workspace ' || ws::text || ' must have exactly one active owner';
    END IF;
  END LOOP;
  RETURN NULL;
END
$$""",
    "REVOKE ALL ON FUNCTION public.business_has_owner() FROM PUBLIC",
    # S6: session_user, not current_user (a definer function owned by finbot_owner would
    # otherwise bypass this when called by finbot_app).
    """CREATE FUNCTION public.audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS
$$
BEGIN
  IF session_user <> 'finbot_owner' THEN
    RAISE EXCEPTION USING ERRCODE = 'insufficient_privilege',
      MESSAGE = 'audit_events is append-only: ' || TG_OP || ' refused for ' || session_user;
  END IF;
  IF TG_LEVEL = 'STATEMENT' THEN
    RETURN NULL;
  ELSIF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END
$$""",
    "REVOKE ALL ON FUNCTION public.audit_events_append_only() FROM PUBLIC",
)

_TRIGGERS = (
    "CREATE TRIGGER trg_personal_membership_is_owner "
    "BEFORE INSERT OR UPDATE ON public.workspace_memberships "
    "FOR EACH ROW EXECUTE FUNCTION public.personal_membership_is_owner()",
    "CREATE CONSTRAINT TRIGGER trg_business_has_owner "
    "AFTER INSERT OR UPDATE OR DELETE ON public.workspace_memberships "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.business_has_owner()",
    # Also at workspace creation, so a business workspace cannot be committed without an owner.
    "CREATE CONSTRAINT TRIGGER trg_business_has_owner "
    "AFTER INSERT ON public.workspaces "
    "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.business_has_owner()",
    "CREATE TRIGGER trg_audit_events_append_only "
    "BEFORE UPDATE OR DELETE ON public.audit_events "
    "FOR EACH ROW EXECUTE FUNCTION public.audit_events_append_only()",
    "CREATE TRIGGER trg_audit_events_no_truncate "
    "BEFORE TRUNCATE ON public.audit_events "
    "FOR EACH STATEMENT EXECUTE FUNCTION public.audit_events_append_only()",
)

# ---------------------------------------------------------------------------- RLS (D5, S5)
# The policies below interpolate only the module constants _WS, _USER and _member(<column>).

_RLS_TABLES = ("users", "workspaces", "workspace_memberships", "audit_events")

_POLICIES = (
    # Own row, or users who are (or were) members of the context workspace, when the caller is
    # an active member of it. Updates only to the caller's own row (columns limited by S7).
    f"""CREATE POLICY tenant_isolation ON public.users
  USING (id = {_USER}
         OR ({_member(_WS)}
             AND id IN (SELECT m.user_id FROM public.workspace_memberships m
                        WHERE m.workspace_id = {_WS})))
  WITH CHECK (id = {_USER})""",  # noqa: S608
    # Every workspace the caller is an active member of; writes only in the context workspace.
    f"""CREATE POLICY tenant_isolation ON public.workspaces
  USING ({_member("id")})
  WITH CHECK (id = {_WS} AND {_member("id")})""",
    # The context workspace's members (caller active there), plus the caller's own memberships.
    f"""CREATE POLICY tenant_isolation ON public.workspace_memberships
  USING ((workspace_id = {_WS} AND {_member("workspace_id")}) OR user_id = {_USER})
  WITH CHECK (workspace_id = {_WS} AND {_member("workspace_id")})""",
    f"""CREATE POLICY tenant_isolation_select ON public.audit_events FOR SELECT
  USING (workspace_id = {_WS} AND {_member("workspace_id")})""",
    f"""CREATE POLICY tenant_isolation_insert ON public.audit_events FOR INSERT
  WITH CHECK (workspace_id IS NULL OR (workspace_id = {_WS} AND {_member("workspace_id")}))""",
)

# ------------------------------------------------------------------------- app grants (S7)

_APP_GRANTS = (
    # users: rows are created by SECURITY DEFINER onboarding (Phase 2), never directly.
    "GRANT SELECT ON public.users TO finbot_app",
    "GRANT UPDATE (display_name, locale, timezone, updated_at) ON public.users TO finbot_app",
    "GRANT SELECT, INSERT ON public.workspaces TO finbot_app",
    "GRANT UPDATE (name, timezone, currency, status, deletion_requested_at, "
    "deletion_requested_by, updated_at, version) ON public.workspaces TO finbot_app",
    "GRANT SELECT, INSERT ON public.workspace_memberships TO finbot_app",
    "GRANT UPDATE (role, status, ended_at, version) ON public.workspace_memberships TO finbot_app",
    # Append-only: INSERT and SELECT, never UPDATE or DELETE (docs/09 §6.2).
    "GRANT SELECT, INSERT ON public.audit_events TO finbot_app",
)


def upgrade() -> None:
    statements = [
        *_DATABASE_ACL,
        *_EXTENSIONS,
        *_PLATFORM_META,
        _environment_marker(),
        *_CONTEXT_HELPERS,
        *_TABLES,
        *_FUNCTIONS,
        *_TRIGGERS,
    ]
    for table in _RLS_TABLES:
        statements.append(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    statements += [*_POLICIES, *_APP_GRANTS]
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        # Policies first: the users policy depends on workspace_memberships (its subquery).
        "DROP POLICY tenant_isolation_insert ON public.audit_events",
        "DROP POLICY tenant_isolation_select ON public.audit_events",
        "DROP POLICY tenant_isolation ON public.workspace_memberships",
        "DROP POLICY tenant_isolation ON public.workspaces",
        "DROP POLICY tenant_isolation ON public.users",
        # Then tables: their triggers, indexes and grants go with them.
        "DROP TABLE public.audit_events",
        "DROP TABLE public.workspace_memberships",
        "DROP TABLE public.workspaces",
        "DROP TABLE public.users",
        "DROP TABLE public.platform_meta",
        "DROP FUNCTION public.audit_events_append_only()",
        "DROP FUNCTION public.business_has_owner()",
        "DROP FUNCTION public.personal_membership_is_owner()",
        "DROP FUNCTION public.app_is_active_member(uuid)",
        "DROP FUNCTION public.app_workspace_id()",
        "DROP FUNCTION public.app_user_id()",
        "DROP EXTENSION citext",
        "REVOKE SELECT ON public.alembic_version FROM finbot_backup",
        "ALTER DEFAULT PRIVILEGES FOR ROLE finbot_owner IN SCHEMA public "
        "REVOKE SELECT ON SEQUENCES FROM finbot_backup",
        "ALTER DEFAULT PRIVILEGES FOR ROLE finbot_owner IN SCHEMA public "
        "REVOKE SELECT ON TABLES FROM finbot_backup",
        "ALTER DEFAULT PRIVILEGES FOR ROLE finbot_owner GRANT EXECUTE ON FUNCTIONS TO PUBLIC",
        "REVOKE USAGE ON SCHEMA public FROM finbot_app, finbot_backup",
        "GRANT USAGE ON SCHEMA public TO PUBLIC",
        """DO $finbot$
BEGIN
  EXECUTE 'REVOKE CONNECT ON DATABASE '
    || pg_catalog.quote_ident(pg_catalog.current_database()) || ' FROM finbot_app, finbot_backup';
  EXECUTE 'GRANT CONNECT, TEMPORARY ON DATABASE '
    || pg_catalog.quote_ident(pg_catalog.current_database()) || ' TO PUBLIC';
END
$finbot$""",
    )
    for statement in statements:
        op.execute(statement)
