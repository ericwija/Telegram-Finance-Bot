"""The finance handlers over the in-memory store and a fake Bot API (fast; the end-to-end flows
on PostgreSQL are ``tests/integration/test_telegram_finance_db.py``)."""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from finbot.adapters.telegram import messages_id as msg
from finbot.adapters.telegram.bot import (
    BotGuards,
    BotServices,
    build_application,
    quiet_transport_loggers,
)
from finbot.adapters.telegram.finance import FinanceServices
from finbot.adapters.telegram.ratelimit import RecentUpdates, SlidingWindowLimiter
from finbot.domain.finance import RetryLaterError
from finbot.domain.identity import PrincipalRecord, UserStatus, WorkspaceSummary
from finbot.services.capture import CaptureService
from finbot.services.ledger import LedgerService
from finbot.services.onboarding import OnboardingService
from finbot.services.platform_invites import PlatformInviteService
from finbot.services.principals import PrincipalService
from tests.support.fake_finance import FakeFinanceStore
from tests.support.finance import Clock
from tests.support.telegram import (
    BOT_TOKEN,
    Buttons,
    FakeBotApi,
    callback_update,
    message_update,
    shown,
)
from tests.unit.test_platform_invites import FakePlatform

pytestmark = pytest.mark.asyncio
USER, SUSPENDED, STRANGER = 4401, 4402, 4403


class Identities:
    def __init__(self, users: dict[int, PrincipalRecord]) -> None:
        self.users = users

    async def resolve_principal(self, telegram_user_id: int) -> PrincipalRecord | None:
        return self.users.get(telegram_user_id)

    async def redeem_onboarding_invite(self, *_: Any) -> uuid.UUID | None:
        return None

    async def personal_workspace(self, user_id: uuid.UUID) -> WorkspaceSummary | None:
        return WorkspaceSummary(uuid.uuid4(), "Pribadi")


class Bot:
    def __init__(self) -> None:
        self.store = FakeFinanceStore()
        user, _ = self.store.add_person()
        suspended, _ = self.store.add_person()
        identities = Identities({
            USER: PrincipalRecord(user, UserStatus.ACTIVE, False),
            SUSPENDED: PrincipalRecord(suspended, UserStatus.SUSPENDED, False),
        })  # fmt: skip
        clock = self.clock = Clock()
        self.user_id = user
        self.services = BotServices(
            onboarding=OnboardingService(identities),
            invites=PlatformInviteService(identities, FakePlatform()),
            finance=FinanceServices(
                PrincipalService(identities),
                LedgerService(self.store, clock=clock),
                CaptureService(self.store, clock=clock),
            ),
        )
        self.api = FakeBotApi()
        self.app = self._build()

    def _build(self) -> Any:
        guards = BotGuards(SlidingWindowLimiter(1000), SlidingWindowLimiter(1000), RecentUpdates())
        return build_application(BOT_TOKEN, self.services, guards=guards, request=self.api)

    async def restart(self) -> None:
        """A new process: nothing in memory survives, the database (here: the store) does."""
        await self.app.shutdown()
        self.app = self._build()
        await self.app.initialize()

    async def send(self, text: str, user: int = USER) -> tuple[str, Buttons]:
        await self.app.process_update(message_update(self.app, user, text))
        return shown(self.api, user)[-1]

    async def press(self, data: str, user: int = USER) -> tuple[str, Buttons]:
        await self.app.process_update(callback_update(self.app, user, data))
        return shown(self.api, user)[-1]


def button(buttons: Buttons, label: str) -> str:
    return next(data for text, data in buttons if text == label)


@pytest_asyncio.fixture
async def bot() -> AsyncIterator[Bot]:
    harness = Bot()
    await harness.app.initialize()
    yield harness
    await harness.app.shutdown()


async def _save_expense(bot: Bot, args: str, category: str) -> None:
    _, buttons = await bot.send(f"/pengeluaran {args}")
    _, buttons = await bot.press(button(buttons, category))
    if any(label == msg.BUTTON_SKIP for label, _ in buttons):
        _, buttons = await bot.press(button(buttons, msg.BUTTON_SKIP))
    await bot.press(button(buttons, msg.BUTTON_SAVE))


