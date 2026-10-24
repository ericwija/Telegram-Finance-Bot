"""AT-DEP-01 (docs/07 §9): static checks of the staging compose project. Starts nothing.

Two layers:

* Raw-file checks (always run): no published ports, pinned image, no inline secrets, no
  interpolation, no V1 or production names, pg_hba.conf and 00-roles.sh hardening.
* Rendered checks: the model printed by ``docker compose config --format json``, which needs
  only the Docker CLI (no daemon, no images, no secret files). Skipped when the CLI is missing,
  except in CI, where that is an error.
"""

import json
import os
import re
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "staging" / "compose.yml"
HBA = ROOT / "deploy" / "staging" / "postgres" / "pg_hba.conf"
INIT_ROLES = ROOT / "deploy" / "postgres" / "initdb" / "00-roles.sh"
SECRETS_DIR = ROOT / "deploy" / "secrets" / "staging"

IMAGE_RE = re.compile(r"^postgres:18\.\d+-trixie@sha256:[0-9a-f]{64}$")
PGDATA = "/var/lib/postgresql/18/docker"
INIT_MARKER = "/var/lib/postgresql/18/finbot-init.done"
DB_SUBNET = "172.31.250.0/27"
DB_GATEWAY = "172.31.250.1/32"
APP_ROLES = "finbot_owner,finbot_app,finbot_backup,finbot_readonly"
FORBIDDEN_NAMES = (
    # V1 (root docker-compose.yml / .env)
    "finance-bot",
    "bot-data",
    "TELEGRAM_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_SHEETS",
    "SPREADSHEET_ID",
    "AUTHORIZED_USER_ID",
    # future production project
    "finbot-prod",
    "finbot_prod",
    "secrets/prod",
)


def _code_lines(path: Path) -> list[str]:
    """Lines with ``#`` comments removed (none of these files uses ``#`` inside values)."""
    return [line.split("#", 1)[0].rstrip() for line in path.read_text("utf-8").splitlines()]


def _code(path: Path) -> str:
    return "\n".join(_code_lines(path))


# ------------------------------------------------------------------------ raw compose file


def test_AT_DEP_01_no_ports_expose_or_host_networking_in_raw_file() -> None:
    code = _code(COMPOSE)
    for key in ("ports", "expose", "network_mode", "privileged", "pid", "ipc", "extra_hosts"):
        assert not re.search(rf"^\s*{key}\s*:", code, re.M), f"`{key}:` must not appear"
    assert "docker.sock" not in code


def test_AT_DEP_01_image_pinned_by_version_and_digest() -> None:
    images = re.findall(r"^\s*image:\s*(\S+)\s*$", _code(COMPOSE), re.M)
    assert images, "postgres image line not found"
    for image in images:
        assert IMAGE_RE.fullmatch(image), image


def test_no_interpolation_env_files_or_inline_secrets() -> None:
    code = _code(COMPOSE)
    assert "${" not in code, "no variable interpolation in staging compose"
    assert "$$" not in code, "no variable interpolation in staging compose"
    assert not re.search(r"^\s*env_file\s*:", code, re.M)
    # Only *_FILE variables may name a secret, and they must point into /run/secrets.
    for name, value in re.findall(r"^\s+([A-Z][A-Z0-9_]*):\s*(.*)$", code, re.M):
        if re.search(r"PASSWORD|TOKEN|SECRET|API_KEY|CREDENTIAL", name):
            assert name.endswith("_FILE"), f"{name} must be a *_FILE variable"
            assert value.startswith("/run/secrets/"), name


def test_staging_names_are_isolated_from_v1_and_production() -> None:
    text = COMPOSE.read_text("utf-8")
    code = _code(COMPOSE)
    assert re.search(r"^name:\s*finbot-staging\s*$", code, re.M)
    for forbidden in FORBIDDEN_NAMES:
        assert forbidden not in code, forbidden
    assert "/var/lib/postgresql/data" not in code, "PG 18 layout: mount /var/lib/postgresql"
    assert "../secrets/staging/" in text
    assert re.search(r"^\s*FINBOT_DB_NAME:\s*finbot_staging\s*$", code, re.M)
    assert re.search(r"^\s*FINBOT_ENV:\s*staging\s*$", code, re.M)


