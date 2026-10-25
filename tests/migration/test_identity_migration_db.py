"""Lifecycle of revision 0002 (identity and onboarding) on a real PostgreSQL 18.6 (S9).

0001 -> 0002 -> 0001 restores exactly the 0001 state; head -> base leaves only an empty
alembic_version; the chain upgrades again. Runs only with FINBOT_TEST_PG_BACKEND set.
"""

from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from tests.migration.test_baseline_migration_db import _query, _state, _with_downgrade
from tests.support import pg

pytestmark = pytest.mark.db

IDENTITY_TABLES = {"telegram_identities", "onboarding_invites"}


def _functions(cluster: pg.ClusterInfo, database: str) -> set[str]:
    return {
        r[0]
        for r in _query(
            cluster,
            database,
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e' "
            "WHERE n.nspname = 'public' AND d.objid IS NULL",
        )
    }


def test_upgrade_to_head_adds_identity_tables_and_functions(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, "0001")
    baseline = _state(pg_cluster, test_database)
    command.upgrade(alembic_config, "head")
    state = _state(pg_cluster, test_database)
    assert state["version"] == ["0002"]
    assert state["tables"] == baseline["tables"] | IDENTITY_TABLES
    assert {
        "resolve_principal(bigint)",
        "redeem_onboarding_invite(bytea,bigint,text)",
    } <= _functions(pg_cluster, test_database)


def test_downgrade_to_0001_restores_the_baseline_exactly(
    alembic_config: Config,
    migration_settings: Any,
    pg_cluster: pg.ClusterInfo,
    test_database: str,
) -> None:
    command.upgrade(alembic_config, "0001")
    baseline = (_state(pg_cluster, test_database), _functions(pg_cluster, test_database))
    command.upgrade(alembic_config, "head")
    command.downgrade(_with_downgrade(alembic_config, migration_settings), "0001")
    assert (_state(pg_cluster, test_database), _functions(pg_cluster, test_database)) == baseline
    command.upgrade(alembic_config, "head")
    assert _state(pg_cluster, test_database)["version"] == ["0002"]


def test_head_downgrades_to_an_empty_database(
    alembic_config: Config,
    migration_settings: Any,
    pg_cluster: pg.ClusterInfo,
    test_database: str,
) -> None:
    pristine = _state(pg_cluster, test_database)
    command.upgrade(alembic_config, "head")
    command.downgrade(_with_downgrade(alembic_config, migration_settings), "base")
    assert _state(pg_cluster, test_database) == {
        **pristine,
        "tables": {"alembic_version"},
        "version": [],
    }
