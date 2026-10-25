"""Finance domain values: names, dates and months in the workspace timezone, the capture and
edit state machines, ledger entry rules and report aggregation (pure, no database)."""

import uuid
from datetime import UTC, date, datetime

import pytest

from finbot.domain.finance import (
    CATEGORY_NAME_MAX,
    DEFAULT_CATEGORIES,
    DEFAULT_CATEGORY_NAME,
    CaptureState,
    CaptureStep,
    Category,
    EditField,
    EditState,
    HistoryFilter,
    InvalidInputError,
    Month,
    StepError,
    aggregate_totals,
    category_key,
    draft_ref,
    draft_state,
    normalize_category_name,
    normalize_description,
    parse_date,
    parse_month,
    today_in,
    validate_page,
)
from finbot.domain.ledger import (
    EntrySpec,
    LedgerRuleError,
    Leg,
    TxnType,
    build_entries,
    check_entries,
)
from finbot.domain.money import ParsedAmount, Rupiah

TODAY = date(2026, 10, 25)
ACCOUNT = uuid.uuid4()
OTHER = uuid.uuid4()


def _category(kind: TxnType = TxnType.EXPENSE, *, archived: bool = False) -> Category:
    return Category(uuid.uuid4(), kind, "Makanan", False, archived)


# -------------------------------------------------------------------------------- names


def test_category_names_are_normalized_and_compared_case_insensitively() -> None:
    assert normalize_category_name("  Makan \t  Siang\n") == "Makan Siang"
    assert normalize_category_name("Café") == "Café"  # NFC
    assert normalize_category_name("a\u200bb") == "ab"  # format characters dropped
    assert category_key("MAKANAN") == category_key("makanan")
    with pytest.raises(InvalidInputError, match="category_name_empty"):
        normalize_category_name(" \n ")
    with pytest.raises(InvalidInputError, match="category_name_too_long"):
        normalize_category_name("x" * (CATEGORY_NAME_MAX + 1))


def test_descriptions() -> None:
    assert normalize_description(None) is None
    assert normalize_description("   ") is None
    assert normalize_description("makan\x00 siang <b>") == "makan siang <b>"
    with pytest.raises(InvalidInputError):
        normalize_description("x" * 501)


def test_documented_default_categories() -> None:
    assert DEFAULT_CATEGORIES[TxnType.INCOME] == (
        "Gaji",
        "Bonus",
        "Penjualan",
        "Investasi",
        "Hadiah",
        "Lainnya",
    )
    assert DEFAULT_CATEGORIES[TxnType.EXPENSE][-1] == DEFAULT_CATEGORY_NAME
    assert len(DEFAULT_CATEGORIES[TxnType.EXPENSE]) == 9


# -------------------------------------------------------------------------------- dates


def test_today_uses_the_workspace_timezone() -> None:
    # 2026-10-24 17:30 UTC is already 25 October in Jakarta (UTC+7).
    late = datetime(2026, 10, 24, 17, 30, tzinfo=UTC)
    assert today_in("Asia/Jakarta", late) == date(2026, 10, 25)
    assert today_in("UTC", late) == date(2026, 10, 24)
    with pytest.raises(ValueError, match="timezone-aware"):
        today_in("Asia/Jakarta", datetime(2026, 10, 24))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hari ini", TODAY),
        ("Kemarin", date(2026, 10, 24)),
        ("kemarin lusa", date(2026, 10, 23)),
        ("01/10/2026", date(2026, 10, 1)),
        ("1-10-2026", date(2026, 10, 1)),
        ("2026-10-01", date(2026, 10, 1)),
        ("5/10", date(2026, 10, 5)),
        ("01/01/2000", date(2000, 1, 1)),
    ],
)
def test_parse_date(text: str, expected: date) -> None:
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("besok", "date_invalid"),
        ("31/02/2026", "date_invalid"),
        ("26/10/2026", "date_in_future"),
        ("31/12/1999", "date_too_old"),
        ("2026/10/01", "date_invalid"),
    ],
)
def test_invalid_dates(text: str, reason: str) -> None:
    with pytest.raises(InvalidInputError, match=reason):
        parse_date(text, TODAY)


def test_months() -> None:
    assert parse_month("", TODAY) == Month(2026, 10)
    assert parse_month("bulan lalu", TODAY) == Month(2026, 9)
    assert parse_month("bulan lalu", date(2026, 1, 3)) == Month(2025, 12)
    assert parse_month("2026-02", TODAY) == Month(2026, 2)
    assert parse_month("03/2025", TODAY) == Month(2025, 3)
    assert parse_month("Oktober", TODAY) == Month(2026, 10)
    assert parse_month("februari 2024", TODAY).last_day == date(2024, 2, 29)
    assert Month(2026, 12).first_day == date(2026, 12, 1)
    for bad, reason in (
        ("2026-11", "month_in_future"),
        ("13/2026", "month_invalid"),
        ("bulan depan", "month_invalid"),
        ("maret x", "month_invalid"),
    ):
        with pytest.raises(InvalidInputError, match=reason):
            parse_month(bad, TODAY)


def test_history_filter_and_pages() -> None:
    with pytest.raises(InvalidInputError, match="date_range"):
        HistoryFilter(date_from=date(2026, 10, 2), date_to=date(2026, 10, 1))
    with pytest.raises(InvalidInputError, match="filter_type"):
        HistoryFilter(txn_type=TxnType.TRANSFER)
    assert validate_page(0) == 0
    for bad in (-1, 100):
        with pytest.raises(InvalidInputError):
            validate_page(bad)


