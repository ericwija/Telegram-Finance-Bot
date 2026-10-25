"""Step 6C regressions on a real PostgreSQL 18.6 as ``finbot_app`` (second independent review:
F01, F06, F10, F12, N01, N02; docs/10 §14).

Orderings are made deterministic with real PostgreSQL locks: one side holds a row lock in an
open transaction, the other side is started and observed waiting in ``pg_locks`` before the
first side commits. Service steps are paused with ``asyncio.Event`` hooks where an interleaving
inside the application matters (N01).
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from finbot.adapters.telegram import finance_callbacks as cb
from finbot.adapters.telegram import finance_render as render
from finbot.adapters.telegram import messages_id as msg
from finbot.authz.policy import ForbiddenError
from finbot.domain.finance import (
    CATEGORY_MAX_PAGES,
    CATEGORY_QUOTA_PER_KIND,
    DEFAULT_CATEGORIES,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    TransactionChanges,
)
from finbot.domain.ledger import TxnStatus, TxnType
from finbot.infra.db.connection import create_engine
from finbot.infra.db.finance import PgFinanceSession
from tests.integration.test_telegram_finance_db import Bot, button
from tests.plugins.db import _target
from tests.support import pg
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


@pytest.fixture
def f(migrated_database: str, app_engine: Any) -> Finance:
    return finance(app_engine)


async def _exec(owner_engine: Any, sql: str, params: dict[str, Any] | None = None) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(text(sql), params or {})


async def _ready(f: Finance, actor: Any, txn_type: TxnType = E) -> Any:
    view = await f.capture.start(actor, txn_type, amount_text="10rb", description="x")
    return await f.capture.choose_category(actor, view.ref, view.categories[0].id)


async def _waiting(owner_engine: Any, at_least: int = 1) -> None:
    """Until at least ``at_least`` sessions wait for a lock (``pg_locks`` is visible to all)."""
    async with asyncio.timeout(10):
        while True:
            n = await owner_scalar(owner_engine, "SELECT count(*) FROM pg_locks WHERE NOT granted")
            if n >= at_least:
                return
            await asyncio.sleep(0.02)


@asynccontextmanager
async def _held(owner_engine: Any, sql: str, params: dict[str, Any]) -> AsyncIterator[Any]:
    """An owner transaction that has run ``sql`` (taking its locks) and commits on exit."""
    async with owner_engine.connect() as conn:
        tx = await conn.begin()
        await conn.execute(text(sql), params)
        try:
            yield conn
        finally:
            await tx.commit()


def _pause(target: Any, name: str) -> tuple[asyncio.Event, asyncio.Event, Callable[[], None]]:
    """Pause ``target.name`` (an async method) before it runs: (reached, release, undo)."""
    reached, release = asyncio.Event(), asyncio.Event()
    original = getattr(target, name)

    async def paused(*args: Any, **kwargs: Any) -> Any:
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    setattr(target, name, paused)
    return reached, release, lambda: setattr(target, name, original)


async def _status(owner_engine: Any, table: str, row_id: uuid.UUID) -> str:
    return str(
        await owner_scalar(
            owner_engine,
            f"SELECT status FROM {table} WHERE id = :id",  # noqa: S608 - test constant
            {"id": row_id},
        )
    )


async def _denials(owner_engine: Any, user: Person) -> list[tuple[str, str]]:
    rows = await owner_rows(
        owner_engine,
        "SELECT entity_type, metadata->>'permission' AS p FROM audit_events "
        "WHERE action = 'security.authz_denied' AND actor_user_id = :u ORDER BY occurred_at, id",
        {"u": user.user_id},
    )
    return [(r.entity_type, r.p) for r in rows]


# ------------------------------------------------- N01: denial cleanup targets one conversation


async def test_N01_a_paused_denial_cleanup_never_expires_the_newer_draft(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    actor = editor.actor(ws)
    old = await _ready(f, actor)
    await set_role(owner_engine, ws, editor, "viewer")
    reached, release, undo = _pause(f.capture, "_invalidate")
    try:
        denied = asyncio.create_task(f.capture.confirm(actor, old.ref))  # request A
        await reached.wait()  # A was refused and rolled back; its cleanup has not run yet
        await set_role(owner_engine, ws, editor, "editor")  # authorization restored
        undo()  # B runs normally
        newer = await _ready(f, actor)  # request B: a valid new draft (replaces the old one)
        release.set()
        with pytest.raises(ForbiddenError):
            await denied
    finally:
        undo()
        release.set()
    assert await _status(owner_engine, "drafts", old.id) == "cancelled"  # replaced by B
    assert await _status(owner_engine, "drafts", newer.id) == "pending"  # untouched by A
    saved = await f.capture.confirm(actor, newer.ref)  # and still usable
    assert saved.already is False


async def test_N01_cleanup_expires_the_refused_draft_when_nothing_replaced_it(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    view = await _ready(f, editor.actor(ws))
    await set_role(owner_engine, ws, editor, "viewer")
    with pytest.raises(ForbiddenError):
        await f.capture.confirm(editor.actor(ws), view.ref)
    assert await _status(owner_engine, "drafts", view.id) == "expired"
    assert await _denials(owner_engine, editor) == [("draft", "transaction.create")]


async def test_N01_category_prompt_cleanup_is_targeted_too(f: Finance, owner_engine: Any) -> None:
    owner, admin = await person(owner_engine, "O"), await person(owner_engine, "A")
    ws = await business(owner_engine, owner, {admin: "admin"})
    actor = admin.actor(ws)
    old = await f.capture.start_category_create(actor, E)
    await set_role(owner_engine, ws, admin, "editor")  # loses category.manage
    reached, release, undo = _pause(f.capture, "_invalidate")
    try:
        denied = asyncio.create_task(f.capture.input_text(actor, "Kopi"))
        await reached.wait()
        await set_role(owner_engine, ws, admin, "admin")
        undo()
        newer = await f.capture.start_category_create(actor, INC)
        release.set()
        with pytest.raises(ForbiddenError):
            await denied
    finally:
        undo()
        release.set()
    assert await _status(owner_engine, "pending_actions", old.action.id) == "cancelled"
    assert await _status(owner_engine, "pending_actions", newer.action.id) == "pending"
    done = await f.capture.input_text(actor, "Dividen")
    assert done is not None
    assert await _denials(owner_engine, admin) == [("category", "category.manage")]


# ------------------------------------------------------- F01: cancellation authorization


async def test_F01_cancel_after_downgrade_is_refused_and_invalidates_only_that_draft(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    view = await _ready(f, editor.actor(ws))
    mine = await _ready(f, owner.actor(ws))  # another member's conversation
    await set_role(owner_engine, ws, editor, "viewer")
    with pytest.raises(ForbiddenError):
        await f.capture.cancel(editor.actor(ws))  # /batal: refused, the draft invalidated
    assert await f.capture.cancel(editor.actor(ws), view.ref) is False  # nothing left
    assert await _status(owner_engine, "drafts", view.id) == "expired"  # invalidated
    assert await _status(owner_engine, "drafts", mine.id) == "pending"
    assert await _denials(owner_engine, editor) == [("draft", "transaction.create")]


async def test_F01_cancel_after_suspension_removal_or_pending_deletion(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    actor = editor.actor(ws)
    view = await _ready(f, actor)
    await _exec(
        owner_engine, "UPDATE users SET status = 'suspended' WHERE id = :u", {"u": editor.user_id}
    )
    with pytest.raises(ForbiddenError):  # refused before any conversation is read
        await f.capture.cancel(actor, view.ref)
    assert await _status(owner_engine, "drafts", view.id) == "pending"  # unusable, unchanged
    await _exec(
        owner_engine, "UPDATE users SET status = 'active' WHERE id = :u", {"u": editor.user_id}
    )
    await _exec(
        owner_engine,
        "UPDATE workspaces SET status = 'pending_deletion', deletion_requested_at = now() "
        "WHERE id = :ws",
        {"ws": ws},
    )
    with pytest.raises(ForbiddenError):  # read-only workspace: transaction.create is refused
        await f.capture.cancel(actor, view.ref)
    assert await _status(owner_engine, "drafts", view.id) == "expired"
    await _exec(
        owner_engine,
        "UPDATE workspaces SET status = 'active', deletion_requested_at = NULL WHERE id = :ws",
        {"ws": ws},
    )
    second = await _ready(f, actor)
    await remove_member(owner_engine, ws, editor)
    with pytest.raises(NotFoundError):
        await f.capture.cancel(actor, second.ref)
    assert await _status(owner_engine, "drafts", second.id) == "pending"  # invisible now
    await _exec(
        owner_engine,
        "UPDATE workspace_memberships SET status = 'active', ended_at = NULL "
        "WHERE workspace_id = :ws AND user_id = :u",
        {"ws": ws, "u": editor.user_id},
    )
    assert await f.capture.cancel(actor, second.ref) is False  # rejoined: stale epoch
    assert await _status(owner_engine, "drafts", second.id) == "expired"


async def test_F01_cancel_of_a_category_prompt_needs_category_manage(
    f: Finance, owner_engine: Any
) -> None:
    owner, admin = await person(owner_engine, "O"), await person(owner_engine, "A")
    ws = await business(owner_engine, owner, {admin: "admin"})
    prompt = await f.capture.start_category_create(admin.actor(ws), E)
    await set_role(owner_engine, ws, admin, "editor")
    with pytest.raises(ForbiddenError):
        await f.capture.cancel(admin.actor(ws), prompt.ref)
    assert await _status(owner_engine, "pending_actions", prompt.action.id) == "expired"
    assert await _denials(owner_engine, admin) == [("category", "category.manage")]


async def test_F01_cancel_waits_for_a_concurrent_role_change_and_sees_it(
    f: Finance, owner_engine: Any
) -> None:
    owner, editor = await person(owner_engine, "O"), await person(owner_engine, "E")
    ws = await business(owner_engine, owner, {editor: "editor"})
    view = await _ready(f, editor.actor(ws))
    ids = {"ws": ws, "u": editor.user_id}
    async with _held(
        owner_engine,
        "UPDATE workspace_memberships SET role = 'viewer' "
        "WHERE workspace_id = :ws AND user_id = :u",
        ids,
    ):
        cancel = asyncio.create_task(f.capture.cancel(editor.actor(ws), view.ref))
        await _waiting(owner_engine)  # blocked on the membership row (FOR SHARE)
        assert not cancel.done()
    with pytest.raises(ForbiddenError):  # evaluated with the committed new role
        await cancel
    assert await _status(owner_engine, "drafts", view.id) == "expired"


# --------------------------------------------------------- F12: logs ↔ audit correlation


async def test_F12_every_telegram_request_logs_one_line_whose_id_is_on_its_audit_events(
    migrated_database: str, app_engine: Any, owner_engine: Any, caplog: pytest.LogCaptureFixture
) -> None:
    bot = Bot(app_engine)
    await bot.app.initialize()
    try:
        budi = await person(owner_engine)
        with caplog.at_level(logging.INFO, logger="finbot"):
            _, buttons = await bot.send(budi, "/pengeluaran 25rb ZZKOPIZZ")
            _, buttons = await bot.press(budi, button(buttons, "Makanan"))
            await bot.press(budi, button(buttons, msg.BUTTON_SAVE))
            await bot.press(budi, f"f:tx:{uuid.uuid4()}")  # refused: not found
    finally:
        await bot.app.shutdown()
    requests = [r for r in caplog.records if getattr(r, "event", None) == "finance.request"]
    assert [(r.component, r.outcome) for r in requests] == [  # type: ignore[attr-defined]
        ("start", "ok"), ("button.ca", "ok"), ("button.ok", "ok"), ("button.tx", "refused"),
    ]  # fmt: skip
    assert len({r.request_id for r in requests}) == 4  # type: ignore[attr-defined]
    confirm = requests[2].request_id  # type: ignore[attr-defined]
    actions = await owner_rows(
        owner_engine,
        "SELECT action FROM audit_events WHERE request_id = :r ORDER BY occurred_at, id",
        {"r": confirm},
    )
    assert [a.action for a in actions] == ["transaction.created"]
    first = requests[0].request_id  # type: ignore[attr-defined]
    seeded = await owner_scalar(
        owner_engine, "SELECT count(*) FROM audit_events WHERE request_id = :r", {"r": first}
    )
    assert seeded == sum(len(v) for v in DEFAULT_CATEGORIES.values()) + 1  # first-use seeding
    assert "ZZKOPIZZ" not in caplog.text
    assert "25.000" not in caplog.text
    assert "25rb" not in caplog.text


async def test_F12_denials_are_classified_by_the_refused_object(
    f: Finance, owner_engine: Any
) -> None:
    owner, admin = await person(owner_engine, "O"), await person(owner_engine, "A")
    ws = await business(owner_engine, owner, {admin: "admin"})
    actor = admin.actor(ws)
    saved = (await record(f, owner.actor(ws), E, "5rb", "Makanan")).record
    draft = await _ready(f, actor)
    await set_role(owner_engine, ws, admin, "viewer")
    with pytest.raises(ForbiddenError):
        await f.capture.skip_description(actor, draft.ref)  # a draft step
    with pytest.raises(ForbiddenError):
        await f.ledger.void_transaction(actor, saved.id, saved.version)
    with pytest.raises(ForbiddenError):
        await f.ledger.create_category(actor, E, "X")
    assert await _denials(owner_engine, admin) == [
        ("draft", "transaction.create"),
        ("transaction", "transaction.delete"),
        ("category", "category.manage"),
    ]


# --------------------------------------------------------- financial integrity


async def test_an_injected_failure_mid_confirm_rolls_everything_back(
    f: Finance, owner_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    budi = await person(owner_engine)
    view = await _ready(f, budi.actor())

    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("injected after the header and entries were written")

    monkeypatch.setattr(PgFinanceSession, "insert_revision", boom)
    with pytest.raises(RuntimeError, match="injected"):
        await f.capture.confirm(budi.actor(), view.ref)
    monkeypatch.undo()
    for table in ("transactions", "transaction_entries", "transaction_revisions"):
        assert await owner_scalar(owner_engine, f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    assert (
        await owner_scalar(
            owner_engine, "SELECT count(*) FROM audit_events WHERE action = 'transaction.created'"
        )
        == 0
    )
    assert await _status(owner_engine, "drafts", view.id) == "pending"
    saved = await f.capture.confirm(budi.actor(), view.ref)  # the retry stores exactly one
    assert saved.already is False
    assert await owner_scalar(owner_engine, "SELECT count(*) FROM transactions") == 1


async def test_confirm_replay_after_the_services_are_rebuilt(
    f: Finance, owner_engine: Any, pg_cluster: pg.ClusterInfo, migrated_database: str
) -> None:
    budi = await person(owner_engine)
    view = await _ready(f, budi.actor())
    first = await f.capture.confirm(budi.actor(), view.ref)
    engine = create_engine(_target(pg_cluster, migrated_database, pg.APP_ROLE))  # a new process
    try:
        again = await finance(engine).capture.confirm(budi.actor(), view.ref)
    finally:
        await engine.dispose()
    assert (again.already, again.record.id) == (True, first.record.id)
    assert await owner_scalar(owner_engine, "SELECT count(*) FROM transactions") == 1


async def test_archive_committed_first_is_seen_by_a_waiting_confirm(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    view = await _ready(f, budi.actor())
    category = view.category.id
    async with _held(
        owner_engine, "UPDATE categories SET archived_at = now() WHERE id = :c", {"c": category}
    ):
        confirm = asyncio.create_task(f.capture.confirm(budi.actor(), view.ref))
        await _waiting(owner_engine)  # the posting waits for the archive (FOR SHARE)
        assert not confirm.done()
    with pytest.raises(InvalidInputError, match="category_archived"):
        await confirm
    assert await owner_scalar(owner_engine, "SELECT count(*) FROM transactions") == 0


async def test_a_posting_in_progress_makes_the_archive_wait(
    f: Finance, owner_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    budi = await person(owner_engine)
    view = await _ready(f, budi.actor())
    category = view.category.id
    reached, release = asyncio.Event(), asyncio.Event()
    original = PgFinanceSession.insert_revision

    async def paused(self: PgFinanceSession, *args: Any, **kwargs: Any) -> None:
        reached.set()
        await release.wait()  # inside the confirm transaction, category locked FOR SHARE
        await original(self, *args, **kwargs)

    monkeypatch.setattr(PgFinanceSession, "insert_revision", paused)
    confirm = asyncio.create_task(f.capture.confirm(budi.actor(), view.ref))
    await reached.wait()
    archive = asyncio.create_task(f.ledger.set_category_active(budi.actor(), category, False))
    await _waiting(owner_engine)
    assert not archive.done()  # the archive waits for the posting
    release.set()
    saved = (await confirm).record
    assert (await archive).archived
    row = (
        await owner_rows(
            owner_engine,
            "SELECT t.category_id, t.created_at <= c.archived_at AS before FROM transactions t "
            "JOIN categories c ON c.id = t.category_id WHERE t.id = :t",
            {"t": saved.id},
        )
    )[0]
    assert (row.category_id, row.before) == (category, True)  # posted, then archived


async def _ledger_invariants(owner_engine: Any, transaction_id: uuid.UUID) -> None:
    """Versions and entry revisions are contiguous; one leg per revision; sums match."""
    header = (
        await owner_rows(
            owner_engine,
            "SELECT version, entry_revision, amount, status FROM transactions WHERE id = :t",
            {"t": transaction_id},
        )
    )[0]
    versions = [
        r.version
        for r in await owner_rows(
            owner_engine,
            "SELECT version FROM transaction_revisions WHERE transaction_id = :t ORDER BY version",
            {"t": transaction_id},
        )
    ]
    assert versions == list(range(1, header.version + 1))
    entries = await owner_rows(
        owner_engine,
        "SELECT revision, count(*) AS legs, sum(amount) AS total FROM transaction_entries "
        "WHERE transaction_id = :t GROUP BY revision ORDER BY revision",
        {"t": transaction_id},
    )
    assert [e.revision for e in entries] == list(range(1, header.entry_revision + 1))
    assert all(e.legs == 1 for e in entries)
    assert entries[-1].total == -header.amount  # an expense: one negative leg
    balance = await owner_scalar(
        owner_engine,
        "SELECT coalesce(sum(amount), 0) FROM current_entries WHERE transaction_id = :t "
        "AND status = 'posted'",
        {"t": transaction_id},
    )
    assert balance == (-header.amount if header.status == "posted" else 0)


async def test_edit_versus_delete_one_wins_and_the_ledger_stays_consistent(
    f: Finance, owner_engine: Any
) -> None:
    owner = await person(owner_engine, "O")
    saved = (await record(f, owner.actor(), E, "10rb", "Makanan")).record
    async with _held(
        owner_engine, "SELECT 1 FROM transactions WHERE id = :t FOR UPDATE", {"t": saved.id}
    ):
        edit = asyncio.create_task(
            f.ledger.update_transaction(
                owner.actor(), saved.id, 1, TransactionChanges(amount=20_000)
            )
        )
        void = asyncio.create_task(f.ledger.void_transaction(owner.actor(), saved.id, 1))
        await _waiting(owner_engine, at_least=2)  # both queue on the same row
    results = await asyncio.gather(edit, void, return_exceptions=True)
    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], ConflictError)
    assert str(losers[0]) in ("stale", "voided")
    final = winners[0]
    assert final.version == 2
    assert final.status in (TxnStatus.POSTED, TxnStatus.VOIDED)
    await _ledger_invariants(owner_engine, saved.id)


async def test_RLS_two_active_members_of_one_workspace_never_reach_each_others_drafts(
    f: Finance, owner_engine: Any, app_engine: Any
) -> None:
    ani, budi = await person(owner_engine, "A"), await person(owner_engine, "B")
    ws = await business(owner_engine, ani, {budi: "admin"})
    mine = await _ready(f, ani.actor(ws))
    await _ready(f, budi.actor(ws))
    async with app_engine.begin() as conn:  # raw SQL as finbot_app, Budi's context
        await conn.execute(
            text("SELECT set_config('app.user_id', :u, true)"), {"u": str(budi.user_id)}
        )
        await conn.execute(text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(ws)})
        users = {r[0] for r in (await conn.execute(text("SELECT user_id FROM drafts"))).all()}
        assert users == {budi.user_id}
        changed = await conn.execute(
            text("UPDATE drafts SET status = 'cancelled' WHERE id = :d"), {"d": mine.id}
        )
        assert changed.rowcount == 0

    async def forge() -> None:
        async with app_engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.user_id', :u, true)"), {"u": str(budi.user_id)}
            )
            await conn.execute(
                text("SELECT set_config('app.workspace_id', :w, true)"), {"w": str(ws)}
            )
            await conn.execute(
                text(
                    "INSERT INTO drafts (workspace_id, user_id, proposal, source, expires_at) "
                    "VALUES (:w, :u, '{}', 'manual', now())"
                ),
                {"w": ws, "u": ani.user_id},
            )

    with pytest.raises(DBAPIError, match="row-level security"):
        await forge()
    assert await _status(owner_engine, "drafts", mine.id) == "pending"


# ------------------------------------------------------------- F06: quota and paging


async def _fill(owner_engine: Any, who: Person, kind: TxnType, up_to: int) -> None:
    """Insert synthetic categories as the owner until the kind has ``up_to`` of them."""
    await _exec(
        owner_engine,
        "INSERT INTO categories (workspace_id, kind, name, created_by) "
        "SELECT :ws, :kind, 'Isi ' || lpad(g::text, 3, '0'), :u FROM generate_series(1, "
        "(:n - (SELECT count(*) FROM categories WHERE workspace_id = :ws AND kind = :kind))) g",
        {"ws": who.personal_ws, "kind": kind.value, "u": who.user_id, "n": up_to},
    )


async def test_F06_the_quota_is_enforced_by_the_service_and_the_database(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    await f.ledger.balance(budi.actor())  # seeded
    await _fill(owner_engine, budi, E, CATEGORY_QUOTA_PER_KIND - 1)
    results = await asyncio.gather(
        *(f.ledger.create_category(budi.actor(), E, f"Baru {n}") for n in range(4)),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1  # exactly the 200th
    assert all(
        isinstance(r, InvalidInputError) and r.reason == "category_limit"
        for r in results
        if isinstance(r, BaseException)
    )
    with pytest.raises(DBAPIError, match="category quota of 200 per kind reached"):
        await _exec(
            owner_engine,
            "INSERT INTO categories (workspace_id, kind, name, created_by) "
            "VALUES (:ws, 'expense', 'Langsung', :u)",
            {"ws": budi.personal_ws, "u": budi.user_id},
        )
    assert (
        await owner_scalar(
            owner_engine,
            "SELECT count(*) FROM categories WHERE workspace_id = :ws AND kind = 'expense'",
            {"ws": budi.personal_ws},
        )
        == CATEGORY_QUOTA_PER_KIND
    )
    await f.ledger.create_category(budi.actor(), INC, "Pemasukan lain")  # the other kind: free


async def test_F06_every_page_at_full_quota_is_reachable_and_every_button_parses(
    f: Finance, owner_engine: Any
) -> None:
    budi = await person(owner_engine)
    actor = budi.actor()
    await f.ledger.balance(actor)
    await _fill(owner_engine, budi, E, CATEGORY_QUOTA_PER_KIND)
    await _fill(owner_engine, budi, INC, CATEGORY_QUOTA_PER_KIND)
    await _exec(  # some deactivated ones, to page the archived view as well
        owner_engine,
        "UPDATE categories SET archived_at = now() WHERE workspace_id = :ws AND name LIKE 'Isi 0%'",
        {"ws": budi.personal_ws},
    )
    for kind, archived in ((None, False), (E, False), (INC, True), (None, True)):
        seen: list[tuple[TxnType, str]] = []
        page, pages = 0, 0
        while True:
            listing = await f.ledger.categories(actor, kind, archived=archived, page=page)
            text_, keyboard = render.render_categories(listing)
            assert render.fits_telegram(text_, keyboard)
            parsed = [cb.parse(d) for row in keyboard for _, d in row]  # every button is valid
            seen += [(c.kind, c.name) for c in listing.categories]
            pages += 1
            nxt = [p for p in parsed if p.action == "kl" and p.page == page + 1]
            if not nxt:
                break
            page = nxt[0].page
        assert len(seen) == len(set(seen))
        assert pages <= CATEGORY_MAX_PAGES
        if not archived:
            assert any(
                name == "Lainnya" for _, name in seen
            )  # the fallback is on the last page, which is reachable
    view = await f.capture.start(actor, E, amount_text="1rb")
    offered = []
    while True:
        offered += [c.name for c in view.categories]
        _, keyboard = render.render_draft(view)
        [cb.parse(d) for row in keyboard for _, d in row]
        if not view.categories_has_next:
            break
        view = await f.capture.category_page(actor, view.ref, view.category_page + 1)
    assert "Lainnya" in offered
    assert len(offered) == await owner_scalar(
        owner_engine,
        "SELECT count(*) FROM categories WHERE workspace_id = :ws AND kind = 'expense' "
        "AND archived_at IS NULL",
        {"ws": budi.personal_ws},
    )


# ----------------------------------------------------- R01: the per-role log verbosity


async def test_R01_terse_error_verbosity_for_the_app_role_only(
    migrated_database: str, pg_cluster: pg.ClusterInfo, app_engine: Any, owner_engine: Any
) -> None:
    import asyncpg  # noqa: PLC0415

    superuser = await asyncpg.connect(
        host=pg_cluster.host,
        port=pg_cluster.port,
        user=pg.SUPERUSER,
        password=pg_cluster.password(pg.SUPERUSER),
        database=migrated_database,
    )
    try:  # the exact statement of docs/10 §14.6, with this test database's name
        await superuser.execute(
            f'ALTER ROLE finbot_app IN DATABASE "{migrated_database}" '
            "SET log_error_verbosity = 'terse'"
        )
    finally:
        await superuser.close()
    async with app_engine.connect() as conn:
        assert (await conn.execute(text("SHOW log_error_verbosity"))).scalar() == "terse"
    async with owner_engine.connect() as conn:
        assert (await conn.execute(text("SHOW log_error_verbosity"))).scalar() == "default"
        setting = (
            await conn.execute(
                text(
                    "SELECT s.setconfig FROM pg_db_role_setting s "
                    "JOIN pg_database d ON d.oid = s.setdatabase "
                    "WHERE d.datname = current_database() AND s.setrole = 'finbot_app'::regrole"
                )
            )
        ).scalar()
    assert setting == ["log_error_verbosity=terse"]
