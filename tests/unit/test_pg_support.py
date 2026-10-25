"""Test-cluster helpers (tests/support/pg.py), without starting PostgreSQL."""

import re
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.support import pg

COMPOSE_TEXT = pg.COMPOSE.read_text(encoding="utf-8")


def test_initdb_args_match_staging_compose() -> None:
    folded = re.search(r"POSTGRES_INITDB_ARGS: >-\n((?:\s{8}.+\n)+)", COMPOSE_TEXT)
    assert folded, "POSTGRES_INITDB_ARGS block not found"
    staging_args = folded.group(1).split()
    assert tuple(staging_args) == pg.INITDB_ARGS


def test_server_settings_match_staging_compose() -> None:
    staging = dict(
        line.strip().removeprefix("- ").split("#", 1)[0].strip().split("=", 1)
        for line in COMPOSE_TEXT.splitlines()
        if re.match(r"^\s+- [a-z_]+=\S", line)
    )
    staging.pop("hba_file")
    assert staging == dict(pg.SERVER_SETTINGS)
    # Test-only speed settings never appear in the staging file.
    for name, _ in pg.TEST_ONLY_SETTINGS:
        assert f"{name}=" not in COMPOSE_TEXT


def test_pinned_image_is_the_staging_digest() -> None:
    assert re.fullmatch(r"postgres:18\.6-trixie@sha256:[0-9a-f]{64}", pg.pinned_image())


def test_test_secrets_are_random_distinct_and_private(tmp_path: Path) -> None:
    directory = pg.write_test_secrets(tmp_path / "s")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    values = []
    for name in pg.SECRET_NAMES.values():
        path = directory / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        values.append(path.read_text())
    assert len(set(values)) == len(values)
    assert all(len(v) >= 32 for v in values)


@pytest.mark.parametrize("tcp", [False, True])
def test_test_hba_is_scram_only_and_rejects_everything_else(tcp: bool) -> None:
    rules = [
        line.split()
        for line in pg.render_test_hba(tcp=tcp).splitlines()
        if line and not line.startswith("#")
    ]
    assert {rule[-1] for rule in rules} == {"scram-sha-256", "reject"}
    assert rules[-1] == ["host", "replication", "all", "all", "reject"]
    allowed_dbs = {rule[1] for rule in rules if rule[-1] == "scram-sha-256"}
    assert allowed_dbs <= {"all", r"/^finbot_test(_[0-9a-f]{12})?$"}
    assert any(rule[0] == "host" for rule in rules if rule[-1] == "scram-sha-256") is tcp
    for rule in rules:  # "all" databases only for the superuser, which creates test databases
        if rule[-1] == "scram-sha-256" and rule[1] == "all":
            assert rule[2] == "postgres"


def test_database_names_and_drop_guard() -> None:
    name = pg.new_test_database_name()
    assert pg.TEST_DB_RE.fullmatch(name)
    for foreign in ("finbot_staging", "finbot_test", "postgres", "finbot_test_x; DROP"):
        assert not pg.TEST_DB_RE.fullmatch(foreign)


def test_remove_owned_tree_refuses_directories_it_did_not_create(tmp_path: Path) -> None:
    foreign = tmp_path / "not-ours"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("x")
    with pytest.raises(pg.ClusterError, match="refusing to delete"):
        pg.remove_owned_tree(foreign)
    assert (foreign / "keep.txt").exists()
    ours = tmp_path / "ours"
    ours.mkdir()
    (ours / pg.CLUSTER_SENTINEL).write_text("x")
    pg.remove_owned_tree(ours)
    assert not ours.exists()


def test_userspace_backend_requires_binaries_of_the_expected_version(tmp_path: Path) -> None:
    with pytest.raises(pg.ClusterError, match="needs FINBOT_TEST_PG_BIN"):
        pg.make_cluster("userspace", tmp_path / "c")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "postgres"
    fake.write_text("#!/bin/sh\necho 'postgres (PostgreSQL) 17.2'\n")
    fake.chmod(0o755)
    cluster = pg.make_cluster("userspace", tmp_path / "c", bindir=str(bindir))
    with pytest.raises(pg.ClusterError, match=r"expected PostgreSQL 18\.6"):
        cluster.start()
    assert not (tmp_path / "c").exists(), "nothing is created before the version check passes"


