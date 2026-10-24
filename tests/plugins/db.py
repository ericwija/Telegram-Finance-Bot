"""pytest plugin for ``@pytest.mark.db`` tests (docs/10 §4).

Environment variables:

* ``FINBOT_TEST_PG_BACKEND`` = ``container`` (CI, pinned image) or ``userspace`` (development).
  Unset: database tests are skipped, with a loud summary line, unless one of the next two holds.
* ``FINBOT_REQUIRE_DB_TESTS=1`` (``make test-db``, CI database job): the backend must be set, at
  least one database test must be collected, and **any skipped database test fails the run**.
* ``CI=true``: a missing backend is an error, never a silent skip.

The hooks are imported into ``tests/conftest.py``; heavy imports (asyncpg, SQLAlchemy, Alembic,
testcontainers) happen only inside fixtures that actually run.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from tests.support import pg

DB_MARKER = "db"
BACKENDS = frozenset({"container", "userspace"})
_TRUE = frozenset({"1", "true", "yes", "on"})

#: Harness variables, captured once in pytest_configure: the autouse ``clean_finbot_env``
#: fixture removes every FINBOT_* variable before each test (so app-config tests stay clean).
HARNESS_VARS = (
    "FINBOT_TEST_PG_BACKEND",
    "FINBOT_TEST_PG_BIN",
    "FINBOT_TEST_PG_LIBDIR",
    "FINBOT_REQUIRE_DB_TESTS",
)
_HARNESS_ENV: dict[str, str] = {}


def harness_env(name: str) -> str:
    """A harness variable as captured at session start (falls back to the live value)."""
    if name in _HARNESS_ENV:
        return _HARNESS_ENV[name]
    return os.environ.get(name, "")


#: Node ids of skipped / executed database tests in this session (reset by pytest_configure).
_SKIPPED: list[str] = []
_RAN: list[str] = []


def _truthy(name: str) -> bool:
    value = harness_env(name) if name in HARNESS_VARS else os.environ.get(name, "")
    return value.strip().lower() in _TRUE


def db_backend() -> str | None:
    """The configured backend, or None. An unknown value is a usage error."""
    value = harness_env("FINBOT_TEST_PG_BACKEND").strip().lower()
    if value in {"", "none"}:
        return None
    if value not in BACKENDS:
        raise pytest.UsageError(
            f"FINBOT_TEST_PG_BACKEND={value!r}: expected one of {sorted(BACKENDS)}"
        )
    return value


def db_tests_required() -> bool:
    return _truthy("FINBOT_REQUIRE_DB_TESTS")


def _skip_is_failure() -> bool:
    """Skipping a database test is allowed only without a backend, outside CI, when not required."""
    return db_tests_required() or _truthy("CI") or db_backend() is not None


# ------------------------------------------------------------------------------------- hooks


def pytest_configure(config: pytest.Config) -> None:
    _HARNESS_ENV.clear()
    _HARNESS_ENV.update({name: os.environ.get(name, "") for name in HARNESS_VARS})
    _SKIPPED.clear()
    _RAN.clear()


@pytest.hookimpl(trylast=True)  # after `-m`/`-k` deselection: judge only selected tests
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    db_items = [item for item in items if item.get_closest_marker(DB_MARKER) is not None]
    backend = db_backend()
    required = db_tests_required()
    if required and not db_items:
        raise pytest.UsageError(
            "FINBOT_REQUIRE_DB_TESTS=1 but no @pytest.mark.db tests were collected"
        )
    if backend is None and db_items:
        if required or _truthy("CI"):
            raise pytest.UsageError(
                "database tests are required here but FINBOT_TEST_PG_BACKEND is not set "
                "(container in CI, userspace in development; docs/10 §4)"
            )
        reason = "no database backend: set FINBOT_TEST_PG_BACKEND (docs/10 §4)"
        for item in db_items:
            item.add_marker(pytest.mark.skip(reason=reason))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if DB_MARKER not in report.keywords:
        return
    if report.skipped and report.nodeid not in _SKIPPED:
        _SKIPPED.append(report.nodeid)
    elif report.when == "call" and report.nodeid not in _RAN:
        _RAN.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    skipped = list(_SKIPPED)
    if skipped and _skip_is_failure() and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    skipped = list(_SKIPPED)
    if not skipped:
        if _RAN:
            terminalreporter.write_line(f"database tests: {len(_RAN)} executed, none skipped")
        return
    if _skip_is_failure():
        terminalreporter.write_line(
            f"FAILED: {len(skipped)} database test(s) were skipped, which is not allowed here:",
            red=True,
            bold=True,
        )
        for nodeid in skipped:
            terminalreporter.write_line(f"  {nodeid}", red=True)
    else:
        terminalreporter.write_line(
            f"NOTE: {len(skipped)} database test(s) SKIPPED (no database backend). They have NOT "
            "run; use `make test-db` with a backend before relying on database behaviour.",
            yellow=True,
            bold=True,
        )


# ---------------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def pg_cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[pg.ClusterInfo]:
    """One disposable PostgreSQL 18.6 cluster per test session."""
    backend = db_backend()
    if backend is None:  # pragma: no cover - collection already skipped or failed these tests
        pytest.fail("pg_cluster requested without FINBOT_TEST_PG_BACKEND")
    base = Path(tmp_path_factory.mktemp("pgparent", numbered=True)) / "cluster"
    cluster = pg.make_cluster(
        backend,
        base,
        bindir=harness_env("FINBOT_TEST_PG_BIN"),
        libdir=harness_env("FINBOT_TEST_PG_LIBDIR"),
    )
    try:
        info = cluster.start()
    except BaseException:
        cluster.stop()
        raise
    try:
        yield info
    finally:
        cluster.stop()


@pytest.fixture
def test_database(pg_cluster: pg.ClusterInfo) -> Iterator[str]:
    """A fresh, empty database for one test, dropped afterwards."""
    name = asyncio.run(pg.create_test_database(pg_cluster))
    try:
        yield name
    finally:
        asyncio.run(pg.drop_test_database(pg_cluster, name))


@pytest.fixture
def migration_settings(pg_cluster: pg.ClusterInfo, test_database: str) -> Any:
    """Owner-role MigrationSettings for the per-test database (test environment)."""
    from finbot.migration_settings import MigrationSettings  # noqa: PLC0415

    return MigrationSettings(
        env="test",
        db_host=pg_cluster.host,
        db_port=pg_cluster.port,
        db_name=test_database,
        db_owner_password_file=pg_cluster.password_file(pg.OWNER_ROLE),
    )


def _target(cluster: pg.ClusterInfo, database: str, role: str) -> Any:
    from pydantic import SecretStr  # noqa: PLC0415

    from finbot.infra.db.connection import ConnectionTarget  # noqa: PLC0415

    return ConnectionTarget(
        host=cluster.host,
        port=cluster.port,
        database=database,
        user=role,
        password=SecretStr(cluster.password(role)),
        application_name=f"finbot-tests-{role}",
    )


@pytest_asyncio.fixture
async def owner_engine(pg_cluster: pg.ClusterInfo, test_database: str) -> AsyncIterator[Any]:
    """Async engine connected as finbot_owner (schema setup and assertions only)."""
    from finbot.infra.db.connection import create_engine  # noqa: PLC0415

    engine = create_engine(_target(pg_cluster, test_database, pg.OWNER_ROLE))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(pg_cluster: pg.ClusterInfo, test_database: str) -> AsyncIterator[Any]:
    """Async engine connected as finbot_app: what every tenant-data test must use."""
    from finbot.infra.db.connection import create_engine  # noqa: PLC0415

    engine = create_engine(_target(pg_cluster, test_database, pg.APP_ROLE))
    yield engine
    await engine.dispose()


@pytest.fixture
def alembic_config(migration_settings: Any) -> Any:
    """Alembic config for the repository's migrations, bound to the per-test database."""
    import io  # noqa: PLC0415

    from finbot.adapters.cli.migrate import DEFAULT_INI, alembic_config  # noqa: PLC0415

    return alembic_config(DEFAULT_INI, migration_settings, io.StringIO())


@pytest.fixture
def migrated_database(alembic_config: Any, test_database: str) -> str:
    """The per-test database upgraded to head by the repository's migrations, as finbot_owner."""
    from alembic import command  # noqa: PLC0415

    command.upgrade(alembic_config, "head")
    return test_database
