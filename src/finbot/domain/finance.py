"""Finance core values: categories, dates, filters, reports and the capture state (pure).

Rules implemented here (docs/02 §5, §6, §8; docs/05 §5):

* **Categories** have a kind (``income``/``expense``) and a name of 1 to 40 printable characters
  (NFC, whitespace collapsed). Names are unique per workspace and kind among *active*
  categories, compared case-insensitively with ``str.lower`` (the database index uses
  ``lower(name)``). Archived ("nonaktif") categories stay on old transactions (FR-CAT-04).
* **Defaults** (FR-CAT-01, FR-IDN-04) are defined here, in code, and seeded idempotently by the
  service; ``Lainnya`` is the default (fallback) category of each kind.
* **Dates** are business dates in the workspace timezone (G-7). A transaction date lies between
  2000-01-01 and today; back-dating is allowed (D17: no period locking), future dates are not.
* **Capture** (FR-TXN-04..06, FR-TXN-08): a draft walks type → amount → category →
  description → confirm. The account is resolved when the draft starts (the member's default
  account, FR-ACC-05) and stored in the draft, so a later change of the default cannot
  redirect it. Before confirming, any field (type, amount, category, description, date) can be
  corrected; every correction returns to the complete confirmation (FR-TXN-05). Nothing is a
  transaction until it is confirmed.
"""

import calendar
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Self
from uuid import UUID
from zoneinfo import ZoneInfo

from finbot.domain.ledger import TxnSource, TxnStatus, TxnType
from finbot.domain.money import ParsedAmount, Rupiah, validate_amount

CATEGORY_NAME_MAX: Final = 40
DESCRIPTION_MAX: Final = 500
VOID_REASON_MAX: Final = 200
MIN_TXN_DATE: Final = date(2000, 1, 1)
DRAFT_TTL: Final = timedelta(hours=24)  # FR-TXN-06
PENDING_ACTION_TTL: Final = timedelta(minutes=30)
HISTORY_PAGE_SIZE: Final = 10  # FR-TXN-09
HISTORY_MAX_PAGES: Final = 100  # no listing deeper than 1,000 rows; filter by date instead
CATEGORY_PAGE_SIZE: Final = 20  # buttons per category page (Telegram: at most 100 per message)
CATEGORY_MAX_PAGES: Final = 100  # 2,000 categories per listing; nobody pages further

# --------------------------------------------------------------------------- defaults

DEFAULT_CATEGORY_NAME: Final = "Lainnya"
DEFAULT_CATEGORIES: Final[dict[TxnType, tuple[str, ...]]] = {
    TxnType.INCOME: ("Gaji", "Bonus", "Penjualan", "Investasi", "Hadiah", "Lainnya"),
    TxnType.EXPENSE: (
        "Makanan",
        "Transportasi",
        "Belanja",
        "Tagihan",
        "Kesehatan",
        "Pendidikan",
        "Hiburan",
        "Iuran",
        "Lainnya",
    ),
}
DEFAULT_ACCOUNT_NAME: Final = "Kas"
DEFAULT_ACCOUNT_TYPE: Final = "cash"
#: The seeded "Kas" has opening balance 0 and opens at the earliest allowed date, so every
#: back-dated entry counts (docs/05 §7: entries before opening_date do not).
DEFAULT_ACCOUNT_OPENING_DATE: Final = MIN_TXN_DATE

CATEGORY_KINDS: Final = (TxnType.INCOME, TxnType.EXPENSE)


# ----------------------------------------------------------------------------- errors


class FinanceError(Exception):
    """Base class for expected, user-facing finance errors (never system failures)."""


class NotFoundError(FinanceError):
    """The resource does not exist in the caller's workspace (G-3: also for other workspaces)."""


class ConflictError(FinanceError):
    """The resource changed since it was shown (optimistic lock) or is in the wrong state."""


class DuplicateNameError(FinanceError):
    """An active category with this name and kind already exists."""


