"""``finbot-migrate`` entry point and connection targets (no database, no network)."""

import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from finbot.adapters.cli import migrate
from finbot.infra.db import connection
from finbot.infra.db.connection import ConnectionTarget, owner_target
from finbot.migration_settings import MigrationSettings
from tests.conftest import SENTINEL_SECRET

SecretFile = Callable[..., Path]


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = migrate.main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def no_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to create a database engine."""

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a database connection was attempted")

    monkeypatch.setattr(connection, "create_engine", refuse)


@pytest.mark.usefixtures("no_connections")
def test_check_config_prints_redacted_summary(
    monkeypatch: pytest.MonkeyPatch, secret_file: SecretFile
) -> None:
    path = secret_file(SENTINEL_SECRET, "db_owner_password")
    monkeypatch.setenv("FINBOT_ENV", "staging")
    monkeypatch.setenv("FINBOT_DB_OWNER_PASSWORD_FILE", str(path))
    code, out, err = _run("check-config", "--json")
    assert code == migrate.EXIT_OK, err
    summary = json.loads(out)
    assert summary["db_name"] == "finbot_staging"
    assert summary["db_owner_user"] == "finbot_owner"
    assert summary["downgrade_permitted"] is False
    assert SENTINEL_SECRET not in out + err


@pytest.mark.usefixtures("no_connections")
def test_missing_password_file_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "staging")
    code, _, err = _run("upgrade")
    assert code == migrate.EXIT_CONFIG
    assert "FINBOT_DB_OWNER_PASSWORD_FILE is required to connect" in err


@pytest.mark.usefixtures("no_connections")
def test_application_password_is_never_a_fallback(
    monkeypatch: pytest.MonkeyPatch, secret_file: SecretFile
) -> None:
    path = secret_file(SENTINEL_SECRET, "db_app_password")
    monkeypatch.setenv("FINBOT_ENV", "staging")
    monkeypatch.setenv("FINBOT_DB_APP_PASSWORD_FILE", str(path))
    code, out, err = _run("upgrade")
    assert code == migrate.EXIT_CONFIG
    assert "FINBOT_DB_APP_PASSWORD_FILE belongs to another process" in err
    assert SENTINEL_SECRET not in out + err


@pytest.mark.usefixtures("no_connections")
@pytest.mark.parametrize("env", ["staging", "production"])
def test_downgrade_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, secret_file: SecretFile, env: str
) -> None:
    monkeypatch.setenv("FINBOT_ENV", env)
    monkeypatch.setenv(
        "FINBOT_DB_OWNER_PASSWORD_FILE", str(secret_file(SENTINEL_SECRET, "db_owner_password"))
    )
    code, _, err = _run("downgrade", "base")
    assert code == migrate.EXIT_REFUSED
    assert "forward fixes" in err
    code, out, err = _run("sql", "downgrade", "head:base")
    assert code == migrate.EXIT_REFUSED
    assert out == ""


@pytest.mark.usefixtures("no_connections")
def test_downgrade_without_flag_refused_in_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "test")
    code, _, err = _run("sql", "downgrade", "head:base")
    assert code == migrate.EXIT_REFUSED
    assert "FINBOT_MIGRATION_ALLOW_DOWNGRADE=true" in err


@pytest.mark.usefixtures("no_connections")
def test_offline_sql_needs_no_password_and_is_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "staging")
    code, out, err = _run("sql", "upgrade", "head")
    assert code == migrate.EXIT_OK, err
    assert "$finbot_guard$" in out
    assert "'finbot_staging'" in out


@pytest.mark.usefixtures("no_connections")
def test_history_works_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    code, _, err = _run("history")
    assert code == migrate.EXIT_OK, err


@pytest.mark.usefixtures("no_connections")
def test_missing_ini_is_a_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    code, _, err = _run("--ini", str(tmp_path / "absent.ini"), "history")
    assert code == migrate.EXIT_CONFIG
    assert "alembic.ini not found" in err


def test_guard_refusal_detection_trims_to_the_reason() -> None:
    exc = RuntimeError(
        "(asyncpg) <class ...>: finbot-migrate guard: connected to database x\n[SQL: DO ...]"
    )
    assert migrate._is_guard_refusal(exc)
    assert not migrate._is_guard_refusal(RuntimeError("other"))


# ---------------------------------------------------------------------- connection target


def test_connection_target_keeps_password_out_of_url_and_repr(secret_file: SecretFile) -> None:
    settings = MigrationSettings(
        env="staging", db_owner_password_file=secret_file(SENTINEL_SECRET, "owner")
    )
    target = owner_target(settings)
    assert target.user == "finbot_owner"
    assert target.database == "finbot_staging"
    assert target.application_name == "finbot-migrate"
    url = target.url()
    for rendered in (str(url), repr(url), url.render_as_string(hide_password=False), repr(target)):
        assert SENTINEL_SECRET not in rendered
    assert url.password is None
    assert url.host is None  # host and port travel in connect_args
    args = target.connect_args()
    assert args["password"] == SENTINEL_SECRET
    assert args["host"] == "postgres"
    assert args["port"] == 5432


def test_owner_target_requires_the_password_file() -> None:
    settings = MigrationSettings(env="staging")
    with pytest.raises(Exception, match="FINBOT_DB_OWNER_PASSWORD_FILE is required"):
        owner_target(settings)


def test_engine_construction_does_not_connect(secret_file: SecretFile) -> None:
    from pydantic import SecretStr  # noqa: PLC0415

    target = ConnectionTarget("db.invalid", 5432, "finbot_dev", "finbot_owner", SecretStr("x" * 30))
    engine = connection.create_engine(target)
    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.url.password is None