async def test_capture_history_detail_and_corrections(bot: Bot) -> None:
    _, buttons = await bot.send("/catat")
    text, buttons = await bot.press(button(buttons, msg.BUTTON_INCOME))
    text, buttons = await bot.send("3jt")
    text, buttons = await bot.press(button(buttons, "Gaji"))
    text, buttons = await bot.send("gaji")
    text, buttons = await bot.press(button(buttons, msg.BUTTON_SAVE))
    assert text.startswith("✅ Tersimpan: ⬆️ Pemasukan Rp 3.000.000")
    await _save_expense(bot, "25rb kopi", "Makanan")
    text, buttons = await bot.send("/riwayat pengeluaran")
    assert "Filter: pengeluaran" in text
    text, detail = await bot.press(button(buttons, "1"))
    for label, field_text in (
        ("Kategori", "Belanja"),
        ("Keterangan", None),
        ("Tanggal", "kemarin"),
    ):
        _, menu = await bot.press(button(detail, msg.BUTTON_EDIT))
        text, edit = await bot.press(button(menu, label))
        if label == "Kategori":
            text, edit = await bot.press(button(edit, "Belanja"))
        elif field_text is None:
            text, edit = await bot.press(button(edit, msg.BUTTON_CLEAR_DESCRIPTION))
        else:
            text, edit = await bot.send(field_text)
        text, _ = await bot.press(button(edit, msg.BUTTON_SAVE))
        assert text.startswith(msg.UPDATED)
        text, detail = await bot.press(button(_, msg.BUTTON_DETAIL))
    assert "Kategori: Belanja" in text
    assert "Tanggal: 24/10/2026" in text
    _, confirm = await bot.press(button(detail, msg.BUTTON_DELETE))
    text, _ = await bot.press(button(confirm, msg.BUTTON_CONFIRM_DELETE))
    assert text == msg.DELETED
    _, deleted = await bot.send("/riwayat dihapus")
    _, detail = await bot.press(button(deleted, "1"))
    text, after = await bot.press(button(detail, msg.BUTTON_RESTORE))
    assert text == msg.RESTORED
    text, _ = await bot.press(button(after, msg.BUTTON_DETAIL))
    assert "tercatat" in text


async def test_reports_and_pagination(bot: Bot) -> None:
    for n in range(11):
        await _save_expense(bot, f"{n + 1}rb", "Makanan")
    text, buttons = await bot.send("/transaksi")
    text, buttons = await bot.press(button(buttons, msg.BUTTON_NEXT))
    assert "11. " in text
    text, buttons = await bot.press(button(buttons, msg.BUTTON_PREV))
    assert "1. " in text
    text, _ = await bot.send("/saldo")
    assert "Kas: -Rp 66.000 ⚠️" in text
    text, _ = await bot.send("/laporan")
    assert "• Makanan: Rp 66.000 (11 transaksi)" in text
    text, _ = await bot.send("/laporan bulan lalu")
    assert msg.SUMMARY_EMPTY in text
    text, _ = await bot.send("/riwayat 01/10/2026 hari ini")
    assert "01/10/2026 s.d. 25/10/2026" in text
    text, _ = await bot.send("/riwayat 2000-01")
    assert msg.HISTORY_EMPTY in text


async def test_categories_from_buttons_and_commands(bot: Bot) -> None:
    text, buttons = await bot.send("/kategori")
    assert "Pemasukan" in text
    text, buttons = await bot.press(button(buttons, msg.BUTTON_NEW_INCOME_CATEGORY))
    text, _ = await bot.send("Sewa Kos")
    assert "Sewa Kos" in text
    text, _ = await bot.send("/kategori tambah pemasukan Dividen")
    assert "Dividen" in text
    text, _ = await bot.send("/kategori tambah lain X")
    assert text == msg.CATEGORY_USAGE
    text, buttons = await bot.send("/kategori pemasukan")
    text, detail = await bot.press(button(buttons, "Dividen"))
    text, _ = await bot.press(button(detail, msg.BUTTON_DEACTIVATE))
    text, buttons = await bot.send("/kategori pemasukan")
    assert "Dividen (nonaktif)" not in text  # only active categories are listed
    text, detail = await bot.press(button(buttons, "Sewa Kos"))
    text, _ = await bot.press(button(detail, msg.BUTTON_CATEGORY_HISTORY))
    assert "kategori Sewa Kos" in text
    text, _ = await bot.press(button(detail, msg.BUTTON_RENAME))
    text, _ = await bot.send("x" * 41)
    assert text == msg.INVALID["category_name_too_long"]


