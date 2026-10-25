"""Money in whole rupiah (G-6, CLAUDE.md rule 3, docs/05 §1): pure, no I/O.

Representation and precision policy:

* An amount is an ``int`` of whole rupiah (IDR has no minor unit in practice; scale 0). The
  database stores ``bigint``; there is no ``float`` or ``Decimal`` anywhere in money paths.
* A transaction amount is **positive**, ``1 ≤ amount ≤ 1,000,000,000,000``; its direction comes
  from the transaction type (income/expense), never from a sign typed by the user.
* User input may contain a fraction (``25.000,50``, ``1,5jt``). It is rounded **half-up** to
  whole rupiah with integer arithmetic only, and the caller is told (``ParsedAmount.rounded``).
* Aggregates are computed by PostgreSQL (``SUM(bigint)`` is exact ``numeric``) and converted
  back to ``int`` without loss.

Accepted input (case-insensitive, surrounding whitespace ignored):

* an optional ``Rp``/``Rp.`` prefix;
* digits with Indonesian separators: ``.`` groups thousands and ``,`` starts the fraction
  (``1.250.000``, ``1.250.000,50``). Exceptions that follow common usage: ``25,000`` (comma
  groups of exactly three digits and no unit) is twenty-five thousand, and a single ``.``
  before a unit is a fraction (``1.5jt``);
* an optional unit: ``rb``/``ribu``/``k`` (times 1,000), ``jt``/``juta`` (times 1,000,000) and
  ``miliar``/``milyar`` (times 1,000,000,000), attached (``25rb``) or after a space
  (``2 juta``).

:func:`split_leading_amount` finds the amount at the start of a command argument with the same
grammar, so ``/pengeluaran 2 juta sewa`` is two million with the description ``sewa``, never two
rupiah with the description ``juta sewa``.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NewType

Rupiah = NewType("Rupiah", int)

MIN_AMOUNT: Final = 1
MAX_AMOUNT: Final = 1_000_000_000_000
CURRENCY: Final = "IDR"  # the only currency in the MVP (workspaces.currency CHECK)

_UNIT_WORDS: Final = "rb|ribu|k|jt|juta|miliar|milyar"
_UNITS: Final = {
    "rb": 1_000,
    "ribu": 1_000,
    "k": 1_000,
    "jt": 1_000_000,
    "juta": 1_000_000,
    "miliar": 1_000_000_000,
    "milyar": 1_000_000_000,
}
_AMOUNT_RE = re.compile(rf"^(?:rp\.?\s*)?(?P<number>\d[\d.,]*)\s*(?P<unit>{_UNIT_WORDS})?$")
#: The same grammar at the start of a longer text; a unit must end at a word boundary.
_LEADING_AMOUNT_RE = re.compile(
    rf"^(?:rp\.?\s*)?\d[\d.,]*(?:\s*(?:{_UNIT_WORDS})(?!\w))?(?=\s|$)", re.IGNORECASE
)
#: A word right after a unit-less number that looks like a misspelt unit (``2 jutaan``).
_UNIT_LIKE_RE = re.compile(r"^(?:rb|ribu|jt|juta|miliar|milyar)", re.IGNORECASE)
_THOUSANDS_DOT = re.compile(r"^\d{1,3}(?:\.\d{3})+$")
_THOUSANDS_COMMA = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_MAX_INPUT = 40


class AmountError(StrEnum):
    EMPTY = "empty"
    INVALID = "invalid"
    NEGATIVE = "negative"
    ZERO = "zero"
    TOO_LARGE = "too_large"


class InvalidAmountError(ValueError):
    def __init__(self, reason: AmountError) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ParsedAmount:
    value: Rupiah
    rounded: bool  # the input had a fraction that was rounded half-up


def validate_amount(value: int) -> Rupiah:
    """A positive transaction amount within the G-6 bounds (``bool`` is not an amount)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidAmountError(AmountError.INVALID)
    if value < 0:
        raise InvalidAmountError(AmountError.NEGATIVE)
    if value == 0:
        raise InvalidAmountError(AmountError.ZERO)
    if value > MAX_AMOUNT:
        raise InvalidAmountError(AmountError.TOO_LARGE)
    return Rupiah(value)


