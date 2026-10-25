"""Shared finance service machinery: the actor, authorized sessions, seeding, ledger writes.

Every public finance service method opens exactly one unit of work through
:meth:`FinanceServiceBase._session`, which authorizes **inside** that database transaction with
the membership it just read (G-2: never a cached role), seeds the workspace's finance defaults
the first time they are missing, and records ``security.authz_denied`` in its own short
transaction when access is refused (G-4, docs/09 §6.2). The ledger writes below are the only
code that creates headers, entries and revisions (D1, CLAUDE.md rule 3).
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from finbot.authz.context import WorkspaceContext
from finbot.authz.permissions import Permission
from finbot.authz.policy import ForbiddenError, authorize
from finbot.domain.finance import (
    DEFAULT_ACCOUNT_NAME,
    DEFAULT_ACCOUNT_OPENING_DATE,
    DEFAULT_ACCOUNT_TYPE,
    DEFAULT_CATEGORIES,
    DEFAULT_CATEGORY_NAME,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    TransactionChanges,
    TransactionRecord,
    normalize_description,
    normalize_void_reason,
    today_in,
    validate_txn_date,
)
from finbot.domain.ledger import TxnSource, TxnStatus, TxnType, build_entries
from finbot.domain.money import validate_amount
from finbot.ports.finance import FinanceSession, FinanceStore

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Actor:
    """Who acts: an onboarded user, in a workspace (None: the active workspace)."""

    user_id: UUID
    workspace_id: UUID | None = None
    channel: str = "telegram"


@dataclass(frozen=True, slots=True)
class WorkspaceInfo:
    """What a reply shows about the context (G-8: name and type on every confirmation)."""

    name: str
    kind: str
    currency: str
    timezone: str

    @classmethod
    def of(cls, ctx: WorkspaceContext) -> "WorkspaceInfo":
        return cls(ctx.workspace_name, ctx.workspace_kind, ctx.currency, ctx.timezone)


class FinanceServiceBase:
    def __init__(self, store: FinanceStore, *, clock: Clock = utc_now) -> None:
        self._store = store
        self._clock = clock

    def _today(self, ctx: WorkspaceContext) -> date:
        return today_in(ctx.timezone, self._clock())

    @asynccontextmanager
    async def _session(
        self, actor: Actor, permission: Permission | None, *, write: bool = False
    ) -> AsyncIterator[FinanceSession]:
        ctx: WorkspaceContext | None = None
        try:
            async with self._store.session(actor.user_id, actor.workspace_id, write=write) as s:
                ctx = s.ctx
                if permission is not None:
                    authorize(s.ctx, permission)
                await ensure_finance_defaults(s)
                yield s
        except ForbiddenError as exc:
            logger.info(
                "finance access denied",
                extra={"event": "security.authz_denied", "outcome": exc.reason},
            )
            if ctx is not None:
                await self._store.record_denial(
                    user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id,
                    permission=exc.permission.value,
                    role=exc.role,
                    entity_type=None,
                    channel=actor.channel,
                )
            raise


# ---------------------------------------------------------------------------- seeding


async def ensure_finance_defaults(s: FinanceSession) -> None:
    """Seed "Kas" and the default categories once per workspace, and the member's default
    account (FR-IDN-04, FR-CAT-01, FR-ACC-05). Idempotent and safe under concurrency: every
    insert is ``ON CONFLICT DO NOTHING`` against the unique name indexes. Read-only
    (pending deletion) workspaces are never seeded."""
    if s.ctx.default_account_id is not None or s.ctx.workspace_status != "active":
        return
    if await s.count_categories() == 0:
        for kind, names in DEFAULT_CATEGORIES.items():
            for name in names:
                is_default = name == DEFAULT_CATEGORY_NAME
                category = await s.insert_category(kind, name, is_default=is_default)
                if category is not None:
                    await s.audit(
                        "category.created",
                        "category",
                        category.id,
                        {"kind": kind.value, "is_default": is_default, "seeded": True},
                        actor_kind="system",
                    )
    accounts = await s.list_accounts()
    if not accounts:
        account = await s.insert_account(
            DEFAULT_ACCOUNT_NAME, DEFAULT_ACCOUNT_TYPE, DEFAULT_ACCOUNT_OPENING_DATE
        )
        if account is not None:
            await s.audit(
                "account.created",
                "account",
                account.id,
                {"type": DEFAULT_ACCOUNT_TYPE, "seeded": True},
                actor_kind="system",
            )
        accounts = await s.list_accounts()
    if accounts:
        await s.set_default_account(accounts[0].id)
        logger.info("finance defaults ready", extra={"event": "finance.defaults_ready"})


# ----------------------------------------------------------------------- ledger writes


def snapshot(record: TransactionRecord) -> dict[str, Any]:
    """Structured fields only (docs/05 §5.2): never the free-text description."""
    return {
        "type": record.type.value,
        "amount": record.amount,
        "currency": record.currency,
        "txn_date": record.txn_date.isoformat(),
        "category_id": str(record.category_id) if record.category_id else None,
        "status": record.status.value,
        "entry_revision": record.entry_revision,
        "account_id": str(record.account_id) if record.account_id else None,
        "source": record.source.value,
        "has_description": record.description is not None,
    }


async def _require(s: FinanceSession, transaction_id: UUID) -> TransactionRecord:
    record = await s.get_transaction(transaction_id, for_update=True)
    if record is None:
        raise NotFoundError("transaction")
    return record


async def _active_category(s: FinanceSession, category_id: UUID, txn_type: TxnType) -> None:
    category = await s.get_category(category_id)
    if category is None:
        raise NotFoundError("category")  # also a category of another workspace (G-3)
    if category.archived:
        raise InvalidInputError("category_archived")
    if category.kind is not txn_type:
        raise InvalidInputError("category_kind_mismatch")


async def create_transaction(
    s: FinanceSession,
    *,
    txn_type: TxnType,
    amount: int,
    txn_date: date,
    category_id: UUID,
    description: str | None,
    source: TxnSource,
    today: date,
) -> TransactionRecord:
    """Header + revision-1 entries + revision row + audit, in the caller's transaction."""
    authorize(s.ctx, Permission.TRANSACTION_CREATE)
    value = validate_amount(amount)
    validate_txn_date(txn_date, today)
    await _active_category(s, category_id, txn_type)
    account_id = s.ctx.default_account_id
    account = None if account_id is None else await s.get_account(account_id)
    if account is None or account.archived:
        raise InvalidInputError("account_unavailable")
    transaction_id = await s.insert_transaction(
        txn_type=txn_type,
        amount=value,
        txn_date=txn_date,
        category_id=category_id,
        description=normalize_description(description),
        source=source,
    )
    await s.insert_entries(transaction_id, txn_type, 1, build_entries(txn_type, value, account.id))
    record = await s.get_transaction(transaction_id)
    if record is None:  # pragma: no cover - just inserted in this transaction
        raise NotFoundError("transaction")
    await s.insert_revision(transaction_id, record.version, "create", snapshot(record), [])
    await s.audit(
        "transaction.created",
        "transaction",
        transaction_id,
        {
            "type": txn_type.value,
            "amount": value,
            "currency": record.currency,
            "txn_date": txn_date.isoformat(),
            "back_dated": txn_date < today,
            "source": source.value,
        },
    )
    return record


