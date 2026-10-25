"""Row-level security, privileges and integrity of the Phase 1 schema (docs/07 §4.2, docs/09 §4).

Every query in these tests runs as ``finbot_app`` (the bot's role) on a database migrated to head
by the real revisions, unless the test is explicitly about owner behaviour. Runs only with
FINBOT_TEST_PG_BACKEND set.
"""

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.exc import DBAPIError

from tests.support.phase1 import World, as_app, rows, scalar, seed

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

# Revision 0004 (ledger core) adds tenant tables; the coverage and privilege checks below
# include them, so a table added without RLS, FORCE or a policy fails here.
LEDGER_TABLES = (
    "accounts",
    "categories",
    "transactions",
    "transaction_entries",
    "transaction_revisions",
    "drafts",
    "pending_actions",
)
RLS_TABLES = ("users", "workspaces", "workspace_memberships", "audit_events", *LEDGER_TABLES)
NON_RLS_TABLES = {"alembic_version", "platform_meta"}  # no tenant data; app access is SELECT-only
# Revision 0002 (head): RLS + FORCE + a deny-all policy, no app grants (docs/09 §4.3).
IDENTITY_TABLES = ("telegram_identities", "onboarding_invites")
INSERT_MEMBER = "INSERT INTO workspace_memberships (workspace_id, workspace_kind, user_id, role) "


@pytest_asyncio.fixture
async def world(migrated_database: str, owner_engine: Any) -> World:
    return await seed(owner_engine)


# ------------------------------------------------------------------ visibility (fail-closed)


