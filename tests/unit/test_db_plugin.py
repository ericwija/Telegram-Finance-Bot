"""The database-test plugin never lets database tests disappear silently (docs/10 §4).

Each case runs an inner pytest session in a subprocess (``pytester``) with the plugin hooks and a
controlled environment. No database is started: the inner tests do not request ``pg_cluster``.
"""

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[2]
CONFTEST = """
from tests.plugins.db import (  # noqa: F401
    pytest_collection_modifyitems,
    pytest_configure,
    pytest_runtest_logreport,
    pytest_sessionfinish,
    pytest_terminal_summary,
)
"""
INI = """
[pytest]
markers =
    db: database test
"""
TESTS = """
import pytest

def test_plain():
    pass

@pytest.mark.skip(reason="not a database test")
def test_plain_skipped():
    pass

@pytest.mark.db
def test_database():
    pass
"""
SELF_SKIPPING = """
import pytest

@pytest.mark.db
def test_database_skips_itself():
    pytest.skip("pretend the backend was unusable")
"""
PLAIN_ONLY = """
def test_plain():
    pass
"""


@pytest.fixture
def run(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    pytester.makeconftest(CONFTEST)
    pytester.makeini(INI)
    for name in ("CI", "FINBOT_TEST_PG_BACKEND", "FINBOT_REQUIRE_DB_TESTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PYTHONPATH", str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))

    def _run(source: str, *args: str, **env: str) -> pytest.RunResult:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        pytester.makepyfile(test_inner=source)
        return pytester.runpytest_subprocess("-p", "no:cacheprovider", *args)

    return _run


def test_skipped_locally_without_backend_but_reported(run) -> None:  # type: ignore[no-untyped-def]
    result = run(TESTS)
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1, skipped=2)
    result.stdout.fnmatch_lines(["*NOTE: 1 database test(s) SKIPPED (no database backend)*"])


def test_required_without_backend_is_a_usage_error(run) -> None:  # type: ignore[no-untyped-def]
    result = run(TESTS, FINBOT_REQUIRE_DB_TESTS="1")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*FINBOT_TEST_PG_BACKEND is not set*"])


def test_ci_without_backend_is_a_usage_error(run) -> None:  # type: ignore[no-untyped-def]
    result = run(TESTS, CI="true")
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_ci_job_deselecting_database_tests_passes(run) -> None:  # type: ignore[no-untyped-def]
    # The CI "checks" job runs `-m "not db"` with CI=true and no backend.
    result = run(TESTS, "-m", "not db", CI="true")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1, skipped=1, deselected=1)


def test_required_with_no_database_tests_collected(run) -> None:  # type: ignore[no-untyped-def]
    result = run(PLAIN_ONLY, FINBOT_REQUIRE_DB_TESTS="1", FINBOT_TEST_PG_BACKEND="userspace")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*no @pytest.mark.db tests were collected*"])


def test_unexpected_skip_with_backend_fails_the_run(run) -> None:  # type: ignore[no-untyped-def]
    result = run(SELF_SKIPPING, FINBOT_TEST_PG_BACKEND="userspace")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*FAILED: 1 database test(s) were skipped*"])


def test_unexpected_skip_when_required_fails_the_run(run) -> None:  # type: ignore[no-untyped-def]
    result = run(SELF_SKIPPING, FINBOT_TEST_PG_BACKEND="container", FINBOT_REQUIRE_DB_TESTS="1")
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_required_and_executed_passes(run) -> None:  # type: ignore[no-untyped-def]
    result = run(TESTS, FINBOT_TEST_PG_BACKEND="userspace", FINBOT_REQUIRE_DB_TESTS="1")
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=2, skipped=1)  # only the non-database skip remains
    result.stdout.fnmatch_lines(["*database tests: 1 executed, none skipped*"])


def test_unknown_backend_is_a_usage_error(run) -> None:  # type: ignore[no-untyped-def]
    result = run(TESTS, FINBOT_TEST_PG_BACKEND="staging")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