async def _changed_values(
    s: FinanceSession, record: TransactionRecord, changes: TransactionChanges, today: date
) -> tuple[dict[str, Any], list[str]]:
    """Column values that differ from ``record``, validated, and the changed field names."""
    values: dict[str, Any] = {}
    changed: list[str] = []
    if changes.amount is not None and changes.amount != record.amount:
        values["amount"] = validate_amount(changes.amount)
        values["entry_revision"] = record.entry_revision + 1
        changed.append("amount")
    if changes.category_id is not None and changes.category_id != record.category_id:
        await _active_category(s, changes.category_id, record.type)
        values["category_id"] = changes.category_id
        changed.append("category")
    description = normalize_description(changes.description)
    if changes.clear_description and record.description is not None:
        values["description"] = None
        changed.append("description")
    elif description is not None and description != record.description:
        values["description"] = description
        changed.append("description")
    if changes.txn_date is not None and changes.txn_date != record.txn_date:
        values["txn_date"] = validate_txn_date(changes.txn_date, today)
        changed.append("txn_date")
    return values, changed


async def update_transaction(
    s: FinanceSession,
    transaction_id: UUID,
    expected_version: int,
    changes: TransactionChanges,
    *,
    today: date,
) -> TransactionRecord:
    """FR-TXN-10: optimistic lock, a new complete entry set when the amount changes."""
    authorize(s.ctx, Permission.TRANSACTION_UPDATE)
    record = await _require(s, transaction_id)
    if record.status is TxnStatus.VOIDED:
        raise ConflictError("voided")  # R-8: restore first
    if record.version != expected_version:
        raise ConflictError("stale")
    values, changed = await _changed_values(s, record, changes, today)
    if not values:
        raise InvalidInputError("no_changes")
    if "amount" in values:
        account = None if record.account_id is None else await s.get_account(record.account_id)
        if account is None or account.archived:
            raise InvalidInputError("account_unavailable")
    if not await s.update_transaction(transaction_id, expected_version, values):
        raise ConflictError("stale")  # pragma: no cover - the row is locked FOR UPDATE
    if "amount" in values:
        await s.insert_entries(
            transaction_id,
            record.type,
            values["entry_revision"],
            build_entries(record.type, values["amount"], record.account_id),  # type: ignore[arg-type]
        )
    updated = await _require(s, transaction_id)
    await s.insert_revision(transaction_id, updated.version, "update", snapshot(updated), changed)
    metadata: dict[str, Any] = {"changed_fields": changed, "version": updated.version}
    if "amount" in values:
        metadata |= {
            "amount_before": record.amount,
            "amount_after": updated.amount,
            "entry_revision": updated.entry_revision,
        }
    if "txn_date" in values:
        metadata |= {
            "txn_date_before": record.txn_date.isoformat(),
            "txn_date_after": updated.txn_date.isoformat(),
            "back_dated": updated.txn_date < today,
        }
    if "category" in changed:
        metadata |= {
            "category_before": str(record.category_id),
            "category_after": str(updated.category_id),
        }
    await s.audit("transaction.updated", "transaction", transaction_id, metadata)
    return updated