def test_server_args_put_hba_first_and_include_test_only_settings() -> None:
    args = pg.server_args("/etc/postgresql/pg_hba.conf")
    assert args[:2] == ["-c", "hba_file=/etc/postgresql/pg_hba.conf"]
    assert "fsync=off" in args
    assert "log_parameter_max_length=0" in args


def test_unknown_backend_rejected(tmp_path: Path) -> None:
    with pytest.raises(pg.ClusterError, match="unknown"):
        pg.make_cluster("staging", tmp_path)


# ------------------------------------------------- container readiness (no Docker needed)


class _FakeWrapped:
    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses
        self.status = "created"

    def reload(self) -> None:
        self.status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]

    def logs(self, *, tail: int) -> bytes:
        assert tail == 40
        return b"initdb: error: example failure\n"


class _FakeContainer:
    def __init__(self, statuses: list[str], results: list[tuple[int, bytes]]) -> None:
        self.wrapped = _FakeWrapped(statuses)
        self.results = results
        self.commands: list[list[str]] = []

    def get_wrapped_container(self) -> _FakeWrapped:
        return self.wrapped

    def exec(self, command: list[str]) -> Any:
        self.commands.append(command)
        code, output = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return SimpleNamespace(exit_code=code, output=output)


def _waiting_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, container: _FakeContainer
) -> pg.ContainerCluster:
    monkeypatch.setattr(pg.time, "sleep", lambda _s: None)
    cluster = pg.ContainerCluster(tmp_path / "c")
    cluster.container = container
    return cluster


def test_container_ready_check_names_the_user_like_the_staging_healthcheck() -> None:
    staging = re.search(r"pg_isready (-h .+)$", COMPOSE_TEXT, re.M)
    assert staging
    # The runner uid has no passwd entry in the image: without -U, pg_isready never connects.
    assert f"-U {pg.SUPERUSER}" in pg.CONTAINER_READY_CHECK
    assert f"-U {pg.SUPERUSER}" in staging.group(1)
    assert "pg_isready -h 127.0.0.1 -p 5432 -d postgres -U postgres" in pg.CONTAINER_READY_CHECK


def test_container_ready_check_reports_a_missing_marker() -> None:
    assert not Path(pg.CONTAINER_MARKER).exists()
    result = subprocess.run(
        ["sh", "-c", pg.CONTAINER_READY_CHECK], capture_output=True, text=True, check=False
    )
    assert (result.returncode, result.stdout.strip()) == (10, "init marker missing")


def test_container_wait_returns_once_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    container = _FakeContainer(["running"], [(10, b"init marker missing"), (0, b"accepting")])
    _waiting_cluster(tmp_path, monkeypatch, container)._wait_until_initialised(timeout=5)
    assert container.commands == [["sh", "-c", pg.CONTAINER_READY_CHECK]] * 2


def test_container_exit_fails_fast_with_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeContainer(["running", "exited"], [(10, b"init marker missing")])
    cluster = _waiting_cluster(tmp_path, monkeypatch, container)
    with pytest.raises(pg.ClusterError) as excinfo:
        cluster._wait_until_initialised(timeout=60)
    message = str(excinfo.value)
    assert "test container is exited during start-up" in message
    assert "last readiness check: exit 10: init marker missing" in message
    assert "initdb: error: example failure" in message


def test_container_timeout_reports_the_last_readiness_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(pg.time, "monotonic", lambda: float(next(clock)))
    container = _FakeContainer(["running"], [(3, b"127.0.0.1:5432 - no attempt")])
    cluster = _waiting_cluster(tmp_path, monkeypatch, container)
    with pytest.raises(pg.ClusterError) as excinfo:
        cluster._wait_until_initialised(timeout=30)
    message = str(excinfo.value)
    assert "did not finish initialisation in 30s" in message
    assert "last readiness check: exit 3: 127.0.0.1:5432 - no attempt" in message
    assert "container log (last 40 lines):" in message