def test_secret_files_are_git_ignored_and_not_tracked() -> None:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("git checkout not available")
    for name in ("db_superuser_password", "db_owner_password", "db_app_password"):
        probe = SECRETS_DIR / name
        result = subprocess.run(
            [git, "check-ignore", "--quiet", "--no-index", str(probe)], cwd=ROOT, check=False
        )
        assert result.returncode == 0, f"{probe} is not git-ignored"
    tracked = subprocess.run(
        [git, "ls-files", "--", "deploy/secrets"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert tracked == "", "files under deploy/secrets/ must never be tracked"


# ---------------------------------------------------------------------- rendered compose


@cache
def _rendered() -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        if os.environ.get("CI", "").lower() in {"1", "true", "yes"}:
            pytest.fail("Docker CLI is required in CI for AT-DEP-01")
        pytest.skip("Docker CLI not available")
    result = subprocess.run(
        [docker, "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/")},
    )
    assert result.returncode == 0, result.stderr
    rendered: dict[str, Any] = json.loads(result.stdout)
    return rendered


def _postgres() -> dict[str, Any]:
    service: dict[str, Any] = _rendered()["services"]["postgres"]
    return service


def test_AT_DEP_01_rendered_postgres_has_no_ports_and_is_internal_only() -> None:
    model = _rendered()
    assert model["name"] == "finbot-staging"
    pg = _postgres()
    assert not pg.get("ports")
    assert not pg.get("expose")
    assert "network_mode" not in pg
    assert set(pg["networks"]) == {"db"}
    network = model["networks"]["db"]
    assert network["name"] == "finbot-staging_db"
    assert network.get("internal") is True
    assert not network.get("external")


def test_db_network_has_no_host_path_and_a_fixed_subnet() -> None:
    network = _rendered()["networks"]["db"]
    # Without "isolated", host processes can reach container IPs on an internal network and
    # containers can reach host services on 0.0.0.0 via the bridge address (Docker docs).
    assert network["driver"] == "bridge"
    assert network["driver_opts"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated"
    assert network.get("enable_ipv6") is False
    assert [c["subnet"] for c in network["ipam"]["config"]] == [DB_SUBNET]
    assert DB_SUBNET in HBA.read_text("utf-8"), "pg_hba.conf must name the compose subnet"


def test_AT_DEP_01_rendered_image_pinned_and_healthcheck_present() -> None:
    pg = _postgres()
    assert IMAGE_RE.fullmatch(pg["image"])
    health = pg["healthcheck"]
    assert health["test"][0] == "CMD-SHELL"
    command = health["test"][1]
    # Healthy only after a verified initialisation (marker) AND when TCP is accepted.
    assert command.startswith(f"test -f {INIT_MARKER} && pg_isready -h 127.0.0.1 ")
    assert "-d finbot_staging" in command
    assert pg["environment"]["FINBOT_INIT_MARKER"] == INIT_MARKER
    assert INIT_MARKER.startswith("/var/lib/postgresql/"), "marker must persist in the volume"
    assert not INIT_MARKER.startswith(PGDATA + "/"), "marker stays outside PGDATA"
    assert health.get("disable") is not True
    assert pg["stop_signal"] == "SIGINT"
    assert pg["stop_grace_period"] == "1m0s"
    assert pg["restart"] == "unless-stopped"


def test_rendered_volume_and_pgdata_follow_pg18_layout() -> None:
    model = _rendered()
    pg = _postgres()
    assert model["volumes"]["pgdata"]["name"] == "finbot-staging_pgdata"
    assert not model["volumes"]["pgdata"].get("external")
    mounts = {m["target"]: m for m in pg["volumes"]}
    assert mounts["/var/lib/postgresql"]["type"] == "volume"
    assert mounts["/var/lib/postgresql"]["source"] == "pgdata"
    assert pg["environment"]["PGDATA"] == PGDATA
    for target, mount in mounts.items():
        if mount["type"] == "bind":
            assert mount.get("read_only") is True, f"bind mount {target} must be read-only"
            assert "docker.sock" not in mount["source"]


def test_rendered_secrets_come_from_git_ignored_staging_files() -> None:
    model = _rendered()
    pg = _postgres()
    env = pg["environment"]
    assert "POSTGRES_PASSWORD" not in env
    assert env["POSTGRES_PASSWORD_FILE"] == "/run/secrets/db_superuser_password"
    for key in env:
        if re.search(r"PASSWORD|TOKEN|SECRET|API_KEY", key):
            assert key.endswith("_FILE"), key
    used = {s["source"] for s in pg["secrets"]}
    assert used == {
        "db_superuser_password",
        "db_owner_password",
        "db_app_password",
        "db_backup_password",
    }
    for name, secret in model["secrets"].items():
        source = Path(secret["file"])
        assert source.parent == SECRETS_DIR, name
        assert "environment" not in secret, name
        assert "content" not in secret, name


def test_rendered_container_is_hardened_and_bounded() -> None:
    pg = _postgres()
    user = str(pg["user"])
    uid = user.split(":", 1)[0]
    assert uid not in {"", "0", "root", "999", "postgres"}, "dedicated non-root uid required"
    assert pg["cap_drop"] == ["ALL"]
    assert not pg.get("cap_add")
    assert "no-new-privileges:true" in pg["security_opt"]
    assert pg.get("read_only") is True
    assert not pg.get("privileged")
    limits = pg["deploy"]["resources"]["limits"]
    assert int(limits["memory"]) <= 1024**3
    assert limits.get("pids")
    assert limits.get("cpus")
    assert pg["logging"]["driver"] == "json-file"
    assert pg["logging"]["options"]["max-size"]
    assert pg["logging"]["options"]["max-file"]
    assert pg["container_name"].startswith("finbot-staging-")


def test_rendered_server_settings_keep_pii_out_of_logs() -> None:
    command = _postgres()["command"]
    settings = dict(c.split("=", 1) for c in command if "=" in c)
    assert settings["log_parameter_max_length"] == "0"
    assert settings["log_parameter_max_length_on_error"] == "0"
    assert settings["log_statement"] in {"none", "ddl"}
    assert settings["password_encryption"] == "scram-sha-256"
    assert settings["hba_file"] == "/etc/postgresql/pg_hba.conf"
    assert "trust" not in json.dumps(_postgres()["environment"])


# ----------------------------------------------------------------------------- pg_hba.conf


def _hba_rules() -> list[list[str]]:
    return [line.split() for line in _code_lines(HBA) if line.strip()]


def test_pg_hba_has_no_weak_methods_and_ends_with_reject() -> None:
    rules = _hba_rules()
    assert rules
    for rule in rules:
        assert rule[-1] in {"scram-sha-256", "reject"}, rule
    assert rules[-2:] == [
        ["host", "all", "all", "all", "reject"],
        ["host", "replication", "all", "all", "reject"],
    ]


def test_pg_hba_uses_explicit_addresses_and_rejects_the_bridge_gateway() -> None:
    rules = _hba_rules()
    for rule in rules:
        if rule[0] == "host":
            assert rule[3] in {"127.0.0.1/32", DB_SUBNET, DB_GATEWAY, "all"}, rule
        assert rule[0] in {"local", "host"}, rule  # no hostssl/hostnossl/hostgssenc variants
    gateway = rules.index(["host", "all", "all", DB_GATEWAY, "reject"])
    first_network_allow = next(
        i for i, r in enumerate(rules) if r[0] == "host" and r[3] == DB_SUBNET
    )
    assert gateway < first_network_allow


def test_pg_hba_superuser_never_reachable_from_a_network() -> None:
    for rule in _hba_rules():
        if rule[0] == "host" and "postgres" in rule[2].split(","):
            assert rule[3] in {"127.0.0.1/32", "all"}, rule
            if rule[3] == "all":
                assert rule[-1] == "reject", rule


def test_pg_hba_app_roles_limited_to_staging_db_on_attached_network() -> None:
    allowed = [r for r in _hba_rules() if r[0] == "host" and r[-1] == "scram-sha-256"]
    app_rules = [r for r in allowed if "postgres" not in r[2].split(",")]
    assert app_rules == [
        ["host", "finbot_staging", APP_ROLES, DB_SUBNET, "scram-sha-256"],
        ["host", "finbot_staging", APP_ROLES, "127.0.0.1/32", "scram-sha-256"],
    ]


# ----------------------------------------------------------------------------- 00-roles.sh


def test_init_roles_script_is_executable_and_valid_bash() -> None:
    assert os.access(INIT_ROLES, os.X_OK), "entrypoint would source, not execute, the script"
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    result = subprocess.run([bash, "-n", str(INIT_ROLES)], check=False, capture_output=True)
    assert result.returncode == 0, result.stderr


def _create_role(script: str, role: str) -> str:
    """Attributes applied by the (idempotent) ``ALTER ROLE`` that runs on every execution."""
    matches = re.findall(rf"ALTER ROLE {role}\b(.*?);", script, re.S)
    assert len(matches) == 1, f"exactly one ALTER ROLE {role} expected"
    return " ".join(matches[0].split())


def test_init_roles_script_is_idempotent_and_marks_success_last() -> None:
    script = INIT_ROLES.read_text("utf-8")
    # Roles and the database are created only when missing, with no attributes at creation.
    assert re.search(r"format\('CREATE ROLE %I NOLOGIN', r\).*?WHERE NOT EXISTS", script, re.S)
    assert re.search(r"CREATE DATABASE %I OWNER finbot_owner.*?WHERE NOT EXISTS", script, re.S)
    assert len(re.findall(r"^CREATE (ROLE|DATABASE)\b", script, re.M)) == 0
    # The marker is removed first and written only after psql (ON_ERROR_STOP, set -e) succeeded.
    end_of_sql = script.index("\nSQL\n")
    assert script.index('rm -f "$FINBOT_INIT_MARKER"') < script.index("psql --no-psqlrc")
    assert script.index(': >"$FINBOT_INIT_MARKER"') > end_of_sql
    assert "ON_ERROR_STOP=1" in script
    assert "set -Eeuo pipefail" in script


def test_AT_RLS_05_app_role_is_provisioned_without_privileges() -> None:
    script = INIT_ROLES.read_text("utf-8")
    attrs = _create_role(script, "finbot_app").split()
    for required in ("LOGIN", "NOSUPERUSER", "NOBYPASSRLS", "NOCREATEDB", "NOCREATEROLE"):
        assert required in attrs, required
    assert "NOREPLICATION" in attrs
    assert "NOINHERIT" in attrs
    for forbidden in ("SUPERUSER", "BYPASSRLS", "CREATEDB", "CREATEROLE", "REPLICATION"):
        assert forbidden not in attrs, forbidden
    assert not re.search(r"\bGRANT\b", script), "grants belong to Alembic (as finbot_owner)"
    assert 'ALTER DATABASE :"dbname" OWNER TO finbot_owner;' in script
    # The final check also rejects any role membership involving finbot_* roles.
    assert "pg_auth_members" in script
    assert "OR grp.rolname LIKE 'finbot\\_%'" in script


def test_other_roles_match_the_documented_role_model() -> None:
    script = INIT_ROLES.read_text("utf-8")
    owner = _create_role(script, "finbot_owner").split()
    backup = _create_role(script, "finbot_backup").split()
    readonly = _create_role(script, "finbot_readonly").split()
    for attrs in (owner, backup, readonly):
        assert "NOSUPERUSER" in attrs
        assert "NOCREATEROLE" in attrs
        assert "NOREPLICATION" in attrs
    assert "BYPASSRLS" in owner
    assert "BYPASSRLS" in backup
    assert "NOLOGIN" in readonly
    assert "NOBYPASSRLS" in readonly


def test_init_roles_script_never_exposes_passwords() -> None:
    script = INIT_ROLES.read_text("utf-8")
    first_password = script.index("PASSWORD :'")
    assert script.index("SET log_statement = 'none'") < first_password
    assert script.index("SET log_min_error_statement = 'panic'") < first_password
    # Passwords are read by psql from the secret files: never via argv or the environment.
    for var in ("owner_pw", "app_pw", "backup_pw"):
        assert re.search(rf"\\set {var} `cat :'secrets_dir'/db_\w+_password`", script), var
        assert f"\\unset {var}" in script
    assert "--set ECHO" not in script
    assert "--echo" not in script
    assert " -a " not in script
    assert not re.search(r"(echo|printf)[^\n]*(\$\(cat|PGPASSWORD|_pw\b)", script)
    assert not re.search(r"^\s*set\s+-[a-zA-Z]*x", script, re.M), "no shell tracing"
    # PGPASSWORD may only be loaded from the superuser secret file, into the environment.
    # The secrets directory is /run/secrets except in dev/test (disposable test clusters).
    assert "secrets_dir=/run/secrets\n" in script
    assert re.search(r"dev \| test\) secrets_dir=\"\$FINBOT_SECRETS_DIR\" ;;", script)
    assert re.findall(r"PGPASSWORD=[^\n]*", script) == [
        'PGPASSWORD="$(cat "$secrets_dir/db_superuser_password")"'
    ]


# --------------------------------------------------------------- credential separation (S10)


def _service_secrets(path: Path) -> dict[str, list[str]]:
    """Map service name -> secret names, from the raw compose file (2-space YAML indentation)."""
    services: dict[str, list[str]] = {}
    section = current = None
    in_secrets = False
    for line in _code_lines(path):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            section = line.strip().rstrip(":")
            current = None
            continue
        if section != "services":
            continue
        if indent == 2:
            current = line.strip().rstrip(":")
            services[current] = []
            in_secrets = False
        elif indent == 4 and current is not None:
            in_secrets = line.strip() == "secrets:"
        elif indent == 6 and in_secrets and current is not None:
            item = line.strip().removeprefix("- ").strip()
            services[current].append(item.removeprefix("source:").strip())
    return services


def test_owner_and_application_credentials_never_share_a_service() -> None:
    services = _service_secrets(COMPOSE)
    assert services["postgres"] == [
        "db_superuser_password",
        "db_owner_password",
        "db_app_password",
        "db_backup_password",
    ], "parser sanity check: the database container provisions every role"
    app_secrets = {"db_app_password", "telegram_token", "gemini_api_key"}
    for name, used in services.items():
        if name == "postgres":
            continue
        assert "db_superuser_password" not in used, name
        if "db_owner_password" in used:
            assert name == "migrate", (
                f"only the migrate service may hold the owner password: {name}"
            )
            assert not app_secrets & set(used), f"migrate must not hold application secrets: {used}"
        if name != "backup":
            assert "db_backup_password" not in used, name
