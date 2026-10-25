"""python-telegram-bot wiring: handlers, gates and error handling (Step 4C).

Order of processing for every update (handler group -1 runs first):

1. **Duplicates** (same ``update_id`` seen recently) are dropped.
2. **Non-private chats** are ignored (T16). When the bot is added to a group, supergroup or
   channel it leaves at once.
3. Updates without a human sender are ignored.
4. **Per-user rate limit** (NFR-SEC-07, 30/min): updates over it are dropped, with a single
   notice per window. This happens before any database access.

Then the command handlers run (private chats only): ``/start`` with deep links (FR-IDN-01/02/
07), the platform-admin invite commands and the finance commands, conversation text and buttons
(``finance.py``; callback queries pass the same gate). The acting user is always the verified
Telegram sender. ``/start`` payloads are also limited per account (T17, 10 bad payloads/min)
before the database is touched; the refusal text never depends on whether the account or invite
exists.

Replies use ``parse_mode=HTML``; the reply builders escape every interpolated value. Telegram
API failures on a reply (the user blocked the bot, a bad request, flood control) are logged by
type only. Unexpected handler errors are logged by type only and answered with a generic
message. Message texts, tokens and credentials are never logged.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    BaseUpdateProcessor,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
    filters,
)
from telegram.request import BaseRequest

from finbot.adapters.telegram import admin_invites
from finbot.adapters.telegram import messages_id as msg
from finbot.adapters.telegram.finance import FinanceServices, register_finance_handlers
from finbot.adapters.telegram.ratelimit import (
    BAD_STARTS_PER_MINUTE,
    UPDATES_PER_MINUTE,
    RecentUpdates,
    SlidingWindowLimiter,
)
from finbot.adapters.telegram.start import StartUpdate, render, start_request
from finbot.services.onboarding import OnboardingService, StartOutcome
from finbot.services.platform_invites import PlatformInviteService

logger = logging.getLogger(__name__)

#: Update types the bot asks Telegram for; everything else is never delivered.
ALLOWED_UPDATES = (Update.MESSAGE, Update.CALLBACK_QUERY, Update.MY_CHAT_MEMBER)
#: Bounded so that concurrent handlers stay well under finbot_app's CONNECTION LIMIT 20.
MAX_CONCURRENT_UPDATES = 8
_BAD_START = frozenset({StartOutcome.NEEDS_INVITE, StartOutcome.INVITE_REFUSED})
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


@dataclass(frozen=True, slots=True)
class BotServices:
    onboarding: OnboardingService
    invites: PlatformInviteService
    timezone: str = "Asia/Jakarta"  # for listing invite expiry (G-7 default)
    finance: FinanceServices | None = None  # Step 6: the finance commands and buttons


@dataclass(slots=True)
class BotGuards:
    updates: SlidingWindowLimiter = field(
        default_factory=lambda: SlidingWindowLimiter(UPDATES_PER_MINUTE)
    )
    bad_starts: SlidingWindowLimiter = field(
        default_factory=lambda: SlidingWindowLimiter(BAD_STARTS_PER_MINUTE)
    )
    seen: RecentUpdates = field(default_factory=RecentUpdates)


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_html(text, link_preview_options=_NO_PREVIEW, do_quote=False)
    except TelegramError as exc:  # Forbidden (blocked), BadRequest, RetryAfter, NetworkError
        logger.warning(
            "telegram reply failed",
            extra={"event": "telegram.reply_failed", "error_type": type(exc).__name__},
        )


class _Handlers:
    def __init__(self, services: BotServices, guards: BotGuards) -> None:
        self.services = services
        self.guards = guards

    # ------------------------------------------------------------------------------ gate

    async def gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.guards.seen.first_time(update.update_id):
            logger.info("duplicate update dropped", extra={"event": "telegram.duplicate"})
            raise ApplicationHandlerStop
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            raise ApplicationHandlerStop  # T16; my_chat_member handled in leave_groups
        user = update.effective_user
        if user is None or user.is_bot:
            raise ApplicationHandlerStop
        decision = self.guards.updates.hit(user.id)
        if not decision.allowed:
            logger.info("update rate limited", extra={"event": "security.rate_limited"})
            if decision.notify:
                await _reply(update, msg.RATE_LIMITED)
            raise ApplicationHandlerStop

    async def leave_groups(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        member = update.my_chat_member
        if member is None or member.chat.type == ChatType.PRIVATE:
            return
        if member.new_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            raise ApplicationHandlerStop
        try:
            await context.bot.leave_chat(member.chat.id)
            logger.info("left non-private chat", extra={"event": "telegram.left_group"})
        except TelegramError as exc:
            logger.warning(
                "leaving chat failed",
                extra={"event": "telegram.leave_failed", "error_type": type(exc).__name__},
            )
        raise ApplicationHandlerStop

    # --------------------------------------------------------------------------- /start

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message, user, chat = update.effective_message, update.effective_user, update.effective_chat
        if message is None or user is None or chat is None:
            return
        request = start_request(
            StartUpdate(user.id, chat.type, user.first_name, message.text or "")
        )
        if request is None:
            return
        if request.payload is not None:
            limit = self.guards.bad_starts.check(user.id)
            if not limit.allowed:
                logger.info("start payload rate limited", extra={"event": "security.rate_limited"})
                if limit.notify:
                    await _reply(update, msg.RATE_LIMITED)
                return
        result = await self.services.onboarding.start(request)
        if request.payload is not None and result.outcome in _BAD_START:
            self.guards.bad_starts.record(user.id)
        await _reply(update, render(result))

    # -------------------------------------------------------------------- admin invites

    async def invite_create(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (user := update.effective_user) and (message := update.effective_message):
            await _reply(
                update,
                await admin_invites.create_invite(
                    self.services.invites,
                    user.id,
                    message.text or "",
                    bot_username=context.bot.username,
                ),
            )

    async def invite_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (user := update.effective_user) and (message := update.effective_message):
            await _reply(
                update,
                await admin_invites.list_invites(
                    self.services.invites,
                    user.id,
                    message.text or "",
                    timezone=self.services.timezone,
                ),
            )

    async def invite_revoke(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (user := update.effective_user) and (message := update.effective_message):
            await _reply(
                update,
                await admin_invites.revoke_invite(
                    self.services.invites, user.id, message.text or ""
                ),
            )

    # ---------------------------------------------------------------------------- errors

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(
            "update handler failed",
            extra={"event": "telegram.handler_error", "error_type": type(context.error).__name__},
        )
        if (
            isinstance(update, Update)
            and update.effective_chat is not None
            and update.effective_chat.type == ChatType.PRIVATE
        ):
            await _reply(update, msg.GENERIC_ERROR)


def register_handlers(
    application: Application[Any, Any, Any, Any, Any, Any],
    services: BotServices,
    guards: BotGuards | None = None,
) -> None:
    handlers = _Handlers(services, guards or BotGuards())
    private = filters.ChatType.PRIVATE
    application.add_handler(
        ChatMemberHandler(handlers.leave_groups, ChatMemberHandler.MY_CHAT_MEMBER), group=-2
    )
    application.add_handler(TypeHandler(Update, handlers.gate), group=-1)
    application.add_handler(CommandHandler("start", handlers.start, filters=private))
    application.add_handler(
        CommandHandler(admin_invites.CREATE_COMMAND, handlers.invite_create, filters=private)
    )
    application.add_handler(
        CommandHandler(admin_invites.LIST_COMMAND, handlers.invite_list, filters=private)
    )
    application.add_handler(
        CommandHandler(admin_invites.REVOKE_COMMAND, handlers.invite_revoke, filters=private)
    )
    if services.finance is not None:
        register_finance_handlers(application, services.finance)
    application.add_error_handler(handlers.error)


def build_application(
    token: str,
    services: BotServices,
    *,
    guards: BotGuards | None = None,
    request: BaseRequest | None = None,
    concurrent_updates: int | bool | BaseUpdateProcessor = MAX_CONCURRENT_UPDATES,
) -> Application[Any, Any, Any, Any, Any, Any]:
    """The configured application. Nothing connects until it is initialized or run."""
    builder = ApplicationBuilder().token(token).concurrent_updates(concurrent_updates)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    application = builder.build()
    register_handlers(application, services, guards)
    return application


def quiet_transport_loggers() -> None:
    """httpx logs every request URL at INFO, and Bot API URLs contain the bot token."""
    for name in ("httpx", "httpcore", "telegram", "telegram.ext"):
        logging.getLogger(name).setLevel(logging.WARNING)
