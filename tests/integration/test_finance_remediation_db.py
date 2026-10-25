"""Step 6B regressions on a real PostgreSQL 18.6 as ``finbot_app`` (findings F01 to F12 and
risks R01 to R03 of the independent review; docs/10 §13).

Concurrency is real here: every service call opens its own connection (NullPool), and
``asyncio.gather`` interleaves them in the server. Lock behaviour is proven with ``lock_timeout``
on a competing owner connection, so a missing lock fails the test instead of passing by luck.
"""

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from finbot.authz.policy import ForbiddenError
from finbot.domain.finance import (
    DEFAULT_CATEGORIES,
    ConflictError,
    DraftExpiredError,
    DraftField,
    DuplicateNameError,
    EditField,
    InvalidInputError,
    NotFoundError,
    TransactionChanges,
)
from finbot.domain.ledger import TxnStatus, TxnType
from finbot.ports.finance import SessionMode
from finbot.services.capture import CategoryPrompt
from finbot.services.finance import Actor
from tests.support.finance import (
    Finance,
    Person,
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
TODAY = date(2026, 10, 25)


@pytest.fixture
def f(migrated_database: str, app_engine: Any) -> Finance:
    return finance(app_engine)


async def _owner_exec(owner_engine: Any, sql: str, params: dict[str, Any] | None = None) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text(sql), params or {})


async def _blocked(owner_engine: Any, sql: str, params: dict[str, Any]) -> bool:
    """True if ``sql`` (as the owner) has to wait for a lock held elsewhere."""
    try:
        async with owner_engine.begin() as conn:
            await conn.execute(text("SET LOCAL lock_timeout = '300ms'"))
            await conn.execute(text(sql), params)
    except DBAPIError as exc:
        if "lock timeout" in str(exc):
            return True
        raise
    return False


async def _draft_status(owner_engine: Any, user: Person) -> list[str]:
    rows = await owner_rows(
        owner_engine,
        "SELECT status FROM drafts WHERE user_id = :u ORDER BY created_at, id",
        {"u": user.user_id},
    )
    return [r.status for r in rows]


async def _transactions(owner_engine: Any) -> int:
    return int(await owner_scalar(owner_engine, "SELECT count(*) FROM transactions"))


async def _ready_draft(
    f: Finance, actor: Actor, txn_type: TxnType = E, amount: str = "10rb"
) -> Any:
    view = await f.capture.start(actor, txn_type, amount_text=amount, description="x")
    return await f.capture.choose_category(actor, view.ref, view.categories[0].id)


# ------------------------------------------------------------- F01: replay authorization


