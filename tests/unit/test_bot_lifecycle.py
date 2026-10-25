"""The bot lifecycle (Step 4D): guard order, poller lock, shutdown, heartbeat and healthcheck.

The real PTB application runs against the fake Bot API; the environment check and the lock are
fakes here. ``tests/integration/test_poller_lock_db.py`` runs the same lifecycle on PostgreSQL.
"""

import asyncio
import io
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from finbot.adapters.telegram import runtime
from finbot.adapters.telegram.bot import BotServices, build_application
from finbot.adapters.telegram.runtime import (
    BotStartupError,
    PollerLockLostError,
    Timings,
    install_stop_signals,
    remove_stop_signals,
    serve,
)
from finbot.infra.db.guards import EnvironmentMismatchError
from finbot.infra.db.poller_lock import PollerLockHeldError
from finbot.infra.heartbeat import BotState, Heartbeat, check_health
from finbot.services.onboarding import OnboardingService
from finbot.services.platform_invites import PlatformInviteService
from tests.support.telegram import BOT_TOKEN, BOT_USERNAME, FakeBotApi, eventually
from tests.unit.test_onboarding_service import FakeGateway
from tests.unit.test_platform_invites import FakeIdentities, FakePlatform

FAST = Timings(heartbeat_interval=0.02)


class FakeLock:
    def __init__(self, verdicts: list[bool] | None = None) -> None:
        self.verdicts = verdicts or []
        self.checks = 0
        self.released = False

    async def verify(self) -> bool:
        self.checks += 1
        return self.verdicts.pop(0) if self.verdicts else True

    async def release(self) -> None:
        self.released = True


class Run:
    def __init__(self, tmp_path: Path, *, expected_username: str | None = BOT_USERNAME) -> None:
        self.api = FakeBotApi()
        self.app = build_application(
            BOT_TOKEN,
            BotServices(
                onboarding=OnboardingService(FakeGateway()),
                invites=PlatformInviteService(FakeIdentities(None), FakePlatform()),
            ),
            request=self.api,
        )
        self.lock = FakeLock()
        self.steps: list[str] = []
        self.stop = asyncio.Event()
        self.health = tmp_path / "heartbeat.json"
        self.expected_username = expected_username
        self.check_environment: Callable[[], Awaitable[None]] = self._env_ok
        self.acquire_lock: Callable[[], Awaitable[Any]] = self._lock_ok

    async def _env_ok(self) -> None:
        self.steps.append("env")

    async def _lock_ok(self) -> FakeLock:
        self.steps.append("lock")
        return self.lock

    def serve(self) -> Awaitable[None]:
        return serve(
            self.app,
            check_environment=self.check_environment,
            acquire_lock=self.acquire_lock,
            expected_username=self.expected_username,
            stop=self.stop,
            heartbeat=Heartbeat(self.health, "test"),
            timings=FAST,
        )

    def state(self) -> dict[str, Any]:
        return dict(json.loads(self.health.read_text()))


# ------------------------------------------------------------------------------- guards


@pytest.mark.asyncio
async def test_environment_mismatch_refuses_before_the_lock_and_telegram(tmp_path: Path) -> None:
    run = Run(tmp_path)

    async def mismatch() -> None:
        raise EnvironmentMismatchError("database environment marker is 'x' (G-ENV-3)")

    run.check_environment = mismatch
    with pytest.raises(BotStartupError, match="G-ENV-3"):
        await run.serve()
    assert run.steps == []
    assert run.api.calls == []
    assert run.state()["state"] == BotState.FAILED


@pytest.mark.asyncio
async def test_AT_LOCK_01_lock_held_refuses_without_contacting_telegram(tmp_path: Path) -> None:
    run = Run(tmp_path)

    async def held() -> FakeLock:
        raise PollerLockHeldError("poller lock held by another process (G-ENV-6)")

    run.acquire_lock = held
    with pytest.raises(BotStartupError, match="poller lock held"):
        await run.serve()
    assert run.api.calls == []
    assert run.state() | {"updated_at": None, "pid": None} == {
        "state": "failed",
        "env": "test",
        "lock_held": False,
        "polling": False,
        "updated_at": None,
        "pid": None,
    }