async def test_AT_RLS_00_no_context_sees_nothing(world: World, app_engine: Any) -> None:
    async with as_app(app_engine) as conn:
        for table in RLS_TABLES:
            assert await scalar(conn, f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608


async def test_AT_RLS_01_context_sees_only_its_workspace(world: World, app_engine: Any) -> None:
    async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
        members = await rows(
            conn, "SELECT workspace_id::text, user_id::text FROM workspace_memberships"
        )
        # B1's members (including the removed one), plus u1's own memberships elsewhere.
        assert {(w, u) for w, u in members} == {
            (world.b1, world.u1),
            (world.b1, world.u4),
            (world.b2, world.u1),
            (world.p1, world.u1),
        }
        audit = await rows(conn, "SELECT workspace_id::text FROM audit_events")
        assert [a for (a,) in audit] == [world.b1]
        # Workspace switcher: every workspace u1 is an active member of, none other.
        visible = {w for (w,) in await rows(conn, "SELECT id::text FROM workspaces")}
        assert visible == {world.b1, world.b2, world.p1}
        users = {u for (u,) in await rows(conn, "SELECT id::text FROM users")}
        assert users == {world.u1, world.u4}  # self and B1's (former) members; never u2 or u3


async def test_S5_context_without_active_membership_sees_nothing(
    world: World, app_engine: Any
) -> None:
    for user in (world.u3, world.u4, world.u2):  # stranger, removed member, other ws's owner
        async with as_app(app_engine, workspace=world.b1, user=user) as conn:
            assert await scalar(conn, "SELECT count(*) FROM audit_events") == 0
            assert (
                await scalar(
                    conn, "SELECT count(*) FROM workspaces WHERE id = :b1", {"b1": world.b1}
                )
                == 0
            )
            others = await scalar(
                conn,
                "SELECT count(*) FROM workspace_memberships WHERE user_id <> :u",
                {"u": user},
            )
            assert others == 0, user
    async with as_app(app_engine, workspace=world.b1) as conn:  # workspace set, user unset
        assert await scalar(conn, "SELECT count(*) FROM workspace_memberships") == 0


async def test_AT_RLS_04_context_is_transaction_local(world: World, app_engine: Any) -> None:
    from tests.support.phase1 import set_context  # noqa: PLC0415

    async with app_engine.connect() as conn:  # one physical connection, two transactions
        async with conn.begin():
            await set_context(conn, workspace=world.b1, user=world.u1)
            assert await scalar(conn, "SELECT count(*) FROM audit_events") == 1
        async with conn.begin():
            assert await scalar(conn, "SELECT count(*) FROM audit_events") == 0
            assert await scalar(conn, "SELECT current_setting('app.workspace_id', true)") in (
                "",
                None,
            )


async def test_policies_do_not_recurse(world: World, app_engine: Any) -> None:
    # users -> workspace_memberships -> app_is_active_member (definer): no infinite recursion.
    async with as_app(app_engine, workspace=world.b2, user=world.u1) as conn:
        names = await rows(
            conn,
            "SELECT u.display_name FROM users u JOIN workspace_memberships m ON m.user_id = u.id "
            "JOIN workspaces w ON w.id = m.workspace_id WHERE w.id = :b2 ORDER BY 1",
            {"b2": world.b2},
        )
        assert [n for (n,) in names] == ["U1", "U2"]


# ----------------------------------------------------------------------- write isolation


async def test_AT_RLS_02_insert_into_other_workspace_is_rejected(
    world: World, app_engine: Any
) -> None:
    with pytest.raises(DBAPIError, match="row-level security policy"):
        async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
            await conn.execute(
                *_sql(
                    "INSERT INTO audit_events (workspace_id, actor_kind, channel, action) "
                    "VALUES (:ws, 'user', 'telegram', 'x')",
                    ws=world.b2,
                )
            )
    with pytest.raises(DBAPIError, match="row-level security policy"):
        async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
            await conn.execute(
                *_sql(
                    INSERT_MEMBER + "VALUES (:ws, 'business', :u3, 'viewer')",
                    ws=world.b2,
                    u3=world.u3,
                )
            )


async def test_AT_RLS_02_audit_insert_in_context_and_platform_level(
    world: World, app_engine: Any
) -> None:
    async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
        for ws in (world.b1, None):
            await conn.execute(
                *_sql(
                    "INSERT INTO audit_events (workspace_id, actor_kind, channel, action) "
                    "VALUES (:ws, 'user', 'telegram', 'member.role_changed')",
                    ws=ws,
                )
            )
        assert await scalar(conn, "SELECT count(*) FROM audit_events") == 2  # b1's two, not NULL's


async def test_AT_RLS_03_update_outside_context_is_rejected(world: World, app_engine: Any) -> None:
    # u1 can see B2 (member there) but the context is B1: the new row fails WITH CHECK.
    with pytest.raises(DBAPIError, match="row-level security policy"):
        async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
            await conn.execute(
                *_sql("UPDATE workspaces SET name = 'x' WHERE id = :b2", b2=world.b2)
            )
    # Moving a row to another workspace is not even grantable (S7).
    with pytest.raises(DBAPIError, match="permission denied"):
        async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
            await conn.execute(
                *_sql("UPDATE workspace_memberships SET workspace_id = :b2", b2=world.b2)
            )


async def test_owner_can_update_own_workspace_and_members(world: World, app_engine: Any) -> None:
    async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
        result = await conn.execute(
            *_sql(
                "UPDATE workspaces SET name = 'B1 renamed', version = version + 1 WHERE id = :b1",
                b1=world.b1,
            )
        )
        assert result.rowcount == 1
        await conn.execute(
            *_sql(
                "INSERT INTO workspace_memberships "
                "(workspace_id, workspace_kind, user_id, role, invited_by) "
                "VALUES (:b1, 'business', :u3, 'viewer', :u1)",
                b1=world.b1,
                u3=world.u3,
                u1=world.u1,
            )
        )
        result = await conn.execute(
            *_sql(
                "UPDATE workspace_memberships SET role = 'editor' WHERE user_id = :u3", u3=world.u3
            )
        )
        assert result.rowcount == 1


# ------------------------------------------------------------- column privileges (S7)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("users", "is_platform_admin"),
        ("users", "status"),
        ("users", "email"),
        ("users", "password_hash"),
        ("users", "id"),
        ("workspaces", "kind"),
        ("workspaces", "personal_owner_id"),
        ("workspaces", "created_at"),
        ("workspace_memberships", "user_id"),
        ("workspace_memberships", "workspace_kind"),
    ],
)
async def test_S7_protected_columns_are_not_updatable(
    world: World, app_engine: Any, table: str, column: str
) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
            await conn.execute(*_sql(f"UPDATE {table} SET {column} = {column}"))  # noqa: S608


async def test_S7_user_updates_only_own_profile(world: World, app_engine: Any) -> None:
    async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
        own = await conn.execute(
            *_sql("UPDATE users SET display_name = 'Me' WHERE id = :u1", u1=world.u1)
        )
        assert own.rowcount == 1
    with pytest.raises(DBAPIError, match="row-level security policy"):
        async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:  # u4 is visible
            await conn.execute(
                *_sql("UPDATE users SET display_name = 'X' WHERE id = :u4", u4=world.u4)
            )


# ----------------------------------------------------------------- audit append-only (S6)


