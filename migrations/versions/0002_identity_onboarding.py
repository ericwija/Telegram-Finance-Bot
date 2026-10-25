"""Phase 2 identity: telegram_identities, onboarding_invites and their SECURITY DEFINER functions.

Revision ID: 0002
Revises: 0001

Specification: docs/05 §3 (identity tables) and the P2 row of §11; docs/09 §4.3 (no direct app
grants on these tables; access only through ``resolve_principal`` and
``redeem_onboarding_invite``), §4.4, §6.1 (audit catalogue); FR-IDN-01..04, FR-IDN-07; D6
(invite-only onboarding, documented default (a)); T17 (hashed single-use tokens).

The definer functions are the narrow bootstrap that Phase 1 left open (docs/10 §9 item 1, 4):
``finbot_app`` cannot insert users, and S5 forbids it from creating a workspace together with
its first membership. ``redeem_onboarding_invite`` does exactly one thing, for exactly one new
user: it creates the user, the Telegram identity, the *personal* workspace and its owner
membership, consumes the invite and audits it, all in the caller's transaction. It accepts no
workspace ID, role or user ID, so it cannot add anyone to an existing workspace.

Same file rules as 0001 (docs/10 §3): one statement per ``op.execute``; no percent sign and no
colon-prefixed names in the SQL (psql-safe offline output); RLS in the same revision (D5); the
downgrade removes everything the upgrade created.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# ----------------------------------------------------------------------------------- tables

_TABLES = (
    """CREATE TABLE public.telegram_identities (
  id                 uuid PRIMARY KEY DEFAULT uuidv7(),
  user_id            uuid NOT NULL REFERENCES public.users (id),
  telegram_user_id   bigint NOT NULL CHECK (telegram_user_id > 0),
  telegram_username  text,
  linked_at          timestamptz NOT NULL DEFAULT now(),
  unlinked_at        timestamptz
)""",
    # One active binding per Telegram account and per user (FR-IDN-03).
    "CREATE UNIQUE INDEX uq_tg_identity_active_tg ON public.telegram_identities "
    "(telegram_user_id) WHERE unlinked_at IS NULL",
    "CREATE UNIQUE INDEX uq_tg_identity_active_user ON public.telegram_identities "
    "(user_id) WHERE unlinked_at IS NULL",
    """CREATE TABLE public.onboarding_invites (
  id          uuid PRIMARY KEY DEFAULT uuidv7(),
  token_hash  bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
  created_by  uuid NOT NULL REFERENCES public.users (id),
  note        text,
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz,
  used_by     uuid REFERENCES public.users (id),
  revoked_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT onboarding_invites_used_consistent CHECK ((used_at IS NULL) = (used_by IS NULL))
)""",
)

# ------------------------------------------------------- RLS: no direct access for the app
# docs/09 §4.3 grants finbot_app nothing on these tables. RLS ENABLE + FORCE with a deny-all
# policy keeps them closed even if a grant is ever added by mistake; the definer functions
# below run as finbot_owner (BYPASSRLS).

_RLS_TABLES = ("telegram_identities", "onboarding_invites")

# --------------------------------------------------------------- SECURITY DEFINER functions

_FUNCTIONS = (
    # Middleware, before any context exists (docs/04 §5.1). Only the declared columns; only an
    # active (not unlinked) binding.
    """CREATE FUNCTION public.resolve_principal(tg_user_id bigint)
