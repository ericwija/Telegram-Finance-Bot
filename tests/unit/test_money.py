"""Whole-rupiah amounts (G-6, AT-ENT-20, AT-PARSE-PROP): parsing, rounding, bounds, format."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finbot.domain.money import (
    MAX_AMOUNT,
    AmountError,
    InvalidAmountError,
    format_rupiah,
    parse_amount,
    validate_amount,
)


@pytest.mark.parametrize(
    ("text", "value", "rounded"),
    [
        ("25000", 25_000, False),
        ("25.000", 25_000, False),
        ("25,000", 25_000, False),  # comma thousands without a unit
        ("Rp 1.250.000", 1_250_000, False),
        ("rp.25.000", 25_000, False),
        ("Rp1.250.000,50", 1_250_001, True),  # half-up
        ("1.250.000,49", 1_250_000, True),
        ("25rb", 25_000, False),
        ("25 ribu", 25_000, False),
        ("25k", 25_000, False),
        ("1,5jt", 1_500_000, False),
        ("1.5jt", 1_500_000, False),
        ("2 juta", 2_000_000, False),
        ("1,25 miliar", 1_250_000_000, False),
        ("12,5", 13, True),
        ("0,5", 1, True),
        ("1", 1, False),
        ("1.000.000.000.000", MAX_AMOUNT, False),
        ("  30000  ", 30_000, False),
    ],
)
def test_parse_amount(text: str, value: int, rounded: bool) -> None:
    parsed = parse_amount(text)
    assert parsed.value == value
    assert parsed.rounded is rounded
    assert type(parsed.value) is int


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", AmountError.EMPTY),
        ("   ", AmountError.EMPTY),
        ("-5000", AmountError.NEGATIVE),
        ("\u22125000", AmountError.NEGATIVE),
        ("0", AmountError.ZERO),
        ("0,4", AmountError.ZERO),  # rounds to zero
        ("1.000.000.000.001", AmountError.TOO_LARGE),
        ("2 miliar 500", AmountError.INVALID),
        ("abc", AmountError.INVALID),
        ("25.00.0", AmountError.INVALID),
        ("1,2,3", AmountError.INVALID),
        ("12,345.67", AmountError.INVALID),
        ("1e6", AmountError.INVALID),
        ("25rb rb", AmountError.INVALID),
        ("9" * 41, AmountError.INVALID),
    ],
)
def test_invalid_amounts(text: str, reason: AmountError) -> None:
    with pytest.raises(InvalidAmountError) as caught:
        parse_amount(text)
    assert caught.value.reason is reason


def test_AT_ENT_20_bounds_and_types() -> None:
    assert validate_amount(1) == 1
    assert validate_amount(MAX_AMOUNT) == MAX_AMOUNT
    for bad in (0, -1, MAX_AMOUNT + 1):
        with pytest.raises(InvalidAmountError):
            validate_amount(bad)
    for not_int in (True, 1.0, "1"):
        with pytest.raises(InvalidAmountError):
            validate_amount(not_int)  # type: ignore[arg-type]


def test_format_rupiah() -> None:
    assert format_rupiah(0) == "Rp 0"
    assert format_rupiah(1_250_000) == "Rp 1.250.000"
    assert format_rupiah(-35_000) == "-Rp 35.000"
    with pytest.raises(TypeError):
        format_rupiah(1.5)  # type: ignore[arg-type]


@given(st.integers(min_value=1, max_value=MAX_AMOUNT))
def test_AT_PARSE_PROP_format_then_parse_round_trips(value: int) -> None:
    for text in (format_rupiah(value), str(value), format_rupiah(value).removeprefix("Rp ")):
        parsed = parse_amount(text)
        assert parsed.value == value
        assert not parsed.rounded
