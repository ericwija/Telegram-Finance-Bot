"""Lifecycle of the Phase 1 baseline revision on a real PostgreSQL 18.6 (docs/10 §3, S9).

Upgrade to 0001 records the environment marker and leaves everything owned by finbot_owner; a
second upgrade is a no-op that still runs the guard; a downgrade (test environment, explicit
opt-in) removes every object and privilege change, and the chain upgrades again cleanly.
Runs only with FINBOT_TEST_PG_BACKEND set.
"""

import asyncio
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from finbot.infra.db.connection import create_engine
from finbot.infra.db.migration_runtime import SETTINGS_ATTRIBUTE
from tests.support import pg

pytestmark = pytest.mark.db

BASELINE = "0001"
PHASE1_TABLES = {"users", "workspaces", "workspace_memberships", "audit_events", "platform_meta"}


def _query(cluster: pg.ClusterInfo, database: str, sql: str) -> list[Any]:
    from tests.plugins.db import _target  # noqa: PLC0415

    async def run() -> list[Any]:
        engine = create_engine(_target(cluster, database, pg.OWNER_ROLE))
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql))
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _state(cluster: pg.ClusterInfo, database: str) -> dict[str, Any]:
    """Everything the baseline touches, as plain values."""

    def one(sql: str) -> Any:
        return _query(cluster, database, sql)[0][0]

    return {
        "tables": {
            r[0]
            for r in _query(
                cluster, database, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        },
        "functions": one(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public'"
        ),
        "extensions": {r[0] for r in _query(cluster, database, "SELECT extname FROM pg_extension")},
        "default_acls": one("SELECT count(*) FROM pg_default_acl"),
        # A role with no grants of its own shows what PUBLIC may do.
        "public_connect": one(
            "SELECT has_database_privilege('finbot_readonly', current_database(), 'CONNECT')"
        ),
        "public_temp": one(
            "SELECT has_database_privilege('finbot_readonly', current_database(), 'TEMPORARY')"
        ),
        "public_schema_usage": one(
            "SELECT has_schema_privilege('finbot_readonly', 'public', 'USAGE')"
        ),
        "backup_reads_version": one(
            "SELECT coalesce(has_table_privilege('finbot_backup', "
            "to_regclass('public.alembic_version'), 'SELECT'), false)"
        ),
        "version": (
            [r[0] for r in _query(cluster, database, "SELECT version_num FROM alembic_version")]
            if one("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            else []
        ),
    }


def _with_downgrade(config: Config, settings: Any) -> Config:
    config.attributes[SETTINGS_ATTRIBUTE] = settings.model_copy(
        update={"migration_allow_downgrade": True}
    )
    return config


def test_upgrade_creates_the_phase1_baseline_owned_by_finbot_owner(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, BASELINE)
    state = _state(pg_cluster, test_database)
    assert state["tables"] == PHASE1_TABLES | {"alembic_version"}
    assert state["version"] == ["0001"]
    assert "citext" in state["extensions"]
    assert state["public_connect"] is False
    assert state["public_temp"] is False
    assert state["public_schema_usage"] is False
    marker = _query(pg_cluster, test_database, "SELECT key, value FROM platform_meta")
    assert [tuple(r) for r in marker] == [("environment", "test")]
    not_owned = _query(
        pg_cluster,
        test_database,
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'i', 'v', 'S') "
        "AND pg_get_userbyid(c.relowner) <> 'finbot_owner' "
        "UNION ALL SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e' "
        "WHERE n.nspname = 'public' AND d.objid IS NULL "
        "AND pg_get_userbyid(p.proowner) <> 'finbot_owner'",
    )
    assert not_owned == []  # extension members (citext) excepted


def test_second_upgrade_is_a_noop_and_still_checks_the_marker(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, BASELINE)
    command.upgrade(alembic_config, BASELINE)
    assert _state(pg_cluster, test_database)["version"] == ["0001"]
    _query(pg_cluster, test_database, "UPDATE platform_meta SET value = 'staging'")
    with pytest.raises(Exception, match="environment marker is not test"):
        command.upgrade(alembic_config, BASELINE)


def test_downgrade_is_refused_without_opt_in_and_changes_nothing(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, BASELINE)
    before = _state(pg_cluster, test_database)
    with pytest.raises(Exception, match="forward fixes"):
        command.downgrade(alembic_config, "base")
    assert _state(pg_cluster, test_database) == before


def test_downgrade_removes_everything_and_the_chain_upgrades_again(
    alembic_config: Config,
    migration_settings: Any,
    pg_cluster: pg.ClusterInfo,
    test_database: str,
) -> None:
    pristine = _state(pg_cluster, test_database)  # empty database, before any migration
    command.upgrade(alembic_config, BASELINE)
    command.downgrade(_with_downgrade(alembic_config, migration_settings), "base")
    after = _state(pg_cluster, test_database)
    # alembic_version itself stays (empty), as it would after any downgrade to base.
    assert after == {**pristine, "tables": {"alembic_version"}, "version": []}
    command.upgrade(alembic_config, BASELINE)
    assert _state(pg_cluster, test_database)["tables"] == PHASE1_TABLES | {"alembic_version"}