class InvalidInputError(FinanceError):
    """Input that fails validation. ``reason`` is a stable code for the message catalogue."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DraftExpiredError(FinanceError):
    """The draft or pending action is gone: confirmed elsewhere, cancelled or expired."""


class StepError(FinanceError):
    """Input that the current step of the conversation does not expect."""


class RetryLaterError(FinanceError):
    """The request collided with concurrent work and was rolled back as a whole; nothing was
    stored, and repeating it is safe."""


# -------------------------------------------------------------------------- text values


def _clean(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    without_controls = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch
        for ch in normalized
        if unicodedata.category(ch) != "Cf"
    )
    return " ".join(without_controls.split())


def normalize_category_name(text: str) -> str:
    name = _clean(text)
    if not name:
        raise InvalidInputError("category_name_empty")
    if len(name) > CATEGORY_NAME_MAX:
        raise InvalidInputError("category_name_too_long")
    return name


def category_key(name: str) -> str:
    """The case-insensitive comparison key (same semantics as the ``lower(name)`` index)."""
    return name.lower()


def normalize_description(text: str | None) -> str | None:
    if text is None:
        return None
    description = _clean(text)
    if len(description) > DESCRIPTION_MAX:
        raise InvalidInputError("description_too_long")
    return description or None


def normalize_void_reason(text: str | None) -> str | None:
    reason = _clean(text or "")
    if len(reason) > VOID_REASON_MAX:
        raise InvalidInputError("void_reason_too_long")
    return reason or None


def category_kind(value: TxnType) -> TxnType:
    if value not in CATEGORY_KINDS:
        raise InvalidInputError("category_kind")
    return value


# -------------------------------------------------------------------------------- dates


def today_in(timezone: str, now: datetime | None = None) -> date:
    """Today's calendar date in the workspace timezone (G-7)."""
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return instant.astimezone(ZoneInfo(timezone)).date()


def validate_txn_date(value: date, today: date) -> date:
    if value < MIN_TXN_DATE:
        raise InvalidInputError("date_too_old")
    if value > today:
        raise InvalidInputError("date_in_future")
    return value


_RELATIVE_DAYS: Final = {"hari ini": 0, "sekarang": 0, "kemarin": 1, "kemarin lusa": 2}
_DMY_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{4}))?$")
_YMD_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def parse_date(text: str, today: date) -> date:
    """``hari ini``, ``kemarin``, ``kemarin lusa``, ``DD/MM[/YYYY]``, ``DD-MM-YYYY``,
    ``YYYY-MM-DD``; validated against :func:`validate_txn_date`."""
    raw = " ".join(text.strip().lower().split())
    if raw in _RELATIVE_DAYS:
        return validate_txn_date(today - timedelta(days=_RELATIVE_DAYS[raw]), today)
    if (m := _YMD_RE.fullmatch(raw)) is not None:
        year, month, day = int(m[1]), int(m[2]), int(m[3])
    elif (m := _DMY_RE.fullmatch(raw)) is not None:
        year, month, day = int(m[3]) if m[3] else today.year, int(m[2]), int(m[1])
    else:
        raise InvalidInputError("date_invalid")
    try:
        value = date(year, month, day)
    except ValueError:
        raise InvalidInputError("date_invalid") from None
    return validate_txn_date(value, today)


_MONTHS: Final = 12
_MAX_YEAR: Final = 9999


@dataclass(frozen=True, slots=True, order=True)
class Month:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= _MONTHS or not MIN_TXN_DATE.year <= self.year <= _MAX_YEAR:
            raise InvalidInputError("month_invalid")

    @classmethod
    def of(cls, day: date) -> Self:
        return cls(day.year, day.month)

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def previous(self) -> "Month":
        return Month(self.year - 1, 12) if self.month == 1 else Month(self.year, self.month - 1)


MONTH_NAMES: Final = (
    "januari",
    "februari",
    "maret",
    "april",
    "mei",
    "juni",
    "juli",
    "agustus",
    "september",
    "oktober",
    "november",
    "desember",
)
_MONTH_NUMERIC_RE = re.compile(r"^(?:(\d{4})-(\d{1,2})|(\d{1,2})[/-](\d{4}))$")


