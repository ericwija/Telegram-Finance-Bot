"""Composition of the Telegram bot from settings: ``finbot bot`` and ``finbot bot --dry-run``.

``--dry-run`` builds the complete application (settings, database engine object, services,
handlers) with a transport that refuses every request, prints what would run, and exits. It
contacts neither Telegram nor PostgreSQL, needs no bot token, and is the local way to check the
wiring.

A real run (Step 5, staging only, operated by ``deploy``) long-polls Telegram. Its lifecycle
(:func:`serve`) is strictly ordered, and polling starts only after every guard passed:

1. G-ENV-2: the live connection's ``current_database()`` equals ``FINBOT_DB_NAME``; then
   G-ENV-3: the database environment marker equals ``FINBOT_ENV``.
2. G-ENV-6: the single-poller advisory lock is taken on a dedicated connection
   (:mod:`finbot.infra.db.poller_lock`). If another process holds it, the bot exits with
   "poller lock held" (AC-P1-04) without ever contacting Telegram.
3. G-ENV-4: ``getMe`` (application initialisation) returns
   ``FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME``.
4. Polling starts. Every ``heartbeat_interval`` the lock is re-checked on its connection and the
   heartbeat file is rewritten. If the check fails (connection lost, backend terminated) polling
   stops at once and the process exits with :data:`PollerLockLostError`, so the container
   restarts and must win the lock again.

SIGTERM and SIGINT request a graceful stop: polling stops, in-flight handlers finish, the
application shuts down, the lock is released and the engine is disposed. Every refusal is a
:class:`BotStartupError` whose message names the guard and never contains a secret.
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO, override

from telegram._utils.defaultvalue import DEFAULT_NONE
from telegram._utils.types import ODVInput
from telegram.ext import Application
from telegram.request import BaseRequest, RequestData

from finbot.adapters.telegram.bot import (
    ALLOWED_UPDATES,
    MAX_CONCURRENT_UPDATES,
    BotServices,
    build_application,
    quiet_transport_loggers,
)
from finbot.adapters.telegram.finance import FinanceServices
from finbot.adapters.telegram.ratelimit import BAD_STARTS_PER_MINUTE, UPDATES_PER_MINUTE
from finbot.config import STRICT_ENVIRONMENTS, Settings
from finbot.infra.heartbeat import BotState, Heartbeat
from finbot.services.capture import CaptureService
from finbot.services.ledger import LedgerService
from finbot.services.onboarding import OnboardingService
from finbot.services.platform_invites import PlatformInviteService
from finbot.services.principals import PrincipalService

logger = logging.getLogger(__name__)

#: Placeholder for --dry-run; never valid for the Bot API, and the transport refuses anyway.
DRY_RUN_TOKEN = "0:dry-run-no-network"  # noqa: S105 - not a secret; refused offline


class BotStartupError(RuntimeError):
    """A start-up guard refused to run the bot."""


class PollerLockLostError(RuntimeError):
    """The poller lock was lost while polling; polling was stopped (G-ENV-6)."""


class HeldLock(Protocol):
    async def verify(self) -> bool: ...
    async def release(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Timings:
    #: How often the lock is re-checked and the heartbeat rewritten.
    heartbeat_interval: float = 10.0


class OfflineRequest(BaseRequest):
    """A transport that refuses every Bot API call (dry runs)."""

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    @override
    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: ODVInput[float] = DEFAULT_NONE,
        write_timeout: ODVInput[float] = DEFAULT_NONE,
        connect_timeout: ODVInput[float] = DEFAULT_NONE,
        pool_timeout: ODVInput[float] = DEFAULT_NONE,
    ) -> tuple[int, bytes]:
        raise RuntimeError("dry run: the Telegram Bot API is never contacted")


def _services(settings: Settings, engine: Any) -> BotServices:
    from finbot.infra.db.finance import PgFinanceStore  # noqa: PLC0415
    from finbot.infra.db.identity import PgIdentityGateway  # noqa: PLC0415
    from finbot.infra.db.platform import PgPlatformGateway  # noqa: PLC0415

    identities = PgIdentityGateway(engine)
    store = PgFinanceStore(engine)
    return BotServices(
        onboarding=OnboardingService(identities),
        invites=PlatformInviteService(identities, PgPlatformGateway(engine)),
        timezone=settings.default_timezone,
        finance=FinanceServices(
            principals=PrincipalService(identities),
            ledger=LedgerService(store),
            capture=CaptureService(store),
        ),
    )


def _engine(settings: Settings, *, offline: bool = False) -> Any:
    from pydantic import SecretStr  # noqa: PLC0415

    from finbot.infra.db.connection import (  # noqa: PLC0415
        ConnectionTarget,
        app_target,
        create_engine,
    )

    if offline:  # never connects; the dry run needs no password
        target = ConnectionTarget(
            settings.db_host,
            settings.db_port,
            settings.db_name,
            settings.db_app_user,
            SecretStr("dry-run"),
            "finbot-bot",
        )
        return create_engine(target)
    return create_engine(app_target(settings, application_name="finbot-bot"))


def dry_run(settings: Settings, out: TextIO) -> None:
    """Build everything offline and describe it. Raises on any wiring error."""
    import asyncio  # noqa: PLC0415

    engine = _engine(settings, offline=True)  # an engine object only; never connected
    try:
        application = build_application(
            DRY_RUN_TOKEN, _services(settings, engine), request=OfflineRequest()
        )
        commands = sorted(
            command
            for handlers in application.handlers.values()
            for handler in handlers
            for command in getattr(handler, "commands", ())
        )
        out.write("bot dry run OK (no Telegram or database connection was made)\n")
        out.write(f"  env                 {settings.env}\n")
        out.write(f"  expected bot        {settings.telegram_expected_bot_username}\n")
        out.write(f"  commands            {', '.join('/' + c for c in commands)}\n")
        out.write(f"  allowed updates     {', '.join(ALLOWED_UPDATES)}\n")
        out.write("  chats               private only (groups are left)\n")
        out.write(
            f"  rate limits         {UPDATES_PER_MINUTE} updates/min per user; "
            f"{BAD_STARTS_PER_MINUTE} bad /start payloads/min per user\n"
        )
        out.write(f"  concurrent updates  {MAX_CONCURRENT_UPDATES}\n")
        out.write("  single poller       advisory lock per environment, re-checked every ")
        out.write(f"{Timings().heartbeat_interval:.0f}s (G-ENV-6)\n")
    finally:
        asyncio.run(engine.dispose())


def _check_username(app: Application[Any, Any, Any, Any, Any, Any], expected: str | None) -> None:
    if expected is not None and app.bot.username.lower() != expected.lstrip("@").lower():
        raise BotStartupError("the bot token belongs to another bot (G-ENV-4)")


async def _guard[T](step: Callable[[], Awaitable[T]], what: str) -> T:
    """Run a start-up step; turn unexpected failures into a refusal naming only the error type."""
    from finbot.infra.db.guards import EnvironmentMismatchError  # noqa: PLC0415
    from finbot.infra.db.poller_lock import PollerLockHeldError  # noqa: PLC0415

    try:
        return await step()
    except BotStartupError:
        raise
    except (EnvironmentMismatchError, PollerLockHeldError) as exc:
        raise BotStartupError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - reported by type only; messages may hold hosts
        raise BotStartupError(f"{what} failed ({type(exc).__name__})") from None


async def serve(
    application: Application[Any, Any, Any, Any, Any, Any],
    *,
    check_environment: Callable[[], Awaitable[None]],
    acquire_lock: Callable[[], Awaitable[HeldLock]],
    expected_username: str | None,
    stop: asyncio.Event,
    heartbeat: Heartbeat,
    timings: Timings = Timings(),  # noqa: B008 - frozen dataclass
) -> None:
    """Guards, then poll until ``stop`` is set or the lock is lost (see the module docstring).

    Raises :class:`BotStartupError` if a guard refuses (nothing was polled) and
    :class:`PollerLockLostError` if the lock was lost while polling (polling was stopped).
    """
    heartbeat.write(BotState.STARTING, lock_held=False, polling=False)
    state = BotState.FAILED
    try:
        await _guard(check_environment, "database environment check")
        lock = await _guard(acquire_lock, "poller lock acquisition")
        logger.info("poller lock acquired", extra={"event": "poller_lock.acquired"})
        lost = False
        try:
            try:
                await _guard(application.initialize, "Telegram getMe")
                _check_username(application, expected_username)
                if stop.is_set():  # a signal arrived during start-up: never start polling
                    state = BotState.STOPPED
                    return
                await application.start()
                if application.updater is None:
                    raise BotStartupError("the application has no updater")
                await application.updater.start_polling(
                    allowed_updates=list(ALLOWED_UPDATES), drop_pending_updates=False
                )
                heartbeat.write(BotState.READY, lock_held=True, polling=True)
                logger.info("bot polling started", extra={"event": "bot.polling_started"})
                lost = await _watch(lock, stop, heartbeat, timings)
            finally:
                heartbeat.write(BotState.STOPPING, lock_held=not lost, polling=False)
                await _stop_application(application)
        finally:
            await lock.release()
            logger.info("poller lock released", extra={"event": "poller_lock.released"})
        if lost:
            raise PollerLockLostError("poller lock lost while polling; polling stopped (G-ENV-6)")
        state = BotState.STOPPED
    finally:
        heartbeat.write(state, lock_held=False, polling=False)
        logger.info("bot stopped", extra={"event": "bot.stopped", "outcome": state.value})


async def _watch(
    lock: HeldLock, stop: asyncio.Event, heartbeat: Heartbeat, timings: Timings
) -> bool:
    """Wait for ``stop``; re-check the lock every interval. Returns True if the lock was lost."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timings.heartbeat_interval)
        except TimeoutError:
            pass
        else:
            return False
        if not await lock.verify():
            logger.error("poller lock lost; stopping polling", extra={"event": "poller_lock.lost"})
            return True
        heartbeat.write(BotState.READY, lock_held=True, polling=True)
    return False


