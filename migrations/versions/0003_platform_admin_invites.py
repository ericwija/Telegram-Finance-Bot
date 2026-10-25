"""Platform-admin bootstrap and onboarding-invite management (Step 4B).

Revision ID: 0003
Revises: 0002

Specification: FR-IDN-01 (a platform admin creates single-use invites with a 7-day expiry via
CLI), docs/08 §3.3 (``platform.onboarding_invite.create/revoke``; platform admins get no
workspace access), docs/09 §4.3-4.4 (definer functions, owner-only operations), §6.1 (audit),
S10 (owner credentials only in ``finbot-migrate``); owner decision 2026-10-25: the first
platform admin is created once, by the operator, as ``finbot_owner`` through
``finbot-migrate bootstrap-admin``.

* ``bootstrap_platform_admin`` is **not** ``SECURITY DEFINER`` and has **no** ``EXECUTE``
  grant: only its owner, ``finbot_owner``, can run it, and it refuses any other session user.
  It writes the permanent ``platform_meta`` key ``platform_admin_bootstrapped`` first (primary
  key, ``ON CONFLICT DO NOTHING``), so concurrent or repeated bootstraps fail and the marker
  survives later suspension or removal of the admin.
* The three invite functions are ``SECURITY DEFINER`` with ``EXECUTE`` for ``finbot_app`` only.
  Each checks first that ``app_user_id()`` is an **active platform admin**, so the bot can
  manage invites only on behalf of an admin principal it resolved server-side. They never take
  or return a token: the caller hashes a fresh token and passes only the digest, and listings
  never include ``token_hash``.

Same file rules as 0001 (docs/10 §3).
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

#: First statement of every invite function: the acting user must be an active platform admin.
_REQUIRE_ADMIN = """  IF NOT EXISTS (SELECT 1 FROM public.users u
                 WHERE u.id = public.app_user_id() AND u.is_platform_admin
                   AND u.status = 'active') THEN
    RAISE EXCEPTION USING ERRCODE = 'insufficient_privilege',
      MESSAGE = 'an active platform admin is required';
  END IF;"""

_CHANNEL_CHECK = """  IF channel IS NULL OR channel NOT IN ('cli', 'telegram') THEN
    RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
      MESSAGE = 'channel must be cli or telegram';
  END IF;"""

_FUNCTIONS = (
    # ---------------------------------------------------------------- owner-only bootstrap
    """CREATE FUNCTION public.bootstrap_platform_admin(tg_user_id bigint, display_name text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, public AS
$$
DECLARE
  v_name text := pg_catalog.btrim(bootstrap_platform_admin.display_name);
  v_user uuid;
  v_identity uuid;
  v_workspace uuid;
BEGIN
  IF session_user <> 'finbot_owner' THEN
    RAISE EXCEPTION USING ERRCODE = 'insufficient_privilege',
      MESSAGE = 'bootstrap_platform_admin runs only as finbot_owner';
  END IF;
  IF bootstrap_platform_admin.tg_user_id IS NULL OR bootstrap_platform_admin.tg_user_id <= 0 THEN
    RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
      MESSAGE = 'tg_user_id must be a positive Telegram user ID';
  END IF;
  IF v_name IS NULL OR pg_catalog.char_length(v_name) NOT BETWEEN 1 AND 64
     OR v_name ~ '[\\x01-\\x1f\\x7f-\\x9f]' THEN
    RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
      MESSAGE = 'display_name must be 1 to 64 printable characters';
  END IF;

  -- The marker is the one-time lock: a concurrent bootstrap waits here and then finds it.
  INSERT INTO public.platform_meta (key, value)
  VALUES ('platform_admin_bootstrapped', pg_catalog.now()::text)
  ON CONFLICT (key) DO NOTHING;
  IF NOT FOUND THEN
    RAISE EXCEPTION USING ERRCODE = 'object_not_in_prerequisite_state',
      MESSAGE = 'platform admin bootstrap was already completed';
  END IF;
  IF EXISTS (SELECT 1 FROM public.telegram_identities t
             WHERE t.telegram_user_id = bootstrap_platform_admin.tg_user_id
               AND t.unlinked_at IS NULL) THEN
    RAISE EXCEPTION USING ERRCODE = 'object_not_in_prerequisite_state',
      MESSAGE = 'this Telegram account is already linked to a user';
  END IF;

  INSERT INTO public.users (display_name, is_platform_admin) VALUES (v_name, true)
  RETURNING id INTO v_user;
  INSERT INTO public.telegram_identities (user_id, telegram_user_id)
  VALUES (v_user, bootstrap_platform_admin.tg_user_id) RETURNING id INTO v_identity;
  INSERT INTO public.workspaces (kind, name, personal_owner_id)
  VALUES ('personal', 'Pribadi', v_user) RETURNING id INTO v_workspace;
  INSERT INTO public.workspace_memberships (workspace_id, workspace_kind, user_id, role)
  VALUES (v_workspace, 'personal', v_user, 'owner');

  INSERT INTO public.audit_events
    (workspace_id, actor_user_id, actor_kind, channel, action, entity_type, entity_id)
  VALUES
    (NULL, NULL, 'system', 'cli', 'platform_admin.bootstrapped', 'user', v_user),
    (NULL, NULL, 'system', 'cli', 'user.onboarded', 'user', v_user),
    (NULL, NULL, 'system', 'cli', 'identity.linked', 'telegram_identity', v_identity),
    (v_workspace, NULL, 'system', 'cli', 'workspace.created', 'workspace', v_workspace);
  RETURN v_user;
END
$$""",
    "REVOKE ALL ON FUNCTION public.bootstrap_platform_admin(bigint, text) FROM PUBLIC",
    # ------------------------------------------------------------------- invite: create
    # Fixed 7-day expiry and single use (FR-IDN-01). Returns the new invite's ID.
    f"""CREATE FUNCTION public.create_onboarding_invite(
  token_hash bytea, note text, channel text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
DECLARE
  v_note text := nullif(pg_catalog.btrim(create_onboarding_invite.note), '');
  v_invite uuid;
BEGIN
{_REQUIRE_ADMIN}
{_CHANNEL_CHECK}
  IF create_onboarding_invite.token_hash IS NULL
     OR pg_catalog.octet_length(create_onboarding_invite.token_hash) <> 32 THEN
    RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
      MESSAGE = 'token_hash must be a 32-byte SHA-256 digest';
  END IF;
  IF v_note IS NOT NULL AND (pg_catalog.char_length(v_note) > 200
                             OR v_note ~ '[\\x01-\\x1f\\x7f-\\x9f]') THEN
    RAISE EXCEPTION USING ERRCODE = 'invalid_parameter_value',
      MESSAGE = 'note must be at most 200 printable characters';
  END IF;
  INSERT INTO public.onboarding_invites (token_hash, created_by, note, expires_at)
  VALUES (create_onboarding_invite.token_hash, public.app_user_id(), v_note,
          pg_catalog.now() + interval '7 days')
  RETURNING id INTO v_invite;
  INSERT INTO public.audit_events
    (actor_user_id, actor_kind, channel, action, entity_type, entity_id)
  VALUES (public.app_user_id(), 'platform_admin', create_onboarding_invite.channel,
          'onboarding_invite.created', 'onboarding_invite', v_invite);
  RETURN v_invite;
END
$$""",  # noqa: S608 - interpolates module constants only
    "REVOKE ALL ON FUNCTION public.create_onboarding_invite(bytea, text, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.create_onboarding_invite(bytea, text, text) TO finbot_app",
    # ------------------------------------------------------------------- invite: revoke
    # Returns 'revoked', or why nothing changed: 'not_found', 'used', 'already_revoked',
    # 'expired'.
    f"""CREATE FUNCTION public.revoke_onboarding_invite(invite_id uuid, channel text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
DECLARE
  v_invite public.onboarding_invites;
BEGIN
{_REQUIRE_ADMIN}
{_CHANNEL_CHECK}
  SELECT i.* INTO v_invite FROM public.onboarding_invites i
  WHERE i.id = revoke_onboarding_invite.invite_id
  FOR UPDATE;
  IF NOT FOUND THEN
    RETURN 'not_found';
  ELSIF v_invite.used_at IS NOT NULL THEN
    RETURN 'used';
  ELSIF v_invite.revoked_at IS NOT NULL THEN
    RETURN 'already_revoked';
  ELSIF v_invite.expires_at <= pg_catalog.now() THEN
    RETURN 'expired';
  END IF;
  UPDATE public.onboarding_invites i SET revoked_at = pg_catalog.now()
  WHERE i.id = v_invite.id;
  INSERT INTO public.audit_events
    (actor_user_id, actor_kind, channel, action, entity_type, entity_id)
  VALUES (public.app_user_id(), 'platform_admin', revoke_onboarding_invite.channel,
          'onboarding_invite.revoked', 'onboarding_invite', v_invite.id);
  RETURN 'revoked';
END
$$""",  # noqa: S608 - interpolates module constants only
    "REVOKE ALL ON FUNCTION public.revoke_onboarding_invite(uuid, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.revoke_onboarding_invite(uuid, text) TO finbot_app",
    # --------------------------------------------------------------------- invite: list
    # Metadata only (never token_hash). Newest first; inactive ones only when asked for.
    f"""CREATE FUNCTION public.list_onboarding_invites(include_inactive boolean)
RETURNS TABLE (invite_id uuid, status text, created_at timestamptz, expires_at timestamptz,
               note text, created_by uuid, used_by uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
BEGIN
{_REQUIRE_ADMIN}
  RETURN QUERY
  SELECT s.id, s.status, s.created_at, s.expires_at, s.note, s.created_by, s.used_by
  FROM (SELECT i.*, CASE WHEN i.used_at IS NOT NULL THEN 'used'
                         WHEN i.revoked_at IS NOT NULL THEN 'revoked'
                         WHEN i.expires_at <= pg_catalog.now() THEN 'expired'
                         ELSE 'active' END AS status
        FROM public.onboarding_invites i) s
  WHERE list_onboarding_invites.include_inactive OR s.status = 'active'
  ORDER BY s.created_at DESC, s.id DESC;
END
$$""",  # noqa: S608 - interpolates module constants only
    "REVOKE ALL ON FUNCTION public.list_onboarding_invites(boolean) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION public.list_onboarding_invites(boolean) TO finbot_app",
)


def upgrade() -> None:
    for statement in _FUNCTIONS:
        op.execute(statement)


def downgrade() -> None:
    # The platform_admin_bootstrapped marker is data, not schema: it stays (S9: downgrades run
    # only in dev/test/drill, and re-upgrading must not reopen a completed bootstrap).
    for statement in (
        "DROP FUNCTION public.list_onboarding_invites(boolean)",
        "DROP FUNCTION public.revoke_onboarding_invite(uuid, text)",
        "DROP FUNCTION public.create_onboarding_invite(bytea, text, text)",
        "DROP FUNCTION public.bootstrap_platform_admin(bigint, text)",
    ):
        op.execute(statement)
