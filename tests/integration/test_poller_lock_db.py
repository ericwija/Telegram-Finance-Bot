"""The single-poller lock on a real PostgreSQL 18.6 as finbot_app (G-ENV-6, AT-LOCK-01, AC-P1-04).

Competing pollers, connection loss (the lock's backend is terminated), restart after loss,
graceful stop and lock release, first with :class:`PollerLock` alone and then through the full
:func:`serve` lifecycle with the fake Bot API. Runs only with FINBOT_TEST_PG_BACKEND set.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from finbot.adapters.telegram.bot import BotServices, build_application
from finbot.adapters.telegram.runtime import BotStartupError, PollerLockLostError, Timings, serve
from finbot.infra.db.guards import require_environment_marker
from finbot.infra.db.identity import PgIdentityGateway
from finbot.infra.db.platform import PgPlatformGateway
from finbot.infra.db.poller_lock import POLLER_LOCK_CLASS, PollerLock, PollerLockHeldError
from finbot.infra.heartbeat import BotState, Heartbeat, check_health
from finbot.services.onboarding import OnboardingService
from finbot.services.platform_invites import PlatformInviteService
from tests.support.telegram import BOT_TOKEN, BOT_USERNAME, FakeBotApi, eventually

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ENV = "test"


async def lock_holders(engine: Any) -> list[int]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT pid FROM pg_catalog.pg_locks WHERE locktype = 'advisory' AND granted "
                "AND classid::bigint = :cls AND objsubid = 2"
            ),
            {"cls": POLLER_LOCK_CLASS},
        )
        return [int(pid) for (pid,) in rows]


async def terminate(engine: Any, pid: int) -> None:
    """Simulate connection loss: finbot_app may terminate its own role's backends."""
    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT pg_catalog.pg_terminate_backend(:pid)"), {"pid": pid})
        ).scalar_one()


# ---------------------------------------------------------------------------- the lock


async def test_AT_LOCK_01_second_poller_cannot_take_the_lock(
    migrated_database: str, app_engine: Any
) -> None:
    first = await PollerLock.acquire(app_engine, ENV)
    try:
        assert await first.verify()
        with pytest.raises(PollerLockHeldError, match="poller lock held"):
            await PollerLock.acquire(app_engine, ENV)
        assert await lock_holders(app_engine) == [first.backend_pid]
    finally:
        await first.release()
    assert await lock_holders(app_engine) == []
    second = await PollerLock.acquire(app_engine, ENV)  # free again after a clean release
    await second.release()
    await second.release()  # idempotent
    assert not await second.verify()


async def test_lock_is_scoped_to_the_environment(migrated_database: str, app_engine: Any) -> None:
    test_lock = await PollerLock.acquire(app_engine, "test")
    other = await PollerLock.acquire(app_engine, "dev")
    try:
        assert await test_lock.verify()
        assert await other.verify()
    finally:
        await other.release()
        await test_lock.release()


async def test_connection_loss_releases_the_lock_and_a_restart_takes_it(
    migrated_database: str, app_engine: Any
) -> None:
    first = await PollerLock.acquire(app_engine, ENV)
    await terminate(app_engine, first.backend_pid)
    assert not await first.verify()  # the holder notices the loss
    await first.release()  # and cleaning up a dead connection never raises
    restarted = await PollerLock.acquire(app_engine, ENV)
    try:
        assert await restarted.verify()
        assert await lock_holders(app_engine) == [restarted.backend_pid]
    finally:
        await restarted.release()


async def test_lock_connection_is_not_left_in_a_transaction(
    migrated_database: str, app_engine: Any
) -> None:
    lock = await PollerLock.acquire(app_engine, ENV)
    try:
        await lock.verify()
        async with app_engine.connect() as conn:
            state = (
                await conn.execute(
                    text("SELECT state FROM pg_catalog.pg_stat_activity WHERE pid = :pid"),
                    {"pid": lock.backend_pid},
                )
            ).scalar_one()
        assert state == "idle"
    finally:
        await lock.release()


# ----------------------------------------------------------------------- full lifecycle


class Poller:
    def __init__(self, app_engine: Any, health: Path) -> None:
        identities = PgIdentityGateway(app_engine)
        self.api = FakeBotApi()
        self.app = build_application(
            BOT_TOKEN,
            BotServices(
                onboarding=OnboardingService(identities),
                invites=PlatformInviteService(identities, PgPlatformGateway(app_engine)),
            ),
            request=self.api,
        )
        self.engine = app_engine
        self.stop = asyncio.Event()
        self.health = health

    def serve(self) -> Any:
        return serve(
            self.app,
            check_environment=lambda: require_environment_marker(self.engine, ENV),
            acquire_lock=lambda: PollerLock.acquire(self.engine, ENV),
            expected_username=BOT_USERNAME,
            stop=self.stop,
            heartbeat=Heartbeat(self.health, ENV),
            timings=Timings(heartbeat_interval=0.05),
        )

    def polling(self) -> bool:
        return self.api.count("getUpdates") > 0


async def test_AC_P1_04_competing_pollers_loss_restart_and_shutdown(
    migrated_database: str, app_engine: Any, tmp_path: Path
) -> None:
    a = Poller(app_engine, tmp_path / "a.json")
    a_task = asyncio.create_task(a.serve())
    await eventually(a.polling)
    assert check_health(a.health).healthy

    # A second process is refused before it contacts Telegram.
    b = Poller(app_engine, tmp_path / "b.json")
    with pytest.raises(BotStartupError, match="poller lock held"):
        await b.serve()
    assert b.api.calls == []
    assert not check_health(b.health).healthy

    # Connection loss: A stops polling and exits with "lock lost".
    (holder,) = await lock_holders(app_engine)
    await terminate(app_engine, holder)
    with pytest.raises(PollerLockLostError):
        await asyncio.wait_for(a_task, 10)
    assert a.app.updater is not None
    assert not a.app.updater.running
    polls = a.api.count("getUpdates")
    await asyncio.sleep(0.2)
    assert a.api.count("getUpdates") == polls
    assert check_health(a.health).reason == "bot state is failed"

    # Restart: a new process takes the lock and polls.
    c = Poller(app_engine, tmp_path / "c.json")
    c_task = asyncio.create_task(c.serve())
    await eventually(c.polling)
    assert len(await lock_holders(app_engine)) == 1

    # Graceful stop (what SIGTERM does): polling stops and the lock is released.
    c.stop.set()
    await asyncio.wait_for(c_task, 10)
    assert await lock_holders(app_engine) == []
    assert not c.app.running
    assert check_health(c.health).reason == f"bot state is {BotState.STOPPED}"
