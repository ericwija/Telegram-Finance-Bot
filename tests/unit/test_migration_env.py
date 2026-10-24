"""Alembic environment policy, exercised through real Alembic commands in offline mode.

Each test builds a temporary script directory with a copy of the repository's ``migrations/env.py``
and synthetic revisions, then runs Alembic with ``sql=True`` (offline), so no database is needed.
Covers: guard SQL first, downgrade restrictions (S9) at every layer, autogenerate refusal and the
manual revision policy. Online behaviour is covered by tests/migration (database tests).
"""

import io
import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.config import main as alembic_main

from finbot.infra.db import migration_runtime as runtime
from finbot.infra.db.migration_runtime import (
    MigrationRefusedError,
    RevisionRecord,
    guard_sql,
    revision_policy_problems,
)
from finbot.migration_settings import MigrationSettings

ROOT = Path(__file__).resolve().parents[2]
REVISION = '''"""synthetic {rev}"""
from alembic import op

revision = "{rev}"
down_revision = {down!r}
branch_labels = {labels!r}
depends_on = None


def upgrade() -> None:
    op.execute("SELECT 'up {rev}'")


def downgrade() -> None:
    op.execute("SELECT 'down {rev}'")
'''

MakeScripts = Callable[..., Path]


@pytest.fixture
def make_scripts(tmp_path: Path) -> MakeScripts:
    """Create alembic.ini + migrations/ (real env.py) with the given revisions."""

    def _make(*revisions: tuple[str, str | None], filenames: dict[str, str] | None = None) -> Path:
        scripts = tmp_path / "migrations"
        (scripts / "versions").mkdir(parents=True)
        shutil.copy(ROOT / "migrations" / "env.py", scripts / "env.py")
        shutil.copy(ROOT / "migrations" / "script.py.mako", scripts / "script.py.mako")
        for rev, down in revisions:
            name = (filenames or {}).get(rev, f"{rev}_synthetic.py")
            (scripts / "versions" / name).write_text(
                REVISION.format(rev=rev, down=down, labels=None)
            )
        ini = tmp_path / "alembic.ini"
        ini.write_text(
            (ROOT / "alembic.ini").read_text().replace("%(here)s/migrations", str(scripts))
        )
        return ini

    return _make


def _config(ini: Path, settings: MigrationSettings) -> tuple[Config, io.StringIO]:
    out = io.StringIO()
    config = Config(str(ini), stdout=out, output_buffer=out)
    config.attributes[runtime.SETTINGS_ATTRIBUTE] = settings
    return config, out


def _settings(env: str = "staging", *, allow_downgrade: bool = False) -> MigrationSettings:
    return MigrationSettings(env=env, migration_allow_downgrade=allow_downgrade)  # type: ignore[arg-type]


TWO = (("0001", None), ("0002", "0001"))


# ------------------------------------------------------------------------------ upgrade


def test_offline_upgrade_emits_guard_before_any_migration(make_scripts: MakeScripts) -> None:
    config, out = _config(make_scripts(*TWO), _settings("staging"))
    command.upgrade(config, "head", sql=True)
    sql = out.getvalue()
    guard_at = sql.index("$finbot_guard$")
    assert sql.index("SET LOCAL lock_timeout = '10s'") < guard_at
    assert sql.index("pg_advisory_xact_lock") < guard_at
    assert guard_at < sql.index("CREATE TABLE public.alembic_version")
    assert sql.index("CREATE TABLE public.alembic_version") < sql.index("SELECT 'up 0001'")
    assert sql.index("SELECT 'up 0001'") < sql.index("SELECT 'up 0002'")
    assert sql.count("BEGIN;") == 1  # one transaction for the whole run
    assert sql.count("COMMIT;") == 1
    assert "'finbot_staging'" in sql
    assert "%" not in sql  # runs unchanged under psql (S4 fallback)


def test_repository_migration_chain_passes_policy() -> None:
    config, out = _config(ROOT / "alembic.ini", _settings("dev"))
    command.upgrade(config, "head", sql=True)
    assert "$finbot_guard$" in out.getvalue()


# ----------------------------------------------------------------------- downgrades (S9)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_offline_downgrade_refused_before_any_step(make_scripts: MakeScripts, env: str) -> None:
    config, out = _config(make_scripts(*TWO), _settings(env))
    with pytest.raises(MigrationRefusedError, match="forward fixes"):
        command.downgrade(config, "0002:0001", sql=True)
    sql = out.getvalue()
    assert "Running downgrade" not in sql
    assert "down 0002" not in sql


@pytest.mark.parametrize("env", ["dev", "test", "drill"])
def test_downgrade_needs_the_explicit_flag(make_scripts: MakeScripts, env: str) -> None:
    config, out = _config(make_scripts(*TWO), _settings(env))
    with pytest.raises(MigrationRefusedError):
        command.downgrade(config, "0002:0001", sql=True)
    assert "down 0002" not in out.getvalue()


