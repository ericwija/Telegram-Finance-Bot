"""The finance adapter's pure parts: callback codec, rendering (AT-RENDER-01), argument parsing."""

import uuid
from datetime import UTC, date, datetime

import pytest

from finbot.adapters.telegram import finance_callbacks as cb
from finbot.adapters.telegram import finance_render as render
from finbot.adapters.telegram import messages_id as msg
from finbot.adapters.telegram.finance import COMMANDS, error_text, history_filter, split_amount
from finbot.authz.permissions import Permission
from finbot.authz.policy import ForbiddenError
from finbot.domain.finance import (
    CaptureState,
    CaptureStep,
    Category,
    ConflictError,
    DraftExpiredError,
    DraftView,
    EditField,
    HistoryFilter,
    HistoryPage,
    InvalidInputError,
    NotFoundError,
    TransactionRecord,
)
from finbot.domain.ledger import TxnSource, TxnStatus, TxnType
from finbot.domain.money import Rupiah
from finbot.services.finance import WorkspaceInfo
from finbot.services.ledger import HistoryResult, TransactionDetail
from finbot.services.principals import UnknownAccountError

TXN = uuid.uuid4()
CAT = uuid.uuid4()
WS = WorkspaceInfo("Pribadi <&>", "personal", "IDR", "Asia/Jakarta")
HOSTILE = "<b>x</b> & *y* _z_ [a](javascript:alert(1)) </code>"


def _record(**overrides: object) -> TransactionRecord:
    values: dict[str, object] = {
        "id": TXN,
        "type": TxnType.EXPENSE,
        "amount": Rupiah(25_000),
        "currency": "IDR",
        "txn_date": date(2026, 10, 25),
        "category_id": CAT,
        "category_name": HOSTILE,
        "category_archived": False,
        "description": HOSTILE,
        "source": TxnSource.MANUAL,
        "status": TxnStatus.POSTED,
        "version": 3,
        "entry_revision": 1,
        "account_id": uuid.uuid4(),
        "account_name": "Kas",
        "created_by": uuid.uuid4(),
        "created_at": datetime(2026, 10, 25, 3, 0, tzinfo=UTC),
        "updated_at": None,
        "voided_at": None,
    }
    values.update(overrides)
    return TransactionRecord(**values)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- callbacks


def test_every_callback_round_trips_and_fits_64_bytes() -> None:
    ref = "deadbeef"
    filt = HistoryFilter(TxnType.INCOME, date(2026, 10, 1), date(2026, 10, 25), voided=True)
    samples = [
        cb.draft_type(ref, TxnType.EXPENSE),
        cb.draft_category(ref, CAT),
        cb.draft_skip(ref),
        cb.draft_date(ref),
        cb.draft_confirm(ref),
        cb.draft_cancel(ref),
        cb.history(999, filt),
        cb.history(3, HistoryFilter(category_id=CAT)),
        cb.transaction(TXN),
        cb.edit_menu(TXN, 1_000_000),
        cb.edit_field(EditField.DESCRIPTION, TXN, 7),
        cb.delete_ask(TXN, 2),
        cb.delete_yes(TXN, 2),
        cb.restore(TXN, 2),
        cb.category_list(None),
        cb.category_list(TxnType.INCOME),
        cb.category_detail(CAT),
        cb.category_new(TxnType.EXPENSE),
        cb.category_rename(CAT),
        cb.category_active(CAT, False),
    ]
    for data in samples:
        assert len(data.encode()) <= cb.MAX_BYTES, data
        cb.parse(data)  # never raises for our own buttons
    parsed = cb.parse(cb.history(999, filt))
    assert (parsed.page, parsed.history) == (999, filt)
    edit = cb.parse(cb.edit_field(EditField.DESCRIPTION, TXN, 7))
    assert (edit.field, edit.entity_id, edit.version) == (EditField.DESCRIPTION, TXN, 7)
    assert cb.parse(cb.category_active(CAT, True)).flag is True


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "x:ok:deadbeef",
        "f:ok",
        "f:ok:DEADBEEF",
        "f:ok:deadbeef:extra",
        "f:zz:deadbeef",
        f"f:tx:{str(TXN).upper()}",
        "f:tx:not-a-uuid",
        f"f:ed:{TXN}:-1",
        f"f:ed:{TXN}:99999999",
        "f:h:1:x:-:-:0",
        "f:h:1:a:20261301:-:0",
        "f:h:1:a:20261002:20261001:0",  # from > to
        "f:h:1000:a:-:-:0",
        f"f:ka:{CAT}:2",
        f"f:ef:z:{TXN}:1",
        "f:" + "a" * 70,
    ],
)
def test_forged_or_malformed_callbacks_are_refused(data: str | None) -> None:
    with pytest.raises(cb.InvalidCallbackError):
        cb.parse(data)


def test_callbacks_never_carry_a_workspace() -> None:
    # Only draft refs, transaction/category IDs, versions, pages and filters (docs/09 L1).
    for name in dir(cb):
        assert "workspace" not in name.lower()


# ------------------------------------------------------------------------------ render


