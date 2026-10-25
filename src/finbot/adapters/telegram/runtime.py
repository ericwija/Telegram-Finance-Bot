"""Composition of the Telegram bot from settings: ``finbot bot`` and ``finbot bot --dry-run``.

``--dry-run`` builds the complete application (settings, database engine object, services,
handlers) with a transport that refuses every request, prints what would run, and exits. It
contacts neither Telegram nor PostgreSQL, needs no bot token, and is the local way to check the
wiring.

A real run (Step 5, staging only, operated by ``deploy``) long-polls Telegram. Before any
update is processed it checks the database environment marker (G-ENV-3) and that the token
belongs to ``FINBOT_TELEGRAM_EXPECTED_BOT_USERNAME`` (G-ENV-4). The single-poller advisory
lock (G-ENV-6, AC-P1-04) is not implemented yet; run one bot process per environment.
"""

from typing import Any, TextIO, override

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
from finbot.adapters.telegram.ratelimit import BAD_STARTS_PER_MINUTE, UPDATES_PER_MINUTE
from finbot.config import Settings
from finbot.services.onboarding import OnboardingService
from finbot.services.platform_invites import PlatformInviteService

#: Placeholder for --dry-run; never valid for the Bot API, and the transport refuses anyway.
DRY_RUN_TOKEN = "0:dry-run-no-network"  # noqa: S105 - not a secret; refused offline


class BotStartupError(RuntimeError):
    """A start-up guard refused to run the bot."""


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
    from finbot.infra.db.identity import PgIdentityGateway  # noqa: PLC0415
    from finbot.infra.db.platform import PgPlatformGateway  # noqa: PLC0415

    identities = PgIdentityGateway(engine)
    return BotServices(
        onboarding=OnboardingService(identities),
        invites=PlatformInviteService(identities, PgPlatformGateway(engine)),
        timezone=settings.default_timezone,
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
    finally:
        asyncio.run(engine.dispose())


def run_polling(settings: Settings) -> None:
    """Long-poll Telegram until stopped. Never called by tests or the dry run."""
    from finbot.infra.db.guards import require_environment_marker  # noqa: PLC0415

    if settings.telegram_token is None:
        raise BotStartupError("FINBOT_TELEGRAM_TOKEN_FILE is required to run the bot")
    quiet_transport_loggers()
    engine = _engine(settings)
    application = build_application(
        settings.telegram_token.get_secret_value(), _services(settings, engine)
    )

    async def post_init(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await require_environment_marker(engine, settings.env)
        expected = settings.telegram_expected_bot_username
        if expected is not None and app.bot.username.lower() != expected.lstrip("@").lower():
            raise BotStartupError("the bot token belongs to another bot (G-ENV-4)")

    async def post_shutdown(_: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await engine.dispose()

    application.post_init = post_init
    application.post_shutdown = post_shutdown
    application.run_polling(allowed_updates=list(ALLOWED_UPDATES), drop_pending_updates=False)
