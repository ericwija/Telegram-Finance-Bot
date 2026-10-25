"""An in-memory ``FinanceStore`` for fast service and adapter unit tests (no database).

It implements the port's semantics that the services rely on: one session per transaction with
rollback on exception (a deep copy restored), the active-membership check, the membership
version, per-workspace filtering, per-user drafts, unique active names, optimistic versions and
append-only rows. Locks are no-ops (a single event loop interleaves nothing inside a session),
so it does **not** replace the database tests: RLS, grants, triggers, locking, isolation levels
and concurrency are proven only against real PostgreSQL (``tests/integration``), as CLAUDE.md
requires for tenant data. ``sessions`` records the mode of every session opened.
"""

import copy
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

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
    draft_ref,
)
from finbot.domain.identity import UserStatus
from finbot.domain.ledger import EntrySpec, TxnSource, TxnStatus, TxnType
from finbot.domain.money import Rupiah
from finbot.ports.finance import RowLock, SessionMode


@dataclass
class Workspace:
    id: uuid.UUID
    kind: str
    name: str
    status: str = "active"
    timezone: str = "Asia/Jakarta"
    currency: str = "IDR"
    #: user -> {"role", "default", "status", "version"}
    members: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)

    def set_member(
        self, user: uuid.UUID, *, role: str | None = None, status: str | None = None
    ) -> None:
        """A role or status change; bumps the version like trg_membership_version (0005)."""
        member = self.members[user]
        member["role"] = role or member["role"]
        member["status"] = status or member["status"]
        member["version"] += 1


@dataclass
class State:
    users: dict[uuid.UUID, UserStatus] = field(default_factory=dict)
    workspaces: dict[uuid.UUID, Workspace] = field(default_factory=dict)
    accounts: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    categories: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    transactions: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    entries: list[dict[str, Any]] = field(default_factory=list)
    revisions: list[dict[str, Any]] = field(default_factory=list)
    drafts: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    actions: dict[uuid.UUID, dict[str, Any]] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0


def _new_id(state: State) -> uuid.UUID:
    state.seq += 1
    return uuid.UUID(int=(state.seq << 64) | uuid.uuid4().int >> 64)


def _member(role: str) -> dict[str, Any]:
    return {"role": role, "default": None, "status": "active", "version": 1}