# ------------------------------------------------------------------- capture states


def test_capture_walks_type_amount_category_description_confirm() -> None:
    state = CaptureState.start(TODAY)
    assert state.step is CaptureStep.TYPE
    state = state.with_type(TxnType.EXPENSE)
    assert state.step is CaptureStep.AMOUNT
    state = state.with_amount(ParsedAmount(Rupiah(25_000), rounded=False))
    assert state.step is CaptureStep.CATEGORY
    category = _category()
    state = state.with_category(category)
    assert state.step is CaptureStep.DESCRIPTION
    state = state.with_description("Makan siang")
    assert state.step is CaptureStep.CONFIRM
    assert state.complete() == (TxnType.EXPENSE, 25_000, category.id)
    state = state.asking_date().with_date(date(2026, 10, 20), TODAY)
    assert (state.step, state.txn_date) == (CaptureStep.CONFIRM, date(2026, 10, 20))
    assert draft_state(state.to_json()) == state


def test_capture_shortcuts_skip_answered_steps() -> None:
    state = CaptureState.start(
        TODAY, TxnType.INCOME, ParsedAmount(Rupiah(5_000_000), rounded=False), "gaji Oktober"
    )
    assert state.step is CaptureStep.CATEGORY
    state = state.with_category(_category(TxnType.INCOME))
    assert state.step is CaptureStep.CONFIRM  # description already given


def test_capture_rejects_out_of_order_and_mismatched_input() -> None:
    state = CaptureState.start(TODAY, TxnType.EXPENSE)
    with pytest.raises(StepError):
        state.with_category(_category())  # amount first
    with pytest.raises(StepError):
        state.complete()
    state = state.with_amount(ParsedAmount(Rupiah(1), rounded=True))
    with pytest.raises(InvalidInputError, match="category_kind_mismatch"):
        state.with_category(_category(TxnType.INCOME))
    with pytest.raises(InvalidInputError, match="category_archived"):
        state.with_category(_category(archived=True))
    with pytest.raises(InvalidInputError, match="txn_type"):
        CaptureState.start(TODAY, TxnType.TRANSFER)
    done = state.with_category(_category()).with_description(None)
    with pytest.raises(InvalidInputError, match="date_in_future"):
        done.asking_date().with_date(date(2026, 10, 26), TODAY)


def test_edit_state_produces_exactly_one_change() -> None:
    edit = EditState.start(EditField.AMOUNT, 3, TxnType.EXPENSE)
    with pytest.raises(StepError):
        edit.changes()
    edit = edit.with_amount(ParsedAmount(Rupiah(30_000), rounded=False))
    assert edit.changes().amount == 30_000
    assert draft_state(edit.to_json()) == edit
    cleared = EditState.start(EditField.DESCRIPTION, 1, TxnType.INCOME).with_description(None)
    assert cleared.changes().clear_description
    dated = EditState.start(EditField.DATE, 1, TxnType.INCOME).with_date(date(2026, 1, 1), TODAY)
    assert dated.changes().txn_date == date(2026, 1, 1)
    with pytest.raises(StepError):
        EditState.start(EditField.DATE, 1, TxnType.INCOME).with_description("x")


def test_draft_ref_is_the_random_tail() -> None:
    draft_id = uuid.UUID("01890000-0000-7000-8000-0000deadbeef")
    assert draft_ref(draft_id) == "deadbeef"


# ------------------------------------------------------------------------- ledger


def test_entry_rules() -> None:
    assert build_entries(TxnType.INCOME, 50_000, ACCOUNT) == (
        EntrySpec(Leg.SINGLE, ACCOUNT, 50_000),
    )
    assert build_entries(TxnType.EXPENSE, 35_000, ACCOUNT) == (
        EntrySpec(Leg.SINGLE, ACCOUNT, -35_000),
    )
    transfer = build_entries(TxnType.TRANSFER, 500_000, ACCOUNT, OTHER)
    assert sum(e.amount for e in transfer) == 0
    check_entries(TxnType.TRANSFER, 500_000, transfer)
    with pytest.raises(LedgerRuleError):
        build_entries(TxnType.TRANSFER, 1, ACCOUNT, ACCOUNT)
    with pytest.raises(LedgerRuleError):
        build_entries(TxnType.TRANSFER, 1, ACCOUNT)
    with pytest.raises(LedgerRuleError):
        build_entries(TxnType.EXPENSE, 1, ACCOUNT, OTHER)
    with pytest.raises(LedgerRuleError):
        check_entries(TxnType.EXPENSE, 10, (EntrySpec(Leg.SINGLE, ACCOUNT, 10),))
    with pytest.raises(LedgerRuleError):
        check_entries(
            TxnType.TRANSFER,
            10,
            (EntrySpec(Leg.SOURCE, ACCOUNT, -10), EntrySpec(Leg.DESTINATION, OTHER, 9)),
        )


def test_aggregate_totals_never_mixes_currencies() -> None:
    totals = aggregate_totals(
        [("IDR", TxnType.INCOME, 100), ("IDR", TxnType.EXPENSE, 30), ("USD", TxnType.EXPENSE, 5)]
    )
    by_currency = {t.currency: t for t in totals}
    assert (by_currency["IDR"].income, by_currency["IDR"].expense, by_currency["IDR"].net) == (
        100,
        30,
        70,
    )
    assert by_currency["USD"].net == -5
