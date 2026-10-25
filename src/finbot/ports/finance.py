"""Finance persistence port: one unit of work per request (docs/09 §3, L2 to L5).

``FinanceStore.session()`` opens one database transaction for one user in one workspace:

* it sets ``app.user_id``, loads the user and the caller's **active** membership, then sets
  ``app.workspace_id``; RLS applies to every query after that;
* without an active membership there is no session: :class:`NotFoundError` (G-3);
* every method also filters by the context workspace explicitly (rule 1), on top of RLS;
* the transaction commits when the ``async with`` block exits normally, and rolls back on any
  exception, so a refused or failed operation never leaves partial writes;
* a deadlock or serialization failure rolls the whole transaction back and surfaces as
  :class:`~finbot.domain.finance.RetryLaterError` (nothing was stored).

:class:`SessionMode` decides isolation and locks (docs/10 §13.3 has the full lock order):

* ``READ``: one ``REPEATABLE READ READ ONLY`` snapshot, so every figure of one report comes
  from the same committed state; no row locks, no writes;
* ``WRITE``: ``READ COMMITTED``; the user row, the membership and the workspace are locked
  ``FOR SHARE``, so a concurrent suspension, role change, removal or status change waits for
  (and is serialized with) the mutation that was authorized under the old state;
* ``SEED``: as ``WRITE``, but the caller's membership is locked ``FOR NO KEY UPDATE`` from the
  start, because seeding sets its default account. Taking the strongest lock first means a
  lock is never upgraded, which is what deadlocked two concurrent first requests (Step 6B).

Implemented by ``finbot.infra.db.finance``.
"""

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from finbot.authz.context import WorkspaceContext
from finbot.domain.finance import (
    Account,
    AccountBalance,
    Category,
    CategoryTotal,
    DraftPurpose,
    DraftRecord,
    DraftStatus,
    HistoryFilter,
    PendingAction,
    PendingKind,
    TransactionRecord,
)
from finbot.domain.ledger import EntrySpec, TxnSource, TxnType

#: Row lock taken by a read inside a WRITE session: ``share`` to keep a row from changing
#: until commit (an archive waits), ``update`` to change it.
RowLock = Literal["share", "update"]


class SessionMode(StrEnum):
    READ = "read"
    WRITE = "write"
    SEED = "seed"


