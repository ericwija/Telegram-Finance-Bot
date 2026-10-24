"""AT-CFG-01..05 (docs/07 §5): typed configuration, *_FILE secrets, environment rules."""

from collections.abc import Callable
from pathlib import Path

import pytest

from finbot.config import (
    ConfigurationError,
    SecretSource,
    Settings,
    known_env_vars,
    load_settings,
)
from tests.conftest import SENTINEL_SECRET, fake_telegram_token


def _problems(monkeypatch: pytest.MonkeyPatch, **env: str) -> list[str]:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError) as info:
        load_settings()
    return info.value.problems


# --------------------------------------------------------------------------- AT-CFG-01
def test_AT_CFG_01_env_is_required_and_named() -> None:
    with pytest.raises(ConfigurationError) as info:
        load_settings()
    assert any(p.startswith("FINBOT_ENV:") for p in info.value.problems)


def test_AT_CFG_01_minimal_dev_config_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    settings = load_settings()
    assert settings.env == "dev"
    assert settings.db_name == "finbot_dev"
    assert settings.default_timezone == "Asia/Jakarta"
    assert settings.telegram_token is None
    assert settings.ai_enabled is False


def test_AT_CFG_01_does_not_read_dotenv_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("FINBOT_ENV=dev\nTELEGRAM_TOKEN=x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError):
        load_settings()


def test_AT_CFG_01_ignores_v1_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    monkeypatch.setenv("TELEGRAM_TOKEN", fake_telegram_token())  # V1 name, must be ignored
    assert load_settings().telegram_token is None


# --------------------------------------------------------------------------- AT-CFG-02
@pytest.mark.parametrize("value", ["prod", "PRODUCTIONX", "local", ""])
def test_AT_CFG_02_unknown_environment_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV=value)
    assert any("FINBOT_ENV" in p for p in problems)


def test_AT_CFG_02_environment_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "Dev")
    assert load_settings().env == "dev"


def test_AT_CFG_02_unknown_finbot_variable_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_TELEGRAM_TOKN_FILE="/x")
    assert problems == ["unknown setting FINBOT_TELEGRAM_TOKN_FILE"]


def test_AT_CFG_02_secret_sources_cannot_be_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_SECRET_SOURCES='{"x": "file"}')
    assert problems == ["unknown setting FINBOT_SECRET_SOURCES"]


@pytest.mark.parametrize(
    ("env", "db_name"),
    [("staging", "finbot_prod"), ("production", "finbot_staging"), ("dev", "finbot_test")],
)
def test_AT_CFG_02_db_name_must_match_environment(
    monkeypatch: pytest.MonkeyPatch, env: str, db_name: str
) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV=env, FINBOT_DB_NAME=db_name)
    assert any("G-ENV-2" in p for p in problems)


def test_AT_CFG_02_production_db_name_default(
    monkeypatch: pytest.MonkeyPatch, staging_env: dict[str, str]
) -> None:
    monkeypatch.setenv("FINBOT_ENV", "production")
    monkeypatch.setenv("FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME", "finbot_bot")
    assert load_settings().db_name == "finbot_prod"


@pytest.mark.parametrize("user", ["postgres", "finbot_owner", "FINBOT_BACKUP"])
def test_AT_CFG_02_privileged_db_user_rejected(monkeypatch: pytest.MonkeyPatch, user: str) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_DB_APP_USER=user)
    assert any("privileged" in p for p in problems)


def test_AT_CFG_02_invalid_timezone_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    problems = _problems(
        monkeypatch, FINBOT_ENV="dev", FINBOT_DEFAULT_TIMEZONE="Mars/Base", FINBOT_DB_PORT="70000"
    )
    assert any(p.startswith("FINBOT_DEFAULT_TIMEZONE") for p in problems)
    assert any(p.startswith("FINBOT_DB_PORT") for p in problems)


@pytest.mark.parametrize("host", ["postgres://x", "db host", "db:5432", "-bad"])
def test_AT_CFG_02_invalid_db_host(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_DB_HOST=host)
    assert any(p.startswith("FINBOT_DB_HOST") for p in problems)


