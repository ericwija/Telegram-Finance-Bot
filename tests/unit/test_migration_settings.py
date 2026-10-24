"""MigrationSettings and application/owner credential separation (decisions S9, S10)."""

from collections.abc import Callable
from pathlib import Path

import pytest

from finbot.config import ConfigurationError, load_settings
from finbot.migration_settings import (
    MigrationSettings,
    known_migration_env_vars,
    load_migration_settings,
)
from tests.conftest import SENTINEL_SECRET

SecretFile = Callable[..., Path]


@pytest.fixture
def owner_file(secret_file: SecretFile) -> Path:
    return secret_file(SENTINEL_SECRET, "db_owner_password")


def _set(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _problems(monkeypatch: pytest.MonkeyPatch, require_password: bool = True, **env: str) -> str:
    _set(monkeypatch, **env)
    with pytest.raises(ConfigurationError) as info:
        load_migration_settings(require_password=require_password)
    text = "; ".join(info.value.problems)
    assert SENTINEL_SECRET not in text
    return text


# ------------------------------------------------------------------------------ happy paths


def test_staging_settings_from_password_file(
    monkeypatch: pytest.MonkeyPatch, owner_file: Path
) -> None:
    _set(monkeypatch, FINBOT_ENV="staging", FINBOT_DB_OWNER_PASSWORD_FILE=str(owner_file))
    settings = load_migration_settings()
    assert settings.db_name == "finbot_staging"
    assert settings.db_owner_user == "finbot_owner"
    assert settings.require_owner_password().get_secret_value() == SENTINEL_SECRET
    assert settings.downgrade_permitted is False
    rendered = repr(settings) + str(settings.redacted_summary()) + settings.model_dump_json()
    assert SENTINEL_SECRET not in rendered
    assert settings.redacted_summary()["db_owner_password"] == f"set (file: {owner_file})"


def test_offline_mode_needs_no_password(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, FINBOT_ENV="production")
    settings = load_migration_settings(require_password=False)
    assert settings.db_name == "finbot_prod"
    assert settings.db_owner_password is None
    with pytest.raises(ConfigurationError, match="FINBOT_DB_OWNER_PASSWORD_FILE is required"):
        settings.require_owner_password()


def test_test_environment_accepts_per_test_databases_and_socket_dirs(owner_file: Path) -> None:
    settings = MigrationSettings(
        env="test",
        db_name="finbot_test_0123456789ab",
        db_host="/tmp/finbot-pg/run",  # noqa: S108 - a socket directory value, not a file
        db_owner_password_file=owner_file,
    )
    assert settings.db_name == "finbot_test_0123456789ab"


# ------------------------------------------------------------------- credential separation


def test_plaintext_owner_password_is_never_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_DB_OWNER_PASSWORD=SENTINEL_SECRET)
    assert "FINBOT_DB_OWNER_PASSWORD (plaintext) is never accepted" in text


def test_plaintext_owner_password_rejected_by_model_too() -> None:
    with pytest.raises(ValueError, match="plaintext"):
        MigrationSettings(env="dev", db_owner_password=SENTINEL_SECRET)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "foreign",
    [
        "FINBOT_DB_APP_PASSWORD_FILE",
        "FINBOT_DB_APP_PASSWORD",
        "FINBOT_DB_APP_USER",
        "FINBOT_TELEGRAM_TOKEN_FILE",
        "FINBOT_GEMINI_API_KEY_FILE",
        "FINBOT_DB_BACKUP_PASSWORD_FILE",
        "FINBOT_DB_SUPERUSER_PASSWORD_FILE",
    ],
)
def test_migrations_refuse_other_processes_credentials(
    monkeypatch: pytest.MonkeyPatch, owner_file: Path, foreign: str
) -> None:
    text = _problems(
        monkeypatch,
        FINBOT_ENV="staging",
        FINBOT_DB_OWNER_PASSWORD_FILE=str(owner_file),
        **{foreign: str(owner_file)},
    )
    assert f"{foreign} belongs to another process" in text


@pytest.mark.parametrize(
    "privileged",
    [
        "FINBOT_DB_OWNER_PASSWORD_FILE",
        "FINBOT_DB_OWNER_PASSWORD",
        "FINBOT_DB_OWNER_USER",
        "FINBOT_DB_BACKUP_PASSWORD_FILE",
        "FINBOT_DB_SUPERUSER_PASSWORD_FILE",
        "FINBOT_MIGRATION_ALLOW_DOWNGRADE",
    ],
)
def test_application_refuses_privileged_credentials(
    monkeypatch: pytest.MonkeyPatch, staging_env: dict[str, str], privileged: str
) -> None:
    monkeypatch.setenv(privileged, SENTINEL_SECRET)
    with pytest.raises(ConfigurationError) as info:
        load_settings()
    text = "; ".join(info.value.problems)
    assert f"{privileged} is a privileged database/migration setting" in text
    assert SENTINEL_SECRET not in text


