"""Disposable PostgreSQL 18.6 clusters for database tests (docs/10 §4).

Two backends, selected by ``FINBOT_TEST_PG_BACKEND``:

* ``container`` (CI): the **exact pinned image** from ``deploy/staging/compose.yml`` via
  testcontainers, initialised by the real ``deploy/postgres/initdb/00-roles.sh``.
* ``userspace`` (development, user ``claude``): PostgreSQL 18.6 binaries unpacked in user space
  (``FINBOT_TEST_PG_BIN``, optional ``FINBOT_TEST_PG_LIBDIR``); the cluster listens on a private
  Unix socket only (no TCP) and is initialised by the same ``00-roles.sh``.

Both use freshly generated random role passwords in a private temporary directory, the same
initdb arguments and server settings as staging (kept in sync by tests), and per-test databases
``finbot_test_<hex>`` owned by ``finbot_owner``. Cleanup removes only what this module created:
a cluster directory is deleted only if it carries the sentinel file written at creation, and a
database is dropped only if its name matches the per-test pattern. Staging is never contacted.
"""

import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy" / "staging" / "compose.yml"
INIT_ROLES = REPO / "deploy" / "postgres" / "initdb" / "00-roles.sh"

EXPECTED_SERVER_VERSION = "18.6"
SUPERUSER = "postgres"
OWNER_ROLE = "finbot_owner"
APP_ROLE = "finbot_app"
ROLES = (OWNER_ROLE, APP_ROLE, "finbot_backup", "finbot_readonly")
CLUSTER_DB = "finbot_test"
TEST_DB_PREFIX = "finbot_test_"
TEST_DB_RE = re.compile(r"^finbot_test_[0-9a-f]{12}$")
CLUSTER_SENTINEL = ".finbot-test-cluster"
SECRET_NAMES = {
    SUPERUSER: "db_superuser_password",
    OWNER_ROLE: "db_owner_password",
    APP_ROLE: "db_app_password",
    "finbot_backup": "db_backup_password",
}
CONTAINER_PGDATA = "/var/lib/postgresql/18/docker"
CONTAINER_MARKER = "/var/lib/postgresql/18/finbot-init.done"
_MAX_SOCKET_DIR = 90  # Unix socket paths are limited to 107 bytes including the file name

#: Same as POSTGRES_INITDB_ARGS in deploy/staging/compose.yml (tests/unit/test_pg_support.py).
INITDB_ARGS: tuple[str, ...] = (
    "--auth-host=scram-sha-256",
    "--auth-local=scram-sha-256",
    "--data-checksums",
    "--encoding=UTF8",
    "--locale=C.UTF-8",
    "--locale-provider=builtin",
    "--builtin-locale=C.UTF-8",
)

#: Same as the `-c` settings in deploy/staging/compose.yml, except hba_file (test-specific).
SERVER_SETTINGS: tuple[tuple[str, str], ...] = (
    ("password_encryption", "scram-sha-256"),
    ("shared_buffers", "128MB"),
    ("max_connections", "30"),
    ("timezone", "UTC"),
    ("log_timezone", "UTC"),
    ("log_min_duration_statement", "500ms"),
    ("log_parameter_max_length", "0"),
    ("log_parameter_max_length_on_error", "0"),
    ("log_connections", "authentication,authorization"),
    ("log_disconnections", "on"),
    ("log_statement", "ddl"),
)

#: Disposable clusters only: durability is irrelevant, speed matters. Never used elsewhere.
TEST_ONLY_SETTINGS: tuple[tuple[str, str], ...] = (
    ("fsync", "off"),
    ("synchronous_commit", "off"),
    ("full_page_writes", "off"),
    ("jit", "off"),
)

_TEST_DB_HBA = r"/^finbot_test(_[0-9a-f]{12})?$"
_APP_ROLES_HBA = ",".join(ROLES)


class ClusterError(RuntimeError):
    """A test cluster could not be created, started or verified."""


@dataclass(frozen=True, slots=True)
class ClusterInfo:
    """How to reach a running test cluster. Passwords are read from files on demand."""

    backend: str
    host: str  # hostname, or an absolute Unix-socket directory
    port: int
    secrets_dir: Path

    def password_file(self, role: str) -> Path:
        return self.secrets_dir / SECRET_NAMES[role]

    def password(self, role: str) -> str:
        return self.password_file(role).read_text(encoding="utf-8").strip()


# ------------------------------------------------------------------------------------ helpers


def pinned_image() -> str:
    """The postgres image reference (version + digest) from the staging compose file."""
    match = re.search(
        r"^\s*image:\s*(postgres:\S+@sha256:[0-9a-f]{64})\s*$", COMPOSE.read_text(), re.M
    )
    if match is None:
        raise ClusterError(f"no pinned postgres image found in {COMPOSE}")
    return match.group(1)


def write_test_secrets(directory: Path) -> Path:
    """Write distinct random passwords (mode 0600) into a new private directory."""
    directory.mkdir(mode=0o700)
    for name in SECRET_NAMES.values():
        path = directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32))
    return directory