async def test_accounts_errors_and_stale_buttons(
    bot: Bot, caplog: pytest.LogCaptureFixture
) -> None:
    text, _ = await bot.send("/saldo", user=SUSPENDED)
    assert text == msg.START_ACCOUNT_UNAVAILABLE
    text, _ = await bot.send("/saldo", user=STRANGER)
    assert text == msg.START_NEEDS_INVITE
    text, _ = await bot.send("/pengeluaran 0")
    assert text == msg.INVALID["amount_zero"]
    _, buttons = await bot.send("/pengeluaran 5rb")
    await bot.send("/batal")
    text, _ = await bot.press(button(buttons, "Makanan"))
    assert text == msg.DRAFT_EXPIRED
    text, _ = await bot.press(button(buttons, msg.BUTTON_CANCEL))
    assert text == msg.DRAFT_EXPIRED
    text, _ = await bot.press(f"f:tx:{uuid.uuid4()}")
    assert text == msg.NOT_FOUND
    text, _ = await bot.press(f"f:dl:{uuid.uuid4()}:1")
    assert text == msg.NOT_FOUND
    # AT-LOG-01. PTB's own DEBUG logs echo Bot API parameters (message text), which is why the
    # runtime raises the telegram loggers to WARNING (quiet_transport_loggers) in every
    # environment; with that in place, nothing at any level carries the text or the amount.
    names = ("httpx", "httpcore", "telegram", "telegram.ext")
    levels = {name: logging.getLogger(name).level for name in names}
    try:
        quiet_transport_loggers()
        assert logging.getLogger("telegram.ext.ExtBot").getEffectiveLevel() == logging.WARNING
        with caplog.at_level(logging.DEBUG):
            _, buttons = await bot.send("/pengeluaran 77777")
            await bot.press(button(buttons, "Makanan"))
            await bot.send("ZZSECRETZZ")
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)
    assert any(r.name.startswith("finbot") for r in caplog.records)
    assert "ZZSECRETZZ" not in caplog.text
    assert "77777" not in caplog.text


# ------------------------------------------------------------------- Step 6B regressions


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/catat", msg.ASK_TYPE),
        ("/tambah", msg.ASK_TYPE),
        ("/pemasukan", msg.ASK_AMOUNT),
        ("/pengeluaran", msg.ASK_AMOUNT),
        ("/riwayat", msg.HISTORY_EMPTY),
        ("/transaksi", msg.HISTORY_EMPTY),
        ("/saldo", msg.BALANCE_NOTE),
        ("/laporan", msg.SUMMARY_EMPTY),
        ("/ringkasan", msg.SUMMARY_EMPTY),
        ("/kategori", msg.CATEGORIES_TITLE),
        ("/batal", msg.NOTHING_TO_CANCEL),
        ("/bantuan", "/catat"),
        ("/help", "/catat"),
    ],
)
async def test_every_command_and_alias_answers(bot: Bot, command: str, expected: str) -> None:
    text, _ = await bot.send(command)
    assert expected in text


