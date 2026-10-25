"""Telegram onboarding on a real PostgreSQL 18.6, as finbot_app (FR-IDN-01..04/07, T17, S5).

Covers first onboarding, repeat /start, concurrent redeems, rollback (no partial rows), tenant
isolation of the new personal workspaces, and attempts to use the bootstrap functions or the
identity tables for privilege escalation. Runs only with FINBOT_TEST_PG_BACKEND set.
"""

import asyncio
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from finbot.infra.db.identity import PgIdentityGateway
from finbot.services.onboarding import StartOutcome, StartRequest
from tests.support.identity import (
    counts,
    create_admin,
    create_invite,
    invite_state,
    new_token,
    service,
    token_hash,
)
from tests.support.phase1 import as_app, rows, scalar

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

TG_A = 700_000_001
TG_B = 700_000_002
REDEEM = "SELECT redeem_onboarding_invite(:h, :tg, :name)"


@pytest_asyncio.fixture
async def admin(migrated_database: str, owner_engine: Any) -> str:
    return await create_admin(owner_engine)


def _start(tg: int, token: str | None, first_name: str | None = "Budi") -> StartRequest:
    return StartRequest(tg, first_name, None if token is None else f"onb_{token}")


async def _audit(owner_engine: Any) -> list[Any]:
    async with owner_engine.connect() as conn:
        return await rows(
            conn,
            "SELECT action, outcome, workspace_id, actor_user_id, entity_type, entity_id, "
            "metadata FROM audit_events ORDER BY action",
        )


# ---------------------------------------------------------------------- first onboarding


async def test_first_onboarding_creates_user_identity_personal_workspace_and_owner(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    token = await create_invite(owner_engine, admin)
    result = await service(app_engine).start(_start(TG_A, token, "  Budi\u200b  Santoso "))

    assert result.outcome is StartOutcome.ONBOARDED
    assert result.workspace is not None
    assert result.workspace.name == "Pribadi"
    async with owner_engine.connect() as conn:
        user = (
            await rows(
                conn,
                "SELECT u.id, u.display_name, u.status, u.is_platform_admin "
                "FROM users u JOIN telegram_identities t ON t.user_id = u.id "
                "WHERE t.telegram_user_id = :tg AND t.unlinked_at IS NULL",
                {"tg": TG_A},
            )
        )[0]
        memberships = await rows(
            conn,
            "SELECT m.workspace_id, m.workspace_kind, m.role, m.status, w.personal_owner_id "
            "FROM workspace_memberships m JOIN workspaces w ON w.id = m.workspace_id "
            "WHERE m.user_id = :u",
            {"u": user.id},
        )
    assert (user.display_name, user.status, user.is_platform_admin) == (
        "Budi Santoso",
        "active",
        False,
    )
    assert [(m.workspace_kind, m.role, m.status) for m in memberships] == [
        ("personal", "owner", "active")
    ]
    assert memberships[0].workspace_id == result.workspace.id
    assert memberships[0].personal_owner_id == user.id
    used_at, used_by = await invite_state(owner_engine, token)
    assert used_at is not None
    assert used_by == user.id

    events = {e.action: e for e in await _audit(owner_engine)}
    assert set(events) == {
        "identity.linked",
        "onboarding_invite.redeemed",
        "user.onboarded",
        "workspace.created",
    }
    assert all(e.outcome == "success" and e.actor_user_id == user.id for e in events.values())
    assert events["workspace.created"].workspace_id == result.workspace.id
    assert events["user.onboarded"].workspace_id is None


async def test_unknown_account_without_token_touches_nothing(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    before = await counts(owner_engine)
    for payload in (None, "hello", "onb_short", "onb_" + "x" * 61, "onb_bad!chars_aaaaaaaaaaaa"):
        result = await service(app_engine).start(StartRequest(TG_A, "Budi", payload))
        assert result.outcome is StartOutcome.NEEDS_INVITE, payload
    assert await counts(owner_engine) == before


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("unknown", "unknown"), ("used", "used"), ("revoked", "revoked"), ("expired", "expired")],
)
async def test_refused_invite_creates_nothing_and_audits_the_reason(
    admin: str, owner_engine: Any, app_engine: Any, kind: str, reason: str
) -> None:
    if kind == "unknown":
        token = new_token()
    elif kind == "used":
        token = await create_invite(owner_engine, admin)
        assert (await service(app_engine).start(_start(TG_B, token))).outcome is (
            StartOutcome.ONBOARDED
        )
    elif kind == "revoked":
        token = await create_invite(owner_engine, admin, revoked=True)
    else:
        token = await create_invite(owner_engine, admin, expires_in=-1)
    before = await counts(owner_engine)

    result = await service(app_engine).start(_start(TG_A, token))

    assert result.outcome is StartOutcome.INVITE_REFUSED
    after = await counts(owner_engine)
    assert after == {**before, "audit_events": before["audit_events"] + 1}
    failed = [e for e in await _audit(owner_engine) if e.action == "onboarding_invite.failed"]
    assert len(failed) == 1
    assert failed[0].outcome == "failed"
    assert failed[0].metadata == {"reason": reason}
    assert failed[0].workspace_id is None
    assert failed[0].actor_user_id is None