def test_owner_role_cannot_be_changed(monkeypatch: pytest.MonkeyPatch, owner_file: Path) -> None:
    for role in ("finbot_app", "postgres", "finbot_backup"):
        text = _problems(
            monkeypatch,
            FINBOT_ENV="dev",
            FINBOT_DB_OWNER_USER=role,
            FINBOT_DB_OWNER_PASSWORD_FILE=str(owner_file),
        )
        assert "migrations always run as 'finbot_owner'" in text


def test_unknown_variable_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _problems(monkeypatch, False, FINBOT_ENV="dev", FINBOT_DB_OWNR_PASSWORD_FILE="/x")
    assert "FINBOT_DB_OWNR_PASSWORD_FILE is not a migration setting" in text


def test_known_variables_do_not_include_plaintext_password() -> None:
    known = known_migration_env_vars()
    assert "FINBOT_DB_OWNER_PASSWORD_FILE" in known
    assert "FINBOT_DB_OWNER_PASSWORD" not in known


# ------------------------------------------------------------------------- password files


def test_missing_password_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = _problems(
        monkeypatch, FINBOT_ENV="dev", FINBOT_DB_OWNER_PASSWORD_FILE=str(tmp_path / "absent")
    )
    assert "points to a file that does not exist" in text


def test_password_required_when_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _problems(monkeypatch, FINBOT_ENV="staging")
    assert "FINBOT_DB_OWNER_PASSWORD_FILE is required to connect" in text


def test_relative_password_file_rejected_even_in_dev(
    monkeypatch: pytest.MonkeyPatch, owner_file: Path
) -> None:
    monkeypatch.chdir(owner_file.parent)
    text = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_DB_OWNER_PASSWORD_FILE=owner_file.name)
    assert "must be an absolute path" in text


def test_world_readable_password_file_rejected_even_in_dev(
    monkeypatch: pytest.MonkeyPatch, secret_file: SecretFile
) -> None:
    path = secret_file(SENTINEL_SECRET, "owner_open", mode=0o644)
    text = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_DB_OWNER_PASSWORD_FILE=str(path))
    assert "must not be world-readable" in text


def test_empty_password_file_rejected(
    monkeypatch: pytest.MonkeyPatch, secret_file: SecretFile
) -> None:
    path = secret_file("\n", "owner_empty")
    text = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_DB_OWNER_PASSWORD_FILE=str(path))
    assert "empty file" in text


# ------------------------------------------------------------ environment / database rules


@pytest.mark.parametrize(
    ("env", "db"),
    [
        ("staging", "finbot_prod"),
        ("production", "finbot_staging"),
        ("dev", "finbot_test"),
        ("test", "finbot_staging"),
        ("test", "finbot_test_UPPER"),
        ("staging", "finbot_staging_extra"),
    ],
)
def test_database_must_match_environment(
    monkeypatch: pytest.MonkeyPatch, env: str, db: str
) -> None:
    text = _problems(monkeypatch, False, FINBOT_ENV=env, FINBOT_DB_NAME=db)
    assert "(G-ENV-2)" in text


@pytest.mark.parametrize("env", ["staging", "production", "drill"])
def test_socket_directory_host_only_in_dev_and_test(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    text = _problems(monkeypatch, False, FINBOT_ENV=env, FINBOT_DB_HOST="/var/run/postgresql")
    assert "socket directory only in dev and test" in text


# ------------------------------------------------------------------------- downgrades (S9)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_downgrade_flag_is_an_error_in_staging_and_production(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    text = _problems(monkeypatch, False, FINBOT_ENV=env, FINBOT_MIGRATION_ALLOW_DOWNGRADE="true")
    assert "use forward fixes (S9)" in text


@pytest.mark.parametrize(
    ("env", "flag", "permitted"),
    [
        ("dev", "true", True),
        ("test", "true", True),
        ("drill", "true", True),
        ("dev", "false", False),
        ("test", "false", False),
        ("drill", "false", False),
        ("staging", "false", False),
        ("production", "false", False),
    ],
)
def test_downgrade_permission_matrix(
    monkeypatch: pytest.MonkeyPatch, env: str, flag: str, permitted: bool
) -> None:
    _set(monkeypatch, FINBOT_ENV=env, FINBOT_MIGRATION_ALLOW_DOWNGRADE=flag)
    assert load_migration_settings(require_password=False).downgrade_permitted is permitted
