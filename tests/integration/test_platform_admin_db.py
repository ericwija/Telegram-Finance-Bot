"""Platform-admin bootstrap and invite management on a real PostgreSQL 18.6 (Step 4B).

Bootstrap runs as finbot_owner (as ``finbot-migrate bootstrap-admin`` does); everything else
runs as finbot_app through the services, like ``finbot-admin`` and the bot. Covers success,
one-time bootstrap (also after the admin is suspended or demoted), concurrent bootstraps,
rollback, unauthorized callers, expired/revoked/used invites, revoke-vs-redeem races, and
attempts to reach the bootstrap or the invite tables with the app's privileges.
Runs only with FINBOT_TEST_PG_BACKEND set.
"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from finbot.domain.identity import InviteStatus, PlatformAdminRequiredError, RevokeOutcome
from finbot.infra.db.bootstrap import BootstrapRefusedError, bootstrap_platform_admin
from finbot.infra.db.identity import PgIdentityGateway
from finbot.infra.db.platform import PgPlatformGateway
from finbot.services.onboarding import StartOutcome, StartRequest
from finbot.services.platform_invites import PlatformInviteService
from tests.support.identity import counts, service
from tests.support.phase1 import as_app, rows, scalar

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ADMIN_TG = 900_000_001
OTHER_TG = 900_000_002
NEW_TG = 900_000_003


async def _bootstrap(owner_engine: Any, tg: int = ADMIN_TG, name: str = "Operator") -> UUID:
    return await bootstrap_platform_admin(
        owner_engine, env="test", telegram_user_id=tg, display_name=name
    )


def _invites(app_engine: Any) -> PlatformInviteService:
    return PlatformInviteService(PgIdentityGateway(app_engine), PgPlatformGateway(app_engine))


@pytest_asyncio.fixture
async def admin(migrated_database: str, owner_engine: Any) -> UUID:
    return await _bootstrap(owner_engine)


async def _audit_actions(owner_engine: Any) -> list[str]:
    async with owner_engine.connect() as conn:
        return [r[0] for r in await rows(conn, "SELECT action FROM audit_events ORDER BY 1")]


async def _onboard(app_engine: Any, tg: int, payload: str) -> StartOutcome:
    return (await service(app_engine).start(StartRequest(tg, "Tamu", payload))).outcome


# ------------------------------------------------------------------------------ bootstrap


async def test_bootstrap_creates_one_admin_with_identity_workspace_marker_and_audit(
    migrated_database: str, owner_engine: Any, app_engine: Any
) -> None:
    user_id = await _bootstrap(owner_engine, name="  Operator  ")

    principal = await PgIdentityGateway(app_engine).resolve_principal(ADMIN_TG)
    assert principal is not None
    assert principal.user_id == user_id
    assert principal.is_platform_admin
    async with owner_engine.connect() as conn:
        assert await scalar(
            conn, "SELECT display_name FROM users WHERE id = :u", {"u": user_id}
        ) == ("Operator")
        assert (
            await scalar(
                conn, "SELECT count(*) FROM platform_meta WHERE key = 'platform_admin_bootstrapped'"
            )
            == 1
        )
        memberships = await rows(
            conn,
            "SELECT workspace_kind, role FROM workspace_memberships WHERE user_id = :u",
            {"u": user_id},
        )
    assert [tuple(m) for m in memberships] == [("personal", "owner")]  # no business access
    assert await _audit_actions(owner_engine) == [
        "identity.linked",
        "platform_admin.bootstrapped",
        "user.onboarded",
        "workspace.created",
    ]


@pytest.mark.parametrize("change", ["none", "suspended", "demoted", "unlinked"])
async def test_bootstrap_is_one_time_even_after_the_admin_changes(
    admin: UUID, owner_engine: Any, change: str
) -> None:
    statements = {
        "suspended": "UPDATE users SET status = 'suspended' WHERE id = :u",
        "demoted": "UPDATE users SET is_platform_admin = false WHERE id = :u",
        "unlinked": "UPDATE telegram_identities SET unlinked_at = now() WHERE user_id = :u",
    }
    if change in statements:
        async with owner_engine.begin() as conn:
            await conn.execute(text(statements[change]), {"u": admin})
    before = await counts(owner_engine)
    with pytest.raises(BootstrapRefusedError, match="already completed"):
        await _bootstrap(owner_engine, tg=OTHER_TG)
    with pytest.raises(BootstrapRefusedError, match="already completed"):
        await _bootstrap(owner_engine, tg=ADMIN_TG)
    assert await counts(owner_engine) == before


async def test_concurrent_bootstraps_produce_exactly_one_admin(
    migrated_database: str, owner_engine: Any
) -> None:
    # finbot_owner has CONNECTION LIMIT 5 (00-roles.sh): race 4 sessions within it.
    racers = 4
    results = await asyncio.gather(
        *(_bootstrap(owner_engine, tg=ADMIN_TG + n) for n in range(racers)),
        return_exceptions=True,
    )
    winners = [r for r in results if isinstance(r, UUID)]
    refused = [r for r in results if isinstance(r, BootstrapRefusedError)]
    assert len(winners) == 1, results
    assert len(refused) == racers - 1, results
    async with owner_engine.connect() as conn:
        assert await scalar(conn, "SELECT count(*) FROM users WHERE is_platform_admin") == 1
        assert await scalar(conn, "SELECT count(*) FROM telegram_identities") == 1


async def test_failed_bootstrap_leaves_no_marker_and_can_be_retried(
    migrated_database: str, owner_engine: Any
) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE FUNCTION test_fail() RETURNS trigger LANGUAGE plpgsql AS "
                "$$ BEGIN RAISE EXCEPTION 'injected failure'; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER test_fail BEFORE INSERT ON workspace_memberships "
                "FOR EACH ROW EXECUTE FUNCTION test_fail()"
            )
        )
    before = await counts(owner_engine)
    with pytest.raises(DBAPIError, match="injected failure"):
        await _bootstrap(owner_engine)
    assert await counts(owner_engine) == before
    async with owner_engine.begin() as conn:
        assert await scalar(conn, "SELECT count(*) FROM platform_meta") == 1  # environment only
        await conn.execute(text("DROP TRIGGER test_fail ON workspace_memberships"))
    assert isinstance(await _bootstrap(owner_engine), UUID)


@pytest.mark.parametrize(
    ("tg", "name", "message"),
    [
        (0, "Op", "positive"),
        (-1, "Op", "positive"),
        (ADMIN_TG, " ", "printable"),
        (ADMIN_TG, "x" * 65, "printable"),
    ],
)
async def test_bootstrap_rejects_invalid_input_without_writing(
    migrated_database: str, owner_engine: Any, tg: int, name: str, message: str
) -> None:
    before = await counts(owner_engine)
    with pytest.raises(BootstrapRefusedError, match=message):
        await _bootstrap(owner_engine, tg=tg, name=name)
    assert await counts(owner_engine) == before
    assert isinstance(await _bootstrap(owner_engine), UUID)  # nothing was consumed


async def test_bootstrap_refuses_a_database_of_another_environment(
    migrated_database: str, owner_engine: Any
) -> None:
    with pytest.raises(BootstrapRefusedError, match="G-ENV-3"):
        await bootstrap_platform_admin(
            owner_engine, env="staging", telegram_user_id=ADMIN_TG, display_name="Op"
        )
    assert (await counts(owner_engine))["users"] == 0


async def test_bootstrap_refuses_an_already_linked_account(
    migrated_database: str, owner_engine: Any
) -> None:
    async with owner_engine.begin() as conn:
        user = await scalar(conn, "INSERT INTO users (display_name) VALUES ('X') RETURNING id")
        await conn.execute(
            text("INSERT INTO telegram_identities (user_id, telegram_user_id) VALUES (:u, :tg)"),
            {"u": user, "tg": ADMIN_TG},
        )
    with pytest.raises(BootstrapRefusedError, match="already linked"):
        await _bootstrap(owner_engine)
    async with owner_engine.connect() as conn:
        assert await scalar(conn, "SELECT count(*) FROM users WHERE is_platform_admin") == 0
        assert await scalar(conn, "SELECT count(*) FROM platform_meta") == 1


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT bootstrap_platform_admin(1, 'Mallory')",
        "INSERT INTO platform_meta (key, value) VALUES ('platform_admin_bootstrapped', 'x')",
        "DELETE FROM platform_meta WHERE key = 'platform_admin_bootstrapped'",
        "UPDATE users SET is_platform_admin = true",
    ],
)
async def test_the_app_cannot_bootstrap_or_forge_an_admin(
    admin: UUID, app_engine: Any, owner_engine: Any, statement: str
) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with as_app(app_engine, user=str(admin)) as conn:
            await conn.execute(text(statement))
    async with owner_engine.connect() as conn:
        assert await scalar(conn, "SELECT count(*) FROM users WHERE is_platform_admin") == 1


async def test_only_the_owner_can_execute_the_bootstrap(
    migrated_database: str, owner_engine: Any
) -> None:
    async with owner_engine.connect() as conn:
        definition = (
            await rows(
                conn,
                "SELECT prosecdef, pg_get_userbyid(proowner) AS owner, proconfig, "
                "ARRAY(SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
                "                  ELSE pg_get_userbyid(a.grantee) END "
                "      FROM aclexplode(coalesce(proacl, acldefault('f', proowner))) a "
                "      WHERE a.privilege_type = 'EXECUTE') AS executors "
                "FROM pg_proc WHERE oid = 'public.bootstrap_platform_admin(bigint, text)'"
                "::regprocedure",
            )
        )[0]
    assert definition.prosecdef is False  # runs with the caller's rights: the owner's only
    assert definition.owner == "finbot_owner"
    assert definition.proconfig == ["search_path=pg_catalog, public"]
    assert definition.executors == ["finbot_owner"]


# ------------------------------------------------------------------------ invite: create


async def test_admin_creates_a_single_use_seven_day_invite_that_onboards_one_user(
    admin: UUID, owner_engine: Any, app_engine: Any
) -> None:
    created = await _invites(app_engine).create(ADMIN_TG, note="  for   Budi ", channel="cli")
    async with owner_engine.connect() as conn:
        invite = (
            await rows(
                conn,
                "SELECT token_hash, created_by, note, used_at, revoked_at, "
                "expires_at - created_at AS validity FROM onboarding_invites WHERE id = :i",
                {"i": created.invite_id},
            )
        )[0]
        audit = (
            await rows(
                conn,
                "SELECT actor_user_id, actor_kind, channel FROM audit_events "
                "WHERE action = 'onboarding_invite.created'",
            )
        )[0]
    assert invite.token_hash == created.token.digest()
    assert created.token.value.encode() not in invite.token_hash
    assert invite.created_by == admin
    assert invite.note == "for Budi"
    assert str(invite.validity) == "7 days, 0:00:00"
    assert tuple(audit) == (admin, "platform_admin", "cli")

    payload = f"onb_{created.token.value}"
    assert await _onboard(app_engine, NEW_TG, payload) is StartOutcome.ONBOARDED
    assert await _onboard(app_engine, NEW_TG + 1, payload) is StartOutcome.INVITE_REFUSED
    listed = await _invites(app_engine).list(ADMIN_TG, include_inactive=True)
    assert [(i.id, i.status) for i in listed] == [(created.invite_id, InviteStatus.USED)]


@pytest.mark.parametrize("actor", ["unknown", "regular", "suspended_admin", "demoted_admin"])
async def test_non_admins_cannot_create_list_or_revoke(
    admin: UUID, owner_engine: Any, app_engine: Any, actor: str
) -> None:
    invite = await _invites(app_engine).create(ADMIN_TG, note=None, channel="cli")
    actor_tg = ADMIN_TG
    if actor == "unknown":
        actor_tg = 123
    elif actor == "regular":
        regular = await _invites(app_engine).create(ADMIN_TG, note=None, channel="cli")
        assert await _onboard(app_engine, OTHER_TG, f"onb_{regular.token.value}") is (
            StartOutcome.ONBOARDED
        )
        actor_tg = OTHER_TG
    else:
        change = (
            "status = 'suspended'" if actor == "suspended_admin" else "is_platform_admin = false"
        )
        async with owner_engine.begin() as conn:
            await conn.execute(text(f"UPDATE users SET {change} WHERE id = :u"), {"u": admin})  # noqa: S608
    before = await counts(owner_engine)
    invites = _invites(app_engine)
    with pytest.raises(PlatformAdminRequiredError):
        await invites.create(actor_tg, note=None, channel="cli")
    with pytest.raises(PlatformAdminRequiredError):
        await invites.list(actor_tg, include_inactive=True)
    with pytest.raises(PlatformAdminRequiredError):
        await invites.revoke(actor_tg, invite.invite_id, channel="cli")
    assert await counts(owner_engine) == before


async def test_database_enforces_admin_even_if_the_service_is_bypassed(
    admin: UUID, owner_engine: Any, app_engine: Any
) -> None:
    """Calling the functions directly with a non-admin (or no) app.user_id fails closed."""
    async with owner_engine.begin() as conn:
        regular = await scalar(conn, "INSERT INTO users (display_name) VALUES ('R') RETURNING id")
    gateway = PgPlatformGateway(app_engine)
    for actor in (regular, uuid4()):
        with pytest.raises(PlatformAdminRequiredError):
            await gateway.create_invite(actor, b"\x01" * 32, None, "cli")
        with pytest.raises(PlatformAdminRequiredError):
            await gateway.list_invites(actor, include_inactive=True)
        with pytest.raises(PlatformAdminRequiredError):
            await gateway.revoke_invite(actor, uuid4(), "cli")
    for sql in (
        "SELECT create_onboarding_invite(sha256('x'), NULL, 'cli')",
        "SELECT * FROM list_onboarding_invites(true)",
        "SELECT revoke_onboarding_invite(gen_random_uuid(), 'cli')",
    ):
        with pytest.raises(DBAPIError, match="active platform admin is required"):
            async with as_app(app_engine) as conn:  # no app.user_id at all
                await conn.execute(text(sql))


@pytest.mark.parametrize(
    ("digest", "note", "channel", "message"),
    [
        (b"short", None, "cli", "32-byte"),
        (b"\x02" * 32, "x" * 201, "cli", "200 printable"),
        (b"\x02" * 32, "bad\x07note", "cli", "200 printable"),
        (b"\x02" * 32, None, "web", "channel"),
    ],
)
async def test_invite_creation_validates_its_input(
    admin: UUID,
    owner_engine: Any,
    app_engine: Any,
    digest: bytes,
    note: str | None,
    channel: str,
    message: str,
) -> None:
    before = await counts(owner_engine)
    with pytest.raises(DBAPIError, match=message):
        await PgPlatformGateway(app_engine).create_invite(admin, digest, note, channel)
    assert await counts(owner_engine) == before


# -------------------------------------------------------------------- invite: list, revoke


async def test_list_shows_status_and_metadata_but_never_tokens(
    admin: UUID, owner_engine: Any, app_engine: Any
) -> None:
    invites = _invites(app_engine)
    active = await invites.create(ADMIN_TG, note="active", channel="cli")
    used = await invites.create(ADMIN_TG, note="used", channel="cli")
    revoked = await invites.create(ADMIN_TG, note="revoked", channel="cli")
    expired = await invites.create(ADMIN_TG, note="expired", channel="cli")
    await _onboard(app_engine, NEW_TG, f"onb_{used.token.value}")
    assert await invites.revoke(ADMIN_TG, revoked.invite_id, channel="cli") is RevokeOutcome.REVOKED
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE onboarding_invites SET expires_at = now() - interval '1 second' "
                "WHERE id = :i"
            ),
            {"i": expired.invite_id},
        )

    everything = {i.id: i.status for i in await invites.list(ADMIN_TG, include_inactive=True)}
    assert everything == {
        active.invite_id: InviteStatus.ACTIVE,
        used.invite_id: InviteStatus.USED,
        revoked.invite_id: InviteStatus.REVOKED,
        expired.invite_id: InviteStatus.EXPIRED,
    }
    only_active = await invites.list(ADMIN_TG, include_inactive=False)
    assert [i.id for i in only_active] == [active.invite_id]
    async with as_app(app_engine, user=str(admin)) as conn:
        columns = list(
            (await conn.execute(text("SELECT * FROM list_onboarding_invites(true)"))).keys()
        )
    assert columns == [
        "invite_id",
        "status",
        "created_at",
        "expires_at",
        "note",
        "created_by",
        "used_by",
    ]


async def test_revoke_outcomes_and_a_revoked_invite_cannot_onboard(
    admin: UUID, owner_engine: Any, app_engine: Any
) -> None:
    invites = _invites(app_engine)
    created = await invites.create(ADMIN_TG, note=None, channel="cli")
    assert await invites.revoke(ADMIN_TG, created.invite_id, channel="cli") is RevokeOutcome.REVOKED
    assert await invites.revoke(ADMIN_TG, created.invite_id, channel="cli") is (
        RevokeOutcome.ALREADY_REVOKED
    )
    assert await invites.revoke(ADMIN_TG, uuid4(), channel="cli") is RevokeOutcome.NOT_FOUND
    assert await _onboard(app_engine, NEW_TG, f"onb_{created.token.value}") is (
        StartOutcome.INVITE_REFUSED
    )

    used = await invites.create(ADMIN_TG, note=None, channel="telegram")
    await _onboard(app_engine, NEW_TG, f"onb_{used.token.value}")
    assert await invites.revoke(ADMIN_TG, used.invite_id, channel="cli") is RevokeOutcome.USED

    stale = await invites.create(ADMIN_TG, note=None, channel="cli")
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE onboarding_invites SET expires_at = now() WHERE id = :i"),
            {"i": stale.invite_id},
        )
    assert await invites.revoke(ADMIN_TG, stale.invite_id, channel="cli") is RevokeOutcome.EXPIRED

    async with owner_engine.connect() as conn:
        revoked_audits = await scalar(
            conn, "SELECT count(*) FROM audit_events WHERE action = 'onboarding_invite.revoked'"
        )
    assert revoked_audits == 1  # only the revoke that changed something


async def test_revoke_racing_redeem_has_exactly_one_effect(
    admin: UUID, owner_engine: Any, app_engine: Any
) -> None:
    invites = _invites(app_engine)
    for n in range(5):
        created = await invites.create(ADMIN_TG, note=None, channel="cli")
        outcome, revoke = await asyncio.gather(
            _onboard(app_engine, NEW_TG + n, f"onb_{created.token.value}"),
            invites.revoke(ADMIN_TG, created.invite_id, channel="cli"),
        )
        assert (outcome, revoke) in {
            (StartOutcome.ONBOARDED, RevokeOutcome.USED),
            (StartOutcome.INVITE_REFUSED, RevokeOutcome.REVOKED),
        }
        async with owner_engine.connect() as conn:
            state = (
                await rows(
                    conn,
                    "SELECT used_at IS NOT NULL AS used, revoked_at IS NOT NULL AS revoked "
                    "FROM onboarding_invites WHERE id = :i",
                    {"i": created.invite_id},
                )
            )[0]
        assert state.used != state.revoked


async def test_platform_admin_gets_no_access_to_other_workspaces(
    admin: UUID, owner_engine: Any, app_engine: Any
) -> None:
    """docs/08 §7 (AT-RBAC-P-01 at the database level): being an admin opens no workspace."""
    created = await _invites(app_engine).create(ADMIN_TG, note=None, channel="cli")
    await _onboard(app_engine, NEW_TG, f"onb_{created.token.value}")
    gateway = PgIdentityGateway(app_engine)
    principal = await gateway.resolve_principal(NEW_TG)
    assert principal is not None
    other = await gateway.personal_workspace(principal.user_id)
    assert other is not None
    async with as_app(app_engine, workspace=str(other.id), user=str(admin)) as conn:
        assert (
            await scalar(conn, "SELECT count(*) FROM workspaces WHERE id = :w", {"w": other.id})
            == 0
        )
        assert await scalar(conn, "SELECT count(*) FROM audit_events") == 0