# ---------------------------------------------------------------------- repeat onboarding


async def test_repeat_start_is_idempotent_and_never_consumes_another_invite(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    first = await create_invite(owner_engine, admin)
    second = await create_invite(owner_engine, admin)
    onboarded = await service(app_engine).start(_start(TG_A, first))
    after_first = await counts(owner_engine)

    for payload_token in (None, first, second):
        again = await service(app_engine).start(_start(TG_A, payload_token))
        assert again.outcome is StartOutcome.WELCOME_BACK
        assert again.workspace == onboarded.workspace

    # Directly through the function too (the service short-circuits via resolve_principal).
    gateway = PgIdentityGateway(app_engine)
    principal = await gateway.resolve_principal(TG_A)
    assert principal is not None
    assert await gateway.redeem_onboarding_invite(token_hash(second), TG_A, "X") == (
        principal.user_id
    )
    assert await counts(owner_engine) == after_first
    assert await invite_state(owner_engine, second) == (None, None)


async def test_suspended_user_is_refused_and_not_onboarded_again(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    await service(app_engine).start(_start(TG_A, await create_invite(owner_engine, admin)))
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET status = 'suspended' WHERE id = "
                "(SELECT user_id FROM telegram_identities WHERE telegram_user_id = :tg)"
            ),
            {"tg": TG_A},
        )
    before = await counts(owner_engine)
    fresh = await create_invite(owner_engine, admin)
    result = await service(app_engine).start(_start(TG_A, fresh))
    assert result.outcome is StartOutcome.ACCOUNT_UNAVAILABLE
    assert result.workspace is None
    assert await counts(owner_engine) == before
    assert await invite_state(owner_engine, fresh) == (None, None)


# ----------------------------------------------------------------------------- concurrency


