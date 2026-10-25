"""Telegram handlers for the finance core: commands, conversation text and buttons (Step 6).

Each update is handled in three steps, with no business logic here:

1. the verified Telegram sender is resolved to a user (unknown → onboarding explanation only,
   FR-IDN-02; suspended → refused, FR-IDN-06);
2. exactly one service call does the work; the services resolve the active workspace from
   server-side state and authorize inside their database transaction (G-1, G-2);
3. the result is rendered (``finance_render``), or an *expected* error is mapped to a catalogue
   message. Unexpected errors propagate to the application's error handler, which logs only the
   error type and answers with a generic message, so no internal detail reaches the user.

Button presses edit the message they came from, so a confirmed or cancelled draft loses its
buttons. Callback data is decoded strictly (``finance_callbacks``); anything malformed is
answered as a stale button.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from finbot.adapters.telegram import finance_callbacks as cb
from finbot.adapters.telegram import finance_render as render
from finbot.adapters.telegram import messages_id as msg
from finbot.authz.policy import ForbiddenError
from finbot.domain.finance import (
    Category,
    ConflictError,
    DraftExpiredError,
    DuplicateNameError,
    HistoryFilter,
    InvalidInputError,
    NotFoundError,
    PendingKind,
    StepError,
    parse_date,
    parse_month,
)
from finbot.domain.ledger import TxnType
from finbot.services.capture import CaptureService, CategoryDone
from finbot.services.finance import Actor
from finbot.services.ledger import LedgerService, TransactionDetail
from finbot.services.principals import (
    AccountUnavailableError,
    PrincipalService,
    UnknownAccountError,
)

logger = logging.getLogger(__name__)

_NO_PREVIEW: Final = LinkPreviewOptions(is_disabled=True)
_TYPE_WORDS: Final = {"pemasukan": TxnType.INCOME, "pengeluaran": TxnType.EXPENSE}
_VOIDED_WORD: Final = "dihapus"
_NEW_WORD: Final = "tambah"
_CATEGORY_NAME_MAX_ARGS: Final = 200

Reply = render.Reply


@dataclass(frozen=True, slots=True)
class FinanceServices:
    principals: PrincipalService
    ledger: LedgerService
    capture: CaptureService


def _markup(keyboard: render.Keyboard) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in keyboard
        ]
    )


def command_args(text: str | None) -> str:
    """Everything after ``/command`` (or ``/command@bot``)."""
    _, _, rest = (text or "").strip().partition(" ")
    return rest.strip()


def split_amount(args: str) -> tuple[str | None, str | None]:
    """``25rb makan siang`` → (``25rb``, ``makan siang``); ``Rp 25.000 x`` keeps ``Rp``."""
    tokens = args.split()
    if not tokens:
        return None, None
    take = 2 if tokens[0].lower().rstrip(".") == "rp" and len(tokens) > 1 else 1
    amount = " ".join(tokens[:take])
    description = " ".join(tokens[take:]) or None
    return amount, description


def history_filter(args: str, today: date) -> HistoryFilter:
    """``[pemasukan|pengeluaran] [dihapus] [<bulan> | <tanggal> <tanggal>]``."""
    tokens = args.lower().split()
    txn_type = None
    voided = False
    rest: list[str] = []
    for token in tokens:
        if token in _TYPE_WORDS and txn_type is None:
            txn_type = _TYPE_WORDS[token]
        elif token == _VOIDED_WORD:
            voided = True
        else:
            rest.append(token)
    date_from = date_to = None
    if rest:
        date_range = _date_range(rest, today)
        if date_range is None:
            month = parse_month(" ".join(rest), today)
            date_range = month.first_day, min(month.last_day, today)
        date_from, date_to = date_range
    return HistoryFilter(txn_type, date_from, date_to, voided=voided)


def _date_range(tokens: list[str], today: date) -> tuple[date, date] | None:
    """Two dates (``01/10/2026 kemarin``, ``kemarin lusa hari ini``), or None."""
    for split in range(1, len(tokens)):
        try:
            return (
                parse_date(" ".join(tokens[:split]), today),
                parse_date(" ".join(tokens[split:]), today),
            )
        except InvalidInputError:
            continue
    return None


_FIXED_MESSAGES: Final[dict[type[Exception], str]] = {
    UnknownAccountError: msg.START_NEEDS_INVITE,
    AccountUnavailableError: msg.START_ACCOUNT_UNAVAILABLE,
    ForbiddenError: msg.NOT_PERMITTED,
    NotFoundError: msg.NOT_FOUND,
    DraftExpiredError: msg.DRAFT_EXPIRED,
    StepError: msg.USE_BUTTONS,
    cb.InvalidCallbackError: msg.STALE_BUTTON,
}


def error_text(exc: Exception) -> str | None:
    """The catalogue message for an expected error, or None for a system failure."""
    if isinstance(exc, ConflictError):
        return msg.CONFLICT.get(str(exc), msg.CONFLICT["stale"])
    if isinstance(exc, DuplicateNameError):
        return msg.DUPLICATE_CATEGORY.format(name=render.esc(str(exc)))
    if isinstance(exc, InvalidInputError):
        return msg.INVALID.get(exc.reason, msg.INVALID_INPUT)
    for kind, text in _FIXED_MESSAGES.items():
        if isinstance(exc, kind):
            return text
    return None


_EXPECTED: Final = (
    UnknownAccountError,
    AccountUnavailableError,
    ForbiddenError,
    NotFoundError,
    DraftExpiredError,
    ConflictError,
    DuplicateNameError,
    InvalidInputError,
    StepError,
    cb.InvalidCallbackError,
)


async def _send(update: Update, text: str, keyboard: render.Keyboard) -> None:
    """Edit the message a button came from; otherwise reply. Telegram errors: type only."""
    query = update.callback_query
    markup = _markup(keyboard)
    try:
        if query is not None and isinstance(query.message, Message):
            try:
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    link_preview_options=_NO_PREVIEW,
                )
                return
            except BadRequest:
                pass  # "message is not modified" or too old to edit: send a new one
        message = update.effective_message
        if message is not None:
            await message.reply_html(
                text, reply_markup=markup, link_preview_options=_NO_PREVIEW, do_quote=False
            )
    except TelegramError as exc:
        logger.warning(
            "telegram reply failed",
            extra={"event": "telegram.reply_failed", "error_type": type(exc).__name__},
        )


async def _answer(query: CallbackQuery | None, text: str | None = None) -> None:
    if query is None:
        return
    try:
        await query.answer(text)
    except TelegramError as exc:
        logger.warning(
            "callback answer failed",
            extra={"event": "telegram.reply_failed", "error_type": type(exc).__name__},
        )


class FinanceHandlers:
    def __init__(self, services: FinanceServices) -> None:
        self.s = services

    async def _run(self, update: Update, operation: Callable[[Actor], Awaitable[Reply]]) -> None:
        user = update.effective_user
        if user is None:
            return
        try:
            actor = await self.s.principals.actor(user.id)
            text, keyboard = await operation(actor)
        except _EXPECTED as exc:
            logger.info(
                "finance request refused",
                extra={"event": "finance.refused", "error_type": type(exc).__name__},
            )
            text, keyboard = error_text(exc) or msg.GENERIC_ERROR, []
        await _send(update, text, keyboard)

    # ------------------------------------------------------------------- commands

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async def op(_: Actor) -> Reply:
            return msg.HELP, []

        await self._run(update, op)

    async def start_any(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = command_args(_text(update))
        first, _, rest = args.partition(" ")
        txn_type = _TYPE_WORDS.get(first.lower())
        await self._start(update, txn_type, rest if txn_type else args)

    async def start_income(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start(update, TxnType.INCOME, command_args(_text(update)))

    async def start_expense(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._start(update, TxnType.EXPENSE, command_args(_text(update)))

    async def _start(self, update: Update, txn_type: TxnType | None, args: str) -> None:
        amount, description = split_amount(args) if txn_type else (None, None)
        chat = update.effective_chat

        async def op(actor: Actor) -> Reply:
            view = await self.s.capture.start(
                actor,
                txn_type,
                amount_text=amount,
                description=description,
                chat_id=chat.id if chat else None,
            )
            return render.render_draft(view)

        await self._run(update, op)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async def op(actor: Actor) -> Reply:
            cancelled = await self.s.capture.cancel(actor)
            return (msg.CANCELLED if cancelled else msg.NOTHING_TO_CANCEL), []

        await self._run(update, op)

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = command_args(_text(update))

        async def op(actor: Actor) -> Reply:
            today = await self.s.ledger.today(actor) if args else date.min
            filt = history_filter(args, today) if args else HistoryFilter()
            return render.render_history(await self.s.ledger.history(actor, filt))

        await self._run(update, op)

    async def balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        async def op(actor: Actor) -> Reply:
            return render.render_balance(await self.s.ledger.balance(actor))

        await self._run(update, op)

    async def summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = command_args(_text(update))

        async def op(actor: Actor) -> Reply:
            month = parse_month(args, await self.s.ledger.today(actor)) if args else None
            return render.render_summary(await self.s.ledger.summary(actor, month))

        await self._run(update, op)

    async def categories(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = command_args(_text(update))
        first, _, rest = args.partition(" ")

        async def op(actor: Actor) -> Reply:
            if first.lower() == _NEW_WORD:
                kind_word, _, name = rest.strip().partition(" ")
                kind = _TYPE_WORDS.get(kind_word.lower())
                if kind is None or not name.strip() or len(name) > _CATEGORY_NAME_MAX_ARGS:
                    return msg.CATEGORY_USAGE, []
                category = await self.s.ledger.create_category(actor, kind, name)
                return _created_text(category), []
            if first and first.lower() not in _TYPE_WORDS:
                return msg.CATEGORY_USAGE, []
            kind = _TYPE_WORDS.get(first.lower()) if first else None
            return render.render_categories(await self.s.ledger.categories(actor, kind), kind)

        await self._run(update, op)

    # ----------------------------------------------------------------------- text

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = _text(update)
        if not text:
            return

        async def op(actor: Actor) -> Reply:
            outcome = await self.s.capture.input_text(actor, text)
            if outcome is None:
                return msg.NO_CONVERSATION, []
            if isinstance(outcome, CategoryDone):
                return _category_done(outcome), []
            return render.render_draft(outcome)

        await self._run(update, op)

    # -------------------------------------------------------------------- buttons

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        try:
            data = cb.parse(query.data)
        except cb.InvalidCallbackError:
            await _answer(query, msg.STALE_BUTTON)
            return
        await _answer(query)
        await self._run(update, lambda actor: self._dispatch(actor, data))

    async def _dispatch(self, actor: Actor, data: cb.Callback) -> Reply:
        handler = self._BUTTONS.get(data.action)
        if handler is None:
            raise cb.InvalidCallbackError("action")
        return await handler(self, actor, data)

    # Draft buttons: the services check the reference against the caller's pending draft.

    async def _b_type(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_draft(
            await self.s.capture.choose_type(actor, _ref(data), _need(data.txn_type))
        )

    async def _b_category(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_draft(
            await self.s.capture.choose_category(actor, _ref(data), _need(data.entity_id))
        )

    async def _b_skip(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_draft(await self.s.capture.skip_description(actor, _ref(data)))

    async def _b_date(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_draft(await self.s.capture.ask_date(actor, _ref(data)))

    async def _b_confirm(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_confirmed(await self.s.capture.confirm(actor, _ref(data)))

    async def _b_cancel(self, actor: Actor, data: cb.Callback) -> Reply:
        if not await self.s.capture.cancel(actor, _ref(data)):
            raise DraftExpiredError("draft")
        return msg.CANCELLED, []

    # Transactions: looked up in the active workspace; versions guard against stale buttons.

    async def _b_history(self, actor: Actor, data: cb.Callback) -> Reply:
        result = await self.s.ledger.history(actor, _need(data.history), data.page)
        return render.render_history(result)

    async def _b_detail(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_detail(await self.s.ledger.transaction(actor, _need(data.entity_id)))

    async def _current(self, actor: Actor, data: cb.Callback) -> TransactionDetail:
        detail = await self.s.ledger.transaction(actor, _need(data.entity_id))
        if detail.record.version != data.version:
            raise ConflictError("stale")
        return detail

    async def _b_edit_menu(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_edit_menu(await self._current(actor, data))

    async def _b_edit_field(self, actor: Actor, data: cb.Callback) -> Reply:
        view = await self.s.capture.start_edit(
            actor, _need(data.entity_id), _need(data.version), _need(data.field)
        )
        return render.render_draft(view)

    async def _b_delete_ask(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_delete_confirm(await self._current(actor, data))

    async def _b_delete_yes(self, actor: Actor, data: cb.Callback) -> Reply:
        await self.s.ledger.void_transaction(actor, _need(data.entity_id), _need(data.version))
        return msg.DELETED, []

    async def _b_restore(self, actor: Actor, data: cb.Callback) -> Reply:
        record = await self.s.ledger.restore_transaction(
            actor, _need(data.entity_id), _need(data.version)
        )
        return msg.RESTORED, [[(msg.BUTTON_DETAIL, cb.transaction(record.id))]]

    # Categories.

    async def _b_category_list(self, actor: Actor, data: cb.Callback) -> Reply:
        listing = await self.s.ledger.categories(actor, data.txn_type)
        return render.render_categories(listing, data.txn_type)

    async def _b_category_detail(self, actor: Actor, data: cb.Callback) -> Reply:
        return render.render_category(*await self.s.ledger.category(actor, _need(data.entity_id)))

    async def _b_category_new(self, actor: Actor, data: cb.Callback) -> Reply:
        prompt = await self.s.capture.start_category_create(actor, _need(data.txn_type))
        return render.render_category_prompt(prompt)

    async def _b_category_rename(self, actor: Actor, data: cb.Callback) -> Reply:
        prompt = await self.s.capture.start_category_rename(actor, _need(data.entity_id))
        return render.render_category_prompt(prompt)

    async def _b_category_active(self, actor: Actor, data: cb.Callback) -> Reply:
        category = await self.s.ledger.set_category_active(actor, _need(data.entity_id), data.flag)
        template = msg.CATEGORY_ACTIVATED if data.flag else msg.CATEGORY_DEACTIVATED
        return template.format(name=render.esc(category.name)), []

    _BUTTONS: Final[
        dict[str, Callable[["FinanceHandlers", Actor, cb.Callback], Awaitable[Reply]]]
    ] = {
        "ty": _b_type,
        "ca": _b_category,
        "sk": _b_skip,
        "dt": _b_date,
        "ok": _b_confirm,
        "no": _b_cancel,
        "h": _b_history,
        "kh": _b_history,
        "tx": _b_detail,
        "ed": _b_edit_menu,
        "ef": _b_edit_field,
        "dl": _b_delete_ask,
        "dy": _b_delete_yes,
        "rs": _b_restore,
        "kl": _b_category_list,
        "kd": _b_category_detail,
        "kn": _b_category_new,
        "kr": _b_category_rename,
        "ka": _b_category_active,
    }


def _ref(data: cb.Callback) -> str:
    if data.ref is None:
        raise cb.InvalidCallbackError("ref")
    return data.ref


def _need[T](value: T | None) -> T:
    """A field the decoder guarantees for this action (defensive: refuse as stale)."""
    if value is None:
        raise cb.InvalidCallbackError("field")
    return value


def _text(update: Update) -> str:
    message = update.effective_message
    return (message.text or "") if message is not None else ""


def _created_text(category: Category) -> str:
    kind = msg.TYPE_LABEL[category.kind.value].lower()
    return msg.CATEGORY_CREATED.format(name=render.esc(category.name), kind=kind)


def _category_done(done: CategoryDone) -> str:
    if done.kind is PendingKind.CATEGORY_RENAME:
        return msg.CATEGORY_RENAMED.format(name=render.esc(done.category.name))
    return _created_text(done.category)


#: Command → handler method name. The first name is the documented one (docs/02 §12); the
#: others are aliases requested for Step 6.
COMMANDS: Final = {
    ("catat", "tambah"): "start_any",
    ("pemasukan",): "start_income",
    ("pengeluaran",): "start_expense",
    ("riwayat", "transaksi"): "history",
    ("saldo",): "balance",
    ("laporan", "ringkasan"): "summary",
    ("kategori",): "categories",
    ("batal",): "cancel",
    ("bantuan", "help"): "help",
}


def register_finance_handlers(
    application: Application[Any, Any, Any, Any, Any, Any], services: FinanceServices
) -> FinanceHandlers:
    handlers = FinanceHandlers(services)
    private = filters.ChatType.PRIVATE
    for names, method in COMMANDS.items():
        application.add_handler(
            CommandHandler(list(names), getattr(handlers, method), filters=private)
        )
    application.add_handler(CallbackQueryHandler(handlers.button, pattern=r"^f:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & private, handlers.text)
    )
    return handlers