@pytest.mark.parametrize(
    "sql",
    ["UPDATE audit_events SET action = 'x'", "DELETE FROM audit_events", "TRUNCATE audit_events"],
)
async def test_AT_RLS_07_app_cannot_change_audit_events(
    world: World, app_engine: Any, sql: str
) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
            await conn.execute(*_sql(sql))


async def test_S6_definer_function_cannot_bypass_append_only(
    world: World, owner_engine: Any, app_engine: Any
) -> None:
    # A SECURITY DEFINER function owned by finbot_owner runs with current_user = finbot_owner;
    # the trigger checks session_user, which stays finbot_app.
    async with owner_engine.begin() as conn:
        await conn.execute(
            *_sql(
                "CREATE FUNCTION public.test_tamper_audit() RETURNS void LANGUAGE sql "
                "SECURITY DEFINER SET search_path = pg_catalog, public AS "
                "$$ UPDATE public.audit_events SET action = 'tampered' $$"
            )
        )
        await conn.execute(
            *_sql("GRANT EXECUTE ON FUNCTION public.test_tamper_audit() TO finbot_app")
        )
    with pytest.raises(
        DBAPIError, match="audit_events is append-only: UPDATE refused for finbot_app"
    ):
        async with as_app(app_engine, workspace=world.b1, user=world.u1) as conn:
            await conn.execute(*_sql("SELECT public.test_tamper_audit()"))
    async with owner_engine.begin() as conn:  # the owner (retention prune, purge) may
        result = await conn.execute(*_sql("DELETE FROM audit_events WHERE workspace_id IS NULL"))
        assert result.rowcount == 1


# ------------------------------------------------------- tenancy integrity (docs/05 §4)


async def test_personal_membership_must_be_the_owner(world: World, owner_engine: Any) -> None:
    other = World()  # a fresh personal workspace for u2, with u3 as its "member"
    async with owner_engine.begin() as conn:
        await conn.execute(
            *_sql(
                "INSERT INTO workspaces (id, kind, name, personal_owner_id) "
                "VALUES (:p, 'personal', 'P2', :u2)",
                p=other.p1,
                u2=world.u2,
            )
        )
    with pytest.raises(DBAPIError, match="must belong to the workspace owner"):
        async with owner_engine.begin() as conn:
            await conn.execute(
                *_sql(
                    INSERT_MEMBER + "VALUES (:p, 'personal', :u3, 'owner')",
                    p=other.p1,
                    u3=world.u3,
                )
            )


async def test_personal_workspace_is_owner_only(world: World, owner_engine: Any) -> None:
    # The BEFORE trigger rejects any other user; the CHECK keeps the owner's role at 'owner'.
    with pytest.raises(DBAPIError, match="must belong to the workspace owner"):
        async with owner_engine.begin() as conn:
            await conn.execute(
                *_sql(
                    INSERT_MEMBER + "VALUES (:p1, 'personal', :u2, 'owner')",
                    p1=world.p1,
                    u2=world.u2,
                )
            )
    with pytest.raises(DBAPIError, match="workspace_memberships_personal_owner_only"):
        async with owner_engine.begin() as conn:
            await conn.execute(
                *_sql(
                    "UPDATE workspace_memberships SET role = 'editor' WHERE workspace_id = :p1",
                    p1=world.p1,
                )
            )


async def test_personal_workspace_cannot_be_pending_deletion(
    world: World, owner_engine: Any
) -> None:
    with pytest.raises(DBAPIError, match="workspaces_personal_always_active"):
        async with owner_engine.begin() as conn:
            await conn.execute(
                *_sql(
                    "UPDATE workspaces SET status = 'pending_deletion', "
                    "deletion_requested_at = now() "
                    "WHERE id = :p1",
                    p1=world.p1,
                )
            )


async def test_membership_kind_must_match_the_workspace(world: World, owner_engine: Any) -> None:
    with pytest.raises(DBAPIError, match="workspace_memberships_workspace_fkey"):
        async with owner_engine.begin() as conn:
            await conn.execute(
                *_sql(
                    INSERT_MEMBER + "VALUES (:p1, 'business', :u3, 'viewer')",
                    p1=world.p1,
                    u3=world.u3,
                )
            )