@pytest.mark.asyncio
async def test_wrong_bot_identity_refuses_after_the_lock_and_releases_it(tmp_path: Path) -> None:
    run = Run(tmp_path, expected_username="@other_bot")
    with pytest.raises(BotStartupError, match="G-ENV-4"):
        await run.serve()
    assert run.steps == ["env", "lock"]
    assert run.api.count("getMe") == 1
    assert run.api.count("getUpdates") == 0
    assert run.lock.released


@pytest.mark.asyncio
async def test_unexpected_start_up_errors_are_reported_by_type_only(tmp_path: Path) -> None:
    run = Run(tmp_path)

    async def unreachable() -> None:
        raise OSError("connect to db.internal:5432 as finbot_app password=hunter2 failed")

    run.check_environment = unreachable
    with pytest.raises(BotStartupError) as caught:
        await run.serve()
    assert str(caught.value) == "database environment check failed (OSError)"


@pytest.mark.asyncio
async def test_telegram_failure_at_get_me_is_a_refusal(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.api.failures["getMe"] = (401, "Unauthorized")
    with pytest.raises(BotStartupError, match=r"Telegram getMe failed \(InvalidToken\)"):
        await run.serve()
    assert run.lock.released
    assert BOT_TOKEN not in str(run.health.read_text())


# ---------------------------------------------------------------------------- lifecycle


@pytest.mark.asyncio
async def test_polls_after_every_guard_then_stops_cleanly(tmp_path: Path) -> None:
    run = Run(tmp_path)
    task = asyncio.create_task(run.serve())
    await eventually(lambda: run.api.count("getUpdates") > 0)
    assert run.steps == ["env", "lock"]
    endpoints = [name for name, _ in run.api.calls]
    assert endpoints.index("getMe") < endpoints.index("getUpdates")
    await eventually(lambda: run.lock.checks >= 2)
    assert check_health(run.health).healthy
    assert run.app.updater is not None
    assert run.app.updater.running

    run.stop.set()
    await asyncio.wait_for(task, 5)
    assert not run.app.updater.running
    assert not run.app.running
    assert run.lock.released
    assert run.state()["state"] == BotState.STOPPED
    assert not check_health(run.health).healthy
    polls = run.api.count("getUpdates")
    await asyncio.sleep(0.1)
    assert run.api.count("getUpdates") == polls


@pytest.mark.asyncio
async def test_lost_lock_stops_polling_and_raises(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.lock.verdicts = [True, False]
    with pytest.raises(PollerLockLostError, match="G-ENV-6"):
        await asyncio.wait_for(run.serve(), 5)
    assert run.lock.checks == 2
    assert run.lock.released
    assert run.app.updater is not None
    assert not run.app.updater.running
    assert run.state()["state"] == BotState.FAILED
    polls = run.api.count("getUpdates")
    await asyncio.sleep(0.1)
    assert run.api.count("getUpdates") == polls


@pytest.mark.asyncio
async def test_stop_requested_during_start_up_never_polls(tmp_path: Path) -> None:
    run = Run(tmp_path)
    run.stop.set()
    await run.serve()
    assert run.api.count("getUpdates") == 0
    assert run.lock.released
    assert run.state()["state"] == BotState.STOPPED


@pytest.mark.asyncio
async def test_sigterm_and_sigint_request_a_graceful_stop(caplog: pytest.LogCaptureFixture) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        stop = asyncio.Event()
        install_stop_signals(loop, stop)
        try:
            with caplog.at_level(logging.INFO):
                os.kill(os.getpid(), sig)
                await asyncio.wait_for(stop.wait(), 2)
        finally:
            remove_stop_signals(loop)
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events.count("bot.shutdown_requested") == 2


@pytest.mark.asyncio
async def test_lifecycle_logs_hold_no_token_and_name_each_step(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    run = Run(tmp_path)
    run.lock.verdicts = [False]
    with caplog.at_level(logging.DEBUG), pytest.raises(PollerLockLostError):
        await asyncio.wait_for(run.serve(), 5)
    events = [getattr(r, "event", None) for r in caplog.records]
    for event in ("poller_lock.acquired", "bot.polling_started", "poller_lock.lost"):
        assert event in events
    assert events.index("poller_lock.lost") < events.index("poller_lock.released")
    assert BOT_TOKEN not in caplog.text
    assert BOT_TOKEN.split(":")[1] not in caplog.text


# ---------------------------------------------------------------------------- heartbeat


def test_heartbeat_holds_only_operational_fields(tmp_path: Path) -> None:
    path = tmp_path / "hb.json"
    Heartbeat(path, "staging").write(BotState.READY, lock_held=True, polling=True)
    data = json.loads(path.read_text())
    assert set(data) == {"state", "env", "pid", "lock_held", "polling", "updated_at"}
    assert data["pid"] == os.getpid()
    assert path.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["hb.json"]  # no temporary file left


def test_heartbeat_without_a_path_writes_nothing(tmp_path: Path) -> None:
    Heartbeat(None, "dev").write(BotState.READY, lock_held=True, polling=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("state", "lock_held", "polling", "age", "healthy", "reason"),
    [
        (BotState.READY, True, True, 5, True, "ready"),
        (BotState.READY, True, True, 61, False, "old"),
        (BotState.READY, False, True, 5, False, "poller lock"),
        (BotState.STARTING, False, False, 5, False, "starting"),
        (BotState.STOPPED, False, False, 5, False, "stopped"),
        (BotState.FAILED, False, False, 5, False, "failed"),
    ],
)
def test_check_health(
    tmp_path: Path,
    state: BotState,
    lock_held: bool,
    polling: bool,
    age: int,
    healthy: bool,
    reason: str,
) -> None:
    path = tmp_path / "hb.json"
    Heartbeat(path, "test").write(state, lock_held=lock_held, polling=polling)
    report = check_health(path, now=datetime.now(UTC) + timedelta(seconds=age))
    assert report.healthy is healthy
    assert reason in report.reason


def test_check_health_missing_or_corrupt_file(tmp_path: Path) -> None:
    assert check_health(tmp_path / "absent").reason == "no heartbeat file"
    corrupt = tmp_path / "corrupt"
    corrupt.write_text("{not json")
    assert check_health(corrupt).reason == "unreadable heartbeat file"


def test_healthcheck_command_exit_codes(tmp_path: Path) -> None:
    from finbot.__main__ import main  # noqa: PLC0415

    path = tmp_path / "hb.json"
    args = ["healthcheck", "--health-file", str(path)]
    assert main(args, out=io.StringIO(), err=io.StringIO()) == 1
    Heartbeat(path, "test").write(BotState.READY, lock_held=True, polling=True)
    out = io.StringIO()
    assert main([*args, "--json"], out=out, err=io.StringIO()) == 0
    assert json.loads(out.getvalue())["healthy"] is True
    assert main([*args, "--max-age", "-1"], out=io.StringIO(), err=io.StringIO()) == 1


@pytest.mark.parametrize(
    ("error", "code", "prefix"),
    [
        (BotStartupError("poller lock held by another process (G-ENV-6)"), 3, "refused:"),
        (PollerLockLostError("poller lock lost while polling (G-ENV-6)"), 4, "stopped:"),
    ],
)
def test_bot_command_exit_codes(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: int, prefix: str
) -> None:
    from finbot.__main__ import main  # noqa: PLC0415

    def fail(*_: Any, **__: Any) -> None:
        raise error

    monkeypatch.setenv("FINBOT_ENV", "dev")
    monkeypatch.setattr(runtime, "run_polling", fail)
    err = io.StringIO()
    assert main(["bot"], out=io.StringIO(), err=err) == code
    assert err.getvalue().startswith(prefix)
    assert "G-ENV-6" in err.getvalue()