class FinanceSession(Protocol):
    ctx: WorkspaceContext

    # ---------------------------------------------------------------- accounts
    async def list_accounts(self, *, include_archived: bool = False) -> list[Account]: ...
    async def get_account(
        self, account_id: UUID, *, lock: RowLock | None = None
    ) -> Account | None: ...
    async def insert_account(
        self, name: str, account_type: str, opening_date: date
    ) -> Account | None:
        """None when an active account with that name exists (idempotent seeding)."""
        ...

    async def set_default_account(self, account_id: UUID) -> None: ...

    # -------------------------------------------------------------- categories
    async def lock_category_names(self) -> None:
        """Serialize category name changes and seeding in this workspace until commit, so a
        duplicate name is found by a query and never reaches the unique index as an error
        (whose detail would carry the name into the server log; docs/09 §10)."""
        ...

    async def count_categories(self, kind: TxnType | None = None) -> int:
        """Categories of the workspace (of ``kind``), archived ones included."""
        ...

    async def list_categories(
        self,
        kind: TxnType | None = None,
        *,
        archived: bool | None = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Category]:
        """``archived``: False active only, True archived only, None both."""
        ...

    async def get_category(
        self, category_id: UUID, *, lock: RowLock | None = None
    ) -> Category | None: ...
    async def find_active_category(self, kind: TxnType, name: str) -> Category | None: ...
    async def insert_category(
        self, kind: TxnType, name: str, *, is_default: bool = False
    ) -> Category | None:
        """None when an active category with that kind and name exists."""
        ...

    async def rename_category(self, category_id: UUID, name: str) -> Category | None:
        """None when the new name collides with another active category."""
        ...

    async def set_category_archived(self, category_id: UUID, archived: bool) -> Category | None:
        """None when un-archiving would collide with an active category of the same name."""
        ...

    # ------------------------------------------------------------ transactions
    async def insert_transaction(
        self,
        *,
        txn_type: TxnType,
        amount: int,
        txn_date: date,
        category_id: UUID | None,
        description: str | None,
        source: TxnSource,
    ) -> UUID: ...
    async def insert_entries(
        self, transaction_id: UUID, txn_type: TxnType, revision: int, entries: tuple[EntrySpec, ...]
    ) -> None: ...
    async def get_transaction(
        self, transaction_id: UUID, *, for_update: bool = False
    ) -> TransactionRecord | None: ...
    async def history(
        self, history_filter: HistoryFilter, *, limit: int, offset: int
    ) -> list[TransactionRecord]: ...
    async def update_transaction(
        self, transaction_id: UUID, expected_version: int, values: Mapping[str, Any]
    ) -> bool:
        """Apply ``values`` and ``version + 1`` iff the version still matches (optimistic lock)."""
        ...

    async def insert_revision(
        self,
        transaction_id: UUID,
        version: int,
        change_kind: str,
        snapshot: Mapping[str, Any],
        changed_fields: list[str],
    ) -> None: ...

    # ------------------------------------------------------------------ drafts
    async def lock_conversation(self) -> None:
        """Serialize conversation changes of this user in this workspace until commit."""
        ...

    async def pending_draft(self, *, for_update: bool = False) -> DraftRecord | None: ...
    async def get_draft(
        self, draft_id: UUID, *, for_update: bool = False
    ) -> DraftRecord | None: ...
    async def find_draft_by_ref(self, ref: str) -> DraftRecord | None:
        """The caller's most recent draft whose short reference is ``ref`` (any status)."""
        ...

    async def cancel_pending_drafts(self) -> int: ...
    async def insert_draft(
        self,
        purpose: DraftPurpose,
        proposal: Mapping[str, Any],
        source: TxnSource,
        expires_at: datetime,
        *,
        target_transaction_id: UUID | None = None,
        telegram_chat_id: int | None = None,
    ) -> UUID: ...
    async def update_draft(self, draft_id: UUID, proposal: Mapping[str, Any]) -> None: ...
    async def set_draft_status(
        self, draft_id: UUID, status: DraftStatus, confirmed_txn_id: UUID | None = None
    ) -> None: ...

    # --------------------------------------------------------- pending actions
    async def pending_action(self, *, for_update: bool = False) -> PendingAction | None:
        """The caller's pending action (its ``payload`` includes ``membership_version``)."""
        ...

    async def cancel_pending_actions(self) -> int: ...
    async def insert_pending_action(
        self, kind: PendingKind, payload: Mapping[str, Any], expires_at: datetime
    ) -> UUID: ...
    async def set_pending_action_status(self, action_id: UUID, status: str) -> None: ...

    # ------------------------------------------------- targeted invalidation (N01)
    async def expire_draft_if_pending(self, draft_id: UUID) -> bool:
        """Expire exactly this draft, only if it is still pending (a conditional update: a
        newer conversation that replaced it is never touched)."""
        ...

    async def expire_action_if_pending(self, action_id: UUID) -> bool: ...

    # ----------------------------------------------------------------- reports
    async def cash_flow(
        self, date_from: date | None, date_to: date | None
    ) -> list[tuple[str, TxnType, int]]:
        """(currency, type, total) of posted income/expense headers in the date range."""
        ...

    async def category_totals(self, date_from: date, date_to: date) -> list[CategoryTotal]: ...
    async def account_balances(self, as_of: date) -> list[AccountBalance]: ...

    # ------------------------------------------------------------------- audit
    async def audit(
        self,
        action: str,
        entity_type: str | None,
        entity_id: UUID | None,
        metadata: Mapping[str, Any] | None = None,
        *,
        actor_kind: str = "user",
    ) -> None: ...


class FinanceStore(Protocol):
    def session(
        self,
        user_id: UUID,
        workspace_id: UUID | None = None,
        *,
        mode: SessionMode = SessionMode.READ,
        request_id: UUID | None = None,
    ) -> AbstractAsyncContextManager[FinanceSession]:
        """One transaction as ``user_id`` in ``workspace_id`` (None: the personal workspace).
        ``request_id`` correlates every audit event written in it (``audit_events.request_id``)."""
        ...

    async def record_denial(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        permission: str,
        role: str | None,
        entity_type: str | None,
        channel: str,
        request_id: UUID | None = None,
    ) -> None:
        """``security.authz_denied`` in its own short transaction (docs/09 §6.2)."""
        ...
