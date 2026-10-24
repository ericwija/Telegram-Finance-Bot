"""Database harness checks against a real disposable PostgreSQL 18.6 cluster (docs/10 §4).

These prove the test infrastructure itself: the server version, the roles created by the real
``00-roles.sh`` (AT-RLS-05, role attributes part), and that ``finbot_app`` can neither create
objects nor own anything. They run only with FINBOT_TEST_PG_BACKEND set.
"""

from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def _rows(engine: Any, sql: str) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql))).all())


async def test_server_is_postgresql_18_6_with_checksums(app_engine: Any) -> None:
    ((version,),) = await _rows(app_engine, "SHOW server_version")
    assert version.startswith("18.6")
    ((checksums,),) = await _rows(app_engine, "SHOW data_checksums")
    assert checksums == "on"


async def test_AT_RLS_05_role_attributes(app_engine: Any) -> None:
    rows = await _rows(
        app_engine,
        "SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication, "
        "rolinherit, rolcanlogin FROM pg_roles WHERE rolname LIKE 'finbot\\_%' ORDER BY 1",
    )
    roles = {row.rolname: row for row in rows}
    assert set(roles) == {"finbot_app", "finbot_backup", "finbot_owner", "finbot_readonly"}
    for row in rows:
        assert not row.rolsuper
        assert not row.rolcreatedb
        assert not row.rolcreaterole
        assert not row.rolreplication
        assert not row.rolinherit
    assert not roles["finbot_app"].rolbypassrls
    assert roles["finbot_app"].rolcanlogin
    assert roles["finbot_owner"].rolbypassrls
    assert roles["finbot_backup"].rolbypassrls
    assert not roles["finbot_readonly"].rolcanlogin
    assert not roles["finbot_readonly"].rolbypassrls


async def test_app_role_has_no_memberships_and_owns_nothing(app_engine: Any) -> None:
    memberships = await _rows(
        app_engine,
        "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid IN (m.member, m.roleid) "
        "WHERE r.rolname = 'finbot_app'",
    )
    assert memberships == []
    owned = await _rows(
        app_engine,
        "SELECT c.relname FROM pg_class c WHERE pg_get_userbyid(c.relowner) = 'finbot_app'",
    )
    assert owned == []


async def test_app_role_cannot_create_objects(app_engine: Any) -> None:
    from sqlalchemy.exc import ProgrammingError  # noqa: PLC0415

    async with app_engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await conn.execute(text("CREATE TABLE public.app_should_not_create (id int)"))


async def test_test_database_is_owned_by_finbot_owner(
    owner_engine: Any, test_database: str
) -> None:
    ((owner,),) = await _rows(
        owner_engine,
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()",
    )
    assert owner == "finbot_owner"
    ((name,),) = await _rows(owner_engine, "SELECT current_database()")
    assert name == test_database
