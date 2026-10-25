"""Shared finance service machinery: the actor, authorized sessions, seeding, ledger writes.

Every public finance service method opens exactly one unit of work through
:meth:`FinanceServiceBase._session`, which authorizes **inside** that database transaction with
the membership it just read (G-2: never a cached role). Every session is authorized: with the
method's permission, or at least ``workspace.view``, which an inactive user never holds, so no
public path skips the user-status check. Refusals record ``security.authz_denied`` (with the
entity type and the request's correlation id) in their own short transaction (G-4, docs/09
§6.2).

The workspace's finance defaults are seeded the first time they are missing, in a **separate,
short transaction** before the request's own one (:meth:`FinanceServiceBase._seed`). Seeding
is idempotent and posts nothing, so it alone is retried on a deadlock or serialization failure;
a request that writes financial data is never re-run automatically (it fails with
:class:`RetryLaterError` and stores nothing).

The ledger writes below are the only code that creates headers, entries and revisions (D1,
CLAUDE.md rule 3). They lock the category and account they use ``FOR SHARE``, so a concurrent
archive either waits for the posting or is seen by it (R-10).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final
from uuid import UUID

from finbot.authz.context import WorkspaceContext
from finbot.authz.permissions import Permission
from finbot.authz.policy import ForbiddenError, allowed, authorize
from finbot.domain.finance import (
    DEFAULT_ACCOUNT_NAME,
    DEFAULT_ACCOUNT_OPENING_DATE,
    DEFAULT_ACCOUNT_TYPE,
    DEFAULT_CATEGORIES,
    DEFAULT_CATEGORY_NAME,
    Account,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    RetryLaterError,
    TransactionChanges,
    TransactionRecord,
    normalize_description,
    normalize_void_reason,
    today_in,
    validate_txn_date,
)
from finbot.domain.ledger import TxnSource, TxnStatus, TxnType, build_entries
from finbot.domain.money import validate_amount
from finbot.ports.finance import FinanceSession, FinanceStore, SessionMode

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: Attempts of the seeding transaction (it is idempotent and posts nothing).
SEED_ATTEMPTS: Final = 3
_SEED_BACKOFF_SECONDS: Final = 0.05


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Actor:
    """Who acts: an onboarded user, in a workspace (None: the active workspace)."""

    user_id: UUID
    workspace_id: UUID | None = None
    channel: str = "telegram"
    request_id: UUID | None = None  # correlates logs and audit events of one update


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
        self,
        actor: Actor,
        permission: Permission | None,
        *,
        write: bool = False,
        entity: str | None = None,
    ) -> AsyncIterator[FinanceSession]:
        """The request's unit of work, authorized for ``permission`` (default: the baseline
        ``workspace.view``). ``entity`` is the audited entity type of a refusal, unless the
        refusal names a more precise one (a category prompt inside a draft operation)."""
        mode = SessionMode.WRITE if write else SessionMode.READ
        ctx: WorkspaceContext | None = None
        seeded = False
        try:
            while True:
                async with self._store.session(
                    actor.user_id, actor.workspace_id, mode=mode, request_id=actor.request_id
                ) as s:
                    ctx = s.ctx
                    authorize(s.ctx, permission or Permission.WORKSPACE_VIEW)
                    if seeded or not needs_finance_defaults(s.ctx):
                        yield s
                        return
                # Nothing was done in that transaction: seed in its own, then start again.
                await self._seed(actor)
                seeded = True
        except ForbiddenError as exc:
            logger.info(
                "finance access denied",
                extra={
                    "event": "security.authz_denied",
                    "outcome": exc.reason,
                    "request_id": actor.request_id,
                },
            )
            if ctx is not None:
                await self._store.record_denial(
                    user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id,
                    permission=exc.permission.value,
                    role=exc.role,
                    entity_type=exc.entity or entity,
                    channel=actor.channel,
                    request_id=actor.request_id,
                )
            raise

    async def _seed(self, actor: Actor) -> None:
        """Seed the defaults in their own transaction; retried, being idempotent."""
        for attempt in range(1, SEED_ATTEMPTS + 1):
            try:
                async with self._store.session(
                    actor.user_id,
                    actor.workspace_id,
                    mode=SessionMode.SEED,
                    request_id=actor.request_id,
                ) as s:
                    await ensure_finance_defaults(s)
                return
            except RetryLaterError:
                logger.warning(
                    "finance seeding collided; retrying",
                    extra={"event": "finance.seed_retry", "count": attempt},
                )
                if attempt == SEED_ATTEMPTS:
                    raise
                await asyncio.sleep(_SEED_BACKOFF_SECONDS * attempt)


# ---------------------------------------------------------------------------- seeding


def needs_finance_defaults(ctx: WorkspaceContext) -> bool:
    """The member has no default account yet in an active (writable) workspace."""
    return ctx.default_account_id is None and ctx.workspace_status == "active"


async def ensure_finance_defaults(s: FinanceSession) -> None:
    """Seed "Kas" and the default categories once per workspace, and the member's default
    account (FR-IDN-04, FR-CAT-01, FR-ACC-05), in a ``SEED`` session.

    Idempotent and safe under concurrency: the session already holds the member's membership
    row (``FOR NO KEY UPDATE``, re-read after the lock), and the workspace's category-name lock
    serializes seeders of one workspace, so the second one finds the first one's rows. Every
    insert is also ``ON CONFLICT DO NOTHING``. Read-only (pending deletion) workspaces are
    never seeded."""
    if not needs_finance_defaults(s.ctx):
        return
    await s.lock_category_names()
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


def visible(s: FinanceSession, record: TransactionRecord | None) -> TransactionRecord | None:
    """``record`` if the caller may see it now: a voided transaction exists only for roles
    with ``transaction.view_deleted`` (others get NotFound, not a hint that it exists)."""
    if record is None or (
        record.status is TxnStatus.VOIDED
        and not allowed(s.ctx, Permission.TRANSACTION_VIEW_DELETED)
    ):
        return None
    return record


async def _require(s: FinanceSession, transaction_id: UUID) -> TransactionRecord:
    record = visible(s, await s.get_transaction(transaction_id, for_update=True))
    if record is None:
        raise NotFoundError("transaction")
    return record


async def _reload(s: FinanceSession, transaction_id: UUID) -> TransactionRecord:
    record = await s.get_transaction(transaction_id)
    if record is None:  # pragma: no cover - locked and changed in this transaction
        raise NotFoundError("transaction")
    return record


async def _active_category(s: FinanceSession, category_id: UUID, txn_type: TxnType) -> None:
    # FOR SHARE: a concurrent archive waits for this posting, or this posting sees it.
    category = await s.get_category(category_id, lock="share")
    if category is None:
        raise NotFoundError("category")  # also a category of another workspace (G-3)
    if category.archived:
        raise InvalidInputError("category_archived")
    if category.kind is not txn_type:
        raise InvalidInputError("category_kind_mismatch")


async def _usable_account(s: FinanceSession, account_id: UUID | None) -> Account:
    """An active account of the context workspace, locked FOR SHARE until commit."""
    account = None if account_id is None else await s.get_account(account_id, lock="share")
    if account is None or account.archived:
        raise InvalidInputError("account_unavailable")
    return account


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
    account_id: UUID | None = None,
) -> TransactionRecord:
    """Header + revision-1 entries + revision row + audit, in the caller's transaction.

    ``account_id`` is the account the draft resolved when it started (FR-ACC-05); None means
    the member's current default account."""
    authorize(s.ctx, Permission.TRANSACTION_CREATE)
    value = validate_amount(amount)
    validate_txn_date(txn_date, today)
    await _active_category(s, category_id, txn_type)
    account = await _usable_account(s, account_id or s.ctx.default_account_id)
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
        await _usable_account(s, record.account_id)
    if not await s.update_transaction(transaction_id, expected_version, values):
        raise ConflictError("stale")  # pragma: no cover - the row is locked FOR UPDATE
    if "amount" in values:
        await s.insert_entries(
            transaction_id,
            record.type,
            values["entry_revision"],
            build_entries(record.type, values["amount"], record.account_id),  # type: ignore[arg-type]
        )
    updated = await _reload(s, transaction_id)
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
    updated = await _reload(s, transaction_id)
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