async def test_business_workspace_needs_an_owner_at_commit(world: World, owner_engine: Any) -> None:
    other = World()
    with pytest.raises(DBAPIError, match="must have exactly one active owner"):
        async with owner_engine.begin() as conn:  # created without any membership
            await conn.execute(
                *_sql(
                    "INSERT INTO workspaces (id, kind, name) VALUES (:b, 'business', 'B3')",
                    b=other.b1,
                )
            )
    with pytest.raises(DBAPIError, match="must have exactly one active owner"):
        async with owner_engine.begin() as conn:  # the owner leaves without a successor
            await conn.execute(
                *_sql(
                    "UPDATE workspace_memberships SET status = 'left', ended_at = now() "
                    "WHERE workspace_id = :b1 AND user_id = :u1",
                    b1=world.b1,
                    u1=world.u1,
                )
            )


async def test_ownership_transfer_inside_one_transaction(world: World, app_engine: Any) -> None:
    # As the app, in B2's context: u2 hands ownership to u1 (owner check deferred to commit).
    async with as_app(app_engine, workspace=world.b2, user=world.u2) as conn:
        await conn.execute(
            *_sql(
                "UPDATE workspace_memberships SET role = 'admin' WHERE user_id = :u2", u2=world.u2
            )
        )
        await conn.execute(
            *_sql(
                "UPDATE workspace_memberships SET role = 'owner' "
                "WHERE workspace_id = :b2 AND user_id = :u1",
                b2=world.b2,
                u1=world.u1,
            )
        )
    async with as_app(app_engine, workspace=world.b2, user=world.u1) as conn:
        owner = await scalar(
            conn,
            "SELECT user_id::text FROM workspace_memberships "
            "WHERE workspace_id = :b2 AND role = 'owner'",
            {"b2": world.b2},
        )
        assert owner == world.u1


async def test_two_active_owners_are_rejected(world: World, owner_engine: Any) -> None:
    with pytest.raises(DBAPIError, match="uq_one_owner"):
        async with owner_engine.begin() as conn:
            await conn.execute(
                *_sql(
                    "UPDATE workspace_memberships SET role = 'owner' "
                    "WHERE workspace_id = :b2 AND user_id = :u1",
                    b2=world.b2,
                    u1=world.u1,
                )
            )


# ------------------------------------------------------------- catalog-level checks


async def test_AT_RLS_COVERAGE_phase1(migrated_database: str, owner_engine: Any) -> None:
    async with owner_engine.connect() as conn:
        tables = await rows(
            conn,
            """SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                      (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies,
                      EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                              AND a.attname = 'workspace_id' AND NOT a.attisdropped) AS has_ws,
                      EXISTS (SELECT 1 FROM pg_index i JOIN pg_attribute a
                                ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                              WHERE i.indrelid = c.oid AND i.indisunique
                                AND a.attname = 'workspace_id') AS ws_leading_unique,
                      EXISTS (SELECT 1 FROM pg_constraint f
                              WHERE f.contype = 'f' AND f.confrelid = c.oid) AS referenced
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')""",
        )
    names = {t.relname for t in tables}
    assert names == set(RLS_TABLES) | set(IDENTITY_TABLES) | NON_RLS_TABLES
    for t in tables:
        if t.relname in NON_RLS_TABLES:
            continue
        assert t.relrowsecurity, t.relname
        assert t.relforcerowsecurity, t.relname
        assert t.policies >= 1, t.relname
        if t.has_ws:  # parent tables need UNIQUE (workspace_id, ...) for composite FKs
            assert t.ws_leading_unique or not t.referenced, t.relname


