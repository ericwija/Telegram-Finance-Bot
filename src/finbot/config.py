"""Typed, validated V2 configuration.

Specification: docs/06-IMPLEMENTATION-ROADMAP.md §6.1 item 7, docs/04-LOGICAL-ARCHITECTURE.md §8.3
(startup guards G-ENV-1, G-ENV-2 static part, G-ENV-5) and docs/09 §7 (secrets).

Rules:

* Every variable uses the ``FINBOT_`` prefix, so V1's ``TELEGRAM_TOKEN`` and other V1 variables
  are never picked up by V2.
* No ``.env`` file is read implicitly. Configuration comes only from the process environment.
* Each secret may be given as ``FINBOT_<NAME>`` (plaintext; dev/test only) or
  ``FINBOT_<NAME>_FILE`` (path to a file such as a Docker secret). Setting both is an error.
* Secret values never appear in ``repr``, ``str``, serialisation, validation errors or the
  redacted summary.
* Nothing in this module runs at import time. Call :func:`load_settings` explicitly.
* Validation performs local file reads (secret files, timezone data) only and never touches the
  network.
"""

import os
import re
import stat
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "FINBOT_"

Environment = Literal["dev", "test", "staging", "production", "drill"]

#: Environments that must use file-based secrets and full configuration (G-ENV-5).
STRICT_ENVIRONMENTS: frozenset[str] = frozenset({"staging", "production", "drill"})

#: Expected database name per environment (G-ENV-2, static part).
DB_NAME_BY_ENV: Mapping[str, str] = {
    "dev": "finbot_dev",
    "test": "finbot_test",
    "staging": "finbot_staging",
    "production": "finbot_prod",
    "drill": "finbot_drill",
}

#: The runtime must never connect as a privileged role (NFR-SEC-03, docs/05 §8).
FORBIDDEN_APP_DB_USERS: frozenset[str] = frozenset({"postgres", "finbot_owner", "finbot_backup"})

#: Names of secret settings. Each has a companion ``<name>_file`` setting.
SECRET_FIELDS: tuple[str, ...] = ("db_app_password", "telegram_token", "gemini_api_key")

MAX_SECRET_FILE_BYTES = 64 * 1024

_TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,}$")
# Telegram bot usernames: 5-32 characters, letters/digits/underscore, must end in "bot".
_BOT_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,28}[Bb][Oo][Tt]$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class SecretSource(StrEnum):
    """Where a secret value came from (reported in the redacted summary, never the value)."""

    UNSET = "unset"
    ENV = "env"
    FILE = "file"