def parse_month(text: str, today: date) -> Month:
    """``bulan ini``, ``bulan lalu``, ``YYYY-MM``, ``MM/YYYY``, ``<nama bulan> [YYYY]``."""
    raw = " ".join(text.strip().lower().split())
    current = Month.of(today)
    if raw in ("", "bulan ini"):
        return current
    if raw == "bulan lalu":
        return current.previous()
    if (m := _MONTH_NUMERIC_RE.fullmatch(raw)) is not None:
        year, month = (m[1], m[2]) if m[1] else (m[4], m[3])
        result = Month(int(year), int(month))
    else:
        name, _, year_text = raw.partition(" ")
        if name not in MONTH_NAMES or (year_text and not year_text.isdigit()):
            raise InvalidInputError("month_invalid")
        result = Month(int(year_text) if year_text else today.year, MONTH_NAMES.index(name) + 1)
    if result > current:
        raise InvalidInputError("month_in_future")
    return result


# ------------------------------------------------------------------------------ records


@dataclass(frozen=True, slots=True)
class Category:
    id: UUID
    kind: TxnType
    name: str
    is_default: bool
    archived: bool


@dataclass(frozen=True, slots=True)
class Account:
    id: UUID
    name: str
    type: str
    opening_balance: int
    opening_date: date
    archived: bool


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    """One transaction as the user sees it (the header plus its current single entry)."""

    id: UUID
    type: TxnType
    amount: Rupiah
    currency: str
    txn_date: date
    category_id: UUID | None
    category_name: str | None
    category_archived: bool
    description: str | None
    source: TxnSource
    status: TxnStatus
    version: int
    entry_revision: int
    account_id: UUID | None
    account_name: str | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime | None
    voided_at: datetime | None


@dataclass(frozen=True, slots=True)
class TransactionChanges:
    """Fields to change on a posted transaction (FR-TXN-10). ``None`` means unchanged."""

    amount: int | None = None
    category_id: UUID | None = None
    description: str | None = None
    clear_description: bool = False
    txn_date: date | None = None

    def is_empty(self) -> bool:
        return (
            self.amount is None
            and self.category_id is None
            and self.description is None
            and not self.clear_description
            and self.txn_date is None
        )


@dataclass(frozen=True, slots=True)
class HistoryFilter:
    txn_type: TxnType | None = None
    date_from: date | None = None
    date_to: date | None = None
    category_id: UUID | None = None
    voided: bool = False  # the "Dihapus" view (transaction.view_deleted)

    def __post_init__(self) -> None:
        if self.txn_type is TxnType.TRANSFER:
            raise InvalidInputError("filter_type")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise InvalidInputError("date_range")


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: tuple[TransactionRecord, ...]
    page: int
    has_next: bool
    filter: HistoryFilter


def validate_page(page: int) -> int:
    if not 0 <= page < HISTORY_MAX_PAGES:
        raise InvalidInputError("page")
    return page


def validate_category_page(page: int) -> int:
    if not 0 <= page < CATEGORY_MAX_PAGES:
        raise InvalidInputError("page")
    return page


@dataclass(frozen=True, slots=True)
class CurrencyTotals:
    """Cash movement of posted income/expense transactions in one currency."""

    currency: str
    income: int
    expense: int

    @property
    def net(self) -> int:
        return self.income - self.expense


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category_id: UUID | None
    name: str
    kind: TxnType
    currency: str
    total: int
    count: int


@dataclass(frozen=True, slots=True)
class MonthlySummary:
    month: Month
    totals: tuple[CurrencyTotals, ...]
    expense_by_category: tuple[CategoryTotal, ...]
    income_by_category: tuple[CategoryTotal, ...]


@dataclass(frozen=True, slots=True)
class AccountBalance:
    account_id: UUID
    name: str
    currency: str
    balance: int  # opening balance + Σ current posted entries; may be negative (D15)


