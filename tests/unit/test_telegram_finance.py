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
        clock = Clock()
        services = BotServices(
            onboarding=OnboardingService(identities),
            invites=PlatformInviteService(identities, FakePlatform()),
            finance=FinanceServices(
                PrincipalService(identities),
                LedgerService(self.store, clock=clock),
                CaptureService(self.store, clock=clock),
            ),
        )
        guards = BotGuards(SlidingWindowLimiter(1000), SlidingWindowLimiter(1000), RecentUpdates())
        self.api = FakeBotApi()
        self.app = build_application(BOT_TOKEN, services, guards=guards, request=self.api)

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
