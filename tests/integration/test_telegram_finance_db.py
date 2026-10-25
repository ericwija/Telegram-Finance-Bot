"""The finance commands, conversation and buttons end to end: real PTB stack, fake Bot API,
real PostgreSQL 18.6 as finbot_app (Step 6). Nothing contacts Telegram.

Each test drives the bot exactly as a user would: commands, typed answers and button presses
(callback data taken from the keyboards the bot rendered).
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from finbot.adapters.telegram import messages_id as msg
from finbot.adapters.telegram.bot import BotGuards, BotServices, build_application
from finbot.adapters.telegram.finance import FinanceServices
from finbot.adapters.telegram.ratelimit import RecentUpdates, SlidingWindowLimiter
from finbot.infra.db.finance import PgFinanceStore
from finbot.infra.db.identity import PgIdentityGateway
from finbot.infra.db.platform import PgPlatformGateway
from finbot.services.capture import CaptureService
from finbot.services.ledger import LedgerService
from finbot.services.onboarding import OnboardingService
from finbot.services.platform_invites import PlatformInviteService
from finbot.services.principals import PrincipalService
from tests.support.finance import Clock, Person, owner_scalar, person
from tests.support.telegram import (
    BOT_TOKEN,
    Buttons,
    FakeBotApi,
    answers,
    callback_update,
    message_update,
    shown,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


class Bot:
    def __init__(self, app_engine: Any) -> None:
        identities = PgIdentityGateway(app_engine)
        store = PgFinanceStore(app_engine)
        clock = Clock()
        services = BotServices(
            onboarding=OnboardingService(identities),
            invites=PlatformInviteService(identities, PgPlatformGateway(app_engine)),
            finance=FinanceServices(
                principals=PrincipalService(identities),
                ledger=LedgerService(store, clock=clock),
                capture=CaptureService(store, clock=clock),
            ),
        )
        guards = BotGuards(SlidingWindowLimiter(1000), SlidingWindowLimiter(1000), RecentUpdates())
        self.api = FakeBotApi()
        self.app = build_application(
            BOT_TOKEN, services, guards=guards, request=self.api, concurrent_updates=True
        )

    async def send(self, user: Person | int, text: str) -> tuple[str, Buttons]:
        tg = user if isinstance(user, int) else user.telegram_id
        await self.app.process_update(message_update(self.app, tg, text))
        return shown(self.api, tg)[-1]

    async def press(self, user: Person, data: str) -> tuple[str, Buttons]:
        await self.app.process_update(callback_update(self.app, user.telegram_id, data))
        return shown(self.api, user.telegram_id)[-1]


def button(buttons: Buttons, label: str) -> str:
    return next(data for text, data in buttons if text == label)


@pytest_asyncio.fixture
async def bot(migrated_database: str, app_engine: Any) -> AsyncIterator[Bot]:
    harness = Bot(app_engine)
    await harness.app.initialize()
    yield harness
    await harness.app.shutdown()


async def _transactions(owner_engine: Any) -> int:
    return int(await owner_scalar(owner_engine, "SELECT count(*) FROM transactions"))


async def test_pengeluaran_flow_and_duplicate_confirm(bot: Bot, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    text, _ = await bot.send(budi, "/pengeluaran")
    assert msg.ASK_AMOUNT in text
    assert "🏠 <b>Pribadi</b>" in text  # G-8
    text, buttons = await bot.send(budi, "25rb")
    assert msg.ASK_CATEGORY in text
    text, buttons = await bot.press(budi, button(buttons, "Makanan"))
    assert msg.ASK_DESCRIPTION in text
    text, buttons = await bot.send(budi, "Makan <siang> & kopi")
    assert msg.ASK_CONFIRM in text
    assert "Makan &lt;siang&gt; &amp; kopi" in text  # AT-RENDER-01
    assert await _transactions(owner_engine) == 0  # nothing before ✅
    save = button(buttons, msg.BUTTON_SAVE)
    text, _ = await bot.press(budi, save)
    assert text.startswith("✅ Tersimpan: ⬇️ Pengeluaran Rp 25.000 — Makanan")
    text, _ = await bot.press(budi, save)  # double tap / Telegram retry
    assert text.startswith("Transaksi ini sudah disimpan sebelumnya")
    assert await _transactions(owner_engine) == 1


async def test_pemasukan_with_arguments_and_tambah_with_type_buttons(
    bot: Bot, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    _, buttons = await bot.send(budi, "/pemasukan 5jt gaji Oktober")
    text, buttons = await bot.press(budi, button(buttons, "Gaji"))
    assert msg.ASK_CONFIRM in text  # the description came with the command
    text, _ = await bot.press(budi, button(buttons, msg.BUTTON_SAVE))
    assert "⬆️ Pemasukan Rp 5.000.000" in text
    text, buttons = await bot.send(budi, "/tambah")
    assert msg.ASK_TYPE in text
    text, buttons = await bot.press(budi, button(buttons, msg.BUTTON_EXPENSE))
    assert msg.ASK_AMOUNT in text
    text, _ = await bot.send(budi, "abc")
    assert text == msg.INVALID["amount_invalid"]
    text, _ = await bot.send(budi, "-5000")
    assert text == msg.INVALID["amount_negative"]
    text, buttons = await bot.send(budi, "1,5jt")
    text, _ = await bot.press(budi, button(buttons, msg.BUTTON_CANCEL))
    assert text == msg.CANCELLED
    assert await _transactions(owner_engine) == 1


async def test_change_date_before_saving(bot: Bot, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    _, buttons = await bot.send(budi, "/catat pengeluaran 10rb parkir")
    _, buttons = await bot.press(budi, button(buttons, "Transportasi"))
    text, buttons = await bot.press(budi, button(buttons, msg.BUTTON_DATE))
    assert msg.ASK_DATE in text
    text, _ = await bot.send(budi, "besok")
    assert text == msg.INVALID["date_invalid"]
    text, buttons = await bot.send(budi, "kemarin")
    assert "Tanggal: 24/10/2026" in text
    await bot.press(budi, button(buttons, msg.BUTTON_SAVE))
    assert (
        str(await owner_scalar(owner_engine, "SELECT txn_date FROM transactions")) == "2026-10-24"
    )


async def test_riwayat_detail_edit_delete_restore_and_saldo(bot: Bot, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    for args, category in (("50rb listrik", "Tagihan"), ("20rb", "Makanan")):
        _, buttons = await bot.send(budi, f"/pengeluaran {args}")
        _, buttons = await bot.press(budi, button(buttons, category))
        if any(label == msg.BUTTON_SKIP for label, _ in buttons):
            _, buttons = await bot.press(budi, button(buttons, msg.BUTTON_SKIP))
        await bot.press(budi, button(buttons, msg.BUTTON_SAVE))
    text, buttons = await bot.send(budi, "/transaksi")
    assert "1. 25/10/2026 ⬇️" in text
    assert "2. 25/10/2026 ⬇️" in text
    text, buttons = await bot.press(budi, button(buttons, "2"))
    assert msg.DETAIL_TITLE in text
    # Edit the amount through the buttons.
    _, menu = await bot.press(budi, button(buttons, msg.BUTTON_EDIT))
    text, edit = await bot.press(budi, button(menu, "Jumlah"))
    assert msg.ASK_AMOUNT in text
    text, edit = await bot.send(budi, "30rb")
    assert "Jumlah baru: <b>Rp 30.000</b>" in text
    text, _ = await bot.press(budi, button(edit, msg.BUTTON_SAVE))
    assert text.startswith(msg.UPDATED)
    # The old menu is now stale (version changed).
    text, _ = await bot.press(budi, button(menu, "Jumlah"))
    assert text == msg.CONFLICT["stale"]
    # Delete with confirmation, then restore.
    text, buttons = await bot.send(budi, "/riwayat")
    _, detail = await bot.press(budi, button(buttons, "1"))
    text, confirm = await bot.press(budi, button(detail, msg.BUTTON_DELETE))
    assert msg.CONFIRM_DELETE in text
    text, _ = await bot.press(budi, button(confirm, msg.BUTTON_CONFIRM_DELETE))
    assert text == msg.DELETED
    text, _ = await bot.send(budi, "/saldo")
    assert "Kas: -Rp 30.000 ⚠️" in text  # D15: negative balances warn
    assert msg.BALANCE_NOTE in text
    text, buttons = await bot.send(budi, "/riwayat dihapus")
    assert msg.HISTORY_TITLE_VOIDED in text
    _, detail = await bot.press(budi, button(buttons, "1"))
    text, _ = await bot.press(budi, button(detail, msg.BUTTON_RESTORE))
    assert text == msg.RESTORED
    text, _ = await bot.send(budi, "/saldo")
    # 30rb (edited from 50rb) + 20rb (restored): the restore counts again, the edit stays.
    assert "Total tercatat (IDR): <b>-Rp 50.000</b>" in text


async def test_kategori_create_rename_and_deactivate(bot: Bot, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    text, buttons = await bot.send(budi, "/kategori pengeluaran")
    assert "Makanan" in text
    assert "Gaji" not in text
    text, _ = await bot.press(budi, button(buttons, msg.BUTTON_NEW_EXPENSE_CATEGORY))
    assert "Kirim nama kategori pengeluaran baru" in text
    text, _ = await bot.send(budi, "Kopi <b>")
    assert text == msg.CATEGORY_CREATED.format(name="Kopi &lt;b&gt;", kind="pengeluaran")
    text, _ = await bot.send(budi, "/kategori tambah pengeluaran kopi <B>")
    assert text == msg.DUPLICATE_CATEGORY.format(name="kopi &lt;B&gt;")
    _, buttons = await bot.send(budi, "/kategori")
    text, detail = await bot.press(budi, button(buttons, "Kopi <b>"))
    _, _ = await bot.press(budi, button(detail, msg.BUTTON_RENAME))
    text, _ = await bot.send(budi, "Kopi Susu")
    assert text == msg.CATEGORY_RENAMED.format(name="Kopi Susu")
    text, _ = await bot.press(budi, button(detail, msg.BUTTON_DEACTIVATE))
    assert text == msg.CATEGORY_DEACTIVATED.format(name="Kopi Susu")
    _, buttons = await bot.send(budi, "/kategori")
    text, detail = await bot.press(budi, button(buttons, "Lainnya"))
    assert msg.BUTTON_DEACTIVATE not in [label for label, _ in detail]  # the fallback stays
    text, _ = await bot.send(budi, "/kategori hapus semua")
    assert text == msg.CATEGORY_USAGE


async def test_laporan_ringkasan_and_bantuan(bot: Bot, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    text, _ = await bot.send(budi, "/ringkasan")
    assert text.startswith("<b>Ringkasan Oktober 2026</b>")
    assert msg.SUMMARY_EMPTY in text
    _, buttons = await bot.send(budi, "/pemasukan 2jt")
    _, buttons = await bot.press(budi, button(buttons, "Bonus"))
    _, buttons = await bot.press(budi, button(buttons, msg.BUTTON_SKIP))
    await bot.press(budi, button(buttons, msg.BUTTON_SAVE))
    text, _ = await bot.send(budi, "/laporan bulan ini")
    assert "Pemasukan: <b>Rp 2.000.000</b>" in text
    assert "• Bonus: Rp 2.000.000 (1 transaksi)" in text
    text, _ = await bot.send(budi, "/laporan bulan depan")
    assert text == msg.INVALID["month_invalid"]
    text, _ = await bot.send(budi, "/bantuan")
    assert text == msg.HELP


async def test_unknown_accounts_free_text_and_start(bot: Bot, owner_engine: Any) -> None:
    for command in ("/saldo", "/pengeluaran 5000", "/kategori", "halo"):
        text, _ = await bot.send(777_000_111, command)
        assert text == msg.START_NEEDS_INVITE  # FR-IDN-02
    budi = await person(owner_engine)
    text, _ = await bot.send(budi, "halo")
    assert text == msg.NO_CONVERSATION
    text, _ = await bot.send(budi, "/start")
    assert text.startswith("Selamat datang kembali.")
    assert text.endswith(msg.START_MENU_HINT)
    text, _ = await bot.send(budi, "/batal")
    assert text == msg.NOTHING_TO_CANCEL


async def test_forged_stale_and_foreign_buttons(bot: Bot, owner_engine: Any) -> None:
    budi, ani = await person(owner_engine, "Budi"), await person(owner_engine, "Ani")
    _, buttons = await bot.send(ani, "/pengeluaran 9rb")
    _, buttons = await bot.press(ani, button(buttons, "Makanan"))
    _, buttons = await bot.press(ani, button(buttons, msg.BUTTON_SKIP))
    await bot.press(ani, button(buttons, msg.BUTTON_SAVE))
    _, history = await bot.send(ani, "/riwayat")
    ani_detail = button(history, "1")
    # Budi presses Ani's detail button (forged callback): not found, nothing leaks.
    text, _ = await bot.press(budi, ani_detail)
    assert text == msg.NOT_FOUND
    # A keyboard from a replaced draft is stale.
    _, old = await bot.send(budi, "/pengeluaran 1rb")
    await bot.send(budi, "/pemasukan 2rb")
    text, _ = await bot.press(budi, button(old, "Makanan"))
    assert text == msg.DRAFT_EXPIRED
    # Malformed data is answered as a stale button without touching the database.
    before = len(shown(bot.api, budi.telegram_id))
    await bot.app.process_update(callback_update(bot.app, budi.telegram_id, "f:zz:1"))
    assert answers(bot.api)[-1] == msg.STALE_BUTTON
    assert len(shown(bot.api, budi.telegram_id)) == before
    assert await _transactions(owner_engine) == 1