@pytest.mark.parametrize(
    ("args", "amount", "description"),
    [
        ("2 juta sewa kos", "Rp 2.000.000", "sewa kos"),
        ("25 ribu", "Rp 25.000", None),
        ("Rp 2 juta", "Rp 2.000.000", None),
        ("1,5 juta bonus", "Rp 1.500.000", "bonus"),
        ("25.000,50 kopi", "Rp 25.001", "kopi"),
        ("25rb makan siang", "Rp 25.000", "makan siang"),
    ],
)
async def test_F04_command_amounts_keep_separated_units(
    bot: Bot, args: str, amount: str, description: str | None
) -> None:
    _, buttons = await bot.send(f"/pengeluaran {args}")
    text, buttons = await bot.press(button(buttons, "Makanan"))
    if description is None:
        text, buttons = await bot.press(button(buttons, msg.BUTTON_SKIP))
    assert f"<b>{amount}</b>" in text
    if description is not None:
        assert f"Keterangan: {description}" in text
    text, _ = await bot.press(button(buttons, msg.BUTTON_SAVE))
    assert amount in text
    assert [t["amount"] for t in bot.store.state.transactions.values()] == [
        int(amount.removeprefix("Rp ").replace(".", ""))
    ]


async def test_F04_an_ambiguous_amount_is_asked_again_never_guessed(bot: Bot) -> None:
    text, _ = await bot.send("/pengeluaran 2 jutaan")
    assert text == msg.INVALID["amount_invalid"]
    assert bot.store.state.drafts == {}


async def test_F10_corrections_from_the_confirmation_and_the_account_line(bot: Bot) -> None:
    _, buttons = await bot.send("/pengeluaran 10rb kopi")
    text, buttons = await bot.press(button(buttons, "Makanan"))
    assert f"{msg.FIELD_ACCOUNT}: Kas" in text
    assert [label for label, _ in buttons[:2]] == [msg.BUTTON_SAVE, msg.BUTTON_EDIT]
    text, menu = await bot.press(button(buttons, msg.BUTTON_EDIT))
    assert f"{msg.FIELD_ACCOUNT}: Kas" in text
    assert msg.ASK_REVISE in text
    text, buttons = await bot.press(button(menu, msg.EDIT_FIELD_LABEL["amount"]))
    assert msg.ASK_AMOUNT in text
    text, buttons = await bot.send("3 juta")
    assert "<b>Rp 3.000.000</b>" in text
    assert msg.ASK_CONFIRM in text
    _, menu = await bot.press(button(buttons, msg.BUTTON_EDIT))
    text, buttons = await bot.press(button(menu, msg.EDIT_FIELD_LABEL["type"]))
    text, buttons = await bot.press(button(buttons, msg.BUTTON_INCOME))
    assert msg.ASK_CATEGORY in text  # a new type needs a category of that type
    text, buttons = await bot.press(button(buttons, "Bonus"))
    assert msg.ASK_CONFIRM in text
    _, menu = await bot.press(button(buttons, msg.BUTTON_EDIT))
    _, buttons = await bot.press(button(menu, msg.EDIT_FIELD_LABEL["description"]))
    text, buttons = await bot.press(button(buttons, msg.BUTTON_BACK_TO_SUMMARY))
    assert "Keterangan: kopi" in text  # unchanged
    text, _ = await bot.press(button(buttons, msg.BUTTON_SAVE))
    assert text.startswith("✅ Tersimpan: ⬆️ Pemasukan Rp 3.000.000 — Bonus")


async def test_confirmation_replay_after_a_restart(bot: Bot) -> None:
    _, buttons = await bot.send("/pengeluaran 9rb parkir")
    _, buttons = await bot.press(button(buttons, "Transportasi"))
    save = button(buttons, msg.BUTTON_SAVE)
    await bot.press(save)
    await bot.restart()
    text, _ = await bot.press(save)  # Telegram redelivers, or the user taps an old message
    assert text.startswith("Transaksi ini sudah disimpan sebelumnya")
    assert len(bot.store.state.transactions) == 1


async def test_F08_stale_buttons_and_replaced_conversations(bot: Bot) -> None:
    _, old = await bot.send("/pengeluaran 5rb")
    _, prompt = await bot.send("/kategori")
    _, prompt = await bot.press(button(prompt, msg.BUTTON_NEW_EXPENSE_CATEGORY))
    text, _ = await bot.press(button(old, msg.BUTTON_CANCEL))  # the replaced draft's ❌
    assert text == msg.DRAFT_EXPIRED
    text, _ = await bot.send("Kopi")  # the prompt survived the stale ❌
    assert "Kopi" in text
    _, prompt = await bot.send("/kategori")
    _, prompt = await bot.press(button(prompt, msg.BUTTON_NEW_INCOME_CATEGORY))
    text, _ = await bot.press(button(prompt, msg.BUTTON_CANCEL))  # the prompt's own ❌
    assert text == msg.CANCELLED
    text, _ = await bot.send("Dividen")
    assert text == msg.NO_CONVERSATION
    text, _ = await bot.press(button(old, "Makanan"))
    assert text == msg.DRAFT_EXPIRED