async def test_AT_RLS_05_app_and_backup_privileges(
    migrated_database: str, owner_engine: Any
) -> None:
    async with owner_engine.connect() as conn:

        async def check(sql: str) -> Any:
            return await scalar(conn, sql)

        db = "current_database()"
        assert await check(f"SELECT has_database_privilege('finbot_app', {db}, 'CONNECT')")
        assert not await check(f"SELECT has_database_privilege('finbot_app', {db}, 'TEMPORARY')")
        assert not await check(f"SELECT has_database_privilege('finbot_app', {db}, 'CREATE')")
        # PUBLIC no longer connects: a role without its own grant cannot.
        assert not await check(f"SELECT has_database_privilege('finbot_readonly', {db}, 'CONNECT')")
        assert not await check("SELECT has_schema_privilege('finbot_app', 'public', 'CREATE')")
        assert not await check("SELECT has_schema_privilege('finbot_readonly', 'public', 'USAGE')")
        for table in RLS_TABLES:
            assert not await check(
                f"SELECT has_table_privilege('finbot_app', 'public.{table}', 'DELETE')"
            )
            assert not await check(
                f"SELECT has_table_privilege('finbot_app', 'public.{table}', 'TRUNCATE')"
            )
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not await check(
                f"SELECT has_table_privilege('finbot_app', 'public.audit_events', '{priv}')"
            )
        assert not await check("SELECT has_table_privilege('finbot_app', 'public.users', 'INSERT')")
        assert not await check(
            "SELECT has_table_privilege('finbot_app', 'public.platform_meta', 'INSERT')"
        )
        assert not await check(
            "SELECT has_table_privilege('finbot_app', 'public.alembic_version', 'SELECT')"
        )
        assert await check(
            "SELECT has_table_privilege('finbot_app', 'public.platform_meta', 'SELECT')"
        )
        for table in IDENTITY_TABLES:  # only through the SECURITY DEFINER functions
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES"):
                assert not await check(
                    f"SELECT has_table_privilege('finbot_app', 'public.{table}', '{priv}')"
                ), (table, priv)
        for table in (*RLS_TABLES, *IDENTITY_TABLES, *NON_RLS_TABLES):  # complete dumps
            assert await check(
                f"SELECT has_table_privilege('finbot_backup', 'public.{table}', 'SELECT')"
            ), table
            assert not await check(
                f"SELECT has_table_privilege('finbot_backup', 'public.{table}', 'INSERT')"
            ), table


async def test_AT_RLS_06_definer_functions_are_narrow(
    migrated_database: str, owner_engine: Any
) -> None:
    async with owner_engine.connect() as conn:
        functions = await rows(
            conn,
            """SELECT p.proname, pg_get_userbyid(p.proowner) AS owner, p.proconfig,
                      p.prorettype::regtype::text AS returns,
                      ARRAY(SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                                        ELSE pg_get_userbyid(a.grantee) END
                            FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                            WHERE a.privilege_type = 'EXECUTE') AS executors
               FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
               WHERE n.nspname = 'public' AND p.prosecdef ORDER BY 1""",
        )
    assert {f.proname for f in functions} == {
        "app_is_active_member",
        "business_has_owner",
        "personal_membership_is_owner",
        "redeem_onboarding_invite",
        "resolve_principal",
        "create_onboarding_invite",
        "list_onboarding_invites",
        "revoke_onboarding_invite",
        # 0004 ledger trigger functions: definer so they see every row of one transaction or
        # one referenced row whatever the caller's RLS context; executable by nobody directly.
        "ledger_entries_balanced",
        "ledger_entry_active_account",
        "ledger_txn_active_category",
        # 0005: the schema revision for the start-up guard (G-ENV-7), without any privilege
        # on alembic_version itself.
        "app_schema_revision",
        # 0006: the category quota trigger counts every row of one workspace (Step 6C).
        "ledger_category_quota",
    }
    for f in functions:
        assert f.owner == "finbot_owner"
        assert f.proconfig == ["search_path=pg_catalog, public"], f.proname
        assert "PUBLIC" not in f.executors, f.proname
        assert f.returns in ("boolean", "trigger", "record", "uuid", "text"), f.proname
    executors = {f.proname: set(f.executors) - {"finbot_owner"} for f in functions}
    assert executors == {
        "app_is_active_member": {"finbot_app"},
        "business_has_owner": set(),
        "personal_membership_is_owner": set(),
        "redeem_onboarding_invite": {"finbot_app"},
        "resolve_principal": {"finbot_app"},
        "create_onboarding_invite": {"finbot_app"},
        "list_onboarding_invites": {"finbot_app"},
        "revoke_onboarding_invite": {"finbot_app"},
        "ledger_entries_balanced": set(),
        "ledger_entry_active_account": set(),
        "ledger_txn_active_category": set(),
        "app_schema_revision": {"finbot_app"},
        "ledger_category_quota": set(),
    }


async def test_environment_marker_is_read_only_for_the_app(world: World, app_engine: Any) -> None:
    async with as_app(app_engine) as conn:
        assert (
            await scalar(conn, "SELECT value FROM platform_meta WHERE key = 'environment'")
            == "test"
        )
    with pytest.raises(DBAPIError, match="permission denied"):
        async with as_app(app_engine) as conn:
            await conn.execute(*_sql("UPDATE platform_meta SET value = 'production'"))


def _sql(statement: str, **params: Any) -> tuple[Any, dict[str, Any]]:
    from sqlalchemy import text  # noqa: PLC0415

    return text(statement), params