@pytest.mark.parametrize("env", ["dev", "test", "drill"])
def test_downgrade_allowed_in_dev_test_drill_with_flag(make_scripts: MakeScripts, env: str) -> None:
    db = "finbot_test" if env == "test" else f"finbot_{env}"
    settings = MigrationSettings(env=env, db_name=db, migration_allow_downgrade=True)  # type: ignore[arg-type]
    config, out = _config(make_scripts(*TWO), settings)
    command.downgrade(config, "0002:0001", sql=True)
    sql = out.getvalue()
    assert sql.index("$finbot_guard$") < sql.index("SELECT 'down 0002'")


def test_alembic_cli_downgrade_and_stamp_refused_in_staging(
    make_scripts: MakeScripts, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ini = make_scripts(*TWO)
    monkeypatch.setenv("FINBOT_ENV", "staging")
    for argv in (["downgrade", "--sql", "0002:0001"], ["stamp", "--sql", "0001"]):
        with pytest.raises(MigrationRefusedError, match="refused in staging"):
            alembic_main(argv=["-c", str(ini), *argv])
    out = capsys.readouterr().out
    assert "down 0002" not in out
    assert "$finbot_guard$" not in out  # refused before any SQL was produced


def test_version_apply_backstop_refuses_downgrade_steps() -> None:
    callback = runtime._on_version_apply(_settings("staging"))
    callback(step=SimpleNamespace(is_upgrade=True), ctx=None, heads=set(), run_args={})
    with pytest.raises(MigrationRefusedError):
        callback(step=SimpleNamespace(is_upgrade=False), ctx=None, heads=set(), run_args={})


# ------------------------------------------------------------------ autogenerate / policy


def test_autogenerate_refused_and_no_file_written(
    make_scripts: MakeScripts, monkeypatch: pytest.MonkeyPatch
) -> None:
    ini = make_scripts(("0001", None))
    versions = ini.parent / "migrations" / "versions"
    before = sorted(p.name for p in versions.iterdir())
    monkeypatch.setenv("FINBOT_ENV", "dev")
    with pytest.raises(MigrationRefusedError, match="autogenerate is disabled"):
        alembic_main(
            argv=["-c", str(ini), "revision", "--autogenerate", "--rev-id", "0002", "-m", "x"]
        )
    assert sorted(p.name for p in versions.iterdir()) == before


@pytest.mark.parametrize(
    ("revisions", "filenames", "message"),
    [
        ((("0001", None), ("0003", "0001")), None, "contiguous from 0001"),
        ((("0001", None),), {"0001": "0001_Bad-Name.py"}, "file name must be NNNN_"),
        ((("0001", None), ("0002", None)), None, "down_revision must be '0001'"),
        ((("abc1", None),), {"abc1": "abc1_x.py"}, "is not NNNN"),
    ],
)
def test_revision_policy_violations_refuse_to_run(
    make_scripts: MakeScripts,
    revisions: tuple[tuple[str, str | None], ...],
    filenames: dict[str, str] | None,
    message: str,
) -> None:
    config, out = _config(make_scripts(*revisions, filenames=filenames), _settings("dev"))
    with pytest.raises(MigrationRefusedError, match=message):
        command.upgrade(config, "head", sql=True)
    assert "$finbot_guard$" not in out.getvalue()


def test_revision_policy_pure_rules() -> None:
    ok = [
        RevisionRecord("0001_platform.py", "0001", None),
        RevisionRecord("0002_identity_tenancy.py", "0002", "0001"),
    ]
    assert revision_policy_problems(ok) == []
    assert revision_policy_problems([]) == []
    merge = [*ok, RevisionRecord("0003_merge.py", "0003", ("0001", "0002"))]
    assert "merge revisions are not allowed" in " ".join(revision_policy_problems(merge))
    labelled = [RevisionRecord("0001_a.py", "0001", None, branch_labels=("x",))]
    assert "branch labels" in " ".join(revision_policy_problems(labelled))
    depends = [RevisionRecord("0001_a.py", "0001", None, depends_on=("x",))]
    assert "depends_on" in " ".join(revision_policy_problems(depends))
    mismatch = [RevisionRecord("0002_a.py", "0001", None)]
    assert "does not start with its revision id" in " ".join(revision_policy_problems(mismatch))


# ------------------------------------------------------------------------------ guard SQL


def test_guard_sql_checks_database_role_owner_and_marker() -> None:
    statements = guard_sql(_settings("staging"))
    assert len(statements) == 3
    guard = statements[2]
    for fragment in (
        "current_database() <> 'finbot_staging'",
        "session_user <> 'finbot_owner' OR current_user <> 'finbot_owner'",
        "rolsuper",
        "pg_get_userbyid(datdba)",
        "to_regclass('public.platform_meta')",
        "value = ''staging''",
    ):
        assert fragment in guard, fragment
    assert all("%" not in s and "$1" not in s for s in statements)


def test_guard_sql_refuses_unsafe_values() -> None:
    settings = _settings("dev").model_copy(update={"db_name": "finbot_dev'; DROP"})
    with pytest.raises(MigrationRefusedError, match="unsafe value"):
        guard_sql(settings)
