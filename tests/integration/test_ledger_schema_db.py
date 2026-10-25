"""Revision 0004 enforced by PostgreSQL itself, with raw SQL that bypasses the services.

AT-ENT-* (D1 entry invariants and immutability), AT-ISO-* at the database level (composite FKs),
AT-RLS-* for the ledger tables (context, WITH CHECK, per-user drafts) and the column grants.
Runs only with FINBOT_TEST_PG_BACKEND set.
"""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from finbot.domain.ledger import TxnType
from tests.support.finance import Finance, Person, finance, owner_scalar, person, record
from tests.support.phase1 import as_app, scalar

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

CHECK, FK, UNIQUE, DENIED, RLS = "23514", "23503", "23505", "42501", "42501"


def sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(
        getattr(orig, "__cause__", None), "sqlstate", None
    )


class World:
    def __init__(self, f: Finance, budi: Person, ani: Person, ids: dict[str, Any]) -> None:
        self.f, self.budi, self.ani, self.ids = f, budi, ani, ids


@pytest_asyncio.fixture
async def world(migrated_database: str, owner_engine: Any, app_engine: Any) -> World:
    f = finance(app_engine)
    budi, ani = await person(owner_engine, "Budi"), await person(owner_engine, "Ani")
    saved = (await record(f, budi.actor(), TxnType.EXPENSE, "10rb", "Makanan")).record
    await f.ledger.balance(ani.actor())  # seeds Ani's workspace
    ids: dict[str, Any] = {"budi_txn": saved.id}
    for who, p in (("budi", budi), ("ani", ani)):
        ids[f"{who}_kas"] = await owner_scalar(
            owner_engine, "SELECT id FROM accounts WHERE workspace_id = :ws", {"ws": p.personal_ws}
        )
        for kind in ("income", "expense"):
            ids[f"{who}_{kind}"] = await owner_scalar(
                owner_engine,
                "SELECT id FROM categories WHERE workspace_id = :ws AND kind = :k AND is_default",
                {"ws": p.personal_ws, "k": kind},
            )
    return World(f, budi, ani, ids)


def _ctx(w: World, who: str = "budi") -> dict[str, str]:
    p = w.budi if who == "budi" else w.ani
    return {"workspace": str(p.personal_ws), "user": str(p.user_id)}


async def _expect(
    app_engine: Any, w: World, code: str, *statements: tuple[str, dict[str, Any]]
) -> None:
    """Run the statements in one app transaction (Budi's context); expect SQLSTATE ``code``."""

    async def run() -> None:
        async with as_app(app_engine, **_ctx(w)) as conn:
            for sql, params in statements:
                await conn.execute(text(sql), params)

    with pytest.raises(DBAPIError) as caught:
        await run()
    assert sqlstate(caught.value) == code, caught.value


def _p(w: World, **extra: Any) -> dict[str, Any]:
    return {"ws": str(w.budi.personal_ws), "u": str(w.budi.user_id), **extra}


_HEADER = (
    "INSERT INTO transactions (id, workspace_id, type, amount, txn_date, category_id, source, "
    "created_by) VALUES (:t, :ws, :type, :amount, '2026-10-01', :cat, 'manual', :u)"
)
_ENTRY = (
    "INSERT INTO transaction_entries (workspace_id, transaction_id, transaction_type, revision, "
    "leg, account_id, amount, created_by) VALUES (:ws, :t, :type, :rev, :leg, :acc, :amt, :u)"
)


# ---------------------------------------------------------------------------- AT-ENT


