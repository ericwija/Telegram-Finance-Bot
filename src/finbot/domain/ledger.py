"""The single place that encodes the entry sign rules (D1, D22(a); docs/05 §5.2): pure.

A transaction is a header with a positive ``amount`` plus signed entries, one per account:

* income: exactly one ``single`` entry, ``+amount``;
* expense: exactly one ``single`` entry, ``-amount``;
* transfer: a ``source`` entry ``-amount`` and a ``destination`` entry ``+amount`` on two
  different accounts (sum zero). The schema and these rules support transfers; the guided
  transfer flow (FR-TXN-07) is not built yet.

The database checks the same rules (row CHECKs plus the deferred ``trg_entries_balanced``), so
application and database cannot disagree. Balances are sums of current entries only.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from finbot.domain.money import Rupiah, validate_amount


class TxnType(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"


class Leg(StrEnum):
    SINGLE = "single"
    SOURCE = "source"
    DESTINATION = "destination"


class TxnStatus(StrEnum):
    POSTED = "posted"
    VOIDED = "voided"


class TxnSource(StrEnum):
    TEXT = "text"
    AI_TEXT = "ai_text"
    RECEIPT = "receipt"
    MANUAL = "manual"
    IMPORT = "import"


@dataclass(frozen=True, slots=True)
class EntrySpec:
    leg: Leg
    account_id: UUID
    amount: int  # signed rupiah


class LedgerRuleError(ValueError):
    """A request that would violate the entry rules (never reaches the database)."""


def build_entries(
    txn_type: TxnType,
    amount: int,
    account_id: UUID,
    destination_account_id: UUID | None = None,
) -> tuple[EntrySpec, ...]:
    """The complete entry set for one revision of a transaction."""
    value: Rupiah = validate_amount(amount)
    if txn_type is TxnType.TRANSFER:
        if destination_account_id is None:
            raise LedgerRuleError("a transfer needs a destination account")
        if destination_account_id == account_id:
            raise LedgerRuleError("a transfer needs two different accounts")
        return (
            EntrySpec(Leg.SOURCE, account_id, -value),
            EntrySpec(Leg.DESTINATION, destination_account_id, value),
        )
    if destination_account_id is not None:
        raise LedgerRuleError(f"{txn_type} has a single account")
    sign = 1 if txn_type is TxnType.INCOME else -1
    return (EntrySpec(Leg.SINGLE, account_id, sign * value),)


def check_entries(txn_type: TxnType, amount: int, entries: tuple[EntrySpec, ...]) -> None:
    """INV-2..INV-5 for one entry set (the same rules the deferred trigger checks)."""
    legs = sorted(e.leg for e in entries)
    if txn_type is TxnType.TRANSFER:
        if legs != [Leg.DESTINATION, Leg.SOURCE]:
            raise LedgerRuleError("a transfer has one source and one destination entry")
        source = next(e for e in entries if e.leg is Leg.SOURCE)
        destination = next(e for e in entries if e.leg is Leg.DESTINATION)
        if destination.amount != amount or source.amount + destination.amount != 0:
            raise LedgerRuleError("transfer legs must be -amount and +amount")
        if source.account_id == destination.account_id:
            raise LedgerRuleError("a transfer needs two different accounts")
        return
    if legs != [Leg.SINGLE]:
        raise LedgerRuleError(f"{txn_type} has exactly one single entry")
    expected = amount if txn_type is TxnType.INCOME else -amount
    if entries[0].amount != expected:
        raise LedgerRuleError(f"{txn_type} entry must be {expected}")
