"""Shared test fixtures for V2.

No real credentials are used anywhere: fake secrets are generated at runtime and are clearly
not real (see ``fake_telegram_token``).
"""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

# Database-test hooks and fixtures (backend selection, skip detection; docs/10 §4). Imported
# names are registered by pytest as if defined here.
from tests.plugins.db import (  # noqa: F401
    alembic_config,
    app_engine,
    migrated_database,
    migration_settings,
    owner_engine,
    pg_cluster,
    pytest_collection_modifyitems,
    pytest_configure,
    pytest_runtest_logreport,
    pytest_sessionfinish,
    pytest_terminal_summary,
    test_database,
)

SENTINEL_SECRET = "ZZ-NOT-A-REAL-SECRET-ZZ"


def fake_telegram_token() -> str:
    """A syntactically valid but fake bot token, built at runtime (never a literal in git)."""
    return "12345" + ":" + "FAKE" * 9


@pytest.fixture(autouse=True)
def clean_finbot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every FINBOT_* variable so tests never depend on the developer's shell."""
    for key in list(os.environ):
        if key.upper().startswith("FINBOT_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def secret_file(tmp_path: Path) -> Callable[..., Path]:
    """Factory writing a secret to a private temporary file (mode 0600 unless given)."""

    def _make(content: str, name: str = "secret", mode: int = 0o600) -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        return path

    return _make


@pytest.fixture
def staging_env(
    monkeypatch: pytest.MonkeyPatch, secret_file: Callable[..., Path]
) -> dict[str, str]:
    """A complete, valid staging configuration using file-based secrets."""
    values = {
        "FINBOT_ENV": "staging",
        "FINBOT_DB_APP_PASSWORD_FILE": str(secret_file(SENTINEL_SECRET, "db_app_password")),
        "FINBOT_TELEGRAM_TOKEN_FILE": str(secret_file(fake_telegram_token(), "telegram_token")),
        "FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME": "finbot_staging_bot",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values
