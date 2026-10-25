"""Categories, transaction history and detail, corrections, voids and reports (docs/02 §5, §6, §8).

Every method is one unit of work; authorization happens inside it (docs/08 §1). A resource of
another workspace is simply not found in the context workspace, so it is reported exactly like
a missing one (G-3). Reports are SQL aggregates over posted headers (cash flow) or current
entries (balances); transfers never count as income or expense, voided transactions never
count, and currencies are reported separately (docs/05 §7). A report is read in one
``REPEATABLE READ READ ONLY`` session, so its balances, totals and category breakdown all come
from the same committed snapshot and always reconcile, even while others post.

Category names change only under the workspace's category-name lock, after a duplicate check:
a duplicate is reported as :class:`DuplicateNameError` without ever violating the unique index,
whose error detail would otherwise put the name into the PostgreSQL server log (docs/09 §10).
"""

import logging
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from finbot.authz.permissions import Permission
from finbot.authz.policy import allowed, authorize
from finbot.domain.finance import (
    CATEGORY_PAGE_SIZE,
    CATEGORY_QUOTA_PER_KIND,
    HISTORY_PAGE_SIZE,
    BalanceReport,
    Category,
    DuplicateNameError,
    HistoryFilter,
    HistoryPage,
    InvalidInputError,
    Month,
    MonthlySummary,
    NotFoundError,
    TransactionChanges,
    TransactionRecord,
    aggregate_totals,
    category_key,
    category_kind,
    normalize_category_name,
    validate_category_page,
    validate_page,
)
from finbot.domain.ledger import TxnStatus, TxnType
from finbot.ports.finance import FinanceSession
from finbot.services.finance import (
    Actor,
    FinanceServiceBase,
    WorkspaceInfo,
    set_voided,
    update_transaction,
    visible,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CategoryListing:
    """One page of categories (``archived``: the "nonaktif" view)."""

    workspace: WorkspaceInfo
    categories: tuple[Category, ...]
    can_manage: bool
    kind: TxnType | None = None
    archived: bool = False
    page: int = 0
    has_next: bool = False


@dataclass(frozen=True, slots=True)
class TransactionDetail:
    workspace: WorkspaceInfo
    record: TransactionRecord
    can_update: bool
    can_delete: bool
    can_restore: bool


@dataclass(frozen=True, slots=True)
class HistoryResult:
    workspace: WorkspaceInfo
    page: HistoryPage
    category: Category | None


@dataclass(frozen=True, slots=True)
class BalanceResult:
    workspace: WorkspaceInfo
    report: BalanceReport


@dataclass(frozen=True, slots=True)
class SummaryResult:
    workspace: WorkspaceInfo
    summary: MonthlySummary


async def create_category_in(s: FinanceSession, kind: TxnType, name: str) -> Category:
    authorize(s.ctx, Permission.CATEGORY_MANAGE)
    kind = category_kind(kind)
    clean = normalize_category_name(name)
    await s.lock_category_names()
    if await s.find_active_category(kind, clean) is not None:
        raise DuplicateNameError(clean)
    if await s.count_categories(kind) >= CATEGORY_QUOTA_PER_KIND:  # F06; also revision 0006
        raise InvalidInputError("category_limit")
    category = await s.insert_category(kind, clean)
    if category is None:  # a concurrent create won
        raise DuplicateNameError(clean)
    await s.audit("category.created", "category", category.id, {"kind": kind.value})
    return category


async def rename_category_in(s: FinanceSession, category_id: UUID, name: str) -> Category:
    authorize(s.ctx, Permission.CATEGORY_MANAGE)
    clean = normalize_category_name(name)
    await s.lock_category_names()
    category = await s.get_category(category_id, lock="update")
    if category is None:
        raise NotFoundError("category")
    if clean == category.name:
        raise InvalidInputError("no_changes")
    other = await s.find_active_category(category.kind, clean)
    if other is not None and other.id != category.id and not category.archived:
        raise DuplicateNameError(clean)
    renamed = await s.rename_category(category_id, clean)
    if renamed is None:
        raise DuplicateNameError(clean)
    await s.audit(
        "category.updated",
        "category",
        category_id,
        {
            "changed_fields": ["name"],
            "case_only": category_key(clean) == category_key(category.name),
        },
    )
    return renamed


class LedgerService(FinanceServiceBase):
    async def today(self, actor: Actor) -> date:
        """Today in the active workspace's timezone (G-7), to interpret dates in commands."""
        async with self._session(actor, Permission.WORKSPACE_VIEW) as s:
            return self._today(s.ctx)

    # ----------------------------------------------------------------- categories

    async def categories(
        self,
        actor: Actor,
        kind: TxnType | None = None,
        *,
        archived: bool = False,
        page: int = 0,
    ) -> CategoryListing:
        """One page of active (or, with ``archived``, only deactivated) categories."""
        validate_category_page(page)
        if kind is not None:
            category_kind(kind)
        async with self._session(actor, Permission.CATEGORY_VIEW) as s:
            items = await s.list_categories(
                kind,
                archived=archived,
                limit=CATEGORY_PAGE_SIZE + 1,
                offset=page * CATEGORY_PAGE_SIZE,
            )
            return CategoryListing(
                WorkspaceInfo.of(s.ctx),
                tuple(items[:CATEGORY_PAGE_SIZE]),
                allowed(s.ctx, Permission.CATEGORY_MANAGE),
                kind,
                archived,
                page,
                len(items) > CATEGORY_PAGE_SIZE,
            )

    async def category(self, actor: Actor, category_id: UUID) -> tuple[Category, bool]:
        """A category of the context workspace and whether the caller may manage it."""
        async with self._session(actor, Permission.CATEGORY_VIEW, entity="category") as s:
            category = await s.get_category(category_id)
            if category is None:
                raise NotFoundError("category")
            return category, allowed(s.ctx, Permission.CATEGORY_MANAGE)

    async def create_category(self, actor: Actor, kind: TxnType, name: str) -> Category:
        async with self._session(
            actor, Permission.CATEGORY_MANAGE, write=True, entity="category"
        ) as s:
            return await create_category_in(s, kind, name)

    async def rename_category(self, actor: Actor, category_id: UUID, name: str) -> Category:
        async with self._session(
            actor, Permission.CATEGORY_MANAGE, write=True, entity="category"
        ) as s:
            return await rename_category_in(s, category_id, name)

    async def set_category_active(self, actor: Actor, category_id: UUID, active: bool) -> Category:
        """Deactivate (archive) or reactivate. Old transactions keep the category (FR-CAT-04)."""
        async with self._session(
            actor, Permission.CATEGORY_MANAGE, write=True, entity="category"
        ) as s:
            await s.lock_category_names()
            category = await s.get_category(category_id, lock="update")
            if category is None:
                raise NotFoundError("category")
            if category.archived == (not active):
                return category
            if category.is_default and not active:
                raise InvalidInputError("category_default")  # the "Lainnya" fallback stays
            if active and await s.find_active_category(category.kind, category.name):
                raise DuplicateNameError(category.name)  # an active namesake exists
            updated = await s.set_category_archived(category_id, not active)
            if updated is None:  # reactivating collides with an active namesake
                raise DuplicateNameError(category.name)
            await s.audit(
                "category.unarchived" if active else "category.archived",
                "category",
                category_id,
                {"kind": category.kind.value},
            )
            return updated

    # --------------------------------------------------------------- transactions

    async def transaction(self, actor: Actor, transaction_id: UUID) -> TransactionDetail:
        async with self._session(actor, Permission.TRANSACTION_VIEW, entity="transaction") as s:
            record = visible(s, await s.get_transaction(transaction_id))
            if record is None:
                raise NotFoundError("transaction")
            posted = record.status is TxnStatus.POSTED
            return TransactionDetail(
                WorkspaceInfo.of(s.ctx),
                record,
                can_update=posted and allowed(s.ctx, Permission.TRANSACTION_UPDATE),
                can_delete=posted and allowed(s.ctx, Permission.TRANSACTION_DELETE),
                can_restore=not posted and allowed(s.ctx, Permission.TRANSACTION_RESTORE),
            )

    async def history(
        self, actor: Actor, history_filter: HistoryFilter, page: int = 0
    ) -> HistoryResult:
        validate_page(page)
        permission = (
            Permission.TRANSACTION_VIEW_DELETED
            if history_filter.voided
            else Permission.TRANSACTION_VIEW
        )
        async with self._session(actor, permission, entity="transaction") as s:
            category = None
            if history_filter.category_id is not None:
                category = await s.get_category(history_filter.category_id)
                if category is None:
                    raise NotFoundError("category")
            rows = await s.history(
                history_filter, limit=HISTORY_PAGE_SIZE + 1, offset=page * HISTORY_PAGE_SIZE
            )
            return HistoryResult(
                WorkspaceInfo.of(s.ctx),
                HistoryPage(
                    tuple(rows[:HISTORY_PAGE_SIZE]),
                    page,
                    len(rows) > HISTORY_PAGE_SIZE,
                    history_filter,
                ),
                category,
            )

    async def update_transaction(
        self,
        actor: Actor,
        transaction_id: UUID,
        expected_version: int,
        changes: TransactionChanges,
    ) -> TransactionRecord:
        async with self._session(
            actor, Permission.TRANSACTION_UPDATE, write=True, entity="transaction"
        ) as s:
            return await update_transaction(
                s, transaction_id, expected_version, changes, today=self._today(s.ctx)
            )

    async def void_transaction(
        self, actor: Actor, transaction_id: UUID, expected_version: int, reason: str | None = None
    ) -> TransactionRecord:
        async with self._session(
            actor, Permission.TRANSACTION_DELETE, write=True, entity="transaction"
        ) as s:
            return await set_voided(
                s, transaction_id, expected_version, voided=True, now=self._clock(), reason=reason
            )

    async def restore_transaction(
        self, actor: Actor, transaction_id: UUID, expected_version: int
    ) -> TransactionRecord:
        async with self._session(
            actor, Permission.TRANSACTION_RESTORE, write=True, entity="transaction"
        ) as s:
            return await set_voided(
                s, transaction_id, expected_version, voided=False, now=self._clock()
            )

    # -------------------------------------------------------------------- reports

    async def balance(self, actor: Actor) -> BalanceResult:
        """FR-RPT-02 / FR-ACC-04: recorded balances as of today (workspace timezone)."""
        async with self._session(actor, Permission.REPORT_VIEW) as s:
            authorize(s.ctx, Permission.ACCOUNT_VIEW)
            today = self._today(s.ctx)
            accounts = await s.account_balances(today)
            all_time = aggregate_totals(await s.cash_flow(None, today))
            return BalanceResult(
                WorkspaceInfo.of(s.ctx), BalanceReport(today, tuple(accounts), all_time)
            )

    async def summary(self, actor: Actor, month: Month | None = None) -> SummaryResult:
        """FR-RPT-01 for one calendar month in the workspace timezone (default: this month)."""
        async with self._session(actor, Permission.REPORT_VIEW) as s:
            today = self._today(s.ctx)
            period = month or Month.of(today)
            if period > Month.of(today):
                raise InvalidInputError("month_in_future")
            date_to = min(period.last_day, today)
            totals = aggregate_totals(await s.cash_flow(period.first_day, date_to))
            by_category = await s.category_totals(period.first_day, date_to)
            return SummaryResult(
                WorkspaceInfo.of(s.ctx),
                MonthlySummary(
                    period,
                    totals,
                    tuple(c for c in by_category if c.kind is TxnType.EXPENSE),
                    tuple(c for c in by_category if c.kind is TxnType.INCOME),
                ),
            )
