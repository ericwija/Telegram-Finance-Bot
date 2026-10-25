"""Finance services on a real PostgreSQL 18.6 as ``finbot_app`` (Step 6).

Seeding, categories, the capture conversation with idempotent confirmation, corrections with
optimistic locking and entry revisions, void/restore, reports, history, workspace isolation,
role permissions at execution time, and audit content. Runs only with FINBOT_TEST_PG_BACKEND.
"""

import asyncio
import logging
import uuid
from datetime import date
from typing import Any

import pytest

from finbot.authz.policy import ForbiddenError
from finbot.domain.finance import (
    DEFAULT_CATEGORIES,
    ConflictError,
    DraftExpiredError,
    DraftField,
    DuplicateNameError,
    EditField,
    HistoryFilter,
    InvalidInputError,
    Month,
    NotFoundError,
    StepError,
    TransactionChanges,
)
from finbot.domain.ledger import TxnStatus, TxnType
from tests.support.finance import (
    Finance,
    business,
    finance,
    owner_rows,
    owner_scalar,
    person,
    record,
    remove_member,
    set_role,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

E, INC = TxnType.EXPENSE, TxnType.INCOME
SEEDED = sum(len(v) for v in DEFAULT_CATEGORIES.values())


@pytest.fixture
def f(migrated_database: str, app_engine: Any) -> Finance:
    return finance(app_engine)


async def _audit(owner_engine: Any, action: str, workspace: uuid.UUID) -> list[Any]:
    return await owner_rows(
        owner_engine,
        "SELECT actor_kind, entity_id, metadata, outcome FROM audit_events "
        "WHERE action = :a AND workspace_id = :ws ORDER BY occurred_at, id",
        {"a": action, "ws": workspace},
    )


# --------------------------------------------------------------------------- seeding


async def test_FR_IDN_04_defaults_are_seeded_once_even_concurrently(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    await asyncio.gather(*(f.ledger.balance(budi.actor()) for _ in range(5)))
    listing = await f.ledger.categories(budi.actor())
    assert len(listing.categories) == SEEDED
    assert {c.name for c in listing.categories if c.is_default} == {"Lainnya"}
    balance = await f.ledger.balance(budi.actor())
    assert [(a.name, a.balance) for a in balance.report.accounts] == [("Kas", 0)]
    assert await owner_scalar(
        owner_engine,
        "SELECT default_account_id IS NOT NULL FROM workspace_memberships WHERE user_id = :u",
        {"u": budi.user_id},
    )
    seeded = await _audit(owner_engine, "category.created", budi.personal_ws)
    assert len(seeded) == SEEDED
    assert all(r.actor_kind == "system" and r.metadata["seeded"] for r in seeded)
    assert len(await _audit(owner_engine, "account.created", budi.personal_ws)) == 1


# ------------------------------------------------------------------------ categories


async def test_category_lifecycle_and_case_insensitive_uniqueness(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    kopi = await f.ledger.create_category(actor, E, "  Kopi  ")
    assert kopi.name == "Kopi"
    for duplicate in ("kopi", "KOPI", "Makanan"):
        with pytest.raises(DuplicateNameError):
            await f.ledger.create_category(actor, E, duplicate)
    await f.ledger.create_category(actor, INC, "Kopi")  # another kind is fine
    renamed = await f.ledger.rename_category(actor, kopi.id, "Kopi & Teh")
    assert renamed.name == "Kopi & Teh"
    with pytest.raises(DuplicateNameError):
        await f.ledger.rename_category(actor, kopi.id, "makanan")
    await f.ledger.rename_category(actor, kopi.id, "KOPI & TEH")  # case-only change
    archived = await f.ledger.set_category_active(actor, kopi.id, False)
    assert archived.archived
    active_names = {c.name for c in (await f.ledger.categories(actor, E)).categories}
    assert "KOPI & TEH" not in active_names
    replacement = await f.ledger.create_category(actor, E, "kopi & teh")  # name free again
    with pytest.raises(DuplicateNameError):
        await f.ledger.set_category_active(actor, kopi.id, True)  # would collide
    await f.ledger.set_category_active(actor, replacement.id, False)
    assert not (await f.ledger.set_category_active(actor, kopi.id, True)).archived
    lainnya = next(c for c in (await f.ledger.categories(actor, E)).categories if c.is_default)
    with pytest.raises(InvalidInputError, match="category_default"):
        await f.ledger.set_category_active(actor, lainnya.id, False)
    for bad in ("", " ", "x" * 41):
        with pytest.raises(InvalidInputError):
            await f.ledger.create_category(actor, E, bad)
    with pytest.raises(InvalidInputError):
        await f.ledger.create_category(actor, TxnType.TRANSFER, "Pindah")
    actions = [
        r[0]
        for r in await owner_rows(
            owner_engine,
            "SELECT action FROM audit_events WHERE workspace_id = :ws AND actor_kind = 'user' "
            "ORDER BY occurred_at, id",
            {"ws": budi.personal_ws},
        )
    ]
    assert actions.count("category.created") == 3
    assert "category.archived" in actions
    assert "category.unarchived" in actions


async def test_FR_CAT_04_archived_category_stays_on_old_transactions(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    kopi = await f.ledger.create_category(actor, E, "Kopi")
    saved = await record(f, actor, E, "18rb", "Kopi")
    await f.ledger.set_category_active(actor, kopi.id, False)
    detail = await f.ledger.transaction(actor, saved.record.id)
    assert (detail.record.category_name, detail.record.category_archived) == ("Kopi", True)
    view = await f.capture.start(actor, E, amount_text="5000")
    assert "Kopi" not in {c.name for c in view.categories}
    with pytest.raises(InvalidInputError, match="category_archived"):
        await f.capture.choose_category(actor, view.ref, kopi.id)


# ------------------------------------------------------------------------- capture


async def test_capture_flow_writes_header_entry_revision_and_audit(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    view = await f.capture.start(actor)
    view = await f.capture.choose_type(actor, view.ref, E)
    with pytest.raises(StepError):
        await f.capture.skip_description(actor, view.ref)  # amount first
    with pytest.raises(InvalidInputError, match="amount_invalid"):
        await f.capture.input_text(actor, "dua puluh")
    view = await f.capture.input_text(actor, "25.000,50")  # type: ignore[assignment]
    assert view.state.rounded
    makanan = next(c for c in view.categories if c.name == "Makanan")
    view = await f.capture.choose_category(actor, view.ref, makanan.id)
    view = await f.capture.input_text(actor, "Makan siang ZZSECRETZZ")  # type: ignore[assignment]
    view = await f.capture.revise(actor, view.ref, DraftField.DATE)
    view = await f.capture.input_text(actor, "kemarin")  # type: ignore[assignment]
    result = await f.capture.confirm(actor, view.ref)
    txn = result.record
    assert (txn.type, txn.amount, txn.txn_date, txn.category_name) == (
        E,
        25_001,
        date(2026, 10, 24),
        "Makanan",
    )
    assert txn.description == "Makan siang ZZSECRETZZ"
    entries = await owner_rows(
        owner_engine,
        "SELECT leg, amount, revision FROM transaction_entries WHERE transaction_id = :t",
        {"t": txn.id},
    )
    assert [(e.leg, e.amount, e.revision) for e in entries] == [("single", -25_001, 1)]
    revisions = await owner_rows(
        owner_engine,
        "SELECT change_kind, snapshot FROM transaction_revisions WHERE transaction_id = :t",
        {"t": txn.id},
    )
    assert [r.change_kind for r in revisions] == ["create"]
    assert "ZZSECRETZZ" not in str(revisions[0].snapshot)
    created = await _audit(owner_engine, "transaction.created", budi.personal_ws)
    assert created[0].metadata["amount"] == 25_001
    assert created[0].metadata["back_dated"] is True
    assert "ZZSECRETZZ" not in str(created)
    assert await f.capture.current(actor) is None  # the draft is closed


async def test_AT_TXN_IDEMP_01_double_confirm_stores_one_transaction(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    view = await f.capture.start(actor, E, amount_text="40rb", description="bensin")
    view = await f.capture.choose_category(
        actor, view.ref, next(c.id for c in view.categories if c.name == "Transportasi")
    )
    results = await asyncio.gather(*(f.capture.confirm(actor, view.ref) for _ in range(4)))
    assert len({r.record.id for r in results}) == 1
    assert sorted(r.already for r in results) == [False, True, True, True]
    assert await owner_scalar(owner_engine, "SELECT count(*) FROM transactions") == 1
    again = await f.capture.confirm(actor, view.ref)  # a Telegram retry, much later
    assert again.already


async def test_concurrent_conversation_starts_leave_exactly_one_pending_draft(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    await f.ledger.balance(budi.actor())  # seed first, so only the conversation races
    views = await asyncio.gather(*(f.capture.start(budi.actor(), E) for _ in range(6)))
    assert len({v.id for v in views}) == 6
    statuses = [r.status for r in await owner_rows(owner_engine, "SELECT status FROM drafts")]
    assert sorted(statuses) == ["cancelled"] * 5 + ["pending"]


async def test_stale_ref_expiry_and_cancel(f: Finance, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    first = await f.capture.start(actor, E, amount_text="10rb")
    second = await f.capture.start(actor, INC, amount_text="20rb")  # replaces the first
    with pytest.raises(DraftExpiredError):
        await f.capture.choose_category(actor, first.ref, second.categories[0].id)
    with pytest.raises(DraftExpiredError):
        await f.capture.confirm(actor, first.ref)
    assert not await f.capture.cancel(actor, first.ref)  # a stale cancel does nothing
    f.clock.advance(hours=25)  # FR-TXN-06: drafts expire after 24 h
    with pytest.raises(DraftExpiredError):
        await f.capture.choose_category(actor, second.ref, second.categories[0].id)
    statuses = await owner_rows(owner_engine, "SELECT status FROM drafts ORDER BY created_at, id")
    assert [s.status for s in statuses] == ["cancelled", "expired"]
    view = await f.capture.start(actor, E)
    assert await f.capture.cancel(actor)
    with pytest.raises(DraftExpiredError):
        await f.capture.confirm(actor, view.ref)
    assert await f.capture.input_text(actor, "25000") is None  # nothing waiting
    assert await owner_scalar(owner_engine, "SELECT count(*) FROM transactions") == 0


# ---------------------------------------------------------------------- corrections


async def test_AT_ENT_11_edit_amount_writes_a_new_entry_set(f: Finance, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    saved = (await record(f, actor, E, "50rb", "Belanja")).record
    view = await f.capture.start_edit(actor, saved.id, saved.version, EditField.AMOUNT)
    view = await f.capture.input_text(actor, "45rb")  # type: ignore[assignment]
    updated = (await f.capture.confirm(actor, view.ref)).record
    assert (updated.amount, updated.entry_revision, updated.version) == (45_000, 2, 2)
    entries = await owner_rows(
        owner_engine,
        "SELECT revision, amount FROM transaction_entries WHERE transaction_id = :t "
        "ORDER BY revision",
        {"t": saved.id},
    )
    assert [(e.revision, e.amount) for e in entries] == [(1, -50_000), (2, -45_000)]
    balance = await f.ledger.balance(actor)
    assert balance.report.accounts[0].balance == -45_000
    audit = await _audit(owner_engine, "transaction.updated", budi.personal_ws)
    assert (audit[0].metadata["amount_before"], audit[0].metadata["amount_after"]) == (
        50_000,
        45_000,
    )


async def test_AT_ENT_12_and_AT_TXN_OCC_01_non_monetary_edit_and_stale_version(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    saved = (await record(f, actor, INC, "5jt", "Gaji")).record
    bonus = next(c for c in (await f.ledger.categories(actor, INC)).categories if c.name == "Bonus")
    first = await f.ledger.update_transaction(
        actor, saved.id, 1, TransactionChanges(category_id=bonus.id, description="THR")
    )
    assert (first.version, first.entry_revision, first.category_name) == (2, 1, "Bonus")
    with pytest.raises(ConflictError, match="stale"):
        await f.ledger.update_transaction(actor, saved.id, 1, TransactionChanges(amount=1))
    with pytest.raises(InvalidInputError, match="no_changes"):
        await f.ledger.update_transaction(actor, saved.id, 2, TransactionChanges(description="THR"))
    makanan = next(
        c for c in (await f.ledger.categories(actor, E)).categories if c.name == "Makanan"
    )
    with pytest.raises(InvalidInputError, match="category_kind_mismatch"):
        await f.ledger.update_transaction(
            actor, saved.id, 2, TransactionChanges(category_id=makanan.id)
        )
    with pytest.raises(InvalidInputError, match="date_in_future"):
        await f.ledger.update_transaction(
            actor, saved.id, 2, TransactionChanges(txn_date=date(2026, 10, 26))
        )
    kinds = await owner_rows(
        owner_engine,
        "SELECT change_kind, changed_fields FROM transaction_revisions "
        "WHERE transaction_id = :t ORDER BY version",
        {"t": saved.id},
    )
    assert [(k.change_kind, k.changed_fields) for k in kinds] == [
        ("create", []),
        ("update", ["category", "description"]),
    ]
    # An edit draft started on an old version is refused at confirmation.
    view = await f.capture.start_edit(actor, saved.id, 2, EditField.DESCRIPTION)
    await f.ledger.update_transaction(actor, saved.id, 2, TransactionChanges(description="x"))
    view = await f.capture.input_text(actor, "y")  # type: ignore[assignment]
    with pytest.raises(ConflictError, match="stale"):
        await f.capture.confirm(actor, view.ref)


async def test_void_and_restore_change_balances_but_never_entries(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    income = (await record(f, actor, INC, "1jt", "Gaji")).record
    expense = (await record(f, actor, E, "200rb", "Tagihan")).record
    assert (await f.ledger.balance(actor)).report.accounts[0].balance == 800_000
    voided = await f.ledger.void_transaction(actor, expense.id, expense.version, "salah catat")
    assert voided.status is TxnStatus.VOIDED
    report = (await f.ledger.balance(actor)).report
    assert report.accounts[0].balance == 1_000_000
    assert [(t.income, t.expense) for t in report.all_time] == [(1_000_000, 0)]
    summary = (await f.ledger.summary(actor)).summary
    assert [(t.income, t.expense) for t in summary.totals] == [(1_000_000, 0)]
    posted = (await f.ledger.history(actor, HistoryFilter())).page.items
    assert [r.id for r in posted] == [income.id]
    deleted = (await f.ledger.history(actor, HistoryFilter(voided=True))).page.items
    assert [r.id for r in deleted] == [expense.id]
    with pytest.raises(ConflictError, match="already_voided"):
        await f.ledger.void_transaction(actor, expense.id, voided.version)
    with pytest.raises(ConflictError, match="voided"):
        await f.ledger.update_transaction(
            actor, expense.id, voided.version, TransactionChanges(amount=1)
        )
    restored = await f.ledger.restore_transaction(actor, expense.id, voided.version)
    assert restored.status is TxnStatus.POSTED
    assert (await f.ledger.balance(actor)).report.accounts[0].balance == 800_000
    assert (
        await owner_scalar(
            owner_engine,
            "SELECT count(*) FROM transaction_entries WHERE transaction_id = :t",
            {"t": expense.id},
        )
        == 1
    )
    void_audit = await _audit(owner_engine, "transaction.voided", budi.personal_ws)
    assert void_audit[0].metadata["reason_given"] is True
    assert "salah catat" not in str(void_audit)


# --------------------------------------------------------------------------- reports


async def test_AT_RPT_01_monthly_summary_matches_a_reference_and_respects_boundaries(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    fixtures = [
        (INC, "5jt", "Gaji", "01/10/2026"),
        (E, "25rb", "Makanan", "01/10/2026"),
        (E, "75rb", "Makanan", "25/10/2026"),
        (E, "300rb", "Tagihan", "15/10/2026"),
        (E, "999rb", "Belanja", "30/09/2026"),  # previous month
        (INC, "1jt", "Bonus", "30/09/2026"),
    ]
    for txn_type, amount, category, day in fixtures:
        await record(f, actor, txn_type, amount, category, date_text=day)
    october = (await f.ledger.summary(actor)).summary
    assert october.month == Month(2026, 10)
    assert [(t.currency, t.income, t.expense, t.net) for t in october.totals] == [
        ("IDR", 5_000_000, 400_000, 4_600_000)
    ]
    assert [(c.name, c.total, c.count) for c in october.expense_by_category] == [
        ("Tagihan", 300_000, 1),
        ("Makanan", 100_000, 2),
    ]
    september = (await f.ledger.summary(actor, Month(2026, 9))).summary
    assert [(t.income, t.expense) for t in september.totals] == [(1_000_000, 999_000)]
    with pytest.raises(InvalidInputError, match="month_in_future"):
        await f.ledger.summary(actor, Month(2026, 11))
    balance = (await f.ledger.balance(actor)).report
    # Reference: opening 0 + incomes - expenses, all posted, all dated on or before today.
    assert balance.recorded_balance() == {"IDR": 6_000_000 - 1_399_000}
    assert [(t.income, t.expense, t.net) for t in balance.all_time] == [
        (6_000_000, 1_399_000, 4_601_000)
    ]


async def test_AT_ENT_19_transfers_never_count_as_income_or_expense(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    await record(f, actor, INC, "1jt", "Gaji")
    async with owner_engine.begin() as conn:  # a transfer Kas → Bank, as the import would
        from sqlalchemy import text  # noqa: PLC0415

        ws = {"ws": budi.personal_ws, "u": budi.user_id}
        kas = (
            await conn.execute(text("SELECT id FROM accounts WHERE workspace_id = :ws"), ws)
        ).scalar_one()
        bank = (
            await conn.execute(
                text(
                    "INSERT INTO accounts (workspace_id, name, type, opening_date, created_by) "
                    "VALUES (:ws, 'Bank', 'bank', '2000-01-01', :u) RETURNING id"
                ),
                ws,
            )
        ).scalar_one()
        txn = (
            await conn.execute(
                text(
                    "INSERT INTO transactions (workspace_id, type, amount, txn_date, source, "
                    "created_by) "
                    "VALUES (:ws, 'transfer', 400000, '2026-10-20', 'manual', :u) RETURNING id"
                ),
                ws,
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO transaction_entries (workspace_id, transaction_id, transaction_type, "
                "revision, leg, account_id, amount, created_by) VALUES "
                "(:ws, :t, 'transfer', 1, 'source', :kas, -400000, :u), "
                "(:ws, :t, 'transfer', 1, 'destination', :bank, 400000, :u)"
            ),
            {**ws, "t": txn, "kas": kas, "bank": bank},
        )
    summary = (await f.ledger.summary(actor)).summary
    assert [(t.income, t.expense) for t in summary.totals] == [(1_000_000, 0)]
    balance = (await f.ledger.balance(actor)).report
    assert sorted((a.name, a.balance) for a in balance.accounts) == [
        ("Bank", 400_000),
        ("Kas", 600_000),
    ]
    assert balance.recorded_balance() == {"IDR": 1_000_000}
    history = (await f.ledger.history(actor, HistoryFilter())).page.items
    assert {r.type for r in history} == {INC, TxnType.TRANSFER}


async def test_history_pagination_filters_and_stable_order(f: Finance, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    for n in range(23):
        day = f"{1 + n % 20:02d}/10/2026"
        await record(f, actor, E if n % 3 else INC, f"{n + 1}000", "Lainnya", date_text=day)
    pages = [await f.ledger.history(actor, HistoryFilter(), page) for page in range(3)]
    items = [r for p in pages for r in p.page.items]
    assert [len(p.page.items) for p in pages] == [10, 10, 3]
    assert [p.page.has_next for p in pages] == [True, True, False]
    assert len({r.id for r in items}) == 23
    keys = [(r.txn_date, r.id) for r in items]
    assert keys == sorted(keys, reverse=True)  # newest date first, then ID: stable
    incomes = await f.ledger.history(actor, HistoryFilter(txn_type=INC))
    assert {r.type for r in incomes.page.items} == {INC}
    ranged = await f.ledger.history(
        actor, HistoryFilter(date_from=date(2026, 10, 5), date_to=date(2026, 10, 6))
    )
    assert {r.txn_date for r in ranged.page.items} == {date(2026, 10, 5), date(2026, 10, 6)}
    lainnya = next(c for c in (await f.ledger.categories(actor, E)).categories if c.is_default)
    by_category = await f.ledger.history(actor, HistoryFilter(category_id=lainnya.id))
    assert {r.type for r in by_category.page.items} == {E}
    with pytest.raises(InvalidInputError, match="page"):
        await f.ledger.history(actor, HistoryFilter(), 100)
    assert (await f.ledger.history(actor, HistoryFilter(), 50)).page.items == ()


# -------------------------------------------------------------- isolation and roles


async def test_AT_ISO_cross_workspace_ids_are_not_found(f: Finance, owner_engine: Any) -> None:
    budi, ani = await person(owner_engine, "Budi"), await person(owner_engine, "Ani")
    ani_txn = (await record(f, ani.actor(), E, "10rb", "Makanan")).record
    ani_category = (await f.ledger.categories(ani.actor(), E)).categories[0]
    actor = budi.actor()
    with pytest.raises(NotFoundError):
        await f.ledger.transaction(actor, ani_txn.id)  # AT-ISO-05
    with pytest.raises(NotFoundError):
        await f.ledger.void_transaction(actor, ani_txn.id, ani_txn.version)
    with pytest.raises(NotFoundError):
        await f.capture.start_edit(actor, ani_txn.id, ani_txn.version, EditField.AMOUNT)
    with pytest.raises(NotFoundError):
        await f.ledger.rename_category(actor, ani_category.id, "Hack")
    with pytest.raises(NotFoundError):
        await f.ledger.history(actor, HistoryFilter(category_id=ani_category.id))
    view = await f.capture.start(actor, E, amount_text="5000")
    with pytest.raises(NotFoundError):  # AT-ISO-03: a B2 category in a B1 draft
        await f.capture.choose_category(actor, view.ref, ani_category.id)
    with pytest.raises(NotFoundError):  # explicit foreign workspace: no membership
        await f.ledger.balance(budi.actor(ani.personal_ws))
    with pytest.raises(NotFoundError):
        await f.ledger.transaction(actor, uuid.uuid4())  # same answer as "never existed"
    assert (await f.ledger.transaction(ani.actor(), ani_txn.id)).record.amount == 10_000


async def test_AT_RBAC_P_01_platform_admin_has_no_workspace_access(
    f: Finance, owner_engine: Any
) -> None:
    admin = await person(owner_engine, "Admin", platform_admin=True)
    ani = await person(owner_engine, "Ani")
    saved = (await record(f, ani.actor(), INC, "1jt", "Gaji")).record
    with pytest.raises(NotFoundError):
        await f.ledger.history(admin.actor(ani.personal_ws), HistoryFilter())
    with pytest.raises(NotFoundError):
        await f.ledger.transaction(admin.actor(), saved.id)


async def test_AT_RBAC_M_finance_permissions_in_a_business_workspace(
    f: Finance, owner_engine: Any
) -> None:
    owner, admin, editor, viewer = [await person(owner_engine, n) for n in ("O", "A", "E", "V")]
    ws = await business(owner_engine, owner, {admin: "admin", editor: "editor", viewer: "viewer"})
    saved = (await record(f, owner.actor(ws), E, "100rb", "Belanja")).record
    # Viewer: read only (and the denial is audited).
    await f.ledger.history(viewer.actor(ws), HistoryFilter())
    await f.ledger.summary(viewer.actor(ws))
    with pytest.raises(ForbiddenError):
        await f.capture.start(viewer.actor(ws), E)
    with pytest.raises(ForbiddenError):
        await f.ledger.create_category(viewer.actor(ws), E, "X")
    denials = await _audit(owner_engine, "security.authz_denied", ws)
    assert [(d.outcome, d.metadata["permission"]) for d in denials] == [
        ("denied", "transaction.create"),
        ("denied", "category.manage"),
    ]
    # Editor: create and update (D2(a)), but no delete, no category management.
    edited = await f.ledger.update_transaction(
        editor.actor(ws), saved.id, saved.version, TransactionChanges(description="edit")
    )
    await record(f, editor.actor(ws), INC, "50rb", "Penjualan")
    with pytest.raises(ForbiddenError):
        await f.ledger.void_transaction(editor.actor(ws), saved.id, edited.version)
    with pytest.raises(ForbiddenError):
        await f.ledger.rename_category(editor.actor(ws), saved.category_id, "Y")  # type: ignore[arg-type]
    with pytest.raises(ForbiddenError):  # G-4: the "Dihapus" list needs view_deleted
        await f.ledger.history(editor.actor(ws), HistoryFilter(voided=True))
    detail = await f.ledger.transaction(editor.actor(ws), saved.id)
    assert (detail.can_update, detail.can_delete) == (True, False)
    # Admin: delete and manage categories.
    voided = await f.ledger.void_transaction(admin.actor(ws), saved.id, edited.version)
    await f.ledger.create_category(admin.actor(ws), E, "Sewa")
    with pytest.raises(NotFoundError):  # a voided transaction is hidden from editors
        await f.ledger.transaction(editor.actor(ws), voided.id)


async def test_AT_RBAC_R9_02_downgrade_during_a_conversation(f: Finance, owner_engine: Any) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    actor = editor.actor(ws)
    view = await f.capture.start(actor, E, amount_text="10rb", description="x")
    view = await f.capture.choose_category(actor, view.ref, view.categories[0].id)
    await set_role(owner_engine, ws, editor, "viewer")  # G-2: rendered buttons are now stale
    with pytest.raises(ForbiddenError):
        await f.capture.confirm(actor, view.ref)
    assert (
        await owner_scalar(
            owner_engine, "SELECT status FROM drafts WHERE user_id = :u", {"u": editor.user_id}
        )
        == "expired"
    )
    assert await owner_scalar(owner_engine, "SELECT count(*) FROM transactions") == 0


async def test_removed_member_loses_access_mid_conversation(f: Finance, owner_engine: Any) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    actor = editor.actor(ws)
    view = await f.capture.start(actor, INC, amount_text="10rb", description="x")
    view = await f.capture.choose_category(actor, view.ref, view.categories[0].id)
    await remove_member(owner_engine, ws, editor)
    with pytest.raises(NotFoundError):
        await f.capture.confirm(actor, view.ref)
    assert await owner_scalar(owner_engine, "SELECT count(*) FROM transactions") == 0


async def test_AT_LOG_01_finance_logs_hold_no_amounts_or_descriptions(
    f: Finance, owner_engine: Any, caplog: pytest.LogCaptureFixture
) -> None:
    budi = await person(owner_engine)
    with caplog.at_level(logging.DEBUG):
        await record(f, budi.actor(), E, "123456", "Makanan", description="ZZSECRETZZ")
        await f.ledger.summary(budi.actor())
    assert "ZZSECRETZZ" not in caplog.text
    assert "123456" not in caplog.text
    assert "123.456" not in caplog.text