class FakeSession:
    def __init__(self, state: State, ctx: WorkspaceContext, request_id: uuid.UUID | None) -> None:
        self.st = state
        self.ctx = ctx
        self.request_id = request_id

    @property
    def _ws(self) -> uuid.UUID:
        return self.ctx.workspace_id

    def _mine(self, rows: Mapping[uuid.UUID, dict[str, Any]]) -> list[dict[str, Any]]:
        return [r for r in rows.values() if r["workspace_id"] == self._ws]

    # accounts
    async def list_accounts(self, *, include_archived: bool = False) -> list[Account]:
        rows = [a for a in self._mine(self.st.accounts) if include_archived or not a["archived"]]
        return [self._account(a) for a in rows]

    @staticmethod
    def _account(a: dict[str, Any]) -> Account:
        return Account(
            a["id"], a["name"], a["type"], a["opening"], a["opening_date"], a["archived"]
        )

    async def get_account(
        self, account_id: uuid.UUID, *, lock: RowLock | None = None
    ) -> Account | None:
        a = self.st.accounts.get(account_id)
        return self._account(a) if a and a["workspace_id"] == self._ws else None

    async def insert_account(
        self, name: str, account_type: str, opening_date: date
    ) -> Account | None:
        if any(
            a["name"].lower() == name.lower() and not a["archived"]
            for a in self._mine(self.st.accounts)
        ):
            return None
        a = {
            "id": _new_id(self.st),
            "workspace_id": self._ws,
            "name": name,
            "type": account_type,
            "opening": 0,
            "opening_date": opening_date,
            "archived": False,
        }
        self.st.accounts[a["id"]] = a
        return self._account(a)

    async def set_default_account(self, account_id: uuid.UUID) -> None:
        self.st.workspaces[self._ws].members[self.ctx.user_id]["default"] = account_id
        self.ctx = replace(self.ctx, default_account_id=account_id)

    # categories
    @staticmethod
    def _category(c: dict[str, Any]) -> Category:
        return Category(c["id"], c["kind"], c["name"], c["is_default"], c["archived"])

    def _active_named(self, kind: TxnType, name: str) -> dict[str, Any] | None:
        return next(
            (
                c
                for c in self._mine(self.st.categories)
                if c["kind"] is kind and not c["archived"] and c["name"].lower() == name.lower()
            ),
            None,
        )

    async def lock_category_names(self) -> None:
        return None

    async def count_categories(self, kind: TxnType | None = None) -> int:
        return sum(kind is None or c["kind"] is kind for c in self._mine(self.st.categories))

    async def list_categories(
        self,
        kind: TxnType | None = None,
        *,
        archived: bool | None = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Category]:
        rows = [
            c
            for c in self._mine(self.st.categories)
            if (kind is None or c["kind"] is kind)
            and (archived is None or c["archived"] is archived)
        ]
        rows.sort(
            key=lambda c: (
                c["kind"] is TxnType.EXPENSE,
                c["archived"],
                c["is_default"],
                c["name"].lower(),
            )
        )
        return [self._category(c) for c in rows[offset : offset + limit]]

    async def get_category(
        self, category_id: uuid.UUID, *, lock: RowLock | None = None
    ) -> Category | None:
        c = self.st.categories.get(category_id)
        return self._category(c) if c and c["workspace_id"] == self._ws else None

    async def find_active_category(self, kind: TxnType, name: str) -> Category | None:
        c = self._active_named(kind, name)
        return self._category(c) if c else None

    async def insert_category(
        self, kind: TxnType, name: str, *, is_default: bool = False
    ) -> Category | None:
        if self._active_named(kind, name) or (
            is_default
            and any(c["is_default"] and c["kind"] is kind for c in self._mine(self.st.categories))
        ):
            return None
        c = {
            "id": _new_id(self.st),
            "workspace_id": self._ws,
            "kind": kind,
            "name": name,
            "is_default": is_default,
            "archived": False,
        }
        self.st.categories[c["id"]] = c
        return self._category(c)

    async def rename_category(self, category_id: uuid.UUID, name: str) -> Category | None:
        c = self.st.categories[category_id]
        other = self._active_named(c["kind"], name)
        if other is not None and other["id"] != category_id and not c["archived"]:
            return None
        c["name"] = name
        return self._category(c)

    async def set_category_archived(
        self, category_id: uuid.UUID, archived: bool
    ) -> Category | None:
        c = self.st.categories[category_id]
        if not archived and self._active_named(c["kind"], c["name"]):
            return None
        c["archived"] = archived
        return self._category(c)

    # transactions
    async def insert_transaction(
        self,
        *,
        txn_type: TxnType,
        amount: int,
        txn_date: date,
        category_id: uuid.UUID | None,
        description: str | None,
        source: TxnSource,
    ) -> uuid.UUID:
        t = {
            "id": _new_id(self.st),
            "workspace_id": self._ws,
            "type": txn_type,
            "amount": amount,
            "currency": self.ctx.currency,
            "txn_date": txn_date,
            "category_id": category_id,
            "description": description,
            "source": source,
            "status": TxnStatus.POSTED,
            "version": 1,
            "entry_revision": 1,
            "created_by": self.ctx.user_id,
            "created_at": datetime.now(UTC),
            "updated_at": None,
            "voided_at": None,
        }
        self.st.transactions[t["id"]] = t
        return t["id"]

    async def insert_entries(
        self,
        transaction_id: uuid.UUID,
        txn_type: TxnType,
        revision: int,
        entries: tuple[EntrySpec, ...],
    ) -> None:
        for e in entries:
            self.st.entries.append(
                {
                    "workspace_id": self._ws,
                    "txn": transaction_id,
                    "revision": revision,
                    "leg": e.leg,
                    "account": e.account_id,
                    "amount": e.amount,
                }
            )

    def _record(self, t: dict[str, Any]) -> TransactionRecord:
        c = self.st.categories.get(t["category_id"]) if t["category_id"] else None
        entry = next(
            (
                e
                for e in self.st.entries
                if e["txn"] == t["id"] and e["revision"] == t["entry_revision"]
            ),
            None,
        )
        account = self.st.accounts.get(entry["account"]) if entry else None
        return TransactionRecord(
            t["id"],
            t["type"],
            Rupiah(t["amount"]),
            t["currency"],
            t["txn_date"],
            t["category_id"],
            c["name"] if c else None,
            bool(c and c["archived"]),
            t["description"],
            t["source"],
            t["status"],
            t["version"],
            t["entry_revision"],
            entry["account"] if entry else None,
            account["name"] if account else None,
            t["created_by"],
            t["created_at"],
            t["updated_at"],
            t["voided_at"],
        )

    async def get_transaction(
        self, transaction_id: uuid.UUID, *, for_update: bool = False
    ) -> TransactionRecord | None:
        t = self.st.transactions.get(transaction_id)
        return self._record(t) if t and t["workspace_id"] == self._ws else None

    async def history(
        self, history_filter: HistoryFilter, *, limit: int, offset: int
    ) -> list[TransactionRecord]:
        f = history_filter
        status = TxnStatus.VOIDED if f.voided else TxnStatus.POSTED
        rows = [
            t
            for t in self._mine(self.st.transactions)
            if t["status"] is status
            and (f.txn_type is None or t["type"] is f.txn_type)
            and (f.date_from is None or t["txn_date"] >= f.date_from)
            and (f.date_to is None or t["txn_date"] <= f.date_to)
            and (f.category_id is None or t["category_id"] == f.category_id)
        ]
        rows.sort(key=lambda t: (t["txn_date"], t["id"]), reverse=True)
        return [self._record(t) for t in rows[offset : offset + limit]]

    async def update_transaction(
        self, transaction_id: uuid.UUID, expected_version: int, values: Mapping[str, Any]
    ) -> bool:
        t = self.st.transactions[transaction_id]
        if t["version"] != expected_version:
            return False
        for key, value in values.items():
            t[key] = TxnStatus(value) if key == "status" else value
        t["version"] += 1
        t["updated_at"] = datetime.now(UTC)
        return True

    async def insert_revision(
        self,
        transaction_id: uuid.UUID,
        version: int,
        change_kind: str,
        snapshot: Mapping[str, Any],
        changed_fields: list[str],
    ) -> None:
        self.st.revisions.append(
            {
                "txn": transaction_id,
                "version": version,
                "kind": change_kind,
                "snapshot": dict(snapshot),
                "fields": changed_fields,
            }
        )

    # drafts
    async def lock_conversation(self) -> None:
        return None  # single-threaded event loop: nothing interleaves inside a session

    def _draft(self, d: dict[str, Any]) -> DraftRecord:
        return DraftRecord(
            d["id"],
            d["purpose"],
            d["status"],
            copy.deepcopy(d["proposal"]),
            d["expires_at"],
            d["target"],
            d["confirmed"],
        )

    def _my_drafts(self) -> list[dict[str, Any]]:
        return [d for d in self._mine(self.st.drafts) if d["user"] == self.ctx.user_id]

    async def pending_draft(self, *, for_update: bool = False) -> DraftRecord | None:
        d = next((d for d in self._my_drafts() if d["status"] is DraftStatus.PENDING), None)
        return self._draft(d) if d else None

    async def get_draft(
        self, draft_id: uuid.UUID, *, for_update: bool = False
    ) -> DraftRecord | None:
        d = next((d for d in self._my_drafts() if d["id"] == draft_id), None)
        return self._draft(d) if d else None

    async def find_draft_by_ref(self, ref: str) -> DraftRecord | None:
        found = [d for d in self._my_drafts() if draft_ref(d["id"]) == ref]
        return self._draft(found[-1]) if found else None

    async def cancel_pending_drafts(self) -> int:
        pending = [d for d in self._my_drafts() if d["status"] is DraftStatus.PENDING]
        for d in pending:
            d["status"] = DraftStatus.CANCELLED
        return len(pending)

    async def insert_draft(
        self,
        purpose: DraftPurpose,
        proposal: Mapping[str, Any],
        source: TxnSource,
        expires_at: datetime,
        *,
        target_transaction_id: uuid.UUID | None = None,
        telegram_chat_id: int | None = None,
    ) -> uuid.UUID:
        d = {
            "id": _new_id(self.st),
            "workspace_id": self._ws,
            "user": self.ctx.user_id,
            "purpose": purpose,
            "status": DraftStatus.PENDING,
            "proposal": dict(proposal),
            "expires_at": expires_at,
            "target": target_transaction_id,
            "confirmed": None,
        }
        self.st.drafts[d["id"]] = d
        return d["id"]

    async def update_draft(self, draft_id: uuid.UUID, proposal: Mapping[str, Any]) -> None:
        self.st.drafts[draft_id]["proposal"] = dict(proposal)

    async def set_draft_status(
        self, draft_id: uuid.UUID, status: DraftStatus, confirmed_txn_id: uuid.UUID | None = None
    ) -> None:
        self.st.drafts[draft_id] |= {"status": status, "confirmed": confirmed_txn_id}

    # pending actions
    def _my_actions(self) -> list[dict[str, Any]]:
        return [a for a in self._mine(self.st.actions) if a["user"] == self.ctx.user_id]

    async def pending_action(self, *, for_update: bool = False) -> PendingAction | None:
        a = next((a for a in self._my_actions() if a["status"] == "pending"), None)
        return PendingAction(a["id"], a["kind"], a["expires_at"], dict(a["payload"])) if a else None

    async def cancel_pending_actions(self) -> int:
        pending = [a for a in self._my_actions() if a["status"] == "pending"]
        for a in pending:
            a["status"] = "cancelled"
        return len(pending)

    async def insert_pending_action(
        self, kind: PendingKind, payload: Mapping[str, Any], expires_at: datetime
    ) -> uuid.UUID:
        a = {
            "id": _new_id(self.st),
            "workspace_id": self._ws,
            "user": self.ctx.user_id,
            "kind": kind,
            "payload": dict(payload),
            "status": "pending",
            "expires_at": expires_at,
        }
        self.st.actions[a["id"]] = a
        return a["id"]

    async def set_pending_action_status(self, action_id: uuid.UUID, status: str) -> None:
        self.st.actions[action_id]["status"] = status

    async def expire_draft_if_pending(self, draft_id: uuid.UUID) -> bool:
        draft = next((d for d in self._my_drafts() if d["id"] == draft_id), None)
        if draft is None or draft["status"] is not DraftStatus.PENDING:
            return False
        draft["status"] = DraftStatus.EXPIRED
        return True

    async def expire_action_if_pending(self, action_id: uuid.UUID) -> bool:
        action = next((a for a in self._my_actions() if a["id"] == action_id), None)
        if action is None or action["status"] != "pending":
            return False
        action["status"] = "expired"
        return True

    # reports
    def _posted(self) -> list[dict[str, Any]]:
        return [t for t in self._mine(self.st.transactions) if t["status"] is TxnStatus.POSTED]

    async def cash_flow(
        self, date_from: date | None, date_to: date | None
    ) -> list[tuple[str, TxnType, int]]:
        sums: dict[tuple[str, TxnType], int] = {}
        for t in self._posted():
            if (
                t["type"] is TxnType.TRANSFER
                or (date_from and t["txn_date"] < date_from)
                or (date_to and t["txn_date"] > date_to)
            ):
                continue
            sums[(t["currency"], t["type"])] = sums.get((t["currency"], t["type"]), 0) + t["amount"]
        return [(c, k, v) for (c, k), v in sums.items()]

    async def category_totals(self, date_from: date, date_to: date) -> list[CategoryTotal]:
        sums: dict[tuple[Any, ...], list[int]] = {}
        for t in self._posted():
            if t["type"] is TxnType.TRANSFER or not date_from <= t["txn_date"] <= date_to:
                continue
            c = self.st.categories.get(t["category_id"])
            key = (t["category_id"], c["name"] if c else "-", t["type"], t["currency"])
            sums.setdefault(key, [0, 0])
            sums[key][0] += t["amount"]
            sums[key][1] += 1
        totals = [CategoryTotal(k[0], k[1], k[2], k[3], v[0], v[1]) for k, v in sums.items()]
        return sorted(totals, key=lambda c: (-c.total, c.name))

    async def account_balances(self, as_of: date) -> list[AccountBalance]:
        result = []
        for a in self._mine(self.st.accounts):
            moved = sum(
                e["amount"]
                for e in self.st.entries
                if e["account"] == a["id"]
                and (t := self.st.transactions[e["txn"]])["status"] is TxnStatus.POSTED
                and e["revision"] == t["entry_revision"]
                and a["opening_date"] <= t["txn_date"] <= as_of
            )
            result.append(
                AccountBalance(a["id"], a["name"], self.ctx.currency, a["opening"] + moved)
            )
        return result

    async def audit(
        self,
        action: str,
        entity_type: str | None,
        entity_id: uuid.UUID | None,
        metadata: Mapping[str, Any] | None = None,
        *,
        actor_kind: str = "user",
    ) -> None:
        self.st.audit.append(
            {
                "request_id": self.request_id,
                "entity_type": entity_type,
                "workspace_id": self._ws,
                "action": action,
                "entity_id": entity_id,
                "metadata": dict(metadata or {}),
                "actor_kind": actor_kind,
            }
        )