async def test_concurrent_starts_for_one_account_onboard_it_exactly_once(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    tokens = [await create_invite(owner_engine, admin) for _ in range(6)]
    tokens += [tokens[0]] * 2  # the same link pressed twice as well
    before = await counts(owner_engine)

    results = await asyncio.gather(
        *(service(app_engine).start(_start(TG_A, token)) for token in tokens)
    )

    # Losers of the race get the winner's user back from the function (or, if they started
    # later, WELCOME_BACK from resolve_principal): never an error, never a second account.
    outcomes = {r.outcome for r in results}
    assert StartOutcome.ONBOARDED in outcomes
    assert outcomes <= {StartOutcome.ONBOARDED, StartOutcome.WELCOME_BACK}
    assert len({r.workspace for r in results}) == 1
    after = await counts(owner_engine)
    assert after["users"] == before["users"] + 1
    assert after["telegram_identities"] == 1
    assert after["workspaces"] == before["workspaces"] + 1
    assert after["workspace_memberships"] == before["workspace_memberships"] + 1
    used = [t for t in set(tokens) if (await invite_state(owner_engine, t))[0] is not None]
    assert len(used) == 1


async def test_concurrent_accounts_racing_for_one_invite_get_one_winner(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    token = await create_invite(owner_engine, admin)
    accounts = [TG_A + n for n in range(6)]

    results = await asyncio.gather(
        *(service(app_engine).start(_start(tg, token)) for tg in accounts)
    )

    outcomes = sorted(r.outcome for r in results)
    assert outcomes == sorted([StartOutcome.ONBOARDED] + [StartOutcome.INVITE_REFUSED] * 5)
    after = await counts(owner_engine)
    assert after["telegram_identities"] == 1
    assert after["users"] == 2  # the admin and the winner
    failed = [e for e in await _audit(owner_engine) if e.action == "onboarding_invite.failed"]
    assert [e.metadata for e in failed] == [{"reason": "used"}] * 5


# -------------------------------------------------------------------------------- rollback


async def test_failure_inside_the_function_leaves_no_partial_rows(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    token = await create_invite(owner_engine, admin)
    async with owner_engine.begin() as conn:  # fail after users/identity/workspace were written
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
        await service(app_engine).start(_start(TG_A, token))

    assert await counts(owner_engine) == before
    assert await invite_state(owner_engine, token) == (None, None)
    async with owner_engine.begin() as conn:
        await conn.execute(text("DROP TRIGGER test_fail ON workspace_memberships"))
    # Retrying after the fault succeeds with the same, still unused invite.
    result = await service(app_engine).start(_start(TG_A, token))
    assert result.outcome is StartOutcome.ONBOARDED


async def test_caller_rollback_discards_the_whole_onboarding(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    token = await create_invite(owner_engine, admin)
    before = await counts(owner_engine)
    async with app_engine.connect() as conn:
        transaction = await conn.begin()
        user_id = await scalar(conn, REDEEM, {"h": token_hash(token), "tg": TG_A, "name": "Budi"})
        assert isinstance(user_id, UUID)
        await transaction.rollback()
    assert await counts(owner_engine) == before
    assert await invite_state(owner_engine, token) == (None, None)


@pytest.mark.parametrize(
    ("digest", "tg", "name", "message"),
    [
        (b"short", TG_A, "Budi", "32-byte"),
        (None, TG_A, "Budi", "32-byte"),
        ("ok", 0, "Budi", "positive"),
        ("ok", -5, "Budi", "positive"),
        ("ok", TG_A, "   ", "printable"),
        ("ok", TG_A, "x" * 65, "printable"),
        ("ok", TG_A, "Bu\x07di", "printable"),
    ],
)
async def test_invalid_arguments_are_rejected_before_any_write(
    admin: str,
    owner_engine: Any,
    app_engine: Any,
    digest: Any,
    tg: int,
    name: str,
    message: str,
) -> None:
    token = await create_invite(owner_engine, admin)
    before = await counts(owner_engine)
    params = {"h": token_hash(token) if digest == "ok" else digest, "tg": tg, "name": name}
    with pytest.raises(DBAPIError, match=message):
        async with as_app(app_engine) as conn:
            await scalar(conn, REDEEM, params)
    assert await counts(owner_engine) == before
    assert await invite_state(owner_engine, token) == (None, None)


# ------------------------------------------------------------------------ tenant isolation


async def test_onboarded_users_see_only_their_own_personal_workspace(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    a = await service(app_engine).start(_start(TG_A, await create_invite(owner_engine, admin)))
    b = await service(app_engine).start(_start(TG_B, await create_invite(owner_engine, admin)))
    gateway = PgIdentityGateway(app_engine)
    pa, pb = await gateway.resolve_principal(TG_A), await gateway.resolve_principal(TG_B)
    assert pa is not None
    assert pb is not None
    assert a.workspace
    assert b.workspace

    async with as_app(app_engine, user=str(pa.user_id)) as conn:
        assert await rows(conn, "SELECT id FROM workspaces") == [(a.workspace.id,)]
    # Even naming B's workspace as the context does not help: A is not a member there (S5).
    async with as_app(app_engine, workspace=str(b.workspace.id), user=str(pa.user_id)) as conn:
        assert (
            await scalar(
                conn, "SELECT count(*) FROM workspaces WHERE id = :w", {"w": b.workspace.id}
            )
            == 0
        )
        assert (
            await scalar(
                conn,
                "SELECT count(*) FROM workspace_memberships WHERE workspace_id = :w",
                {"w": b.workspace.id},
            )
            == 0
        )
        assert (
            await scalar(conn, "SELECT count(*) FROM users WHERE id = :u", {"u": pb.user_id}) == 0
        )
        assert await scalar(conn, "SELECT count(*) FROM audit_events") == 0
    # The gateway's own read is scoped the same way.
    assert await gateway.personal_workspace(pa.user_id) == a.workspace


# --------------------------------------------------------------- privilege escalation


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM telegram_identities",
        "SELECT count(*) FROM onboarding_invites",
        "INSERT INTO telegram_identities (user_id, telegram_user_id) "
        "SELECT id, 1 FROM users LIMIT 1",
        "INSERT INTO onboarding_invites (token_hash, created_by, expires_at) "
        "VALUES (sha256('x'), gen_random_uuid(), now() + interval '1 day')",
        "UPDATE onboarding_invites SET used_at = NULL",
        "DELETE FROM telegram_identities",
        "INSERT INTO users (display_name) VALUES ('Mallory')",
    ],
)
async def test_app_has_no_direct_access_to_identity_tables(
    admin: str, app_engine: Any, statement: str
) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with as_app(app_engine) as conn:
            await conn.execute(text(statement))


async def test_bootstrap_cannot_grant_access_to_an_existing_workspace(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    """The function takes no workspace, role or user: onboarding yields one personal owner
    membership and nothing else, and an existing account cannot be re-bound or upgraded."""
    async with owner_engine.begin() as conn:
        business = await scalar(
            conn,
            "WITH w AS (INSERT INTO workspaces (kind, name) VALUES ('business', 'Toko') "
            "RETURNING id) "
            "INSERT INTO workspace_memberships (workspace_id, workspace_kind, user_id, role) "
            "SELECT id, 'business', :admin, 'owner' FROM w RETURNING workspace_id",
            {"admin": admin},
        )
    result = await service(app_engine).start(_start(TG_A, await create_invite(owner_engine, admin)))
    principal = await PgIdentityGateway(app_engine).resolve_principal(TG_A)
    assert principal is not None
    assert result.workspace is not None
    assert principal.is_platform_admin is False
    async with owner_engine.connect() as conn:
        memberships = await rows(
            conn,
            "SELECT workspace_id, role FROM workspace_memberships WHERE user_id = :u",
            {"u": principal.user_id},
        )
        business_members = await scalar(
            conn,
            "SELECT count(*) FROM workspace_memberships WHERE workspace_id = :w",
            {"w": business},
        )
    assert memberships == [(result.workspace.id, "owner")]
    assert business_members == 1


async def test_only_the_app_can_execute_the_identity_functions(
    migrated_database: str, owner_engine: Any
) -> None:
    async with owner_engine.connect() as conn:
        for function in (
            "public.resolve_principal(bigint)",
            "public.redeem_onboarding_invite(bytea, bigint, text)",
        ):
            for role, allowed in (
                ("finbot_app", True),
                ("finbot_backup", False),
                ("finbot_readonly", False),
            ):
                assert (
                    await scalar(
                        conn,
                        "SELECT has_function_privilege(:role, :fn, 'EXECUTE')",
                        {"role": role, "fn": function},
                    )
                    is allowed
                ), (function, role)
            definition = (
                await rows(
                    conn,
                    "SELECT prosecdef, pg_get_userbyid(proowner) AS owner, proconfig "
                    "FROM pg_proc WHERE oid = CAST(:fn AS regprocedure)",
                    {"fn": function},
                )
            )[0]
            assert definition.prosecdef
            assert definition.owner == "finbot_owner"
            assert definition.proconfig == ["search_path=pg_catalog, public"]


async def test_resolve_principal_returns_only_active_bindings(
    admin: str, owner_engine: Any, app_engine: Any
) -> None:
    gateway = PgIdentityGateway(app_engine)
    assert await gateway.resolve_principal(TG_A) is None
    await service(app_engine).start(_start(TG_A, await create_invite(owner_engine, admin)))
    assert await gateway.resolve_principal(TG_A) is not None
    async with owner_engine.begin() as conn:
        await conn.execute(text("UPDATE telegram_identities SET unlinked_at = now()"))
    assert await gateway.resolve_principal(TG_A) is None
    async with as_app(app_engine) as conn:
        columns = list((await conn.execute(text("SELECT * FROM resolve_principal(1)"))).keys())
    assert columns == ["user_id", "status", "is_platform_admin"]