@pytest.mark.parametrize("name", ["finance", "not_a_b0t", "bot", "x" * 40 + "bot", "1finbot_bot"])
def test_AT_CFG_02_invalid_bot_username(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME=name)
    assert any("bot username" in p for p in problems)


def test_AT_CFG_02_bot_username_at_prefix_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    monkeypatch.setenv("FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME", "@finbot_dev_bot")
    assert load_settings().telegram_expected_bot_username == "finbot_dev_bot"


def test_AT_CFG_02_feature_flag_dependencies(
    monkeypatch: pytest.MonkeyPatch, secret_file: Callable[..., Path]
) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV="dev", FINBOT_RECEIPTS_ENABLED="true")
    assert any("requires FINBOT_AI_ENABLED" in p for p in problems)

    monkeypatch.delenv("FINBOT_RECEIPTS_ENABLED")
    problems = _problems(monkeypatch, FINBOT_AI_ENABLED="true")
    assert any("FINBOT_GEMINI_API_KEY_FILE is required" in p for p in problems)

    monkeypatch.setenv("FINBOT_GEMINI_API_KEY_FILE", str(secret_file("fake-gemini-key")))
    assert load_settings().ai_enabled is True


# --------------------------------------------------------------------------- AT-CFG-03
def test_AT_CFG_03_staging_rejects_plaintext_secrets(
    monkeypatch: pytest.MonkeyPatch, staging_env: dict[str, str]
) -> None:
    monkeypatch.delenv("FINBOT_DB_APP_PASSWORD_FILE")
    problems = _problems(monkeypatch, FINBOT_DB_APP_PASSWORD=SENTINEL_SECRET)
    assert any("plaintext" in p and "G-ENV-5" in p for p in problems)
    assert all(SENTINEL_SECRET not in p for p in problems)


def test_AT_CFG_03_staging_requires_secrets_and_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problems = _problems(monkeypatch, FINBOT_ENV="staging")
    joined = " ".join(problems)
    assert "FINBOT_DB_APP_PASSWORD_FILE is required" in joined
    assert "FINBOT_TELEGRAM_TOKEN_FILE is required" in joined
    assert "FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME is required" in joined


def test_AT_CFG_03_plain_and_file_conflict_rejected(
    monkeypatch: pytest.MonkeyPatch, secret_file: Callable[..., Path]
) -> None:
    problems = _problems(
        monkeypatch,
        FINBOT_ENV="dev",
        FINBOT_DB_APP_PASSWORD=SENTINEL_SECRET,
        FINBOT_DB_APP_PASSWORD_FILE=str(secret_file(SENTINEL_SECRET)),
    )
    assert any("not both" in p for p in problems)
    assert all(SENTINEL_SECRET not in p for p in problems)


def test_AT_CFG_03_dev_allows_plaintext_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    monkeypatch.setenv("FINBOT_DB_APP_PASSWORD", SENTINEL_SECRET)
    settings = load_settings()
    assert settings.secret_sources["db_app_password"] is SecretSource.ENV


def test_AT_CFG_03_valid_staging_loads_file_secrets(staging_env: dict[str, str]) -> None:
    settings = load_settings()
    assert settings.env == "staging"
    assert settings.db_name == "finbot_staging"
    assert settings.db_app_password is not None
    assert settings.db_app_password.get_secret_value() == SENTINEL_SECRET
    assert settings.secret_sources["telegram_token"] is SecretSource.FILE


# --------------------------------------------------------------------------- AT-CFG-04
def test_AT_CFG_04_secret_file_whitespace_trimmed(
    monkeypatch: pytest.MonkeyPatch, secret_file: Callable[..., Path]
) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    monkeypatch.setenv("FINBOT_DB_APP_PASSWORD_FILE", str(secret_file(SENTINEL_SECRET + "\n")))
    password = load_settings().db_app_password
    assert password is not None
    assert password.get_secret_value() == SENTINEL_SECRET


