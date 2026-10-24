"""Behaviour of deploy/postgres/initdb/00-roles.sh without PostgreSQL.

A fake ``psql`` on PATH records that it was called (and whether PGPASSWORD reached it) and
swallows the SQL script; nothing else is faked. This checks the shell-level guards: environment
and database name rules, the dev/test-only FINBOT_SECRETS_DIR override (staging keeps
/run/secrets), secret file checks, and that the success marker is written only after psql.
No secret value is ever printed.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "postgres" / "initdb" / "00-roles.sh"
NAMES = ("db_superuser_password", "db_owner_password", "db_app_password", "db_backup_password")
FAKE_PSQL = """#!/bin/sh
# Records the call; never prints argument values that could be secrets (there are none).
{
  echo called
  [ -n "$PGPASSWORD" ] && echo pgpassword-set
  for a in "$@"; do echo "arg:$a"; done
} > "$FAKE_PSQL_LOG"
cat > /dev/null
exit "${FAKE_PSQL_EXIT:-0}"
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _secrets(directory: Path, *, same: bool = False, short: str | None = None) -> Path:
    directory.mkdir(mode=0o700)
    for index, name in enumerate(NAMES):
        value = "A" * 32 if same else f"{index}" * 32
        if short is not None and name == short:
            value = "short"
        (directory / name).write_text(value)
        (directory / name).chmod(0o600)
    return directory


def _run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    psql = bindir / "psql"
    psql.write_text(FAKE_PSQL)
    psql.chmod(0o755)
    log = tmp_path / "psql.log"
    marker = tmp_path / "init.done"
    base_env = {
        "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "POSTGRES_USER": "postgres",
        "FINBOT_INIT_MARKER": str(marker),
        "FAKE_PSQL_LOG": str(log),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**base_env, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return result, log, marker


def test_test_env_uses_private_secrets_dir_and_marks_success(tmp_path: Path) -> None:
    secrets_dir = _secrets(tmp_path / "secrets")
    result, log, marker = _run(
        tmp_path,
        FINBOT_ENV="test",
        FINBOT_DB_NAME="finbot_test",
        FINBOT_SECRETS_DIR=str(secrets_dir),
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert calls[0] == "called"
    assert "pgpassword-set" in calls
    assert f"arg:secrets_dir={secrets_dir}" in calls
    assert "arg:dbname=finbot_test" in calls
    assert marker.is_file()
    for name in NAMES:  # no secret value reaches stdout, stderr or psql's argv
        value = (secrets_dir / name).read_text()
        assert value not in result.stdout + result.stderr + log.read_text()


@pytest.mark.parametrize("env", ["staging", "production", "drill"])
def test_secrets_dir_override_refused_outside_dev_and_test(tmp_path: Path, env: str) -> None:
    secrets_dir = _secrets(tmp_path / "secrets")
    db = "finbot_prod" if env == "production" else f"finbot_{env}"
    result, log, marker = _run(
        tmp_path, FINBOT_ENV=env, FINBOT_DB_NAME=db, FINBOT_SECRETS_DIR=str(secrets_dir)
    )
    assert result.returncode != 0
    assert "only allowed for dev/test" in result.stderr
    assert not log.exists(), "psql must not run"
    assert not marker.exists()


def test_staging_without_override_reads_only_run_secrets(tmp_path: Path) -> None:
    # /run/secrets/... does not exist on a development host, so the script must stop there
    # (proving it does not fall back to any other location).
    result, log, _ = _run(tmp_path, FINBOT_ENV="staging", FINBOT_DB_NAME="finbot_staging")
    assert result.returncode != 0
    assert "secret db_superuser_password is missing" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    ("env", "db", "message"),
    [
        ("test", "finbot_staging", "FINBOT_DB_NAME must be finbot_test"),
        ("production", "finbot_production", "FINBOT_DB_NAME must be finbot_prod"),
        ("qa", "finbot_qa", "unsupported FINBOT_ENV"),
    ],
)
def test_environment_and_database_name_rules(
    tmp_path: Path, env: str, db: str, message: str
) -> None:
    result, log, _ = _run(tmp_path, FINBOT_ENV=env, FINBOT_DB_NAME=db)
    assert result.returncode != 0
    assert message in result.stderr
    assert not log.exists()


def test_relative_secrets_dir_refused(tmp_path: Path) -> None:
    result, log, _ = _run(
        tmp_path, FINBOT_ENV="test", FINBOT_DB_NAME="finbot_test", FINBOT_SECRETS_DIR="secrets"
    )
    assert result.returncode != 0
    assert "must be an absolute path" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"same": True}, "must differ"),
        ({"short": "db_app_password"}, "secret db_app_password is too short"),
    ],
)
def test_weak_secrets_refused(tmp_path: Path, kwargs: dict[str, object], message: str) -> None:
    secrets_dir = _secrets(tmp_path / "secrets", **kwargs)  # type: ignore[arg-type]
    result, log, marker = _run(
        tmp_path,
        FINBOT_ENV="test",
        FINBOT_DB_NAME="finbot_test",
        FINBOT_SECRETS_DIR=str(secrets_dir),
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert not log.exists()
    assert not marker.exists()


def test_marker_not_written_when_psql_fails(tmp_path: Path) -> None:
    secrets_dir = _secrets(tmp_path / "secrets")
    result, log, marker = _run(
        tmp_path,
        FINBOT_ENV="test",
        FINBOT_DB_NAME="finbot_test",
        FINBOT_SECRETS_DIR=str(secrets_dir),
        FAKE_PSQL_EXIT="3",
    )
    assert result.returncode != 0
    assert log.read_text().startswith("called")
    assert not marker.exists()