def _split(number: str, has_unit: bool) -> tuple[str, str]:
    """(integer digits, fraction digits) of a separated number, or raise."""
    if "." in number and "," in number:  # Indonesian: 1.250.000,50
        whole, _, fraction = number.rpartition(",")
        if not _THOUSANDS_DOT.fullmatch(whole):
            raise InvalidAmountError(AmountError.INVALID)
        return whole.replace(".", ""), fraction
    if "." in number:
        if number.count(".") > 1 or (_THOUSANDS_DOT.fullmatch(number) and not has_unit):
            if not _THOUSANDS_DOT.fullmatch(number):
                raise InvalidAmountError(AmountError.INVALID)
            return number.replace(".", ""), ""
        whole, _, fraction = number.partition(".")  # one dot before a unit, or "12.5"
        return whole, fraction
    if "," in number:
        if _THOUSANDS_COMMA.fullmatch(number) and not has_unit:
            return number.replace(",", ""), ""
        whole, _, fraction = number.partition(",")
        if "," in fraction:
            raise InvalidAmountError(AmountError.INVALID)
        return whole, fraction
    return number, ""


def parse_amount(text: str) -> ParsedAmount:
    """Parse a user-typed rupiah amount (see the module docstring). Integer arithmetic only."""
    raw = " ".join(text.strip().lower().split())
    if not raw:
        raise InvalidAmountError(AmountError.EMPTY)
    if raw[0] in "-\u2212":  # hyphen-minus or the Unicode minus sign
        raise InvalidAmountError(AmountError.NEGATIVE)
    if len(raw) > _MAX_INPUT:
        raise InvalidAmountError(AmountError.INVALID)
    match = _AMOUNT_RE.fullmatch(raw)
    if match is None:
        raise InvalidAmountError(AmountError.INVALID)
    unit = match["unit"]
    whole, fraction = _split(match["number"], unit is not None)
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        raise InvalidAmountError(AmountError.INVALID)
    multiplier = _UNITS[unit] if unit else 1
    denominator = 10 ** len(fraction)
    numerator = (int(whole) * denominator + int(fraction or "0")) * multiplier
    quotient, remainder = divmod(numerator, denominator)
    if remainder * 2 >= denominator and remainder:
        quotient += 1  # half-up
    return ParsedAmount(validate_amount(quotient), rounded=remainder != 0)


def split_leading_amount(text: str) -> tuple[str | None, str | None]:
    """(amount text, rest) of a command argument such as ``Rp 2 juta makan siang``.

    The amount is the longest prefix that the :func:`parse_amount` grammar accepts, separated
    units included. Without such a prefix the first word is returned as the amount, so that
    parsing it reports the error instead of the text silently becoming a description. A
    unit-less number followed by a unit-like word (``2 jutaan``) is returned whole, which
    :func:`parse_amount` then refuses: the bot asks again rather than guessing.
    """
    raw = " ".join(text.split())
    if not raw:
        return None, None
    match = _LEADING_AMOUNT_RE.match(raw)
    if match is None:
        first, _, rest = raw.partition(" ")
        if first.lower().rstrip(".") == "rp" and rest:  # "Rp abc": keep the prefix with it
            second, _, rest = rest.partition(" ")
            first = f"{first} {second}"
        return first, rest or None
    amount, rest = match.group(0), raw[match.end() :].strip()
    has_unit = re.search(rf"(?:{_UNIT_WORDS})$", amount, re.IGNORECASE) is not None
    if rest and not has_unit and _UNIT_LIKE_RE.match(rest):
        word, _, rest = rest.partition(" ")
        return f"{amount} {word}", rest or None
    return amount, rest or None


def format_rupiah(value: int) -> str:
    """``Rp 1.250.000``; negative values (balances) as ``-Rp 1.250.000``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("rupiah amounts are int")
    grouped = f"{abs(value):,}".replace(",", ".")
    return f"-Rp {grouped}" if value < 0 else f"Rp {grouped}"