@pytest.mark.parametrize(
    ("content", "mode", "expected"),
    [
        ("", 0o600, "empty file"),
        ("   \n", 0o600, "empty file"),
        (SENTINEL_SECRET, 0o644, "world-readable"),
        (SENTINEL_SECRET, 0o602, "world-writable"),
    ],
)
def test_AT_CFG_04_bad_secret_files_rejected_in_staging(
    monkeypatch: pytest.MonkeyPatch,
    staging_env: dict[str, str],
    secret_file: Callable[..., Path],
    content: str,
    mode: int,
    expected: str,
) -> None:
    path = secret_file(content, "bad_secret", mode)
    problems = _problems(monkeypatch, FINBOT_DB_APP_PASSWORD_FILE=str(path))
    assert any(expected in p for p in problems)
    assert all(SENTINEL_SECRET not in p for p in problems)


def test_AT_CFG_04_missing_directory_and_relative_paths(
    monkeypatch: pytest.MonkeyPatch, staging_env: dict[str, str], tmp_path: Path
) -> None:
    problems = _problems(monkeypatch, FINBOT_DB_APP_PASSWORD_FILE=str(tmp_path / "nope"))
    assert any("does not exist" in p for p in problems)
    problems = _problems(monkeypatch, FINBOT_DB_APP_PASSWORD_FILE=str(tmp_path))
    assert any("regular file" in p for p in problems)
    problems = _problems(monkeypatch, FINBOT_DB_APP_PASSWORD_FILE="relative/secret")
    assert any("absolute path" in p for p in problems)


def test_AT_CFG_04_world_readable_allowed_in_dev(
    monkeypatch: pytest.MonkeyPatch, secret_file: Callable[..., Path]
) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    monkeypatch.setenv("FINBOT_DB_APP_PASSWORD_FILE", str(secret_file(SENTINEL_SECRET, mode=0o644)))
    assert load_settings().db_app_password is not None


def test_AT_CFG_04_malformed_telegram_token_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch, staging_env: dict[str, str], secret_file: Callable[..., Path]
) -> None:
    path = secret_file(SENTINEL_SECRET, "telegram_token")
    problems = _problems(monkeypatch, FINBOT_TELEGRAM_TOKEN_FILE=str(path))
    assert any("does not look like a bot token" in p for p in problems)
    assert all(SENTINEL_SECRET not in p for p in problems)


# --------------------------------------------------------------------------- AT-CFG-05
def test_AT_CFG_05_secrets_masked_everywhere(staging_env: dict[str, str]) -> None:
    settings = load_settings()
    token = fake_telegram_token()
    rendered = [
        repr(settings),
        str(settings),
        settings.model_dump_json(),
        str(settings.model_dump()),
        str(settings.redacted_summary()),
    ]
    for text in rendered:
        assert SENTINEL_SECRET not in text
        assert token not in text
    summary = settings.redacted_summary()
    assert str(summary["db_app_password"]).startswith("set (file: ")
    assert summary["gemini_api_key"] == "not set"


def test_AT_CFG_05_validation_errors_hide_input(
    monkeypatch: pytest.MonkeyPatch, staging_env: dict[str, str]
) -> None:
    problems = _problems(monkeypatch, FINBOT_DB_PORT="not-a-port-" + SENTINEL_SECRET)
    assert any(p.startswith("FINBOT_DB_PORT") for p in problems)
    assert all(SENTINEL_SECRET not in p for p in problems)


def test_AT_CFG_05_settings_are_immutable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "dev")
    settings = load_settings()
    with pytest.raises(ValueError, match="frozen"):
        settings.env = "production"  # type: ignore[misc]


def test_known_env_vars_cover_all_fields() -> None:
    names = known_env_vars()
    assert "FINBOT_ENV" in names
    assert "FINBOT_TELEGRAM_TOKEN_FILE" in names
    assert "FINBOT_SECRET_SOURCES" not in names
    assert len(names) == len(Settings.model_fields) - 1