RETURNS TABLE (user_id uuid, status text, is_platform_admin boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS
$$ SELECT u.id, u.status, u.is_platform_admin
   FROM public.telegram_identities t JOIN public.users u ON u.id = t.user_id
   WHERE t.telegram_user_id = resolve_principal.tg_user_id AND t.unlinked_at IS NULL $$""",
    "REVOKE ALL ON FUNCTION public.resolve_principal(bigint) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.resolve_principal(bigint) TO finbot_app",
    # FR-IDN-01/04. Returns the user's ID, or NULL when the invite is refused (unknown, used,
    # revoked or expired; audited as onboarding_invite.failed). Idempotent per Telegram account:
    # an already linked account gets its existing user back and the invite is left untouched.
    # Concurrency: a transaction-scoped advisory lock per Telegram account serialises redeems for
    # the same account, and the invite row is locked FOR UPDATE, so two accounts racing for one
    # invite get exactly one winner. Invalid arguments raise (they are caller bugs), which rolls
    # back the whole call. The personal workspace name 'Pribadi' is the only literal default.
    """CREATE FUNCTION public.redeem_onboarding_invite(
  token_hash bytea, tg_user_id bigint, display_name text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
DECLARE
  v_name text := pg_catalog.btrim(redeem_onboarding_invite.display_name);
  v_invite public.onboarding_invites;
  v_user uuid;
  v_identity uuid;
  v_workspace uuid;
  v_reason text;
BEGIN
  IF redeem_onboarding_invite.token_hash IS NULL
     OR pg_catalog.octet_length(redeem_onboarding_invite.token_hash) <> 32 THEN
    RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
      MESSAGE = 'token_hash must be a 32-byte SHA-256 digest';
  END IF;
  IF redeem_onboarding_invite.tg_user_id IS NULL OR redeem_onboarding_invite.tg_user_id <= 0 THEN
    RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
      MESSAGE = 'tg_user_id must be a positive Telegram user ID';
  END IF;
  IF v_name IS NULL OR pg_catalog.char_length(v_name) NOT BETWEEN 1 AND 64
     OR v_name ~ '[\\x01-\\x1f\\x7f-\\x9f]' THEN
    RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
      MESSAGE = 'display_name must be 1 to 64 printable characters';
  END IF;

  PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(
    'finbot.onboarding.' || redeem_onboarding_invite.tg_user_id::text, 0));

  SELECT t.user_id INTO v_user FROM public.telegram_identities t
  WHERE t.telegram_user_id = redeem_onboarding_invite.tg_user_id AND t.unlinked_at IS NULL;
  IF FOUND THEN
    RETURN v_user;
  END IF;

  SELECT i.* INTO v_invite FROM public.onboarding_invites i
  WHERE i.token_hash = redeem_onboarding_invite.token_hash
  FOR UPDATE;
  IF NOT FOUND THEN
    v_reason := 'unknown';
  ELSIF v_invite.used_at IS NOT NULL THEN
    v_reason := 'used';
  ELSIF v_invite.revoked_at IS NOT NULL THEN
    v_reason := 'revoked';
  ELSIF v_invite.expires_at <= pg_catalog.now() THEN
    v_reason := 'expired';
  END IF;
  IF v_reason IS NOT NULL THEN
    INSERT INTO public.audit_events
      (actor_kind, channel, action, entity_type, entity_id, outcome, metadata)
    VALUES ('system', 'telegram', 'onboarding_invite.failed', 'onboarding_invite',
            v_invite.id, 'failed', pg_catalog.jsonb_build_object('reason', v_reason));
    RETURN NULL;
  END IF;

  INSERT INTO public.users (display_name) VALUES (v_name) RETURNING id INTO v_user;
  INSERT INTO public.telegram_identities (user_id, telegram_user_id)
  VALUES (v_user, redeem_onboarding_invite.tg_user_id) RETURNING id INTO v_identity;
  INSERT INTO public.workspaces (kind, name, personal_owner_id)
  VALUES ('personal', 'Pribadi', v_user) RETURNING id INTO v_workspace;
  INSERT INTO public.workspace_memberships (workspace_id, workspace_kind, user_id, role)
  VALUES (v_workspace, 'personal', v_user, 'owner');
  UPDATE public.onboarding_invites i SET used_at = pg_catalog.now(), used_by = v_user
  WHERE i.id = v_invite.id;

  INSERT INTO public.audit_events
    (workspace_id, actor_user_id, actor_kind, channel, action, entity_type, entity_id)
  VALUES
    (NULL, v_user, 'user', 'telegram', 'onboarding_invite.redeemed', 'onboarding_invite',
     v_invite.id),
    (NULL, v_user, 'user', 'telegram', 'user.onboarded', 'user', v_user),
    (NULL, v_user, 'user', 'telegram', 'identity.linked', 'telegram_identity', v_identity),
    (v_workspace, v_user, 'user', 'telegram', 'workspace.created', 'workspace', v_workspace);
  RETURN v_user;
END
$$""",
    "REVOKE ALL ON FUNCTION public.redeem_onboarding_invite(bytea, bigint, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.redeem_onboarding_invite(bytea, bigint, text) TO finbot_app",
)


def upgrade() -> None:
    statements = [*_TABLES]
    for table in _RLS_TABLES:
        statements.append(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        statements.append(
            f"CREATE POLICY no_direct_access ON public.{table} USING (false) WITH CHECK (false)"
        )
    statements += _FUNCTIONS
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        "DROP FUNCTION public.redeem_onboarding_invite(bytea, bigint, text)",
        "DROP FUNCTION public.resolve_principal(bigint)",
        # Policies, indexes and grants go with their tables.
        "DROP TABLE public.onboarding_invites",
        "DROP TABLE public.telegram_identities",
    )
    for statement in statements:
        op.execute(statement)
