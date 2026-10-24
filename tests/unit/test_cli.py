"""`finbot check-config` and `finbot version` (docs/06 §6.1 item 15)."""

import io
import json
import os
import subprocess
import sys
import textwrap

import pytest

from finbot import __version__
from finbot.__main__ import EXIT_CONFIG_INVALID, EXIT_OK, main
from tests.conftest import SENTINEL_SECRET, fake_telegram_token


def _run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_version() -> None:
    code, out, _ = _run("version")
    assert code == EXIT_OK
    assert out.strip() == f"finbot {__version__}"


def test_check_config_valid_staging_prints_redacted_summary(staging_env: dict[str, str]) -> None:
    code, out, err = _run("check-config")
    assert code == EXIT_OK, err
    assert "configuration OK" in out
    assert "finbot_staging" in out
    assert "set (file: " in out
    assert SENTINEL_SECRET not in out + err
    assert fake_telegram_token() not in out + err


def test_check_config_json_output(staging_env: dict[str, str]) -> None:
    code, out, _ = _run("check-config", "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["valid"] is True
    assert payload["settings"]["db_name"] == "finbot_staging"
    assert payload["settings"]["gemini_api_key"] == "not set"
    assert SENTINEL_SECRET not in out


def test_check_config_invalid_exits_2_without_leaking(
    monkeypatch: pytest.MonkeyPatch, staging_env: dict[str, str]
) -> None:
    monkeypatch.delenv("FINBOT_DB_APP_PASSWORD_FILE")
    monkeypatch.setenv("FINBOT_DB_APP_PASSWORD", SENTINEL_SECRET)
    code, out, err = _run("check-config")
    assert code == EXIT_CONFIG_INVALID
    assert "configuration INVALID" in err
    assert "G-ENV-5" in err
    assert SENTINEL_SECRET not in out + err

    code, out, _ = _run("check-config", "--json")
    assert code == EXIT_CONFIG_INVALID
    assert json.loads(out)["valid"] is False
    assert SENTINEL_SECRET not in out


def test_unknown_command_is_usage_error() -> None:
    with pytest.raises(SystemExit) as info:
        _run("deploy")
    assert info.value.code == 2


def test_check_config_makes_no_network_or_subprocess_calls(
    staging_env: dict[str, str],
) -> None:
    """Run the real entry point (`python -m finbot`) under an audit hook in a fresh process."""
    probe = textwrap.dedent(
        """
        import runpy, sys
        events = []
        sys.addaudithook(
            lambda e, a: events.append(e)
            if e.startswith(("socket.", "subprocess.", "os.system", "os.exec")) else None
        )
        sys.argv = ["finbot", "check-config", "--json"]
        try:
            runpy.run_module("finbot", run_name="__main__")
        except SystemExit as exc:
            code = exc.code
        print("AUDIT", code, sorted(set(events)))
        """
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    last = proc.stdout.strip().splitlines()[-1]
    assert last == "AUDIT 0 []", proc.stdout + proc.stderr
    assert SENTINEL_SECRET not in proc.stdout + proc.stderr
