"""PostgreSQL implementation of the finance unit of work (``ports.finance``), as ``finbot_app``.

Every query uses bound parameters only and filters by the context workspace explicitly, on top
of the RLS policies of revision 0004. Statements whose expected failure is a uniqueness
conflict run in a savepoint, so the conflict is reported without aborting the request's
transaction. Free text (descriptions, category names) never reaches logs or audit metadata.
"""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final
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
    NotFoundError,
    PendingAction,
    PendingKind,
    TransactionRecord,
)
from finbot.domain.identity import UserStatus
from finbot.domain.ledger import EntrySpec, TxnSource, TxnStatus, TxnType
from finbot.domain.money import Rupiah

if TYPE_CHECKING:
    from sqlalchemy.engine import Row
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_UNIQUE_VIOLATION: Final = "23505"

#: Columns ``update_transaction`` may set (column grants allow exactly these, plus version).
_UPDATABLE: Final = frozenset(
    {"amount", "txn_date", "category_id", "description", "status", "entry_revision",
     "voided_at", "voided_by", "void_reason"}
)  # fmt: skip

_TXN_SELECT: Final = """
SELECT t.id, t.type, t.amount, t.currency, t.txn_date, t.category_id,
       c.name AS category_name, (c.archived_at IS NOT NULL) AS category_archived,
       t.description, t.source, t.status, t.version, t.entry_revision,
       e.account_id, a.name AS account_name, t.created_by, t.created_at, t.updated_at,
       t.voided_at
FROM public.transactions t
LEFT JOIN public.categories c ON c.workspace_id = t.workspace_id AND c.id = t.category_id
LEFT JOIN public.transaction_entries e
  ON e.workspace_id = t.workspace_id AND e.transaction_id = t.id
 AND e.revision = t.entry_revision AND e.leg IN ('single', 'source')
LEFT JOIN public.accounts a ON a.workspace_id = e.workspace_id AND a.id = e.account_id
"""

_CATEGORY_COLUMNS: Final = "id, kind, name, is_default, (archived_at IS NOT NULL) AS archived"
_DRAFT_COLUMNS: Final = (
    "id, purpose, status, proposal, expires_at, target_transaction_id, confirmed_txn_id"
)


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(
        getattr(orig, "__cause__", None), "sqlstate", None
    )


def _text(sql: str) -> Any:
    from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time

    return text(sql)


def _category(row: "Row[Any]") -> Category:
    return Category(row.id, TxnType(row.kind), row.name, row.is_default, row.archived)


def _account(row: "Row[Any]") -> Account:
    return Account(
        row.id, row.name, row.type, int(row.opening_balance), row.opening_date, row.archived
    )


def _transaction(row: "Row[Any]") -> TransactionRecord:
    return TransactionRecord(
        id=row.id,
        type=TxnType(row.type),
        amount=Rupiah(int(row.amount)),
        currency=row.currency,
        txn_date=row.txn_date,
        category_id=row.category_id,
        category_name=row.category_name,
        category_archived=bool(row.category_archived),
        description=row.description,
        source=TxnSource(row.source),
        status=TxnStatus(row.status),
        version=row.version,
        entry_revision=row.entry_revision,
        account_id=row.account_id,
        account_name=row.account_name,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        voided_at=row.voided_at,
    )


def _draft(row: "Row[Any]") -> DraftRecord:
    return DraftRecord(
        id=row.id,
        purpose=DraftPurpose(row.purpose),
        status=DraftStatus(row.status),
        proposal=dict(row.proposal),
        expires_at=row.expires_at,
        target_transaction_id=row.target_transaction_id,
        confirmed_txn_id=row.confirmed_txn_id,
    )