async def test_AT_ENT_04_05_unbalanced_or_one_legged_sets_never_commit(
    world: World, app_engine: Any, owner_engine: Any
) -> None:
    w = world
    async with owner_engine.begin() as conn:  # a second account for transfers
        bank = (
            await conn.execute(
                text(
                    "INSERT INTO accounts (workspace_id, name, type, opening_date, created_by) "
                    "VALUES (:ws, 'Bank', 'bank', '2000-01-01', :u) RETURNING id"
                ),
                _p(w),
            )
        ).scalar_one()
    kas = w.ids["budi_kas"]
    t = str(uuid.uuid4())
    transfer = _p(w, t=t, type="transfer", amount=500_000, cat=None)
    # AT-ENT-04: header + only the source leg.
    await _expect(
        app_engine,
        w,
        CHECK,
        (_HEADER, transfer),
        (_ENTRY, {**transfer, "rev": 1, "leg": "source", "acc": kas, "amt": -500_000}),
    )
    # AT-ENT-05: legs -500,000 / +499,999.
    await _expect(
        app_engine,
        w,
        CHECK,
        (_HEADER, transfer),
        (_ENTRY, {**transfer, "rev": 1, "leg": "source", "acc": kas, "amt": -500_000}),
        (_ENTRY, {**transfer, "rev": 1, "leg": "destination", "acc": bank, "amt": 499_999}),
    )
    # An expense header without any entry.
    expense = _p(w, t=t, type="expense", amount=1_000, cat=w.ids["budi_expense"])
    await _expect(app_engine, w, CHECK, (_HEADER, expense))
    # An entry amount that disagrees with the header (INV-4).
    await _expect(
        app_engine,
        w,
        CHECK,
        (_HEADER, expense),
        (_ENTRY, {**expense, "rev": 1, "leg": "single", "acc": kas, "amt": -999}),
    )
    # Entries beyond the header's entry_revision.
    await _expect(
        app_engine,
        w,
        CHECK,
        (_HEADER, expense),
        (_ENTRY, {**expense, "rev": 1, "leg": "single", "acc": kas, "amt": -1_000}),
        (_ENTRY, {**expense, "rev": 2, "leg": "single", "acc": kas, "amt": -1_000}),
    )
    # The balanced versions commit.
    async with as_app(app_engine, **_ctx(w)) as conn:
        await conn.execute(text(_HEADER), transfer)
        await conn.execute(
            text(_ENTRY), {**transfer, "rev": 1, "leg": "source", "acc": kas, "amt": -500_000}
        )
        await conn.execute(
            text(_ENTRY), {**transfer, "rev": 1, "leg": "destination", "acc": bank, "amt": 500_000}
        )
    assert (
        await owner_scalar(
            owner_engine,
            "SELECT sum(amount) FROM transaction_entries WHERE transaction_id = :t",
            {"t": t},
        )
        == 0
    )


async def test_AT_ENT_06_to_09_row_level_rules(world: World, app_engine: Any) -> None:
    w = world
    kas, t = w.ids["budi_kas"], str(uuid.uuid4())
    transfer = _p(w, t=t, type="transfer", amount=5, cat=None)
    # AT-ENT-06: both legs on the same account.
    await _expect(
        app_engine,
        w,
        UNIQUE,
        (_HEADER, transfer),
        (_ENTRY, {**transfer, "rev": 1, "leg": "source", "acc": kas, "amt": -5}),
        (_ENTRY, {**transfer, "rev": 1, "leg": "destination", "acc": kas, "amt": 5}),
    )
    expense = _p(w, t=t, type="expense", amount=5, cat=w.ids["budi_expense"])
    # AT-ENT-07: wrong signs and zero.
    for leg, amount in (("single", 5), ("single", 0), ("source", -5)):
        await _expect(
            app_engine,
            w,
            CHECK,
            (_HEADER, expense),
            (_ENTRY, {**expense, "rev": 1, "leg": leg, "acc": kas, "amt": amount}),
        )
    # AT-ENT-08: a second single entry in one revision.
    await _expect(
        app_engine,
        w,
        UNIQUE,
        (_HEADER, expense),
        (_ENTRY, {**expense, "rev": 1, "leg": "single", "acc": kas, "amt": -5}),
        (_ENTRY, {**expense, "rev": 1, "leg": "single", "acc": kas, "amt": -5}),
    )
    # AT-ENT-09: the entry's transaction_type differs from the header type.
    await _expect(
        app_engine,
        w,
        FK,
        (_HEADER, expense),
        (_ENTRY, {**expense, "type": "income", "rev": 1, "leg": "single", "acc": kas, "amt": 5}),
    )


async def test_AT_ENT_10_16_entries_revisions_and_type_are_immutable(
    world: World, app_engine: Any, owner_engine: Any
) -> None:
    w = world
    txn = {"t": str(w.ids["budi_txn"])}
    for sql in (
        "UPDATE transaction_entries SET amount = -1 WHERE transaction_id = :t",
        "DELETE FROM transaction_entries WHERE transaction_id = :t",
        "UPDATE transaction_revisions SET version = 9 WHERE transaction_id = :t",
        "DELETE FROM transaction_revisions WHERE transaction_id = :t",
        "UPDATE transactions SET type = 'income' WHERE id = :t",
        "UPDATE transactions SET created_by = :u WHERE id = :t",
        "UPDATE transactions SET workspace_id = :ws WHERE id = :t",
        "DELETE FROM transactions WHERE id = :t",
        "UPDATE categories SET kind = 'income' WHERE workspace_id = :ws",
        "DELETE FROM categories WHERE workspace_id = :ws",
        "UPDATE audit_events SET action = 'x' WHERE workspace_id = :ws",
    ):
        await _expect(app_engine, w, DENIED, (sql, _p(w, **txn)))
    # The type trigger also stops the owner (D21: void and replace instead).
    with pytest.raises(DBAPIError) as caught:
        async with owner_engine.begin() as conn:
            await conn.execute(text("UPDATE transactions SET type = 'income' WHERE id = :t"), txn)
    assert sqlstate(caught.value) == CHECK