def render_test_hba(*, tcp: bool) -> str:
    """pg_hba.conf for a test cluster: SCRAM only, test databases only, everything else rejected."""
    lines = ["# finbot disposable test cluster (tests/support/pg.py). Never used outside tests."]
    lines.append(f"local all {SUPERUSER} scram-sha-256")
    lines.append(f"local {_TEST_DB_HBA} {_APP_ROLES_HBA} scram-sha-256")
    if tcp:  # container backend: clients arrive through the published port
        lines.append(f"host all {SUPERUSER} all scram-sha-256")
        lines.append(f"host {_TEST_DB_HBA} {_APP_ROLES_HBA} all scram-sha-256")
    lines.append("local all all reject")
    lines.append("host all all all reject")
    lines.append("host replication all all reject")
    return "\n".join(lines) + "\n"


def server_args(hba_file: str) -> list[str]:
    """``-c name=value`` arguments for the postgres server."""
    args: list[str] = ["-c", f"hba_file={hba_file}"]
    for name, value in (*SERVER_SETTINGS, *TEST_ONLY_SETTINGS):
        args += ["-c", f"{name}={value}"]
    return args


def new_test_database_name() -> str:
    return f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"


def remove_owned_tree(path: Path) -> None:
    """Delete a directory created by this module; refuse anything without the sentinel."""
    if not (path / CLUSTER_SENTINEL).is_file():
        raise ClusterError(f"refusing to delete {path}: not a finbot test cluster directory")
    shutil.rmtree(path)


def _run(cmd: list[str], *, env: dict[str, str], what: str) -> None:
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # Output of initdb/pg_ctl/00-roles.sh never contains passwords (files only).
        tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
        raise ClusterError(f"{what} failed ({result.returncode}): " + " | ".join(tail))


# ---------------------------------------------------------------------------- user space


class UserspaceCluster:
    """A temporary cluster from user-space PostgreSQL 18.6 binaries, Unix socket only."""

    def __init__(self, base: Path, bindir: Path, libdir: Path | None = None) -> None:
        self.base = base
        self.bindir = bindir
        self.libdir = libdir
        self.data = base / "data"
        self.socket_dir = base / "run"
        self.started = False

    def _env(self) -> dict[str, str]:
        env = {
            "PATH": f"{self.bindir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(self.base),
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
        }
        if self.libdir is not None:
            env["LD_LIBRARY_PATH"] = str(self.libdir)
        return env

    def _check_version(self) -> None:
        postgres = self.bindir / "postgres"
        if not postgres.is_file():
            raise ClusterError(f"FINBOT_TEST_PG_BIN has no postgres binary: {self.bindir}")
        result = subprocess.run(
            [str(postgres), "--version"],
            env=self._env(),
            capture_output=True,
            text=True,
            check=False,
        )
        version = result.stdout.strip()
        if not re.search(rf"\b{re.escape(EXPECTED_SERVER_VERSION)}\b", version):
            raise ClusterError(f"expected PostgreSQL {EXPECTED_SERVER_VERSION}, found {version!r}")

    def start(self) -> ClusterInfo:
        self._check_version()
        if len(str(self.socket_dir)) > _MAX_SOCKET_DIR:
            raise ClusterError(
                f"socket directory path too long for Unix sockets: {self.socket_dir}"
            )
        self.base.mkdir(mode=0o700)
        (self.base / CLUSTER_SENTINEL).write_text("created by tests/support/pg.py\n")
        secrets_dir = write_test_secrets(self.base / "secrets")
        self.socket_dir.mkdir(mode=0o700)
        hba = self.base / "pg_hba.conf"
        hba.write_text(render_test_hba(tcp=False))
        env = self._env()
        _run(
            [
                str(self.bindir / "initdb"),
                "-D",
                str(self.data),
                "-U",
                SUPERUSER,
                f"--pwfile={secrets_dir / SECRET_NAMES[SUPERUSER]}",
                *INITDB_ARGS,
            ],
            env=env,
            what="initdb",
        )
        options = ["-c", "listen_addresses=", "-c", f"unix_socket_directories={self.socket_dir}"]
        options += server_args(str(hba))
        _run(
            [
                str(self.bindir / "pg_ctl"),
                "-D",
                str(self.data),
                "-l",
                str(self.base / "postgres.log"),
                "-w",
                "-t",
                "60",
                "-o",
                shlex.join(options),
                "start",
            ],
            env=env,
            what="pg_ctl start",
        )
        self.started = True
        marker = self.base / "init.done"
        roles_env = {
            **env,
            "PGHOST": str(self.socket_dir),
            "PGPORT": "5432",
            "POSTGRES_USER": SUPERUSER,
            "FINBOT_ENV": "test",
            "FINBOT_DB_NAME": CLUSTER_DB,
            "FINBOT_INIT_MARKER": str(marker),
            "FINBOT_SECRETS_DIR": str(secrets_dir),
        }
        _run(["bash", str(INIT_ROLES)], env=roles_env, what="00-roles.sh")
        if not marker.is_file():
            raise ClusterError("00-roles.sh did not write its success marker")
        return ClusterInfo("userspace", str(self.socket_dir), 5432, secrets_dir)

    def stop(self) -> None:
        if self.started:
            _run(
                [str(self.bindir / "pg_ctl"), "-D", str(self.data), "-m", "fast", "-w", "stop"],
                env=self._env(),
                what="pg_ctl stop",
            )
            self.started = False
        if self.base.exists():
            remove_owned_tree(self.base)


