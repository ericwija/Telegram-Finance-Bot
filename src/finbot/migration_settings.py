"""Settings for the migration entry point (``finbot-migrate`` and ``migrations/env.py``).

Specification: docs/10-DATABASE-MIGRATIONS-AND-TESTING.md §2, decisions S9 and S10.

Rules (in addition to the general rules in :mod:`finbot.config`):

* This is the **only** configuration that may hold ``finbot_owner`` credentials. The application
  (:func:`finbot.config.load_settings`) rejects every ``FINBOT_DB_OWNER_*`` variable, and this
  loader rejects every application, Telegram, Gemini, backup and superuser variable, so neither
  process can be configured with the other's secrets.
* The owner password is accepted **only** from ``FINBOT_DB_OWNER_PASSWORD_FILE`` (absolute path,
  not world-readable), in every environment. There is no fallback to the application password.
* The role is always ``finbot_owner``; migrations never run as a superuser or as the app role.
* Downgrades are possible only in ``dev``, ``test`` and ``drill`` **and** only with
  ``FINBOT_MIGRATION_ALLOW_DOWNGRADE=true``. Setting that flag anywhere else is an error (S9).
* Nothing runs at import time; validation reads local files only and never touches the network.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from finbot.config import (
    DB_NAME_BY_ENV,
    ENV_PREFIX,
    ConfigurationError,
    Environment,
    env_var_name,
    read_secret_file,
)

#: The only role migrations may run as (docs/05 §8, doc 09 §4.4).
MIGRATION_OWNER_ROLE = "finbot_owner"

#: Environments where a downgrade may run at all (S9). Staging and production use forward fixes.
DOWNGRADE_ENVIRONMENTS: frozenset[str] = frozenset({"dev", "test", "drill"})

#: Environments where ``db_host`` may be a Unix-socket directory (user-space test clusters).
SOCKET_HOST_ENVIRONMENTS: frozenset[str] = frozenset({"dev", "test"})

#: ``finbot_test`` plus per-test databases ``finbot_test_<suffix>`` (test environment only).
_TEST_DB_NAME_RE = re.compile(r"^finbot_test(_[a-z0-9]{1,40})?$")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")

#: Variables the migration process must never receive (the application's and other roles').
FOREIGN_ENV_PREFIXES: tuple[str, ...] = (
    "FINBOT_DB_APP_",
    "FINBOT_DB_BACKUP_",
    "FINBOT_DB_SUPERUSER_",
    "FINBOT_TELEGRAM_",
    "FINBOT_GEMINI_",
)

_PLAINTEXT_OWNER_PASSWORD_VAR = env_var_name("db_owner_password")


class MigrationSettings(BaseSettings):
    """Owner-role settings for schema migrations; construct via :func:`load_migration_settings`."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=None,
        secrets_dir=None,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    env: Environment = Field(description="Deployment environment (G-ENV-1); no default.")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    db_host: str = "postgres"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(default="", description="Defaults to the environment's expected name.")
    db_owner_user: str = MIGRATION_OWNER_ROLE
    # Filled only from db_owner_password_file by the resolver below.
    db_owner_password: SecretStr | None = None
    db_owner_password_file: Path | None = None

    migration_allow_downgrade: bool = False

    @model_validator(mode="before")
    @classmethod
    def _resolve_owner_password(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values: dict[str, Any] = {k: v for k, v in data.items() if v != ""}
        if values.get("db_owner_password") is not None:
            raise ValueError(
                f"{_PLAINTEXT_OWNER_PASSWORD_VAR} (plaintext) is never accepted; use "
                f"{env_var_name('db_owner_password_file')} (S10)"
            )
        file_path = values.get("db_owner_password_file")
        if file_path is not None:
            values["db_owner_password"] = read_secret_file(
                str(file_path), strict=True, var_name=env_var_name("db_owner_password_file")
            )
        env = str(values.get("env", "")).strip().lower()
        if "db_name" not in values and env in DB_NAME_BY_ENV:
            values["db_name"] = DB_NAME_BY_ENV[env]
        return values

    @field_validator("env", mode="before")
    @classmethod
    def _lowercase_env(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("db_host")
    @classmethod
    def _valid_host(cls, value: str) -> str:
        if value.startswith("/"):
            return value  # Unix-socket directory; allowed environments checked below
        if not _HOSTNAME_RE.fullmatch(value):
            raise ValueError("must be a hostname, an IP address or an absolute socket directory")
        return value

    @field_validator("db_owner_user")
    @classmethod
    def _owner_role_only(cls, value: str) -> str:
        if value != MIGRATION_OWNER_ROLE:
            raise ValueError(f"migrations always run as {MIGRATION_OWNER_ROLE!r}")
        return value

    @model_validator(mode="after")
    def _check_environment_rules(self) -> Self:
        problems: list[str] = []
        if self.env == "test":
            if not _TEST_DB_NAME_RE.fullmatch(self.db_name):
                problems.append(
                    f"{env_var_name('db_name')} must be 'finbot_test' or 'finbot_test_<suffix>' "
                    "when FINBOT_ENV=test (G-ENV-2)"
                )
        elif self.db_name != DB_NAME_BY_ENV[self.env]:
            problems.append(
                f"{env_var_name('db_name')} must be {DB_NAME_BY_ENV[self.env]!r} when "
                f"FINBOT_ENV={self.env} (G-ENV-2)"
            )
        if self.db_host.startswith("/") and self.env not in SOCKET_HOST_ENVIRONMENTS:
            problems.append(
                f"{env_var_name('db_host')} may be a socket directory only in dev and test"
            )
        if self.migration_allow_downgrade and self.env not in DOWNGRADE_ENVIRONMENTS:
            problems.append(
                f"{env_var_name('migration_allow_downgrade')} is not allowed in {self.env}: "
                "staging and production use forward fixes (S9)"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def downgrade_permitted(self) -> bool:
        """True only in a downgrade environment with the explicit opt-in flag (S9)."""
        return self.env in DOWNGRADE_ENVIRONMENTS and self.migration_allow_downgrade

    def require_owner_password(self) -> SecretStr:
        """The owner password, or :class:`ConfigurationError` if no password file was given."""
        if self.db_owner_password is None:
            raise ConfigurationError(
                [f"{env_var_name('db_owner_password_file')} is required to connect"]
            )
        return self.db_owner_password

    def redacted_summary(self) -> dict[str, str | int | bool | None]:
        """Settings safe to print: the password is reported only by its source file."""
        return {
            "env": self.env,
            "log_level": self.log_level,
            "db_host": self.db_host,
            "db_port": self.db_port,
            "db_name": self.db_name,
            "db_owner_user": self.db_owner_user,
            "db_owner_password": (
                f"set (file: {self.db_owner_password_file})"
                if self.db_owner_password is not None
                else "not set"
            ),
            "migration_allow_downgrade": self.migration_allow_downgrade,
            "downgrade_permitted": self.downgrade_permitted,
        }


def known_migration_env_vars() -> frozenset[str]:
    """Variables the migration entry point understands (the plaintext password is not one)."""
    return frozenset(
        env_var_name(name) for name in MigrationSettings.model_fields if name != "db_owner_password"
    )


def load_migration_settings(
    environ: Mapping[str, str] | None = None, *, require_password: bool = True
) -> MigrationSettings:
    """Load and validate migration settings from the process environment.

    Args:
        environ: used only to detect foreign or unknown ``FINBOT_*`` variables; values are always
            read from ``os.environ``.
        require_password: ``False`` for offline SQL generation, which never connects.

    Raises:
        ConfigurationError: with secret-free messages, if anything is invalid.
    """
    env = os.environ if environ is None else environ
    known = known_migration_env_vars()
    problems: list[str] = []
    for key in sorted(env):
        upper = key.upper()
        if not upper.startswith(ENV_PREFIX) or upper in known:
            continue
        if upper == _PLAINTEXT_OWNER_PASSWORD_VAR:
            problems.append(
                f"{key} (plaintext) is never accepted; use "
                f"{env_var_name('db_owner_password_file')} (S10)"
            )
        elif upper.startswith(FOREIGN_ENV_PREFIXES):
            problems.append(
                f"{key} belongs to another process and must not be given to migrations (S10)"
            )
        else:
            problems.append(f"{key} is not a migration setting")
    if problems:
        raise ConfigurationError(problems)
    try:
        settings = MigrationSettings()
    except ValidationError as exc:
        found: list[str] = []
        for error in exc.errors(include_url=False, include_input=False, include_context=False):
            loc = ".".join(str(part) for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            found.append(f"{env_var_name(loc)}: {message}" if loc else message)
        raise ConfigurationError(found) from None
    if require_password:
        settings.require_owner_password()
    return settings