async def set_voided(
    s: FinanceSession,
    transaction_id: UUID,
    expected_version: int,
    *,
    voided: bool,
    now: datetime,
    reason: str | None = None,
) -> TransactionRecord:
    """FR-TXN-11: void ("hapus") or restore the whole transaction; entries are untouched."""
    authorize(s.ctx, Permission.TRANSACTION_DELETE if voided else Permission.TRANSACTION_RESTORE)
    record = await _require(s, transaction_id)
    if voided and record.status is TxnStatus.VOIDED:
        raise ConflictError("already_voided")
    if not voided and record.status is TxnStatus.POSTED:
        raise ConflictError("not_voided")
    if record.version != expected_version:
        raise ConflictError("stale")
    values: dict[str, Any] = (
        {
            "status": TxnStatus.VOIDED.value,
            "voided_at": now,
            "voided_by": s.ctx.user_id,
            "void_reason": normalize_void_reason(reason),
        }
        if voided
        else {"status": TxnStatus.POSTED.value, "voided_at": None, "voided_by": None,
              "void_reason": None}
    )  # fmt: skip
    if not await s.update_transaction(transaction_id, expected_version, values):
        raise ConflictError("stale")  # pragma: no cover - the row is locked FOR UPDATE
    updated = await _require(s, transaction_id)
    kind = "void" if voided else "restore"
    await s.insert_revision(transaction_id, updated.version, kind, snapshot(updated), ["status"])
    await s.audit(
        "transaction.voided" if voided else "transaction.restored",
        "transaction",
        transaction_id,
        {
            "version": updated.version,
            "type": updated.type.value,
            "amount": updated.amount,
            "reason_given": voided and values["void_reason"] is not None,
        },
    )
    return updated
