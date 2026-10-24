"""Online migration environment against a real disposable PostgreSQL 18.6 (docs/10 §3, §4).

Runs Alembic online through the repository's env.py, as ``finbot_owner``, on a per-test database:
the empty chain upgrades cleanly; the guard refuses a wrong database, a wrong role and a
mismatched environment marker; downgrades are refused without the opt-in and rolled back by the
single migration transaction. Runs only with FINBOT_TEST_PG_BACKEND set.
"""

import asyncio
import io
import shutil
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from finbot.infra.db.connection import create_engine
from finbot.infra.db.migration_runtime import CONNECTION_ATTRIBUTE, SETTINGS_ATTRIBUTE
from tests.support import pg

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]


def _scalar(engine: Any, sql: str) -> Any:
    async def run() -> Any:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar()

    return asyncio.run(run())


def _owner_engine(cluster: pg.ClusterInfo, database: str) -> Any:
    from tests.plugins.db import _target  # noqa: PLC0415

    return create_engine(_target(cluster, database, pg.OWNER_ROLE))


def _run_with(engine: Any, config: Config, fn: Any) -> None:
    """Run an Alembic command on an existing connection (as the CLI's own engine would)."""

    async def run() -> None:
        async with engine.connect() as conn:

            def go(sync_conn: Any) -> None:
                config.attributes[CONNECTION_ATTRIBUTE] = sync_conn
                fn(config)

            await conn.run_sync(go)
            await conn.commit()

    asyncio.run(run())


def test_empty_chain_upgrade_creates_owner_only_version_table(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    command.upgrade(alembic_config, "head")  # online path: env.py creates its own engine
    engine = _owner_engine(pg_cluster, test_database)
    try:
        assert (
            _scalar(
                engine,
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = 'public.alembic_version'::regclass",
            )
            == "finbot_owner"
        )
        assert (
            _scalar(
                engine,
                "SELECT has_table_privilege('finbot_app', 'public.alembic_version', 'SELECT')",
            )
            is False
        )
    finally:
        asyncio.run(engine.dispose())


def test_guard_refuses_a_different_database(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, migration_settings: Any
) -> None:
    other = asyncio.run(pg.create_test_database(pg_cluster))
    engine = _owner_engine(pg_cluster, other)  # connected to `other`, settings name the fixture DB
    try:
        with pytest.raises(Exception, match="finbot-migrate guard: connected to database"):
            _run_with(engine, alembic_config, lambda cfg: command.upgrade(cfg, "head"))
    finally:
        asyncio.run(engine.dispose())
        asyncio.run(pg.drop_test_database(pg_cluster, other))


def test_guard_refuses_the_app_role(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    from tests.plugins.db import _target  # noqa: PLC0415

    engine = create_engine(_target(pg_cluster, test_database, pg.APP_ROLE))
    try:
        with pytest.raises(Exception, match="finbot-migrate guard: must run as finbot_owner"):
            _run_with(engine, alembic_config, lambda cfg: command.upgrade(cfg, "head"))
    finally:
        asyncio.run(engine.dispose())


def test_guard_refuses_a_mismatched_environment_marker(
    alembic_config: Config, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    engine = _owner_engine(pg_cluster, test_database)

    async def plant() -> None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE public.platform_meta (key text PRIMARY KEY, value text NOT NULL)"
                )
            )
            await conn.execute(
                text("INSERT INTO public.platform_meta VALUES ('environment', 'staging')")
            )

    try:
        asyncio.run(plant())
        with pytest.raises(Exception, match="environment marker is not test"):
            command.upgrade(alembic_config, "head")
    finally:
        asyncio.run(engine.dispose())


def _synthetic_chain(tmp_path: Path, settings: Any) -> Config:
    scripts = tmp_path / "migrations"
    (scripts / "versions").mkdir(parents=True)
    shutil.copy(ROOT / "migrations" / "env.py", scripts / "env.py")
    shutil.copy(ROOT / "migrations" / "script.py.mako", scripts / "script.py.mako")
    (scripts / "versions" / "0001_synthetic.py").write_text(
        'from alembic import op\nrevision = "0001"\ndown_revision = None\n'
        "branch_labels = None\ndepends_on = None\n"
        "def upgrade():\n    op.execute('CREATE TABLE public.synthetic (id int)')\n"
        "def downgrade():\n    op.execute('DROP TABLE public.synthetic')\n"
    )
    ini = tmp_path / "alembic.ini"
    ini.write_text((ROOT / "alembic.ini").read_text().replace("%(here)s/migrations", str(scripts)))
    config = Config(str(ini), stdout=io.StringIO())
    config.attributes[SETTINGS_ATTRIBUTE] = settings
    return config


def test_downgrade_refused_without_opt_in_and_allowed_with_it(
    tmp_path: Path, migration_settings: Any, pg_cluster: pg.ClusterInfo, test_database: str
) -> None:
    config = _synthetic_chain(tmp_path, migration_settings)
    command.upgrade(config, "head")
    engine = _owner_engine(pg_cluster, test_database)
    try:
        with pytest.raises(Exception, match="forward fixes"):
            command.downgrade(config, "base")
        assert _scalar(engine, "SELECT to_regclass('public.synthetic') IS NOT NULL") is True
        assert _scalar(engine, "SELECT version_num FROM public.alembic_version") == "0001"

        config.attributes[SETTINGS_ATTRIBUTE] = migration_settings.model_copy(
            update={"migration_allow_downgrade": True}
        )
        command.downgrade(config, "base")
        assert _scalar(engine, "SELECT to_regclass('public.synthetic') IS NULL") is True
    finally:
        asyncio.run(engine.dispose())