async def test_F01_replay_is_reauthorized_and_hides_later_changes(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    actor = editor.actor(ws)
    view = await _ready_draft(f, actor)
    first = await f.capture.confirm(actor, view.ref)
    assert (await f.capture.confirm(actor, view.ref)).already  # a normal double tap
    # Modified by someone else: the replay shows the current version, never a stale one.
    await f.ledger.update_transaction(
        owner.actor(ws), first.record.id, 1, TransactionChanges(amount=11_000)
    )
    again = await f.capture.confirm(actor, view.ref)
    assert (again.already, again.record.amount, again.record.version) == (True, 11_000, 2)
    # Downgraded to viewer: the replay is refused (and audited), no details.
    await set_role(owner_engine, ws, editor, "viewer")
    with pytest.raises(ForbiddenError):
        await f.capture.confirm(actor, view.ref)
    denial = await owner_rows(
        owner_engine,
        "SELECT entity_type, metadata FROM audit_events WHERE action = 'security.authz_denied' "
        "AND actor_user_id = :u",
        {"u": editor.user_id},
    )
    assert [(d.entity_type, d.metadata["permission"]) for d in denial] == [
        ("transaction", "transaction.create")
    ]
    # Promoted back: a new membership epoch, so the old confirmation is not replayed either.
    await set_role(owner_engine, ws, editor, "editor")
    with pytest.raises(DraftExpiredError):
        await f.capture.confirm(actor, view.ref)


async def test_F01_replay_after_void_suspension_or_removal_reveals_nothing(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    actor = editor.actor(ws)
    view = await _ready_draft(f, actor)
    saved = (await f.capture.confirm(actor, view.ref)).record
    await f.ledger.void_transaction(owner.actor(ws), saved.id, saved.version)
    with pytest.raises(DraftExpiredError):  # the editor cannot see voided transactions
        await f.capture.confirm(actor, view.ref)
    owner_view = await _ready_draft(f, owner.actor(ws))
    mine = (await f.capture.confirm(owner.actor(ws), owner_view.ref)).record
    await f.ledger.void_transaction(owner.actor(ws), mine.id, mine.version)
    with pytest.raises(DraftExpiredError):  # not even the owner gets "already saved"
        await f.capture.confirm(owner.actor(ws), owner_view.ref)
    pending = await _ready_draft(f, actor)
    await _owner_exec(
        owner_engine, "UPDATE users SET status = 'suspended' WHERE id = :u", {"u": editor.user_id}
    )
    for call in (
        f.capture.confirm(actor, view.ref),
        f.capture.current(actor),
        f.capture.cancel(actor),
        f.capture.cancel(actor, pending.ref),
        f.capture.input_text(actor, "12rb"),
    ):
        with pytest.raises(ForbiddenError):
            await call
    await _owner_exec(
        owner_engine, "UPDATE users SET status = 'active' WHERE id = :u", {"u": editor.user_id}
    )
    await remove_member(owner_engine, ws, editor)
    with pytest.raises(NotFoundError):
        await f.capture.confirm(actor, view.ref)
    assert await _transactions(owner_engine) == 2


async def test_F01_current_and_edit_respect_the_permission_of_the_moment(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    actor = editor.actor(ws)
    view = await _ready_draft(f, actor)
    assert await f.capture.current(actor) is not None
    await set_role(owner_engine, ws, editor, "viewer")
    assert await f.capture.current(actor) is None  # not offered to a viewer
    await set_role(owner_engine, ws, editor, "editor")
    assert await f.capture.current(actor) is None  # and stale after the epoch changed
    saved = await record(f, owner.actor(ws), E, "5rb", "Makanan")
    voided = await f.ledger.void_transaction(owner.actor(ws), saved.record.id, 1)
    with pytest.raises(NotFoundError):  # an editor cannot learn that a voided one exists
        await f.capture.start_edit(actor, voided.id, voided.version, EditField.AMOUNT)
    del view


# ----------------------------------------------------------- F02: first-use seeding


async def test_F02_concurrent_first_use_of_an_unseeded_workspace(
    f: Finance, owner_engine: Any
) -> None:
    owner = await person(owner_engine, "O")
    members = [await person(owner_engine, f"M{i}") for i in range(4)]
    ws = await business(owner_engine, owner, dict.fromkeys(members, "editor"))
    everyone = [owner, *members]
    # Writes (FOR SHARE on the membership) and reads, the same user several times at once:
    # before Step 6B two first writes of one user upgraded their shared lock and deadlocked.
    calls = []
    for p in everyone:
        calls += [
            f.capture.start(p.actor(ws), E, amount_text="1rb"),
            f.capture.start(p.actor(ws), INC, amount_text="2rb"),
            f.ledger.balance(p.actor(ws)),
            f.ledger.categories(p.actor(ws)),
        ]
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert [r for r in results if isinstance(r, BaseException)] == []
    assert (
        await owner_scalar(
            owner_engine, "SELECT count(*) FROM categories WHERE workspace_id = :ws", {"ws": ws}
        )
        == SEEDED
    )
    assert (
        await owner_scalar(
            owner_engine, "SELECT count(*) FROM accounts WHERE workspace_id = :ws", {"ws": ws}
        )
        == 1
    )
    defaults = await owner_rows(
        owner_engine,
        "SELECT DISTINCT default_account_id FROM workspace_memberships WHERE workspace_id = :ws",
        {"ws": ws},
    )
    assert len(defaults) == 1
    assert defaults[0].default_account_id is not None
    seeded = await owner_scalar(
        owner_engine,
        "SELECT count(*) FROM audit_events WHERE workspace_id = :ws AND action IN "
        "('category.created', 'account.created')",
        {"ws": ws},
    )
    assert seeded == SEEDED + 1  # seeded exactly once
    # Each user ends with exactly one pending draft (the conversation lock).
    pending = await owner_rows(
        owner_engine,
        "SELECT user_id, count(*) AS n FROM drafts WHERE status = 'pending' GROUP BY user_id",
    )
    assert sorted(r.n for r in pending) == [1] * len(everyone)


async def test_F02_seeding_runs_in_its_own_transaction_and_reads_stay_read_only(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    await f.ledger.balance(budi.actor())  # a READ request on an unseeded workspace
    assert (
        await owner_scalar(
            owner_engine,
            "SELECT count(*) FROM categories WHERE workspace_id = :ws",
            {"ws": budi.personal_ws},
        )
        == SEEDED
    )
    async with f.store.session(budi.user_id, mode=SessionMode.READ) as s:
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await s.insert_category(E, "Tidak boleh")


# ----------------------------------------------------------- F03: report snapshots


async def test_F03_a_report_reads_one_snapshot(f: Finance, owner_engine: Any) -> None:
    budi = await person(owner_engine)
    other = finance(f.store._engine, f.clock)
    await record(f, budi.actor(), INC, "100rb", "Gaji")
    async with f.store.session(budi.user_id, mode=SessionMode.READ) as s:
        balances = await s.account_balances(TODAY)
        # Committed by another connection between the two queries of one report:
        await record(other, budi.actor(), E, "30rb", "Makanan")
        flows = await s.cash_flow(None, TODAY)
    assert [b.balance for b in balances] == [100_000]
    assert flows == [("IDR", INC, 100_000)]  # the same snapshot: still reconciles
    report = (await f.ledger.balance(budi.actor())).report
    assert report.recorded_balance() == {"IDR": 70_000}


async def test_F03_reports_reconcile_under_concurrent_create_edit_and_void(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    seeds = [await record(f, actor, E, f"{n}rb", "Makanan") for n in range(1, 7)]

    async def writer(n: int) -> None:
        txn = seeds[n].record
        if n == 2:  # one conversation at a time per user: a single concurrent capture
            await record(f, actor, INC, "70rb", "Gaji")
        elif n % 3 == 0:
            await f.ledger.void_transaction(actor, txn.id, txn.version)
        elif n % 3 == 1:
            await f.ledger.update_transaction(
                actor, txn.id, txn.version, TransactionChanges(amount=99_000)
            )
        else:
            await f.ledger.update_transaction(
                actor, txn.id, txn.version, TransactionChanges(txn_date=date(2026, 10, 1))
            )

    async def reader() -> list[tuple[int, int]]:
        checks = []
        for _ in range(5):
            report = (await f.ledger.balance(actor)).report
            totals = report.all_time[0] if report.all_time else None
            net = totals.net if totals else 0
            checks.append((report.recorded_balance().get("IDR", 0), net))
            summary = (await f.ledger.summary(actor)).summary
            for t in summary.totals:
                assert t.expense == sum(c.total for c in summary.expense_by_category)
                assert t.income == sum(c.total for c in summary.income_by_category)
        return checks

    results = await asyncio.gather(*(writer(n) for n in range(6)), reader(), reader())
    for checks in results[6:]:
        for balance, net in checks:  # type: ignore[union-attr]
            assert balance == net  # entries and headers from the same snapshot


# ---------------------------------------------------------- F08: exact cancellation


async def test_F08_a_stale_cancel_never_cancels_another_conversation(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    draft = await f.capture.start(actor, E)
    prompt = await f.capture.start_category_create(actor, E)  # replaces the draft
    assert not await f.capture.cancel(actor, draft.ref)  # draft → category: stale ❌
    current = await f.capture.current(actor)
    assert isinstance(current, CategoryPrompt)
    assert current.action.id == prompt.action.id
    newer = await f.capture.start(actor, INC)  # category → draft
    assert not await f.capture.cancel(actor, prompt.ref)  # the prompt's ❌ is stale now
    assert not await f.capture.cancel(actor, draft.ref)  # replaced draft: stale ❌
    assert (await f.capture.current(actor)).id == newer.id  # type: ignore[union-attr]
    assert await f.capture.cancel(actor, newer.ref)  # its own ❌ works
    second = await f.capture.start_category_create(actor, INC)
    assert await f.capture.cancel(actor, second.ref)
    assert await f.capture.current(actor) is None
    statuses = await owner_rows(
        owner_engine, "SELECT status FROM pending_actions ORDER BY created_at, id"
    )
    assert [s.status for s in statuses] == ["cancelled", "cancelled"]


# ---------------------------------------------------------- F09: expiry persists


async def test_F09_an_expired_draft_is_persisted_as_expired_on_confirm(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    view = await _ready_draft(f, actor)
    f.clock.advance(hours=25)
    with pytest.raises(DraftExpiredError):
        await f.capture.confirm(actor, view.ref)
    assert await _draft_status(owner_engine, budi) == ["expired"]
    with pytest.raises(DraftExpiredError):  # still refused, never "already saved"
        await f.capture.confirm(actor, view.ref)
    other = await f.capture.start(actor, E)
    f.clock.advance(hours=25)
    with pytest.raises(DraftExpiredError):
        await f.capture.input_text(actor, "10rb")
    prompt = await f.capture.start_category_create(actor, E)
    f.clock.advance(hours=1)
    with pytest.raises(DraftExpiredError):
        await f.capture.input_text(actor, "Kopi")
    assert await _draft_status(owner_engine, budi) == ["expired", "expired"]
    assert (
        await owner_scalar(
            owner_engine,
            "SELECT status FROM pending_actions WHERE id = :id",
            {"id": prompt.action.id},
        )
        == "expired"
    )
    assert await _transactions(owner_engine) == 0
    del other


# --------------------------------------------------------- F10: account and corrections


async def test_F10_the_draft_keeps_its_account_when_the_default_changes(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    view = await _ready_draft(f, actor, amount="15rb")
    assert view.account is not None
    kas = view.account.id
    bank = uuid.uuid4()
    await _owner_exec(
        owner_engine,
        "INSERT INTO accounts (id, workspace_id, name, type, opening_date, created_by) "
        "VALUES (:id, :ws, 'Bank', 'bank', '2000-01-01', :u)",
        {"id": bank, "ws": budi.personal_ws, "u": budi.user_id},
    )
    await _owner_exec(
        owner_engine,
        "UPDATE workspace_memberships SET default_account_id = :a WHERE user_id = :u",
        {"a": bank, "u": budi.user_id},
    )
    saved = await f.capture.confirm(actor, view.ref)
    assert saved.record.account_id == kas  # what the confirmation showed
    # A new draft resolves the new default; an archived draft account is refused, not moved.
    later = await _ready_draft(f, actor)
    assert later.account is not None
    assert later.account.id == bank
    await _owner_exec(
        owner_engine, "UPDATE accounts SET archived_at = now() WHERE id = :a", {"a": bank}
    )
    with pytest.raises(InvalidInputError, match="account_unavailable"):
        await f.capture.confirm(actor, later.ref)
    assert await _draft_status(owner_engine, budi) == ["confirmed", "pending"]


async def test_F10_corrections_before_saving_reach_the_ledger(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    view = await _ready_draft(f, actor, amount="10rb")
    view = await f.capture.revise(actor, view.ref, DraftField.AMOUNT)
    view = await f.capture.input_text(actor, "2 juta")  # type: ignore[assignment]
    view = await f.capture.revise(actor, view.ref, DraftField.TYPE)
    view = await f.capture.choose_type(actor, view.ref, INC)  # a new type needs a category
    gaji = next(c for c in view.categories if c.name == "Gaji")
    view = await f.capture.choose_category(actor, view.ref, gaji.id)
    view = await f.capture.revise(actor, view.ref, DraftField.DESCRIPTION)
    view = await f.capture.skip_description(actor, view.ref)
    view = await f.capture.revise(actor, view.ref, DraftField.DATE)
    view = await f.capture.input_text(actor, "kemarin")  # type: ignore[assignment]
    view = await f.capture.revise(actor, view.ref, DraftField.CATEGORY)
    view = await f.capture.back(actor, view.ref)  # unchanged
    saved = (await f.capture.confirm(actor, view.ref)).record
    assert (saved.type, saved.amount, saved.category_id, saved.description, saved.txn_date) == (
        INC, 2_000_000, gaji.id, None, date(2026, 10, 24),
    )  # fmt: skip


# --------------------------------------------------------- F06/F07: categories


async def test_F06_F07_every_category_is_reachable_and_reactivation_works(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    for n in range(45):
        await f.ledger.create_category(actor, E, f"Biaya {n:02d}")
    seen: list[str] = []
    page = 0
    while True:
        listing = await f.ledger.categories(actor, E, page=page)
        seen += [c.name for c in listing.categories]
        if not listing.has_next:
            break
        page += 1
    assert len(seen) == len(set(seen)) == 45 + len(DEFAULT_CATEGORIES[E])
    assert "Lainnya" in seen
    view = await f.capture.start(actor, E, amount_text="1rb")
    offered: list[str] = []
    while True:
        offered += [c.name for c in view.categories]
        if not view.categories_has_next:
            break
        view = await f.capture.category_page(actor, view.ref, view.category_page + 1)
    assert sorted(offered) == sorted(seen)
    lainnya = next(c for c in view.categories if c.name == "Lainnya")
    view = await f.capture.choose_category(actor, view.ref, lainnya.id)
    biaya = next(
        c for c in (await f.ledger.categories(actor, E)).categories if c.name == "Biaya 00"
    )
    await f.ledger.set_category_active(actor, biaya.id, False)
    archived = await f.ledger.categories(actor, E, archived=True)
    assert [c.name for c in archived.categories] == ["Biaya 00"]
    assert (await f.ledger.set_category_active(actor, biaya.id, True)).archived is False
    assert (await f.ledger.categories(actor, E, archived=True)).categories == ()


# ----------------------------------------------------------- F12: audit correlation


async def test_F12_audit_events_carry_the_request_id_and_denials_the_entity(
    f: Finance, owner_engine: Any
) -> None:
    owner, viewer = await person(owner_engine, "O"), await person(owner_engine, "V")
    ws = await business(owner_engine, owner, {viewer: "viewer"})
    request = uuid.uuid4()
    actor = Actor(owner.user_id, ws, request_id=request)
    view = await f.capture.start(actor, E, amount_text="7rb", description="RAHASIA")
    view = await f.capture.choose_category(actor, view.ref, view.categories[0].id)
    await f.capture.confirm(actor, view.ref)
    rows = await owner_rows(
        owner_engine,
        "SELECT action, metadata::text AS meta FROM audit_events WHERE request_id = :r",
        {"r": request},
    )
    assert "transaction.created" in {r.action for r in rows}
    assert all("RAHASIA" not in r.meta for r in rows)
    denied = uuid.uuid4()
    with pytest.raises(ForbiddenError):
        await f.ledger.create_category(Actor(viewer.user_id, ws, request_id=denied), E, "X")
    row = (
        await owner_rows(
            owner_engine,
            "SELECT action, entity_type, outcome FROM audit_events WHERE request_id = :r",
            {"r": denied},
        )
    )[0]
    assert (row.action, row.entity_type, row.outcome) == (
        "security.authz_denied", "category", "denied",
    )  # fmt: skip


# ---------------------------------------------------- R01: no constraint errors with names


@contextmanager
def _database_errors(engine: Any) -> Iterator[list[BaseException]]:
    errors: list[BaseException] = []

    def listener(context: Any) -> None:
        errors.append(context.original_exception)

    event.listen(engine.sync_engine, "handle_error", listener)
    try:
        yield errors
    finally:
        event.remove(engine.sync_engine, "handle_error", listener)


async def test_R01_duplicate_names_never_reach_the_unique_index(
    f: Finance, owner_engine: Any, app_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    kopi = await f.ledger.create_category(actor, E, "Kopi")
    teh = await f.ledger.create_category(actor, E, "Teh")
    await f.ledger.set_category_active(actor, teh.id, False)
    await f.ledger.create_category(actor, E, "Teh")  # a new active namesake
    with _database_errors(app_engine) as errors:
        with pytest.raises(DuplicateNameError):
            await f.ledger.create_category(actor, E, "KOPI")
        with pytest.raises(DuplicateNameError):
            await f.ledger.rename_category(actor, kopi.id, "teh")
        with pytest.raises(DuplicateNameError):
            await f.ledger.set_category_active(actor, teh.id, True)
        results = await asyncio.gather(
            *(f.ledger.create_category(actor, INC, "Sewa") for _ in range(4)),
            return_exceptions=True,
        )
    assert sum(isinstance(r, DuplicateNameError) for r in results) == 3
    assert errors == []  # no SQL error at all: nothing for the server log to record


# -------------------------------------------- R02: concurrent authorization and archives


async def test_R02_authorization_changes_wait_for_an_authorized_write(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    await f.ledger.balance(editor.actor(ws))  # seeded
    ids = {"u": editor.user_id, "ws": ws}
    changes = (
        "UPDATE users SET status = 'suspended' WHERE id = :u",
        "UPDATE workspace_memberships SET role = 'viewer' "
        "WHERE workspace_id = :ws AND user_id = :u",
        "UPDATE workspace_memberships SET status = 'removed', ended_at = now() "
        "WHERE workspace_id = :ws AND user_id = :u",
        "UPDATE workspaces SET status = 'pending_deletion', deletion_requested_at = now() "
        "WHERE id = :ws",
    )
    async with f.store.session(editor.user_id, ws, mode=SessionMode.WRITE):
        for sql in changes:  # each waits until the authorized write has committed
            assert await _blocked(owner_engine, sql, ids), sql
    async with f.store.session(editor.user_id, ws, mode=SessionMode.READ):
        assert not await _blocked(owner_engine, changes[1], ids)  # a read holds no locks


async def test_R02_archives_wait_for_a_posting_that_uses_the_row(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    view = await _ready_draft(f, budi.actor())
    category, account = view.category.id, view.account.id  # type: ignore[union-attr]
    async with f.store.session(budi.user_id, mode=SessionMode.WRITE) as s:
        await s.get_category(category, lock="share")
        await s.get_account(account, lock="share")
        assert await _blocked(
            owner_engine, "UPDATE categories SET archived_at = now() WHERE id = :c", {"c": category}
        )
        assert await _blocked(
            owner_engine, "UPDATE accounts SET archived_at = now() WHERE id = :a", {"a": account}
        )


async def test_R02_concurrent_archive_and_confirm_never_post_to_an_archived_category(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    for n in range(6):
        name = f"Sekali {n}"
        category = await f.ledger.create_category(actor, E, name)
        view = await f.capture.start(actor, E, amount_text="1rb", description="x")
        view = await f.capture.choose_category(actor, view.ref, category.id)
        confirm, archive = await asyncio.gather(
            f.capture.confirm(actor, view.ref),
            f.ledger.set_category_active(actor, category.id, False),
            return_exceptions=True,
        )
        assert not isinstance(archive, BaseException)
        if isinstance(confirm, BaseException):  # the archive committed first and was seen
            assert isinstance(confirm, InvalidInputError)
            assert confirm.reason == "category_archived"
    # Whatever the interleaving, the ledger holds no row the archive did not see coming.
    assert await _transactions(owner_engine) <= 6


async def test_R02_concurrent_edits_one_wins_the_other_is_stale(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    saved = (await record(f, owner.actor(ws), E, "10rb", "Makanan")).record
    results = await asyncio.gather(
        f.ledger.update_transaction(
            owner.actor(ws), saved.id, 1, TransactionChanges(amount=20_000)
        ),
        f.ledger.update_transaction(
            editor.actor(ws), saved.id, 1, TransactionChanges(amount=30_000)
        ),
        f.ledger.void_transaction(owner.actor(ws), saved.id, 1),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    # Stale, or (for the editor, who cannot see voided rows) not found once the void won.
    assert all(
        isinstance(r, ConflictError | NotFoundError)
        for r in results
        if isinstance(r, BaseException)
    )
    entries = await owner_scalar(
        owner_engine,
        "SELECT count(*) FROM transaction_entries WHERE transaction_id = :t",
        {"t": saved.id},
    )
    revisions = await owner_scalar(
        owner_engine,
        "SELECT count(*) FROM transaction_revisions WHERE transaction_id = :t",
        {"t": saved.id},
    )
    winner = next(r for r in results if not isinstance(r, BaseException))
    assert revisions == 2
    assert entries == (2 if winner.status is TxnStatus.POSTED else 1)  # type: ignore[union-attr]


# --------------------------------------------------- R03: removal and rejoin, privacy


async def test_R03_removal_and_rejoin_do_not_revive_old_conversations(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "admin"})
    actor = editor.actor(ws)
    view = await _ready_draft(f, actor)
    version = await owner_scalar(
        owner_engine, "SELECT version FROM workspace_memberships WHERE user_id = :u AND "
        "workspace_id = :ws", {"u": editor.user_id, "ws": ws},
    )  # fmt: skip
    await remove_member(owner_engine, ws, editor)
    await _owner_exec(
        owner_engine,
        "UPDATE workspace_memberships SET status = 'active', ended_at = NULL "
        "WHERE workspace_id = :ws AND user_id = :u",
        {"ws": ws, "u": editor.user_id},
    )
    assert await owner_scalar(
        owner_engine, "SELECT version FROM workspace_memberships WHERE user_id = :u AND "
        "workspace_id = :ws", {"u": editor.user_id, "ws": ws},
    ) == version + 2  # trg_membership_version: removal and rejoin  # fmt: skip
    assert await f.capture.current(actor) is None
    with pytest.raises(DraftExpiredError):
        await f.capture.confirm(actor, view.ref)
    assert await _draft_status(owner_engine, editor) == ["expired"]
    prompt = await f.capture.start_category_create(actor, E)
    await remove_member(owner_engine, ws, editor)
    await _owner_exec(
        owner_engine,
        "UPDATE workspace_memberships SET status = 'active', ended_at = NULL "
        "WHERE workspace_id = :ws AND user_id = :u",
        {"ws": ws, "u": editor.user_id},
    )
    with pytest.raises(DraftExpiredError):
        await f.capture.input_text(actor, "Kopi")
    assert (
        await owner_scalar(
            owner_engine,
            "SELECT status FROM pending_actions WHERE id = :id",
            {"id": prompt.action.id},
        )
        == "expired"
    )
    assert await _transactions(owner_engine) == 0
    assert (
        await owner_scalar(owner_engine, "SELECT count(*) FROM categories WHERE name = 'Kopi'") == 0
    )


async def test_membership_version_changes_only_with_role_or_status(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})

    async def version() -> int:
        return int(await owner_scalar(
            owner_engine,
            "SELECT version FROM workspace_memberships WHERE user_id = :u AND workspace_id = :ws",
            {"u": editor.user_id, "ws": ws},
        ))  # fmt: skip

    await f.ledger.balance(editor.actor(ws))  # seeding sets default_account_id: no bump
    assert await version() == 1
    await set_role(owner_engine, ws, editor, "viewer")
    assert await version() == 2
    await _owner_exec(  # an updater that bumps the version itself is not bumped twice
        owner_engine,
        "UPDATE workspace_memberships SET role = 'editor', version = version + 1 "
        "WHERE user_id = :u AND workspace_id = :ws",
        {"u": editor.user_id, "ws": ws},
    )
    assert await version() == 3


async def test_two_members_never_see_or_touch_each_others_drafts(
    f: Finance, owner_engine: Any
) -> None:
    ani, budi = await person(owner_engine, "A"), await person(owner_engine, "B")
    ws = await business(owner_engine, ani, {budi: "admin"})
    mine = await _ready_draft(f, ani.actor(ws))
    assert await f.capture.current(budi.actor(ws)) is None
    with pytest.raises(DraftExpiredError):
        await f.capture.confirm(budi.actor(ws), mine.ref)
    assert not await f.capture.cancel(budi.actor(ws), mine.ref)
    assert await f.capture.input_text(budi.actor(ws), "5rb") is None
    with pytest.raises(DraftExpiredError):
        await f.capture.revise(budi.actor(ws), mine.ref, DraftField.AMOUNT)
    assert await _draft_status(owner_engine, ani) == ["pending"]
    assert (await f.capture.confirm(ani.actor(ws), mine.ref)).already is False
