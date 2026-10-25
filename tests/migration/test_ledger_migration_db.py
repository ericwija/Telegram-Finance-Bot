"""Revision 0004 lifecycle on a database that already has users and workspaces (Step 6).

0003 with onboarded users → 0004: existing rows are untouched, the new tables start empty, and
the first finance request seeds the defaults. 0004 → 0003 restores the 0003 state exactly
(dev/test/drill only, S9), and upgrading again works. Runs only with FINBOT_TEST_PG_BACKEND.
"""

import asyncio
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from finbot.domain.ledger import TxnType
from finbot.infra.db.connection import create_engine
from tests.migration.test_baseline_migration_db import _query, _state, _with_downgrade
from tests.migration.test_identity_migration_db import _functions
from tests.support import pg
from tests.support.finance import finance, person, record

pytestmark = pytest.mark.db

LEDGER_TABLES = {
    "accounts",
    "categories",
    "transactions",
    "transaction_entries",
    "transaction_revisions",
    "drafts",
    "pending_actions",
}


def _engine(cluster: pg.ClusterInfo, database: str, role: str) -> Any:
    from tests.plugins.db import _target  # noqa: PLC0415

    return create_engine(_target(cluster, database, role))


def _existing_users(cluster: pg.ClusterInfo, database: str) -> list[Any]:
    async def run() -> list[Any]:
        owner = _engine(cluster, database, pg.OWNER_ROLE)
        try:
            return [await person(owner, name) for name in ("Lama", "Juga Lama")]
        finally:
            await owner.dispose()

    return asyncio.run(run())


def _snapshot(cluster: pg.ClusterInfo, database: str) -> list[Any]:
    return [
        tuple(r)
        for r in _query(
            cluster,
            database,
            "SELECT u.id, u.display_name, w.id, w.name, m.role, m.status "
            "FROM users u JOIN workspace_memberships m ON m.user_id = u.id "
            "JOIN workspaces w ON w.id = m.workspace_id ORDER BY u.display_name",
        )
    ]


def test_0004_upgrades_a_populated_0003_database_without_touching_its_rows(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, "0003")
    people = _existing_users(pg_cluster, test_database)
    before = _snapshot(pg_cluster, test_database)
    audit_before = _query(pg_cluster, test_database, "SELECT count(*) FROM audit_events")[0][0]
    command.upgrade(alembic_config, "0004")
    state = _state(pg_cluster, test_database)
    assert state["version"] == ["0004"]
    assert state["tables"] >= LEDGER_TABLES
    assert _snapshot(pg_cluster, test_database) == before
    assert (
        _query(pg_cluster, test_database, "SELECT count(*) FROM audit_events")[0][0] == audit_before
    )
    for table in LEDGER_TABLES:
        assert _query(pg_cluster, test_database, f"SELECT count(*) FROM {table}")[0][0] == 0  # noqa: S608
    assert (
        _query(
            pg_cluster,
            test_database,
            "SELECT count(*) FROM workspace_memberships WHERE default_account_id IS NOT NULL",
        )[0][0]
        == 0
    )

    async def first_use() -> Any:
        app = _engine(pg_cluster, test_database, pg.APP_ROLE)
        try:
            f = finance(app)
            saved = await record(f, people[0].actor(), TxnType.EXPENSE, "15rb", "Makanan")
            return saved.record, await f.ledger.balance(people[1].actor())
        finally:
            await app.dispose()

    saved, other_balance = asyncio.run(first_use())
    assert saved.amount == 15_000
    assert [(a.name, a.balance) for a in other_balance.report.accounts] == [("Kas", 0)]


def test_0004_downgrades_to_0003_exactly_and_upgrades_again(
    alembic_config: Config,
    migration_settings: Any,
    pg_cluster: pg.ClusterInfo,
    test_database: str,
) -> None:
    command.upgrade(alembic_config, "0003")
    _existing_users(pg_cluster, test_database)
    before = (_state(pg_cluster, test_database), _functions(pg_cluster, test_database))
    rows_before = _snapshot(pg_cluster, test_database)
    command.upgrade(alembic_config, "0004")
    command.downgrade(_with_downgrade(alembic_config, migration_settings), "0003")
    assert (_state(pg_cluster, test_database), _functions(pg_cluster, test_database)) == before
    assert _snapshot(pg_cluster, test_database) == rows_before
    columns = _query(
        pg_cluster,
        test_database,
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'workspace_memberships' AND column_name = 'default_account_id'",
    )
    assert columns[0][0] == 0
    command.upgrade(alembic_config, "0004")
    assert _state(pg_cluster, test_database)["version"] == ["0004"]