async def test_AT_ENT_17_archived_account_and_category_take_no_new_rows(
    world: World, app_engine: Any, owner_engine: Any
) -> None:
    w = world
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("UPDATE accounts SET archived_at = now() WHERE id = :a"), {"a": w.ids["budi_kas"]}
        )
        archived_cat = (
            await conn.execute(
                text(
                    "INSERT INTO categories (workspace_id, kind, name, archived_at) "
                    "VALUES (:ws, 'expense', 'Lama', now()) RETURNING id"
                ),
                _p(w),
            )
        ).scalar_one()
    t = str(uuid.uuid4())
    expense = _p(w, t=t, type="expense", amount=5, cat=w.ids["budi_expense"])
    await _expect(
        app_engine,
        w,
        CHECK,
        (_HEADER, expense),
        (_ENTRY, {**expense, "rev": 1, "leg": "single", "acc": w.ids["budi_kas"], "amt": -5}),
    )
    await _expect(app_engine, w, CHECK, (_HEADER, {**expense, "cat": archived_cat}))
    # Existing entries on the archived account still count.
    balance = await w.f.ledger.balance(w.budi.actor())
    assert balance.report.accounts[0].balance == -10_000


# ---------------------------------------------------------------- AT-ISO, AT-RLS


async def test_AT_ISO_01_03_cross_workspace_references_fail_in_the_database(
    world: World, app_engine: Any
) -> None:
    w = world
    t = str(uuid.uuid4())
    # AT-ISO-03: Ani's category in Budi's transaction; then Budi's income category on an expense.
    await _expect(
        app_engine, w, FK, (_HEADER, _p(w, t=t, type="expense", amount=5, cat=w.ids["ani_expense"]))
    )
    await _expect(
        app_engine, w, FK, (_HEADER, _p(w, t=t, type="expense", amount=5, cat=w.ids["budi_income"]))
    )
    # AT-ISO-01: an entry on Ani's account.
    expense = _p(w, t=t, type="expense", amount=5, cat=w.ids["budi_expense"])
    await _expect(
        app_engine,
        w,
        FK,
        (_HEADER, expense),
        (_ENTRY, {**expense, "rev": 1, "leg": "single", "acc": w.ids["ani_kas"], "amt": -5}),
    )
    # AT-RLS-02: a row for Ani's workspace written from Budi's context.
    await _expect(
        app_engine,
        w,
        RLS,
        (
            "INSERT INTO categories (workspace_id, kind, name) VALUES (:other, 'expense', 'X')",
            _p(w, other=str(w.ani.personal_ws)),
        ),
    )


async def test_AT_RLS_ledger_rows_need_context_membership_and_ownership(
    world: World, app_engine: Any, owner_engine: Any
) -> None:
    w = world
    tables = (
        "accounts",
        "categories",
        "transactions",
        "transaction_entries",
        "transaction_revisions",
        "drafts",
        "pending_actions",
        "current_entries",
    )
    await w.f.capture.start(w.budi.actor(), TxnType.EXPENSE)  # a pending draft for Budi
    async with as_app(app_engine) as conn:  # AT-RLS-00: no context
        for table in tables:
            assert await scalar(conn, f"SELECT count(*) FROM {table}") == 0, table  # noqa: S608
    async with as_app(app_engine, **_ctx(w, "ani")) as conn:  # AT-RLS-01: Ani sees only hers
        assert await scalar(conn, "SELECT count(*) FROM transactions") == 0
        assert await scalar(conn, "SELECT count(*) FROM drafts") == 0
        assert (
            await scalar(
                conn,
                "SELECT count(*) FROM categories WHERE workspace_id <> :ws",
                {"ws": str(w.ani.personal_ws)},
            )
            == 0
        )
    # The right workspace but another user: drafts stay private (R-9).
    async with as_app(
        app_engine, workspace=str(w.budi.personal_ws), user=str(w.ani.user_id)
    ) as conn:
        assert await scalar(conn, "SELECT count(*) FROM transactions") == 0  # not a member
    assert (
        await owner_scalar(owner_engine, "SELECT count(*) FROM drafts WHERE status = 'pending'")
        == 1
    )
