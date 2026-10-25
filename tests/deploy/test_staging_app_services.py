"""Static checks of the staging `bot` and `migrate` services and the V2 image (Step 5A).

Starts nothing and builds nothing. Rendered checks use the same offline ``docker compose
config`` model as AT-DEP-01 (``test_staging_compose.py``); the environment that compose would
pass is then validated with the real settings loaders, with fake secret files.
"""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from finbot.config import ConfigurationError, load_settings
from finbot.migration_settings import load_migration_settings
from tests.conftest import fake_telegram_token
from tests.deploy.test_staging_compose import APP_IMAGE, ROOT, SECRETS_DIR, _code, _rendered

DOCKERFILE = ROOT / "deploy" / "docker" / "Dockerfile"
DOCKERIGNORE = ROOT / "deploy" / "docker" / "Dockerfile.dockerignore"
CI = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "deploy" / "staging" / "README.md"
HEALTH_FILE = "/run/finbot/heartbeat.json"
SECRETS_GID = "2999"


def _service(name: str) -> dict[str, Any]:
    service: dict[str, Any] = _rendered()["services"][name]
    return service


def _seconds(duration: str) -> int:
    parts = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", duration)
    assert parts, duration
    return int(parts[1] or 0) * 60 + int(parts[2] or 0)


def _with_fake_secrets(
    env: dict[str, str], secret_file: Callable[..., Path], values: dict[str, str]
) -> dict[str, str]:
    """Point every /run/secrets/<name> at a private temporary file holding a fake value."""
    patched = dict(env)
    for key, value in env.items():
        if key.endswith("_FILE"):
            name = value.removeprefix("/run/secrets/")
            patched[key] = str(secret_file(values[name], name, mode=0o440))
    return patched


# ---------------------------------------------------------------------------------- bot


def test_bot_is_a_single_non_root_replica_with_graceful_shutdown() -> None:
    bot = _service("bot")
    assert bot["image"] == APP_IMAGE
    assert bot["pull_policy"] == "never"
    assert bot["deploy"]["replicas"] == 1
    assert bot["restart"] == "unless-stopped"
    assert bot["stop_signal"] == "SIGTERM"
    assert _seconds(bot["stop_grace_period"]) >= 60
    assert bot["init"] is True
    uid, gid = str(bot["user"]).split(":")
    assert uid not in {"0", "2999", "999", "65534"}
    assert gid == SECRETS_GID
    assert bot["container_name"] == "finbot-staging-bot"
    assert "profiles" not in bot