class ConfigurationError(Exception):
    """Configuration is invalid. ``problems`` never contains secret values."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def env_var_name(field_name: str) -> str:
    """Return the environment variable that sets ``field_name``."""
    return f"{ENV_PREFIX}{field_name.upper()}"


def _read_secret_file(raw_path: str, *, strict: bool, var_name: str) -> str:
    """Read one secret from a file. Error messages mention the variable and path, never content."""
    path = Path(raw_path)
    if strict and not path.is_absolute():
        raise ValueError(f"{var_name} must be an absolute path in this environment")
    try:
        st = path.stat()  # follows symlinks (Docker secrets may be mounted that way)
    except FileNotFoundError:
        raise ValueError(f"{var_name} points to a file that does not exist: {path}") from None
    except OSError as exc:
        raise ValueError(f"{var_name} cannot be accessed: {path} ({exc.strerror})") from None
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"{var_name} must point to a regular file: {path}")
    if st.st_mode & stat.S_IWOTH:
        raise ValueError(f"{var_name} must not be world-writable: {path}")
    if strict and st.st_mode & (stat.S_IROTH | stat.S_IXOTH):
        raise ValueError(f"{var_name} must not be world-readable in this environment: {path}")
    if st.st_size > MAX_SECRET_FILE_BYTES:
        raise ValueError(f"{var_name} is larger than {MAX_SECRET_FILE_BYTES} bytes: {path}")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError(f"{var_name} is not valid UTF-8 text: {path}") from None
    except OSError as exc:
        raise ValueError(f"{var_name} cannot be read: {path} ({exc.strerror})") from None
    if not value:
        raise ValueError(f"{var_name} points to an empty file: {path}")
    return value


class Settings(BaseSettings):
    """V2 runtime settings. Construct through :func:`load_settings`."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=None,  # never read .env implicitly (the repo root .env belongs to V1)
        secrets_dir=None,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,  # validation errors must never echo secret input
        validate_default=True,
    )

    # --- environment & logging -----------------------------------------------------------
    env: Environment = Field(description="Deployment environment (G-ENV-1); no default.")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    default_timezone: str = "Asia/Jakarta"

    # --- database (connection happens in later steps; validated statically here) --------
    db_host: str = "postgres"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(default="", description="Defaults to the environment's expected name.")
    db_app_user: str = "finbot_app"
    db_app_password: SecretStr | None = None
    db_app_password_file: Path | None = None

    # --- telegram ----------------------------------------------------------------------------
    telegram_token: SecretStr | None = None
    telegram_token_file: Path | None = None
    telegram_expected_bot_username: str | None = None

    # --- gemini ------------------------------------------------------------------------------
    gemini_api_key: SecretStr | None = None
    gemini_api_key_file: Path | None = None

    # --- feature flags (docs/06 §3) -------------------------------------------------------
    ai_enabled: bool = False
    receipts_enabled: bool = False
    export_enabled: bool = False
    invites_enabled: bool = False

    # Computed by the resolver below; any value supplied from outside is discarded.
    secret_sources: dict[str, SecretSource] = Field(default_factory=dict, exclude=True, repr=False)

    # ------------------------------------------------------------------ input resolution
    @model_validator(mode="before")
    @classmethod
    def _resolve_secrets(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values: dict[str, Any] = {k: v for k, v in data.items() if v != ""}
        env = str(values.get("env", "")).strip().lower()
        strict = env in STRICT_ENVIRONMENTS
        sources: dict[str, SecretSource] = {}
        for name in SECRET_FIELDS:
            plain = values.get(name)
            file_path = values.get(f"{name}_file")
            if plain is not None and file_path is not None:
                raise ValueError(
                    f"set either {env_var_name(name)} or {env_var_name(name + '_file')}, not both"
                )
            if file_path is not None:
                values[name] = _read_secret_file(
                    str(file_path), strict=strict, var_name=env_var_name(name + "_file")
                )
                sources[name] = SecretSource.FILE
            elif plain is not None:
                sources[name] = SecretSource.ENV
            else:
                sources[name] = SecretSource.UNSET
        values["secret_sources"] = sources
        if "db_name" not in values and env in DB_NAME_BY_ENV:
            values["db_name"] = DB_NAME_BY_ENV[env]
        return values

    # ------------------------------------------------------------------ field validation
    @field_validator("env", mode="before")
    @classmethod
    def _lowercase_env(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("default_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown IANA timezone: {value!r}") from None
        return value

    @field_validator("db_host")
    @classmethod
    def _valid_host(cls, value: str) -> str:
        if not _HOSTNAME_RE.fullmatch(value):
            raise ValueError("must be a hostname or IP address without scheme, port or spaces")
        return value

    @field_validator("db_app_user")
    @classmethod
    def _unprivileged_db_user(cls, value: str) -> str:
        if value.lower() in FORBIDDEN_APP_DB_USERS:
            raise ValueError("the runtime must not connect as a privileged database role")
        return value

    @field_validator("telegram_expected_bot_username")
    @classmethod
    def _valid_bot_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.removeprefix("@")
        if not _BOT_USERNAME_RE.fullmatch(value):
            raise ValueError("must be a Telegram bot username (5-32 chars, ending in 'bot')")
        return value

    # ------------------------------------------------------------------ cross-field rules
    @model_validator(mode="after")
    def _check_environment_rules(self) -> Self:
        problems: list[str] = []
        strict = self.env in STRICT_ENVIRONMENTS

        expected_db = DB_NAME_BY_ENV[self.env]
        if self.db_name != expected_db:
            problems.append(
                f"{env_var_name('db_name')} must be {expected_db!r} when "
                f"{env_var_name('env')}={self.env} (G-ENV-2)"
            )

        if strict:
            for name in SECRET_FIELDS:
                if self.secret_sources.get(name) is SecretSource.ENV:
                    problems.append(
                        f"{env_var_name(name)} (plaintext) is not allowed in {self.env}; use "
                        f"{env_var_name(name + '_file')} (G-ENV-5)"
                    )
            for name in ("db_app_password", "telegram_token"):
                if getattr(self, name) is None:
                    problems.append(f"{env_var_name(name + '_file')} is required in {self.env}")
            if self.telegram_expected_bot_username is None:
                problems.append(
                    f"{env_var_name('telegram_expected_bot_username')} is required in {self.env}"
                )

        if self.telegram_token is not None and not _TELEGRAM_TOKEN_RE.fullmatch(
            self.telegram_token.get_secret_value()
        ):
            problems.append(f"{env_var_name('telegram_token')} does not look like a bot token")

        if self.ai_enabled and self.gemini_api_key is None:
            problems.append(
                f"{env_var_name('gemini_api_key_file')} is required when "
                f"{env_var_name('ai_enabled')}=true"
            )
        if self.receipts_enabled and not self.ai_enabled:
            problems.append(
                f"{env_var_name('receipts_enabled')}=true requires "
                f"{env_var_name('ai_enabled')}=true"
            )

        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------ presentation
    def redacted_summary(self) -> dict[str, str | int | bool | None]:
        """Settings safe to print or log: secrets are reported only as their source."""
        summary: dict[str, str | int | bool | None] = {
            "env": self.env,
            "log_level": self.log_level,
            "default_timezone": self.default_timezone,
            "db_host": self.db_host,
            "db_port": self.db_port,
            "db_name": self.db_name,
            "db_app_user": self.db_app_user,
            "telegram_expected_bot_username": self.telegram_expected_bot_username,
            "ai_enabled": self.ai_enabled,
            "receipts_enabled": self.receipts_enabled,
            "export_enabled": self.export_enabled,
            "invites_enabled": self.invites_enabled,
        }
        for name in SECRET_FIELDS:
            source = self.secret_sources.get(name, SecretSource.UNSET)
            if source is SecretSource.FILE:
                summary[name] = f"set (file: {getattr(self, name + '_file')})"
            elif source is SecretSource.ENV:
                summary[name] = "set (env)"
            else:
                summary[name] = "not set"
        return summary


def known_env_vars() -> frozenset[str]:
    """All environment variable names that V2 understands."""
    return frozenset(
        env_var_name(name) for name in Settings.model_fields if name != "secret_sources"
    )


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load and validate settings from the process environment.

    ``environ`` is used only to detect unknown ``FINBOT_*`` variables (typos such as
    ``FINBOT_TELEGRAM_TOKN_FILE``). Values are always read from ``os.environ``.

    Raises:
        ConfigurationError: with secret-free messages, if anything is invalid.
    """
    env = os.environ if environ is None else environ
    known = known_env_vars()
    unknown = sorted(
        key for key in env if key.upper().startswith(ENV_PREFIX) and key.upper() not in known
    )
    if unknown:
        raise ConfigurationError([f"unknown setting {key}" for key in unknown])
    try:
        # Values come from the environment through pydantic-settings, not from arguments.
        return Settings()
    except ValidationError as exc:
        problems: list[str] = []
        for error in exc.errors(include_url=False, include_input=False, include_context=False):
            loc = ".".join(str(part) for part in error["loc"])
            field = env_var_name(loc) if loc else "settings"
            message = error["msg"].removeprefix("Value error, ")
            problems.append(f"{field}: {message}" if loc else message)
        raise ConfigurationError(problems) from None