class FakeFinanceStore:
    def __init__(self) -> None:
        self.state = State()
        self.denials: list[dict[str, Any]] = []
        self.sessions: list[SessionMode] = []

    def add_person(self) -> tuple[uuid.UUID, uuid.UUID]:
        user, ws = uuid.uuid4(), uuid.uuid4()
        self.state.users[user] = UserStatus.ACTIVE
        self.state.workspaces[ws] = Workspace(
            ws, "personal", "Pribadi", members={user: _member("owner")}
        )
        return user, ws

    def add_business(self, members: dict[uuid.UUID, str]) -> uuid.UUID:
        ws = uuid.uuid4()
        self.state.workspaces[ws] = Workspace(
            ws,
            "business",
            "Toko",
            members={u: _member(r) for u, r in members.items()},
        )
        return ws

    @asynccontextmanager
    async def session(
        self,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID | None = None,
        *,
        mode: SessionMode = SessionMode.READ,
        request_id: uuid.UUID | None = None,
    ) -> AsyncIterator[FakeSession]:
        self.sessions.append(mode)
        backup = copy.deepcopy(self.state)
        try:
            if user_id not in self.state.users:
                raise NotFoundError("user")
            if workspace_id is None:
                workspace_id = next(
                    (
                        w.id
                        for w in self.state.workspaces.values()
                        if w.kind == "personal" and user_id in w.members
                    ),
                    None,
                )
            ws = self.state.workspaces.get(workspace_id) if workspace_id else None
            if ws is None or ws.members.get(user_id, {}).get("status") != "active":
                raise NotFoundError("workspace")
            member = ws.members[user_id]
            ctx = WorkspaceContext(
                user_id,
                self.state.users[user_id],
                ws.id,
                ws.kind,
                ws.name,
                ws.status,
                ws.timezone,
                ws.currency,
                member["role"],
                member["default"],
                member["version"],
            )
            session = FakeSession(self.state, ctx, request_id)
            yield session
            if mode is SessionMode.READ and self.state.__dict__ != backup.__dict__:
                raise AssertionError("a READ session wrote")  # PostgreSQL: READ ONLY
        except BaseException:
            self.state.__dict__.update(backup.__dict__)  # rollback
            raise

    async def record_denial(
        self,
        *,
        user_id: uuid.UUID,
        workspace_id: uuid.UUID,
        permission: str,
        role: str | None,
        entity_type: str | None,
        channel: str,
        request_id: uuid.UUID | None = None,
    ) -> None:
        self.denials.append(
            {
                "user": user_id,
                "workspace": workspace_id,
                "permission": permission,
                "entity_type": entity_type,
                "request_id": request_id,
            }
        )