def test_bot_runs_the_poller_with_the_heartbeat_its_healthcheck_reads() -> None:
    bot = _service("bot")
    assert bot["entrypoint"] == ["finbot"]
    assert bot["command"] == ["bot", "--health-file", HEALTH_FILE]
    health = bot["healthcheck"]
    assert health["test"][:5] == ["CMD", "finbot", "healthcheck", "--health-file", HEALTH_FILE]
    assert health.get("disable") is not True
    assert any(m.startswith("/run/finbot:") for m in bot["tmpfs"]), "heartbeat on tmpfs"
    assert bot["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "migrate" not in bot["depends_on"], "migrations are operator-run, never implicit"


def test_bot_is_hardened_bounded_and_publishes_nothing() -> None:
    bot = _service("bot")
    assert not bot.get("ports")
    assert not bot.get("expose")
    assert "network_mode" not in bot
    assert set(bot["networks"]) == {"db", "egress"}
    assert bot.get("read_only") is True
    assert bot["cap_drop"] == ["ALL"]
    assert not bot.get("cap_add")
    assert "no-new-privileges:true" in bot["security_opt"]
    limits = bot["deploy"]["resources"]["limits"]
    assert int(limits["memory"]) <= 512 * 1024**2
    assert limits.get("pids")
    assert bot["logging"]["options"]["max-size"]
    assert not any(m.get("type") == "bind" for m in bot.get("volumes", []))


def test_bot_gets_only_application_secrets_as_files() -> None:
    bot = _service("bot")
    assert {s["source"] for s in bot["secrets"]} == {"db_app_password", "telegram_token"}
    env = bot["environment"]
    assert env["FINBOT_ENV"] == "staging"
    assert env["FINBOT_DB_NAME"] == "finbot_staging"
    assert env["FINBOT_DB_APP_USER"] == "finbot_app"
    assert env["FINBOT_DB_APP_PASSWORD_FILE"] == "/run/secrets/db_app_password"
    assert env["FINBOT_TELEGRAM_TOKEN_FILE"] == "/run/secrets/telegram_token"
    assert not any(k.startswith(("FINBOT_DB_OWNER", "FINBOT_MIGRATION")) for k in env)
    secret = _rendered()["secrets"]["telegram_token"]
    assert Path(secret["file"]) == SECRETS_DIR / "telegram_token"


def test_bot_environment_passes_the_real_settings_loader(
    monkeypatch: pytest.MonkeyPatch, secret_file: Callable[..., Path]
) -> None:
    env = _with_fake_secrets(
        _service("bot")["environment"],
        secret_file,
        {"db_app_password": "fake-app-password", "telegram_token": fake_telegram_token()},
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings(env)
    assert settings.env == "staging"
    assert settings.db_name == "finbot_staging"
    assert settings.telegram_token is not None


def test_owner_password_in_the_bot_environment_is_refused(
    monkeypatch: pytest.MonkeyPatch, secret_file: Callable[..., Path]
) -> None:
    env = _with_fake_secrets(
        _service("bot")["environment"],
        secret_file,
        {"db_app_password": "fake-app-password", "telegram_token": fake_telegram_token()},
    )
    env["FINBOT_DB_OWNER_PASSWORD_FILE"] = str(secret_file("fake-owner", "owner", mode=0o440))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError):
        load_settings(env)


def test_expected_bot_username_placeholder_is_documented() -> None:
    username = _service("bot")["environment"]["FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME"]
    if username == "CHANGE_ME_staging_bot":  # fails closed at G-ENV-4 until replaced
        assert "CHANGE_ME_staging_bot" in README.read_text("utf-8")
    assert username.lower().endswith("bot")


# ------------------------------------------------------------------------------ migrate


def test_migrate_is_operator_run_only_and_holds_only_the_owner_secret() -> None:
    migrate = _service("migrate")
    assert migrate["profiles"] == ["ops"]
    assert migrate["restart"] == "no"
    assert migrate["image"] == APP_IMAGE
    assert migrate["entrypoint"] == ["finbot-migrate"]
    assert migrate["command"] == ["check-config"], "the default action changes nothing"
    assert [s["source"] for s in migrate["secrets"]] == ["db_owner_password"]
    assert set(migrate["networks"]) == {"db"}
    assert not migrate.get("ports")
    assert migrate.get("read_only") is True
    assert migrate["cap_drop"] == ["ALL"]
    assert str(migrate["user"]).split(":")[1] == SECRETS_GID
    env = migrate["environment"]
    assert env["FINBOT_DB_OWNER_PASSWORD_FILE"] == "/run/secrets/db_owner_password"
    assert "FINBOT_MIGRATION_ALLOW_DOWNGRADE" not in env
    assert not any(k.startswith(("FINBOT_TELEGRAM", "FINBOT_DB_APP", "FINBOT_GEMINI")) for k in env)


def test_migrate_environment_passes_the_real_migration_loader(
    monkeypatch: pytest.MonkeyPatch, secret_file: Callable[..., Path]
) -> None:
    env = _with_fake_secrets(
        _service("migrate")["environment"], secret_file, {"db_owner_password": "fake-owner"}
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_migration_settings(env)
    assert settings.env == "staging"
    assert settings.db_name == "finbot_staging"


def test_only_the_bot_reaches_the_internet() -> None:
    model = _rendered()
    egress = model["networks"]["egress"]
    assert egress["name"] == "finbot-staging_egress"
    assert not egress.get("internal")
    assert not egress.get("external")
    members = [n for n, s in model["services"].items() if "egress" in s.get("networks", {})]
    assert members == ["bot"]
    subnets = {c["subnet"] for net in model["networks"].values() for c in net["ipam"]["config"]}
    assert subnets == {"172.31.250.0/27", "172.31.250.32/27"}


# -------------------------------------------------------------------------------- image


def test_image_base_has_no_default_and_uv_matches_ci() -> None:
    text = DOCKERFILE.read_text("utf-8")
    assert re.search(r"^ARG PYTHON_BASE\s*$", text, re.M), "no default: the digest is mandatory"
    assert re.findall(r"^FROM (\S+)", text, re.M) == ["${PYTHON_BASE}", "${PYTHON_BASE}"]
    # A floating tag is refused inside the build, not only by review.
    assert "python:3\\.13\\.\\d+-slim-trixie@sha256:[0-9a-f]{64}" in text
    ci = CI.read_text("utf-8")
    for name in ("UV_VERSION", "UV_SHA256"):
        dockerfile_value = re.search(rf"^ARG {name}=(\S+)$", text, re.M)
        ci_value = re.search(rf'^\s+{name}:\s*"([^"]+)"', ci, re.M)
        assert dockerfile_value
        assert ci_value
        assert dockerfile_value[1] == ci_value[1], name
    assert "hashlib.sha256(data).hexdigest()" in text


def test_image_installs_only_locked_runtime_dependencies_and_no_secrets() -> None:
    text = DOCKERFILE.read_text("utf-8")
    syncs = re.findall(r"^RUN uv sync ([^\n\\&]+)", text, re.M)
    assert len(syncs) == 2
    for flags in syncs:
        assert "--frozen" in flags
        assert "--no-dev" in flags
    copies = re.findall(r"^COPY (?!--from)(.+)$", text, re.M)
    assert not any(re.search(r"(^|\s)\.(\s|$)", c) for c in copies), "no COPY of the whole context"
    for copied in copies:  # never V1 files, deploy/ (secrets) or env files
        assert not re.search(r"main\.py|requirements\.txt|deploy|\.env|secret", copied), copied
    assert not re.search(r"^\s*(ENV|ARG)\b.*(FINBOT_|PASSWORD|TOKEN)", text, re.M)
    users = re.findall(r"^USER (\S+)", text, re.M)
    assert users
    assert users[-1].split(":")[0] not in {"0", "root"}


def test_build_context_is_an_allow_list_without_v1_or_secrets() -> None:
    lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text("utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*", "exclude everything first"
    allowed = {line[1:].rstrip("/") for line in lines if line.startswith("!")}
    assert allowed == {"pyproject.toml", "uv.lock", "README.md", "alembic.ini", "migrations", "src"}
    assert not (ROOT / ".dockerignore").exists(), "a root .dockerignore would change V1 builds"


def test_compose_never_builds_or_interpolates_the_app_image() -> None:
    code = _code(ROOT / "deploy" / "staging" / "compose.yml")
    assert not re.search(r"^\s*build\s*:", code, re.M), "the operator builds; `up` never does"
