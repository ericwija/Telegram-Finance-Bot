"""Revision 0003 lifecycle and ``finbot-migrate bootstrap-admin`` end to end (Step 4B).

The CLI runs exactly as the operator would run it: owner password from a file, FINBOT_ENV and
the database from the environment. Runs only with FINBOT_TEST_PG_BACKEND set.
"""

import io
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from finbot.adapters.cli import migrate
from tests.migration.test_baseline_migration_db import _query, _state, _with_downgrade
from tests.migration.test_identity_migration_db import _functions
from tests.support import pg

pytestmark = pytest.mark.db

ADMIN_TG = "900000001"


@pytest.fixture
def owner_env(
    monkeypatch: pytest.MonkeyPatch, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    monkeypatch.setenv("FINBOT_ENV", "test")
    monkeypatch.setenv("FINBOT_DB_HOST", pg_cluster.host)
    monkeypatch.setenv("FINBOT_DB_PORT", str(pg_cluster.port))
    monkeypatch.setenv("FINBOT_DB_NAME", test_database)
    monkeypatch.setenv(
        "FINBOT_DB_OWNER_PASSWORD_FILE", str(pg_cluster.password_file(pg.OWNER_ROLE))
    )


def _cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = migrate.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_bootstrap_cli_runs_once_and_prints_no_secret(
    owner_env: None, migrated_database: str, pg_cluster: pg.ClusterInfo
) -> None:
    args = ("bootstrap-admin", "--telegram-user-id", ADMIN_TG, "--display-name", "Operator")
    code, out, err = _cli(*args)
    assert (code, err) == (migrate.EXIT_OK, "")
    assert out.startswith("platform admin bootstrapped: user ")
    password = pg_cluster.password(pg.OWNER_ROLE)
    code, out2, err2 = _cli(*args)
    assert code == migrate.EXIT_REFUSED
    assert "already completed" in err2
    for text in (out, err, out2, err2):
        assert password not in text
    admins = _query(
        pg_cluster, migrated_database, "SELECT count(*) FROM users WHERE is_platform_admin"
    )
    assert admins[0][0] == 1


def test_bootstrap_cli_needs_revision_0003(
    owner_env: None, alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, "0002")
    code, _, err = _cli("bootstrap-admin", "--telegram-user-id", ADMIN_TG, "--display-name", "Op")
    assert code == migrate.EXIT_REFUSED
    assert "revision 0003" in err
    assert _query(pg_cluster, test_database, "SELECT count(*) FROM users")[0][0] == 0


def test_upgrade_does_not_bootstrap_anything(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, "head")
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    assert _state(pg_cluster, test_database)["version"] == [head]
    assert _query(pg_cluster, test_database, "SELECT count(*) FROM users")[0][0] == 0
    keys = _query(pg_cluster, test_database, "SELECT key FROM platform_meta")
    assert [k[0] for k in keys] == ["environment"]


def test_0003_downgrades_to_0002_exactly_and_keeps_the_bootstrap_marker(
    owner_env: None,
    alembic_config: Config,
    migration_settings: Any,
    pg_cluster: pg.ClusterInfo,
    test_database: str,
) -> None:
    command.upgrade(alembic_config, "0002")
    before = (_state(pg_cluster, test_database), _functions(pg_cluster, test_database))
    command.upgrade(alembic_config, "head")
    assert _cli("bootstrap-admin", "--telegram-user-id", ADMIN_TG, "--display-name", "Op")[0] == 0
    command.downgrade(_with_downgrade(alembic_config, migration_settings), "0002")
    assert (_state(pg_cluster, test_database), _functions(pg_cluster, test_database)) == before
    command.upgrade(alembic_config, "head")
    # Re-upgrading does not reopen the one-time bootstrap.
    code, _, err = _cli("bootstrap-admin", "--telegram-user-id", "900000002", "--display-name", "X")
    assert code == migrate.EXIT_REFUSED
    assert "already completed" in err