class PgFinanceSession:
    def __init__(self, conn: "AsyncConnection", ctx: WorkspaceContext, channel: str) -> None:
        self._conn = conn
        self.ctx = ctx
        self._channel = channel

    @property
    def _ids(self) -> dict[str, Any]:
        return {"ws": self.ctx.workspace_id, "user": self.ctx.user_id}

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[Any]:
        result = await self._conn.execute(_text(sql), {**self._ids, **(params or {})})
        return list(result.all())

    async def _exec(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        await self._conn.execute(_text(sql), {**self._ids, **(params or {})})

    async def _one(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        result = await self._conn.execute(_text(sql), {**self._ids, **(params or {})})
        return result.one_or_none()

    async def _unique_or_none(self, sql: str, params: Mapping[str, Any]) -> Any:
        """Run in a savepoint; a unique violation yields None instead of aborting."""
        from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

        try:
            async with self._conn.begin_nested():
                return await self._one(sql, params)
        except IntegrityError as exc:
            if _sqlstate(exc) == _UNIQUE_VIOLATION:
                return None
            raise

    # ------------------------------------------------------------------- accounts

    async def list_accounts(self, *, include_archived: bool = False) -> list[Account]:
        rows = await self._rows(
            "SELECT id, name, type, opening_balance, opening_date, "
            "(archived_at IS NOT NULL) AS archived FROM public.accounts "
            "WHERE workspace_id = :ws AND (:all OR archived_at IS NULL) ORDER BY created_at, id",
            {"all": include_archived},
        )
        return [_account(r) for r in rows]

    async def get_account(self, account_id: UUID) -> Account | None:
        row = await self._one(
            "SELECT id, name, type, opening_balance, opening_date, "
            "(archived_at IS NOT NULL) AS archived FROM public.accounts "
            "WHERE workspace_id = :ws AND id = :id",
            {"id": account_id},
        )
        return None if row is None else _account(row)

    async def insert_account(
        self, name: str, account_type: str, opening_date: date
    ) -> Account | None:
        row = await self._one(
            "INSERT INTO public.accounts (workspace_id, name, type, opening_date, created_by) "
            "VALUES (:ws, :name, :type, :opening, :user) ON CONFLICT DO NOTHING "
            "RETURNING id, name, type, opening_balance, opening_date, false AS archived",
            {"name": name, "type": account_type, "opening": opening_date},
        )
        return None if row is None else _account(row)

    async def set_default_account(self, account_id: UUID) -> None:
        await self._exec(
            "UPDATE public.workspace_memberships SET default_account_id = :account "
            "WHERE workspace_id = :ws AND user_id = :user",
            {"account": account_id},
        )
        self.ctx = replace(self.ctx, default_account_id=account_id)

    # ----------------------------------------------------------------- categories

    async def count_categories(self) -> int:
        row = await self._one(
            "SELECT count(*) AS n FROM public.categories WHERE workspace_id = :ws"
        )
        return int(row.n)

    async def list_categories(
        self, kind: TxnType | None = None, *, include_archived: bool = False, limit: int = 100
    ) -> list[Category]:
        rows = await self._rows(
            f"SELECT {_CATEGORY_COLUMNS} FROM public.categories "  # noqa: S608 - constant
            "WHERE workspace_id = :ws AND (CAST(:kind AS text) IS NULL OR kind = :kind) "
            "AND (:all OR archived_at IS NULL) "
            "ORDER BY (kind = 'expense'), (archived_at IS NOT NULL), is_default, "
            "pg_catalog.lower(name), id LIMIT :limit",
            {"kind": kind.value if kind else None, "all": include_archived, "limit": limit},
        )
        return [_category(r) for r in rows]

    async def get_category(self, category_id: UUID, *, for_update: bool = False) -> Category | None:
        lock = " FOR UPDATE" if for_update else ""
        row = await self._one(
            f"SELECT {_CATEGORY_COLUMNS} FROM public.categories "  # noqa: S608 - constants
            f"WHERE workspace_id = :ws AND id = :id{lock}",
            {"id": category_id},
        )
        return None if row is None else _category(row)

    async def find_active_category(self, kind: TxnType, name: str) -> Category | None:
        row = await self._one(
            f"SELECT {_CATEGORY_COLUMNS} FROM public.categories "  # noqa: S608 - constant
            "WHERE workspace_id = :ws AND kind = :kind AND archived_at IS NULL "
            "AND pg_catalog.lower(name) = pg_catalog.lower(:name)",
            {"kind": kind.value, "name": name},
        )
        return None if row is None else _category(row)

    async def insert_category(
        self, kind: TxnType, name: str, *, is_default: bool = False
    ) -> Category | None:
        row = await self._one(
            "INSERT INTO public.categories (workspace_id, kind, name, is_default, created_by) "  # noqa: S608
            "VALUES (:ws, :kind, :name, :is_default, :user) ON CONFLICT DO NOTHING "
            f"RETURNING {_CATEGORY_COLUMNS}",
            {"kind": kind.value, "name": name, "is_default": is_default},
        )
        return None if row is None else _category(row)

    async def rename_category(self, category_id: UUID, name: str) -> Category | None:
        row = await self._unique_or_none(
            "UPDATE public.categories SET name = :name, updated_at = now(), "  # noqa: S608
            "version = version + 1 WHERE workspace_id = :ws AND id = :id "
            f"RETURNING {_CATEGORY_COLUMNS}",
            {"id": category_id, "name": name},
        )
        return None if row is None else _category(row)

    async def set_category_archived(self, category_id: UUID, archived: bool) -> Category | None:
        row = await self._unique_or_none(
            "UPDATE public.categories SET archived_at = CASE WHEN :archived THEN now() END, "  # noqa: S608
            "updated_at = now(), version = version + 1 WHERE workspace_id = :ws AND id = :id "
            f"RETURNING {_CATEGORY_COLUMNS}",
            {"id": category_id, "archived": archived},
        )
        return None if row is None else _category(row)

    # --------------------------------------------------------------- transactions

    async def insert_transaction(
        self,
        *,
        txn_type: TxnType,
        amount: int,
        txn_date: date,
        category_id: UUID | None,
        description: str | None,
        source: TxnSource,
    ) -> UUID:
        row = await self._one(
            "INSERT INTO public.transactions (workspace_id, type, amount, currency, txn_date, "
            "category_id, description, source, created_by) "
            "VALUES (:ws, :type, :amount, :currency, :date, :category, :description, :source, "
            ":user) RETURNING id",
            {
                "type": txn_type.value,
                "amount": amount,
                "currency": self.ctx.currency,
                "date": txn_date,
                "category": category_id,
                "description": description,
                "source": source.value,
            },
        )
        txn_id: UUID = row.id
        return txn_id

    async def insert_entries(
        self, transaction_id: UUID, txn_type: TxnType, revision: int, entries: tuple[EntrySpec, ...]
    ) -> None:
        for entry in entries:
            await self._exec(
                "INSERT INTO public.transaction_entries (workspace_id, transaction_id, "
                "transaction_type, revision, leg, account_id, amount, created_by) "
                "VALUES (:ws, :txn, :type, :revision, :leg, :account, :amount, :user)",
                {
                    "txn": transaction_id,
                    "type": txn_type.value,
                    "revision": revision,
                    "leg": entry.leg.value,
                    "account": entry.account_id,
                    "amount": entry.amount,
                },
            )

    async def get_transaction(
        self, transaction_id: UUID, *, for_update: bool = False
    ) -> TransactionRecord | None:
        lock = " FOR UPDATE OF t" if for_update else ""
        row = await self._one(
            f"{_TXN_SELECT} WHERE t.workspace_id = :ws AND t.id = :id{lock}",
            {"id": transaction_id},
        )
        return None if row is None else _transaction(row)

    async def history(
        self, history_filter: HistoryFilter, *, limit: int, offset: int
    ) -> list[TransactionRecord]:
        rows = await self._rows(
            f"{_TXN_SELECT} WHERE t.workspace_id = :ws AND t.status = :status "
            "AND (CAST(:type AS text) IS NULL OR t.type = :type) "
            "AND (CAST(:from AS date) IS NULL OR t.txn_date >= :from) "
            "AND (CAST(:to AS date) IS NULL OR t.txn_date <= :to) "
            "AND (CAST(:category AS uuid) IS NULL OR t.category_id = :category) "
            "ORDER BY t.txn_date DESC, t.id DESC LIMIT :limit OFFSET :offset",
            {
                "status": (TxnStatus.VOIDED if history_filter.voided else TxnStatus.POSTED).value,
                "type": history_filter.txn_type.value if history_filter.txn_type else None,
                "from": history_filter.date_from,
                "to": history_filter.date_to,
                "category": history_filter.category_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return [_transaction(r) for r in rows]

    async def update_transaction(
        self, transaction_id: UUID, expected_version: int, values: Mapping[str, Any]
    ) -> bool:
        unknown = set(values) - _UPDATABLE
        if unknown or not values:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        assignments = ", ".join(f"{column} = :v_{column}" for column in sorted(values))
        row = await self._one(
            f"UPDATE public.transactions SET {assignments}, "  # noqa: S608 - allow-listed columns
            "version = version + 1, updated_by = :user, updated_at = now() "
            "WHERE workspace_id = :ws AND id = :id AND version = :version RETURNING version",
            {
                "id": transaction_id,
                "version": expected_version,
                **{f"v_{column}": value for column, value in values.items()},
            },
        )
        return row is not None

    async def insert_revision(
        self,
        transaction_id: UUID,
        version: int,
        change_kind: str,
        snapshot: Mapping[str, Any],
        changed_fields: list[str],
    ) -> None:
        await self._exec(
            "INSERT INTO public.transaction_revisions (workspace_id, transaction_id, version, "
            "change_kind, snapshot, changed_fields, changed_by) VALUES (:ws, :txn, :version, "
            ":kind, CAST(:snapshot AS jsonb), CAST(:fields AS text[]), :user)",
            {
                "txn": transaction_id,
                "version": version,
                "kind": change_kind,
                "snapshot": json.dumps(dict(snapshot), sort_keys=True),
                "fields": changed_fields,
            },
        )

    # --------------------------------------------------------------------- drafts

    async def lock_conversation(self) -> None:
        # Transaction-scoped, one key per (user, workspace): two concurrent "start" requests
        # of one user queue instead of colliding on uq_drafts_one_pending.
        await self._exec(
            "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 0))",
            {"key": f"finbot.conversation:{self.ctx.user_id}:{self.ctx.workspace_id}"},
        )

    async def pending_draft(self, *, for_update: bool = False) -> DraftRecord | None:
        lock = " FOR UPDATE" if for_update else ""
        row = await self._one(
            f"SELECT {_DRAFT_COLUMNS} FROM public.drafts "  # noqa: S608 - constants
            f"WHERE workspace_id = :ws AND user_id = :user AND status = 'pending'{lock}"
        )
        return None if row is None else _draft(row)

    async def get_draft(self, draft_id: UUID, *, for_update: bool = False) -> DraftRecord | None:
        lock = " FOR UPDATE" if for_update else ""
        row = await self._one(
            f"SELECT {_DRAFT_COLUMNS} FROM public.drafts "  # noqa: S608 - constants
            f"WHERE workspace_id = :ws AND user_id = :user AND id = :id{lock}",
            {"id": draft_id},
        )
        return None if row is None else _draft(row)

    async def find_draft_by_ref(self, ref: str) -> DraftRecord | None:
        row = await self._one(
            f"SELECT {_DRAFT_COLUMNS} FROM public.drafts "  # noqa: S608 - constants
            "WHERE workspace_id = :ws AND user_id = :user "
            "AND pg_catalog.right(pg_catalog.replace(id::text, '-', ''), 8) = :ref "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            {"ref": ref},
        )
        return None if row is None else _draft(row)

    async def cancel_pending_drafts(self) -> int:
        rows = await self._rows(
            "UPDATE public.drafts SET status = 'cancelled', updated_at = now() "
            "WHERE workspace_id = :ws AND user_id = :user AND status = 'pending' RETURNING id"
        )
        return len(rows)

    async def insert_draft(
        self,
        purpose: DraftPurpose,
        proposal: Mapping[str, Any],
        source: TxnSource,
        expires_at: datetime,
        *,
        target_transaction_id: UUID | None = None,
        telegram_chat_id: int | None = None,
    ) -> UUID:
        row = await self._one(
            "INSERT INTO public.drafts (workspace_id, user_id, purpose, target_transaction_id, "
            "proposal, source, telegram_chat_id, expires_at) VALUES (:ws, :user, :purpose, "
            ":target, CAST(:proposal AS jsonb), :source, :chat, :expires) RETURNING id",
            {
                "purpose": purpose.value,
                "target": target_transaction_id,
                "proposal": json.dumps(dict(proposal)),
                "source": source.value,
                "chat": telegram_chat_id,
                "expires": expires_at,
            },
        )
        draft_id: UUID = row.id
        return draft_id

    async def update_draft(self, draft_id: UUID, proposal: Mapping[str, Any]) -> None:
        await self._exec(
            "UPDATE public.drafts SET proposal = CAST(:proposal AS jsonb), updated_at = now() "
            "WHERE workspace_id = :ws AND user_id = :user AND id = :id",
            {"id": draft_id, "proposal": json.dumps(dict(proposal))},
        )

    async def set_draft_status(
        self, draft_id: UUID, status: DraftStatus, confirmed_txn_id: UUID | None = None
    ) -> None:
        await self._exec(
            "UPDATE public.drafts SET status = :status, confirmed_txn_id = :txn, "
            "updated_at = now() WHERE workspace_id = :ws AND user_id = :user AND id = :id",
            {"id": draft_id, "status": status.value, "txn": confirmed_txn_id},
        )

    # ------------------------------------------------------------ pending actions

    async def pending_action(self, *, for_update: bool = False) -> PendingAction | None:
        lock = " FOR UPDATE" if for_update else ""
        row = await self._one(
            "SELECT id, kind, payload, expires_at FROM public.pending_actions "  # noqa: S608
            f"WHERE workspace_id = :ws AND user_id = :user AND status = 'pending'{lock}"
        )
        if row is None:
            return None
        return PendingAction(row.id, PendingKind(row.kind), row.expires_at, dict(row.payload))

    async def cancel_pending_actions(self) -> int:
        rows = await self._rows(
            "UPDATE public.pending_actions SET status = 'cancelled' "
            "WHERE workspace_id = :ws AND user_id = :user AND status = 'pending' RETURNING id"
        )
        return len(rows)

    async def insert_pending_action(
        self, kind: PendingKind, payload: Mapping[str, Any], expires_at: datetime
    ) -> UUID:
        row = await self._one(
            "INSERT INTO public.pending_actions (workspace_id, user_id, kind, payload, expires_at) "
            "VALUES (:ws, :user, :kind, CAST(:payload AS jsonb), :expires) RETURNING id",
            {"kind": kind.value, "payload": json.dumps(dict(payload)), "expires": expires_at},
        )
        action_id: UUID = row.id
        return action_id

    async def set_pending_action_status(self, action_id: UUID, status: str) -> None:
        await self._exec(
            "UPDATE public.pending_actions SET status = :status, "
            "completed_at = CASE WHEN :status = 'completed' THEN now() END "
            "WHERE workspace_id = :ws AND user_id = :user AND id = :id",
            {"id": action_id, "status": status},
        )

    # -------------------------------------------------------------------- reports

    async def cash_flow(
        self, date_from: date | None, date_to: date | None
    ) -> list[tuple[str, TxnType, int]]:
        rows = await self._rows(
            "SELECT t.currency, t.type, sum(t.amount) AS total FROM public.transactions t "
            "WHERE t.workspace_id = :ws AND t.status = 'posted' "
            "AND t.type IN ('income', 'expense') "
            "AND (CAST(:from AS date) IS NULL OR t.txn_date >= :from) "
            "AND (CAST(:to AS date) IS NULL OR t.txn_date <= :to) "
            "GROUP BY t.currency, t.type",
            {"from": date_from, "to": date_to},
        )
        return [(r.currency, TxnType(r.type), int(r.total)) for r in rows]

    async def category_totals(self, date_from: date, date_to: date) -> list[CategoryTotal]:
        rows = await self._rows(
            "SELECT t.category_id, c.name, t.type, t.currency, sum(t.amount) AS total, "
            "count(*) AS n FROM public.transactions t "
            "LEFT JOIN public.categories c ON c.workspace_id = t.workspace_id "
            "AND c.id = t.category_id "
            "WHERE t.workspace_id = :ws AND t.status = 'posted' "
            "AND t.type IN ('income', 'expense') AND t.txn_date BETWEEN :from AND :to "
            "GROUP BY t.category_id, c.name, t.type, t.currency "
            "ORDER BY sum(t.amount) DESC, c.name, t.category_id",
            {"from": date_from, "to": date_to},
        )
        return [
            CategoryTotal(r.category_id, r.name or "-", TxnType(r.type), r.currency,
                          int(r.total), int(r.n))
            for r in rows
        ]  # fmt: skip

    async def account_balances(self, as_of: date) -> list[AccountBalance]:
        rows = await self._rows(
            "SELECT a.id, a.name, a.opening_balance, ce.currency, "
            "coalesce(sum(ce.amount), 0) AS moved "
            "FROM public.accounts a LEFT JOIN public.current_entries ce "
            "ON ce.workspace_id = a.workspace_id AND ce.account_id = a.id "
            "AND ce.status = 'posted' AND ce.txn_date BETWEEN a.opening_date AND :as_of "
            "WHERE a.workspace_id = :ws "
            "GROUP BY a.id, a.name, a.opening_balance, ce.currency "
            "ORDER BY min(a.created_at), a.id, ce.currency",
            {"as_of": as_of},
        )
        # Opening balances are in the workspace currency; an account without entries has no
        # entry currency (NULL) and is reported in the workspace currency. Currencies never mix.
        balances: dict[tuple[UUID, str], AccountBalance] = {}
        for r in rows:
            currency = r.currency or self.ctx.currency
            previous = balances.get((r.id, currency))
            moved = int(r.moved) + (previous.balance if previous else 0)
            balances[(r.id, currency)] = AccountBalance(r.id, r.name, currency, moved)
        opening = {r.id: int(r.opening_balance) for r in rows}
        result = []
        for (account_id, currency), balance in balances.items():
            extra = opening[account_id] if currency == self.ctx.currency else 0
            result.append(replace(balance, balance=balance.balance + extra))
        return result

    # ---------------------------------------------------------------------- audit

    async def audit(
        self,
        action: str,
        entity_type: str | None,
        entity_id: UUID | None,
        metadata: Mapping[str, Any] | None = None,
        *,
        actor_kind: str = "user",
    ) -> None:
        await self._exec(
            "INSERT INTO public.audit_events (workspace_id, actor_user_id, actor_kind, channel, "
            "action, entity_type, entity_id, metadata) VALUES (:ws, :actor, :kind, :channel, "
            ":action, :entity_type, :entity_id, CAST(:metadata AS jsonb))",
            {
                "actor": None if actor_kind == "system" else self.ctx.user_id,
                "kind": actor_kind,
                "channel": self._channel,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "metadata": json.dumps(dict(metadata or {}), sort_keys=True),
            },
        )


async def _set(conn: "AsyncConnection", name: str, value: UUID) -> None:
    await conn.execute(
        _text("SELECT set_config(:name, :value, true)"), {"name": name, "value": str(value)}
    )


class PgFinanceStore:
    def __init__(self, engine: "AsyncEngine", *, channel: str = "telegram") -> None:
        self._engine = engine
        self._channel = channel

    @asynccontextmanager
    async def session(
        self, user_id: UUID, workspace_id: UUID | None = None, *, write: bool = False
    ) -> AsyncIterator[PgFinanceSession]:
        async with self._engine.begin() as conn:
            await _set(conn, "app.user_id", user_id)
            user = (
                await conn.execute(
                    _text("SELECT status FROM public.users WHERE id = :user"), {"user": user_id}
                )
            ).one_or_none()
            if user is None:
                raise NotFoundError("user")
            if workspace_id is None:  # the active workspace: personal until /ruang (FR-WSP-03)
                workspace_id = (
                    await conn.execute(
                        _text(
                            "SELECT id FROM public.workspaces "
                            "WHERE kind = 'personal' AND personal_owner_id = :user"
                        ),
                        {"user": user_id},
                    )
                ).scalar_one_or_none()
                if workspace_id is None:
                    raise NotFoundError("workspace")
            lock = " FOR SHARE OF m" if write else ""
            row = (
                await conn.execute(
                    _text(
                        "SELECT m.role, m.default_account_id, w.kind, w.name, w.status, "  # noqa: S608
                        "w.timezone, w.currency FROM public.workspace_memberships m "
                        "JOIN public.workspaces w ON w.id = m.workspace_id "
                        "WHERE m.workspace_id = :ws AND m.user_id = :user "
                        f"AND m.status = 'active'{lock}"
                    ),
                    {"ws": workspace_id, "user": user_id},
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError("workspace")
            await _set(conn, "app.workspace_id", workspace_id)
            ctx = WorkspaceContext(
                user_id=user_id,
                user_status=UserStatus(user.status),
                workspace_id=workspace_id,
                workspace_kind=row.kind,
                workspace_name=row.name,
                workspace_status=row.status,
                timezone=row.timezone,
                currency=row.currency,
                role=row.role,
                default_account_id=row.default_account_id,
            )
            yield PgFinanceSession(conn, ctx, self._channel)

    async def record_denial(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID,
        permission: str,
        role: str | None,
        entity_type: str | None,
        channel: str,
    ) -> None:
        async with self._engine.begin() as conn:
            await _set(conn, "app.user_id", user_id)
            await _set(conn, "app.workspace_id", workspace_id)
            await conn.execute(
                _text(
                    "INSERT INTO public.audit_events (workspace_id, actor_user_id, actor_kind, "
                    "channel, action, entity_type, outcome, metadata) VALUES (:ws, :user, "
                    "'user', :channel, 'security.authz_denied', :entity_type, 'denied', "
                    "CAST(:metadata AS jsonb))"
                ),
                {
                    "ws": workspace_id,
                    "user": user_id,
                    "channel": channel,
                    "entity_type": entity_type,
                    "metadata": json.dumps({"permission": permission, "role": role}),
                },
            )