# ---------------------------------------------------------------------------- container (CI)


class ContainerCluster:
    """The pinned staging image via testcontainers (CI). Needs a Docker daemon."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.container: Any = None

    def start(self) -> ClusterInfo:
        from testcontainers.core.container import DockerContainer  # noqa: PLC0415

        self.base.mkdir(mode=0o700)
        (self.base / CLUSTER_SENTINEL).write_text("created by tests/support/pg.py\n")
        secrets_dir = write_test_secrets(self.base / "secrets")
        hba = self.base / "pg_hba.conf"
        hba.write_text(render_test_hba(tcp=True))
        hba.chmod(0o644)
        container = (
            DockerContainer(pinned_image())
            .with_env("POSTGRES_USER", SUPERUSER)
            .with_env("POSTGRES_DB", "postgres")
            .with_env("POSTGRES_PASSWORD_FILE", f"/run/secrets/{SECRET_NAMES[SUPERUSER]}")
            .with_env("POSTGRES_INITDB_ARGS", " ".join(INITDB_ARGS))
            .with_env("PGDATA", CONTAINER_PGDATA)
            .with_env("FINBOT_ENV", "test")
            .with_env("FINBOT_DB_NAME", CLUSTER_DB)
            .with_env("FINBOT_INIT_MARKER", CONTAINER_MARKER)
            .with_volume_mapping(str(secrets_dir), "/run/secrets", "ro")
            .with_volume_mapping(str(INIT_ROLES.parent), "/docker-entrypoint-initdb.d", "ro")
            .with_volume_mapping(str(hba), "/etc/postgresql/pg_hba.conf", "ro")
            .with_exposed_ports(5432)
            # Same uid as the runner so the 0600 test secrets are readable (no world access).
            .with_kwargs(user=f"{os.getuid()}:{os.getgid()}")
            .with_command(["postgres", *server_args("/etc/postgresql/pg_hba.conf")])
        )
        self.container = container.start()
        self._wait_until_initialised()
        host = self.container.get_container_host_ip()
        port = int(self.container.get_exposed_port(5432))
        return ClusterInfo("container", host, port, secrets_dir)

    def _wait_until_initialised(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        check = f"test -f {CONTAINER_MARKER} && pg_isready -h 127.0.0.1 -p 5432 -q"
        while time.monotonic() < deadline:
            result = self.container.exec(["sh", "-c", check])
            if result.exit_code == 0:
                return
            time.sleep(1)
        raise ClusterError("test container did not finish initialisation in time")

    def stop(self) -> None:
        if self.container is not None:
            self.container.stop()
            self.container = None
        if self.base.exists():
            remove_owned_tree(self.base)


def make_cluster(
    backend: str, base: Path, *, bindir: str = "", libdir: str = ""
) -> UserspaceCluster | ContainerCluster:
    """Create (but do not start) the configured backend."""
    if backend == "container":
        return ContainerCluster(base)
    if backend == "userspace":
        if not bindir.strip():
            raise ClusterError("FINBOT_TEST_PG_BACKEND=userspace needs FINBOT_TEST_PG_BIN")
        lib = Path(libdir.strip()) if libdir.strip() else None
        return UserspaceCluster(base, Path(bindir.strip()), lib)
    raise ClusterError(f"unknown FINBOT_TEST_PG_BACKEND {backend!r}")


# ----------------------------------------------------------------------------- databases


async def _superuser_connect(cluster: ClusterInfo) -> Any:
    import asyncpg  # noqa: PLC0415

    return await asyncpg.connect(
        host=cluster.host,
        port=cluster.port,
        user=SUPERUSER,
        password=cluster.password(SUPERUSER),
        database="postgres",
    )


async def create_test_database(cluster: ClusterInfo) -> str:
    """Create an empty per-test database owned by finbot_owner (the superuser does only this)."""
    name = new_test_database_name()
    connection = await _superuser_connect(cluster)
    try:
        await connection.execute(
            f"CREATE DATABASE \"{name}\" OWNER {OWNER_ROLE} TEMPLATE template0 ENCODING 'UTF8'"
        )
    finally:
        await connection.close()
    return name


async def drop_test_database(cluster: ClusterInfo, name: str) -> None:
    """Drop a per-test database. Refuses any name this module did not generate."""
    if not TEST_DB_RE.fullmatch(name):
        raise ClusterError(f"refusing to drop {name!r}: not a per-test database name")
    connection = await _superuser_connect(cluster)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await connection.close()