async def _stop_application(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    """Stop fetching first, then let in-flight handlers finish, then shut down."""
    try:
        if application.updater is not None and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
    finally:
        await application.shutdown()


def install_stop_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    """SIGTERM (docker stop) and SIGINT (Ctrl-C) request a graceful stop."""

    def request_stop(sig: signal.Signals) -> None:
        if not stop.is_set():
            logger.info(
                "shutdown requested", extra={"event": "bot.shutdown_requested", "outcome": sig.name}
            )
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop, sig)


def remove_stop_signals(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.remove_signal_handler(sig)


def run_polling(
    settings: Settings,
    *,
    health_file: Path | None = None,
    timings: Timings = Timings(),  # noqa: B008
) -> None:
    """Long-poll Telegram until SIGTERM/SIGINT. Never called by tests or the dry run."""
    if settings.telegram_token is None:
        raise BotStartupError("FINBOT_TELEGRAM_TOKEN_FILE is required to run the bot")
    if settings.env in STRICT_ENVIRONMENTS and settings.telegram_expected_bot_username is None:
        # Configuration already requires it there; never poll with the identity check skipped.
        raise BotStartupError(
            "FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME is required in this environment (G-ENV-4)"
        )
    quiet_transport_loggers()
    asyncio.run(_run(settings, settings.telegram_token.get_secret_value(), health_file, timings))


async def _run(settings: Settings, token: str, health_file: Path | None, timings: Timings) -> None:
    from finbot.infra.db.guards import check_database  # noqa: PLC0415
    from finbot.infra.db.poller_lock import PollerLock  # noqa: PLC0415

    engine = _engine(settings)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    install_stop_signals(loop, stop)
    try:
        await serve(
            build_application(token, _services(settings, engine)),
            check_environment=lambda: check_database(
                engine, env=settings.env, db_name=settings.db_name
            ),
            acquire_lock=lambda: PollerLock.acquire(engine, settings.env),
            expected_username=settings.telegram_expected_bot_username,
            stop=stop,
            heartbeat=Heartbeat(health_file, settings.env),
            timings=timings,
        )
    finally:
        remove_stop_signals(loop)
        await engine.dispose()