@dataclass(frozen=True, slots=True)
class BalanceReport:
    """Recorded balances: what was entered in this bot, not a bank statement."""

    as_of: date
    accounts: tuple[AccountBalance, ...]
    all_time: tuple[CurrencyTotals, ...]

    def recorded_balance(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for account in self.accounts:
            totals[account.currency] = totals.get(account.currency, 0) + account.balance
        return totals


def aggregate_totals(rows: list[tuple[str, TxnType, int]]) -> tuple[CurrencyTotals, ...]:
    """Fold (currency, type, sum) rows into per-currency totals; currencies never mix."""
    by_currency: dict[str, dict[TxnType, int]] = {}
    for currency, txn_type, total in rows:
        bucket = by_currency.setdefault(currency, {})
        bucket[txn_type] = bucket.get(txn_type, 0) + int(total)
    return tuple(
        CurrencyTotals(c, v.get(TxnType.INCOME, 0), v.get(TxnType.EXPENSE, 0))
        for c, v in sorted(by_currency.items())
    )


# ---------------------------------------------------------------------- capture (drafts)


class DraftPurpose(StrEnum):
    CREATE = "create"
    EDIT = "edit"


class CaptureStep(StrEnum):
    TYPE = "type"
    AMOUNT = "amount"
    CATEGORY = "category"
    DESCRIPTION = "description"
    DATE = "date"
    CONFIRM = "confirm"


class EditField(StrEnum):
    AMOUNT = "amount"
    CATEGORY = "category"
    DESCRIPTION = "description"
    DATE = "date"


class DraftField(StrEnum):
    """A field of a create draft that can be corrected before saving (FR-TXN-05)."""

    TYPE = "type"
    AMOUNT = "amount"
    CATEGORY = "category"
    DESCRIPTION = "description"
    DATE = "date"


_FIELD_STEP: Final = {
    DraftField.TYPE: CaptureStep.TYPE,
    DraftField.AMOUNT: CaptureStep.AMOUNT,
    DraftField.CATEGORY: CaptureStep.CATEGORY,
    DraftField.DESCRIPTION: CaptureStep.DESCRIPTION,
    DraftField.DATE: CaptureStep.DATE,
}

#: Steps whose input is a typed text message (the others use buttons).
TEXT_STEPS: Final = frozenset({CaptureStep.AMOUNT, CaptureStep.DESCRIPTION, CaptureStep.DATE})


@dataclass(frozen=True, slots=True)
class CaptureState:
    """The proposal of a ``create`` draft (stored as ``drafts.proposal``)."""

    step: CaptureStep
    txn_date: date
    txn_type: TxnType | None = None
    amount: int | None = None
    rounded: bool = False
    category_id: UUID | None = None
    description: str | None = None
    description_done: bool = False
    account_id: UUID | None = None  # resolved when the draft starts (FR-ACC-05)

    @classmethod
    def start(
        cls,
        today: date,
        txn_type: TxnType | None = None,
        amount: ParsedAmount | None = None,
        description: str | None = None,
        *,
        account_id: UUID | None = None,
    ) -> "CaptureState":
        if txn_type is not None and txn_type not in CATEGORY_KINDS:
            raise InvalidInputError("txn_type")
        state = cls(CaptureStep.TYPE, today, txn_type, account_id=account_id)
        if amount is not None:
            state = replace(state, amount=amount.value, rounded=amount.rounded)
        if description is not None:
            text = normalize_description(description)
            state = replace(state, description=text, description_done=text is not None)
        return state._advance()

    def _advance(self) -> "CaptureState":
        if self.txn_type is None:
            step = CaptureStep.TYPE
        elif self.amount is None:
            step = CaptureStep.AMOUNT
        elif self.category_id is None:
            step = CaptureStep.CATEGORY
        elif not self.description_done:
            step = CaptureStep.DESCRIPTION
        else:
            step = CaptureStep.CONFIRM
        return replace(self, step=step)

    def _expect(self, *steps: CaptureStep) -> None:
        if self.step not in steps:
            raise StepError(self.step.value)

    @property
    def complete_fields(self) -> bool:
        """Every field has a value: the draft can be (or was) shown for confirmation."""
        return (
            self.txn_type is not None
            and self.amount is not None
            and self.category_id is not None
            and self.description_done
        )

    @property
    def revising(self) -> bool:
        """A correction of a complete draft is in progress (offer "back to the summary")."""
        return self.complete_fields and self.step is not CaptureStep.CONFIRM

    def with_type(self, txn_type: TxnType) -> "CaptureState":
        self._expect(CaptureStep.TYPE)
        if txn_type not in CATEGORY_KINDS:
            raise InvalidInputError("txn_type")
        if txn_type is self.txn_type:
            return self._advance()
        # A category has a kind (FR-CAT-03): a new type needs a new category.
        return replace(self, txn_type=txn_type, category_id=None)._advance()

    def revise(self, draft_field: DraftField) -> "CaptureState":
        """Open one field of the confirmation for correction (FR-TXN-05)."""
        self._expect(CaptureStep.CONFIRM)
        return replace(self, step=_FIELD_STEP[draft_field])

    def back(self) -> "CaptureState":
        """Leave a correction unchanged and return to the confirmation."""
        if not self.revising:
            raise StepError(self.step.value)
        return replace(self, step=CaptureStep.CONFIRM)

    def with_amount(self, amount: ParsedAmount) -> "CaptureState":
        self._expect(CaptureStep.AMOUNT)
        return replace(self, amount=amount.value, rounded=amount.rounded)._advance()

    def with_category(self, category: Category) -> "CaptureState":
        self._expect(CaptureStep.CATEGORY)
        if category.archived:
            raise InvalidInputError("category_archived")
        if category.kind is not self.txn_type:
            raise InvalidInputError("category_kind_mismatch")
        return replace(self, category_id=category.id)._advance()

    def with_description(self, text: str | None) -> "CaptureState":
        self._expect(CaptureStep.DESCRIPTION)
        return replace(
            self, description=normalize_description(text), description_done=True
        )._advance()

    def with_date(self, value: date, today: date) -> "CaptureState":
        self._expect(CaptureStep.DATE)
        return replace(self, txn_date=validate_txn_date(value, today), step=CaptureStep.CONFIRM)

    def complete(self) -> tuple[TxnType, Rupiah, UUID]:
        """(type, amount, category) of a draft ready to confirm, or raise StepError."""
        if (
            self.step is not CaptureStep.CONFIRM
            or self.txn_type is None
            or self.amount is None
            or self.category_id is None
        ):
            raise StepError(self.step.value)
        return self.txn_type, validate_amount(self.amount), self.category_id

    def to_json(self) -> dict[str, Any]:
        return {
            "purpose": DraftPurpose.CREATE.value,
            "step": self.step.value,
            "txn_date": self.txn_date.isoformat(),
            "txn_type": self.txn_type.value if self.txn_type else None,
            "amount": self.amount,
            "rounded": self.rounded,
            "category_id": str(self.category_id) if self.category_id else None,
            "description": self.description,
            "description_done": self.description_done,
            "account_id": str(self.account_id) if self.account_id else None,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "CaptureState":
        return cls(
            step=CaptureStep(data["step"]),
            txn_date=date.fromisoformat(data["txn_date"]),
            txn_type=TxnType(data["txn_type"]) if data.get("txn_type") else None,
            amount=data.get("amount"),
            rounded=bool(data.get("rounded")),
            category_id=UUID(data["category_id"]) if data.get("category_id") else None,
            description=data.get("description"),
            description_done=bool(data.get("description_done")),
            account_id=UUID(data["account_id"]) if data.get("account_id") else None,
        )


@dataclass(frozen=True, slots=True)
class EditState:
    """The proposal of an ``edit`` draft: one field of one posted transaction."""

    field: EditField
    expected_version: int
    txn_type: TxnType
    step: CaptureStep  # AMOUNT/CATEGORY/DESCRIPTION/DATE until a value is given, then CONFIRM
    amount: int | None = None
    rounded: bool = False
    category_id: UUID | None = None
    description: str | None = None
    txn_date: date | None = None
    has_value: bool = False

    @classmethod
    def start(cls, edit_field: EditField, expected_version: int, txn_type: TxnType) -> "EditState":
        step = {
            EditField.AMOUNT: CaptureStep.AMOUNT,
            EditField.CATEGORY: CaptureStep.CATEGORY,
            EditField.DESCRIPTION: CaptureStep.DESCRIPTION,
            EditField.DATE: CaptureStep.DATE,
        }[edit_field]
        return cls(edit_field, expected_version, txn_type, step)

    def _expect(self, step: CaptureStep) -> None:
        if self.step is not step:
            raise StepError(self.step.value)

    def with_amount(self, amount: ParsedAmount) -> "EditState":
        self._expect(CaptureStep.AMOUNT)
        return replace(
            self,
            amount=amount.value,
            rounded=amount.rounded,
            has_value=True,
            step=CaptureStep.CONFIRM,
        )

    def with_category(self, category: Category) -> "EditState":
        self._expect(CaptureStep.CATEGORY)
        if category.archived:
            raise InvalidInputError("category_archived")
        if category.kind is not self.txn_type:
            raise InvalidInputError("category_kind_mismatch")
        return replace(self, category_id=category.id, has_value=True, step=CaptureStep.CONFIRM)

    def with_description(self, text: str | None) -> "EditState":
        self._expect(CaptureStep.DESCRIPTION)
        return replace(
            self, description=normalize_description(text), has_value=True, step=CaptureStep.CONFIRM
        )

    def with_date(self, value: date, today: date) -> "EditState":
        self._expect(CaptureStep.DATE)
        return replace(
            self, txn_date=validate_txn_date(value, today), has_value=True, step=CaptureStep.CONFIRM
        )

    def changes(self) -> TransactionChanges:
        if self.step is not CaptureStep.CONFIRM or not self.has_value:
            raise StepError(self.step.value)
        match self.field:
            case EditField.AMOUNT:
                return TransactionChanges(amount=self.amount)
            case EditField.CATEGORY:
                return TransactionChanges(category_id=self.category_id)
            case EditField.DESCRIPTION:
                if self.description is None:
                    return TransactionChanges(clear_description=True)
                return TransactionChanges(description=self.description)
            case EditField.DATE:
                return TransactionChanges(txn_date=self.txn_date)

    def to_json(self) -> dict[str, Any]:
        return {
            "purpose": DraftPurpose.EDIT.value,
            "field": self.field.value,
            "expected_version": self.expected_version,
            "txn_type": self.txn_type.value,
            "step": self.step.value,
            "amount": self.amount,
            "rounded": self.rounded,
            "category_id": str(self.category_id) if self.category_id else None,
            "description": self.description,
            "txn_date": self.txn_date.isoformat() if self.txn_date else None,
            "has_value": self.has_value,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "EditState":
        return cls(
            field=EditField(data["field"]),
            expected_version=int(data["expected_version"]),
            txn_type=TxnType(data["txn_type"]),
            step=CaptureStep(data["step"]),
            amount=data.get("amount"),
            rounded=bool(data.get("rounded")),
            category_id=UUID(data["category_id"]) if data.get("category_id") else None,
            description=data.get("description"),
            txn_date=date.fromisoformat(data["txn_date"]) if data.get("txn_date") else None,
            has_value=bool(data.get("has_value")),
        )


def draft_state(proposal: dict[str, Any]) -> CaptureState | EditState:
    if proposal.get("purpose") == DraftPurpose.EDIT.value:
        return EditState.from_json(proposal)
    return CaptureState.from_json(proposal)


def draft_ref(draft_id: UUID) -> str:
    """The short, non-secret reference of a draft used in buttons (its random tail).

    It only detects stale keyboards: the draft itself is always looked up as the caller's own
    pending draft in the active workspace (docs/09 L1), never by this reference alone.
    """
    return draft_id.hex[-8:]


@dataclass(frozen=True, slots=True)
class DraftView:
    """A pending draft as the adapter renders it."""

    id: UUID
    ref: str
    state: CaptureState | EditState
    workspace_name: str
    workspace_kind: str
    currency: str
    categories: tuple[Category, ...] = ()  # offered at a category step (one page)
    category: Category | None = None  # the chosen one, for the summary
    target: TransactionRecord | None = None  # edit drafts: the transaction being changed
    account: Account | None = None  # create drafts: the account the transaction will use
    category_page: int = 0
    categories_has_next: bool = False


class DraftStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class DraftRecord:
    id: UUID
    purpose: DraftPurpose
    status: DraftStatus
    proposal: dict[str, Any]
    expires_at: datetime
    target_transaction_id: UUID | None
    confirmed_txn_id: UUID | None


class PendingKind(StrEnum):
    CATEGORY_CREATE = "category_create"
    CATEGORY_RENAME = "category_rename"


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: UUID
    kind: PendingKind
    expires_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)