async def test_F06_F07_category_pages_and_reactivation_by_command(bot: Bot) -> None:
    for n in range(30):
        await bot.send(f"/kategori tambah pengeluaran Biaya <{n:02d}>")
    text, buttons = await bot.send("/kategori pengeluaran")
    names = [label for label, data in buttons if data.startswith("f:kd:")]
    while nxt := [d for label, d in buttons if label == msg.BUTTON_NEXT]:
        text, buttons = await bot.press(nxt[0])
        names += [label for label, data in buttons if data.startswith("f:kd:")]
        assert "&lt;" in text  # names are escaped on every page
    assert len(names) == 39
    assert "Lainnya" in names
    _, detail = await bot.press(button(buttons, "Biaya <29>"))
    text, _ = await bot.press(button(detail, msg.BUTTON_DEACTIVATE))
    text, buttons = await bot.send("/kategori pengeluaran nonaktif")
    assert text.startswith(msg.CATEGORIES_TITLE_ARCHIVED)
    text, detail = await bot.press(button(buttons, "Biaya <29>"))
    assert "nonaktif" in text
    text, _ = await bot.press(button(detail, msg.BUTTON_ACTIVATE))
    assert "diaktifkan kembali" in text
    text, _ = await bot.send("/kategori nonaktif")
    assert msg.CATEGORIES_EMPTY in text
    text, _ = await bot.send("/kategori nonaktif nonaktif")
    assert text == msg.CATEGORY_USAGE
    # The draft's category step is paged as well; the default "Lainnya" is on the last page.
    _, buttons = await bot.send("/pengeluaran 1rb")
    offered: list[str] = []
    while True:
        offered += [label for label, data in buttons if data.startswith("f:ca:")]
        nxt = [d for label, d in buttons if label == msg.BUTTON_NEXT]
        if not nxt:
            break
        _, buttons = await bot.press(nxt[0])
    assert len(offered) == 39
    assert "Lainnya" in offered
    _, buttons = await bot.press(button(buttons, "Lainnya"))
    assert bot.api.rejected == []


async def test_expired_draft_and_authorization_change(bot: Bot) -> None:
    _, buttons = await bot.send("/pengeluaran 5rb x")
    _, buttons = await bot.press(button(buttons, "Makanan"))
    bot.clock.advance(hours=25)
    text, _ = await bot.press(button(buttons, msg.BUTTON_SAVE))
    assert text == msg.DRAFT_EXPIRED
    _, buttons = await bot.send("/pengeluaran 5rb x")
    _, buttons = await bot.press(button(buttons, "Makanan"))
    bot.store.state.users[bot.user_id] = UserStatus.SUSPENDED  # suspended meanwhile
    text, _ = await bot.press(button(buttons, msg.BUTTON_SAVE))
    assert text == msg.NOT_PERMITTED
    assert bot.store.state.transactions == {}


async def test_an_oversized_reply_is_replaced_not_sent(
    bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finbot.adapters.telegram import finance_render  # noqa: PLC0415

    monkeypatch.setattr(finance_render, "render_balance", lambda _: ("x" * 5000, []))
    text, _ = await bot.send("/saldo")
    assert text == msg.MESSAGE_TOO_LONG
    assert bot.api.rejected == []  # never even attempted


async def test_retry_later_is_answered_and_nothing_is_stored(
    bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def busy(*_: Any, **__: Any) -> Any:
        raise RetryLaterError("transient")

    monkeypatch.setattr(bot.services.finance.capture, "start", busy)  # type: ignore[union-attr]
    text, _ = await bot.send("/pengeluaran 5rb")
    assert text == msg.BUSY
