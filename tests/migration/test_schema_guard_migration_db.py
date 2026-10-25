"""Revisions 0005/0006 and the start-up schema guard G-ENV-7 (Steps 6B/6C; docs/10 §13, §14).

* the bot accepts only a supported schema revision and refuses a database that is behind
  (``0003``, ``0004``), never migrated, or at an unexpected revision, before the poller lock,
  before Telegram and before the heartbeat ever says "ready";
* ``0005`` upgrades a populated ``0004`` database without touching its rows, downgrades exactly
  (dev/test/drill only) and upgrades again;
* a failing ``0005`` (failure injection) leaves the database exactly at ``0004``: the migration
  runs in one transaction.

Runs only with FINBOT_TEST_PG_BACKEND.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from finbot.domain.ledger import TxnType
from finbot.infra.db.guards import (
    SchemaMismatchError,
    check_database,
    require_schema_revision,
)
from finbot.infra.heartbeat import BotState
from tests.migration.test_baseline_migration_db import _query, _state, _with_downgrade
from tests.migration.test_identity_migration_db import _functions
from tests.migration.test_ledger_migration_db import _engine, _existing_users, _snapshot
from tests.support import pg
from tests.support.finance import finance, record
from tests.unit.test_bot_lifecycle import Run

pytestmark = pytest.mark.db

HEAD = "0006"


def _guard(cluster: pg.ClusterInfo, database: str, *, full: bool = False) -> None:
    async def run() -> None:
        app = _engine(cluster, database, pg.APP_ROLE)
        try:
            if full:
                await check_database(app, env="test", db_name=database)
            else:
                await require_schema_revision(app)
        finally:
            await app.dispose()

    asyncio.run(run())


def test_AT_ENV_07_compatible_schema_passes_every_guard(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, "head")
    _guard(pg_cluster, test_database, full=True)  # G-ENV-2, G-ENV-3 and G-ENV-7


@pytest.mark.parametrize(
    ("revision", "reason"),
    [
        ("0003", "unreadable"),
        ("0004", "unreadable"),
        ("0005", "is '0005', this release supports 0006"),
    ],
)
def test_AT_ENV_07_a_database_behind_the_release_is_refused(
    alembic_config: Config,
    pg_cluster: pg.ClusterInfo,
    test_database: str,
    revision: str,
    reason: str,
) -> None:
    command.upgrade(alembic_config, revision)
    with pytest.raises(SchemaMismatchError, match=reason):
        _guard(pg_cluster, test_database, full=True)


def test_AT_ENV_07_a_never_migrated_database_is_refused(
    pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    with pytest.raises(SchemaMismatchError, match="unreadable"):
        _guard(pg_cluster, test_database)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE alembic_version SET version_num = '0099'",
        "INSERT INTO alembic_version VALUES ('0007')",  # two rows: no single revision
        "DELETE FROM alembic_version",
    ],
)
def test_AT_ENV_07_an_unexpected_revision_is_refused(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str, sql: str
) -> None:
    command.upgrade(alembic_config, "head")
    _query(pg_cluster, test_database, sql)
    with pytest.raises(SchemaMismatchError, match="this release supports 0006"):
        _guard(pg_cluster, test_database)


@pytest.mark.asyncio
async def test_AT_ENV_07_the_bot_never_polls_or_reports_ready_on_an_old_schema(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str, tmp_path: Path
) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config, "0004")
    app = _engine(pg_cluster, test_database, pg.APP_ROLE)
    run = Run(tmp_path)

    async def check() -> None:
        await check_database(app, env="test", db_name=test_database)

    run.check_environment = check
    try:
        with pytest.raises(Exception, match="G-ENV-7") as caught:
            await run.serve()
    finally:
        await app.dispose()
    assert type(caught.value).__name__ == "BotStartupError"
    assert run.steps == []  # the poller lock was never requested
    assert run.api.calls == []  # Telegram was never contacted
    assert run.state()["state"] == BotState.FAILED  # the healthcheck reports unhealthy
    state = await asyncio.to_thread(_state, pg_cluster, test_database)
    assert state["version"] == ["0004"]  # never auto-migrated


def test_0005_upgrades_a_populated_0004_database_and_downgrades_exactly(
    alembic_config: Config,
    migration_settings: Any,
    pg_cluster: pg.ClusterInfo,
    test_database: str,
) -> None:
    command.upgrade(alembic_config, "0004")
    people = _existing_users(pg_cluster, test_database)

    async def use() -> None:
        app = _engine(pg_cluster, test_database, pg.APP_ROLE)
        try:
            await record(finance(app), people[0].actor(), TxnType.EXPENSE, "15rb", "Makanan")
        finally:
            await app.dispose()

    asyncio.run(use())
    rows = _snapshot(pg_cluster, test_database)
    ledger = _query(pg_cluster, test_database, "SELECT id, amount, version FROM transactions")
    before = (_state(pg_cluster, test_database), _functions(pg_cluster, test_database))
    command.upgrade(alembic_config, HEAD)
    assert _state(pg_cluster, test_database)["version"] == [HEAD]
    assert _snapshot(pg_cluster, test_database) == rows
    assert (
        _query(pg_cluster, test_database, "SELECT id, amount, version FROM transactions") == ledger
    )
    _guard(pg_cluster, test_database)
    command.downgrade(_with_downgrade(alembic_config, migration_settings), "0004")
    assert (_state(pg_cluster, test_database), _functions(pg_cluster, test_database)) == before
    command.upgrade(alembic_config, HEAD)
    assert _state(pg_cluster, test_database)["version"] == [HEAD]


def test_0005_failure_injection_leaves_0004_untouched(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, "0004")
    # A conflicting object makes the last statements of 0005 fail.
    _query(
        pg_cluster,
        test_database,
        "CREATE FUNCTION public.membership_version_bump() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RETURN NEW; END $$",
    )
    before = (_state(pg_cluster, test_database), _functions(pg_cluster, test_database))
    with pytest.raises(Exception, match="already exists"):
        command.upgrade(alembic_config, HEAD)
    assert (_state(pg_cluster, test_database), _functions(pg_cluster, test_database)) == before
    assert _state(pg_cluster, test_database)["version"] == ["0004"]
    assert (
        _query(
            pg_cluster,
            test_database,
            "SELECT count(*) FROM pg_proc WHERE proname = 'app_schema_revision'",
        )[0][0]
        == 0
    )  # the statements before the failure were rolled back too