def test_AT_RENDER_01_user_strings_are_escaped_everywhere() -> None:
    detail = TransactionDetail(WS, _record(), True, True, False)
    history = HistoryResult(WS, HistoryPage((_record(),), 0, True, HistoryFilter()), None)
    for text, _ in (
        render.render_detail(detail),
        render.render_delete_confirm(detail),
        render.render_edit_menu(detail),
        render.render_history(history),
    ):
        assert "<b>x</b>" not in text
        assert "&lt;b&gt;x&lt;/b&gt;" in text
        assert "Pribadi &lt;&amp;&gt;" in text
        assert "</code>" not in text


def test_draft_rendering_shows_workspace_and_offers_buttons_per_step() -> None:
    category = Category(CAT, TxnType.EXPENSE, HOSTILE, False, False)
    state = CaptureState(CaptureStep.CATEGORY, date(2026, 10, 25), TxnType.EXPENSE, 25_000)
    view = DraftView(
        uuid.uuid4(), "deadbeef", state, "Pribadi", "personal", "IDR", categories=(category,)
    )
    text, keyboard = render.render_draft(view)
    assert "🏠 <b>Pribadi</b>" in text  # G-8
    assert "Rp 25.000" in text
    labels = [label for row in keyboard for label, _ in row]
    assert HOSTILE in labels  # button labels are plain text, not HTML
    assert msg.BUTTON_CANCEL in labels
    confirm = DraftView(
        view.id,
        view.ref,
        CaptureState(
            CaptureStep.CONFIRM,
            date(2026, 10, 25),
            TxnType.EXPENSE,
            25_000,
            True,
            CAT,
            HOSTILE,
            True,
        ),
        "Pribadi",
        "personal",
        "IDR",
        category=category,
    )
    text, keyboard = render.render_draft(confirm)
    assert msg.ROUNDED_NOTE in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert [label for label, _ in keyboard[0]] == [msg.BUTTON_SAVE, msg.BUTTON_DATE]


def test_detail_buttons_follow_permissions() -> None:
    _, none = render.render_detail(TransactionDetail(WS, _record(), False, False, False))
    assert none == []
    voided = _record(status=TxnStatus.VOIDED)
    _, keyboard = render.render_detail(TransactionDetail(WS, voided, False, False, True))
    assert [label for label, _ in keyboard[0]] == [msg.BUTTON_RESTORE]


def test_history_pagination_buttons() -> None:
    page = HistoryPage(tuple(_record(id=uuid.uuid4()) for _ in range(10)), 1, True, HistoryFilter())
    text, keyboard = render.render_history(HistoryResult(WS, page, None))
    assert "11. " in text
    assert "20. " in text
    nav = [label for label, _ in keyboard[-1]]
    assert nav == [msg.BUTTON_PREV, msg.BUTTON_NEXT]
    assert len(text) < 4096


# --------------------------------------------------------------------------- arguments


def test_split_amount() -> None:
    assert split_amount("25rb makan siang") == ("25rb", "makan siang")
    assert split_amount("Rp 25.000 kopi") == ("Rp 25.000", "kopi")
    assert split_amount("") == (None, None)


def test_history_arguments() -> None:
    today = date(2026, 10, 25)
    assert history_filter("pemasukan", today) == HistoryFilter(TxnType.INCOME)
    filt = history_filter("pengeluaran dihapus bulan lalu", today)
    assert (filt.txn_type, filt.voided, filt.date_from, filt.date_to) == (
        TxnType.EXPENSE,
        True,
        date(2026, 9, 1),
        date(2026, 9, 30),
    )
    ranged = history_filter("01/10/2026 kemarin", today)
    assert (ranged.date_from, ranged.date_to) == (date(2026, 10, 1), date(2026, 10, 24))
    this_month = history_filter("2026-10", today)
    assert this_month.date_to == today
    with pytest.raises(InvalidInputError):
        history_filter("besok", today)


def test_expected_errors_map_to_catalogue_messages() -> None:
    assert error_text(UnknownAccountError()) == msg.START_NEEDS_INVITE
    assert error_text(ForbiddenError(Permission.TRANSACTION_CREATE, "role")) == msg.NOT_PERMITTED
    assert error_text(NotFoundError("x")) == msg.NOT_FOUND
    assert error_text(DraftExpiredError("x")) == msg.DRAFT_EXPIRED
    assert error_text(ConflictError("stale")) == msg.CONFLICT["stale"]
    assert error_text(InvalidInputError("amount_zero")) == msg.INVALID["amount_zero"]
    assert error_text(InvalidInputError("unknown_reason")) == msg.INVALID_INPUT
    assert error_text(RuntimeError("database password=secret")) is None  # system failure


def test_documented_and_requested_command_names() -> None:
    names = {name for group in COMMANDS for name in group}
    assert {"catat", "riwayat", "laporan", "saldo", "kategori", "bantuan"} <= names  # docs/02 §12
    assert {"tambah", "pemasukan", "pengeluaran", "transaksi", "ringkasan"} <= names  # Step 6
