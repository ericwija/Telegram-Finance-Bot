"""Finance services over the in-memory store (fast; the same rules run on PostgreSQL in
``tests/integration/test_ledger_service_db.py``, which also covers RLS and isolation)."""

import uuid
from datetime import date

import pytest

from finbot.authz.policy import ForbiddenError
from finbot.domain.finance import (
    ConflictError,
    DraftExpiredError,
    DuplicateNameError,
    EditField,
    HistoryFilter,
    InvalidInputError,
    Month,
    NotFoundError,
    PendingKind,
    StepError,
    TransactionChanges,
)
from finbot.domain.identity import UserStatus
from finbot.domain.ledger import TxnStatus, TxnType
from finbot.services.capture import CaptureService, CategoryDone, CategoryPrompt
from finbot.services.finance import Actor
from finbot.services.ledger import LedgerService
from tests.support.fake_finance import FakeFinanceStore
from tests.support.finance import Clock

pytestmark = pytest.mark.asyncio
E, INC = TxnType.EXPENSE, TxnType.INCOME


class World:
    def __init__(self) -> None:
        self.store = FakeFinanceStore()
        self.clock = Clock()
        self.ledger = LedgerService(self.store, clock=self.clock)
        self.capture = CaptureService(self.store, clock=self.clock)
        user, self.ws = self.store.add_person()
        self.actor = Actor(user)

    async def save(
        self,
        txn_type: TxnType,
        amount: str,
        category: str,
        description: str = "x",
        actor: Actor | None = None,
    ) -> object:
        actor = actor or self.actor
        view = await self.capture.start(
            actor, txn_type, amount_text=amount, description=description
        )
        view = await self.capture.choose_category(
            actor, view.ref, next(c.id for c in view.categories if c.name == category)
        )
        return (await self.capture.confirm(actor, view.ref)).record


@pytest.fixture
def w() -> World:
    return World()


async def test_seeding_is_idempotent(w: World) -> None:
    await w.ledger.balance(w.actor)
    await w.ledger.balance(w.actor)
    assert len((await w.ledger.categories(w.actor)).categories) == 15
    assert [a.name for a in (await w.ledger.balance(w.actor)).report.accounts] == ["Kas"]
    assert sum(a["action"] == "category.created" for a in w.store.state.audit) == 15


async def test_capture_confirm_twice_and_conversation_states(w: World) -> None:
    view = await w.capture.start(w.actor)
    with pytest.raises(StepError):
        await w.capture.input_text(w.actor, "25000")  # the type comes from a button
    view = await w.capture.choose_type(w.actor, view.ref, E)
    view = await w.capture.input_text(w.actor, "25rb")  # type: ignore[assignment]
    current = await w.capture.current(w.actor)
    assert current is not None
    view = await w.capture.choose_category(w.actor, view.ref, view.categories[0].id)
    view = await w.capture.skip_description(w.actor, view.ref)
    first = await w.capture.confirm(w.actor, view.ref)
    second = await w.capture.confirm(w.actor, view.ref)
    assert (first.already, second.already, first.record.id) == (False, True, second.record.id)
    assert len(w.store.state.transactions) == 1
    assert await w.capture.current(w.actor) is None


async def test_expired_and_stale_drafts(w: World) -> None:
    old = await w.capture.start(w.actor, E)
    new = await w.capture.start(w.actor, INC)
    with pytest.raises(DraftExpiredError):
        await w.capture.choose_type(w.actor, old.ref, E)
    w.clock.advance(hours=25)
    with pytest.raises(DraftExpiredError):
        await w.capture.input_text(w.actor, "1000")
    with pytest.raises(DraftExpiredError):
        await w.capture.confirm(w.actor, new.ref)
    assert await w.capture.current(w.actor) is None
    prompt = await w.capture.start_category_create(w.actor, E)
    w.clock.advance(hours=1)
    with pytest.raises(DraftExpiredError):
        await w.capture.input_text(w.actor, "Kopi")
    assert prompt.action.kind is PendingKind.CATEGORY_CREATE


async def test_category_prompts_through_text(w: World) -> None:
    prompt = await w.capture.start_category_create(w.actor, E)
    assert isinstance(await w.capture.current(w.actor), CategoryPrompt)
    done = await w.capture.input_text(w.actor, "Kopi")
    assert isinstance(done, CategoryDone)
    rename = await w.capture.start_category_rename(w.actor, done.category.id)
    assert rename.category is not None
    renamed = await w.capture.input_text(w.actor, "Kopi Susu")
    assert isinstance(renamed, CategoryDone)
    assert renamed.category.name == "Kopi Susu"
    with pytest.raises(NotFoundError):
        await w.capture.start_category_rename(w.actor, uuid.uuid4())
    with pytest.raises(InvalidInputError):
        await w.capture.start_category_create(w.actor, TxnType.TRANSFER)
    assert prompt.workspace.name == "Pribadi"
    await w.capture.start_category_create(w.actor, INC)
    assert await w.capture.cancel(w.actor)


async def test_categories(w: World) -> None:
    kopi = await w.ledger.create_category(w.actor, E, "Kopi")
    with pytest.raises(DuplicateNameError):
        await w.ledger.create_category(w.actor, E, "KOPI")
    with pytest.raises(InvalidInputError, match="no_changes"):
        await w.ledger.rename_category(w.actor, kopi.id, "Kopi")
    assert (await w.ledger.category(w.actor, kopi.id)) == (kopi, True)
    assert (await w.ledger.set_category_active(w.actor, kopi.id, False)).archived
    assert (await w.ledger.set_category_active(w.actor, kopi.id, False)).archived  # no-op
    listing = await w.ledger.categories(w.actor, E, archived=True)
    assert [c.name for c in listing.categories] == ["Kopi"]
    with pytest.raises(NotFoundError):
        await w.ledger.category(w.actor, uuid.uuid4())
    with pytest.raises(NotFoundError):
        await w.ledger.set_category_active(w.actor, uuid.uuid4(), True)
    with pytest.raises(InvalidInputError):
        await w.ledger.categories(w.actor, TxnType.TRANSFER)


async def test_edits_void_restore_and_reports(w: World) -> None:
    saved = await w.save(E, "50rb", "Belanja")
    view = await w.capture.start_edit(w.actor, saved.id, 1, EditField.AMOUNT)  # type: ignore[attr-defined]
    assert view.target is not None
    view = await w.capture.input_text(w.actor, "40rb")  # type: ignore[assignment]
    edited = (await w.capture.confirm(w.actor, view.ref)).record
    assert (edited.amount, edited.entry_revision) == (40_000, 2)
    date_edit = await w.capture.start_edit(w.actor, edited.id, edited.version, EditField.DATE)
    date_edit = await w.capture.input_text(w.actor, "01/10/2026")  # type: ignore[assignment]
    dated = (await w.capture.confirm(w.actor, date_edit.ref)).record
    assert dated.txn_date == date(2026, 10, 1)
    with pytest.raises(ConflictError, match="stale"):
        await w.capture.start_edit(w.actor, edited.id, 1, EditField.CATEGORY)
    with pytest.raises(NotFoundError):
        await w.capture.start_edit(w.actor, uuid.uuid4(), 1, EditField.CATEGORY)
    await w.save(INC, "1jt", "Gaji")
    summary = (await w.ledger.summary(w.actor)).summary
    assert [(t.income, t.expense) for t in summary.totals] == [(1_000_000, 40_000)]
    assert summary.month == Month(2026, 10)
    voided = await w.ledger.void_transaction(w.actor, dated.id, dated.version)
    assert voided.status is TxnStatus.VOIDED
    with pytest.raises(ConflictError):
        await w.capture.start_edit(w.actor, dated.id, voided.version, EditField.AMOUNT)
    with pytest.raises(ConflictError, match="not_voided"):
        await w.ledger.restore_transaction(w.actor, (await w.save(E, "1", "Lainnya")).id, 1)  # type: ignore[attr-defined]
    detail = await w.ledger.transaction(w.actor, dated.id)
    assert (detail.can_restore, detail.can_delete) == (True, False)
    restored = await w.ledger.restore_transaction(w.actor, dated.id, voided.version)
    assert restored.status is TxnStatus.POSTED
    balance = (await w.ledger.balance(w.actor)).report
    assert balance.recorded_balance() == {"IDR": 1_000_000 - 40_001}
    with pytest.raises(InvalidInputError):
        await w.ledger.summary(w.actor, Month(2027, 1))


async def test_history_pages_and_filters(w: World) -> None:
    for n in range(12):
        await w.save(E, f"{n + 1}rb", "Makanan")
    first = await w.ledger.history(w.actor, HistoryFilter())
    second = await w.ledger.history(w.actor, HistoryFilter(), 1)
    assert (len(first.page.items), first.page.has_next, len(second.page.items)) == (10, True, 2)
    makanan = next(
        c for c in (await w.ledger.categories(w.actor, E)).categories if c.name == "Makanan"
    )
    filtered = await w.ledger.history(w.actor, HistoryFilter(category_id=makanan.id))
    assert filtered.category == makanan
    with pytest.raises(NotFoundError):
        await w.ledger.history(w.actor, HistoryFilter(category_id=uuid.uuid4()))


async def test_roles_denials_downgrade_and_read_only(w: World) -> None:
    viewer, editor = uuid.uuid4(), uuid.uuid4()
    for user in (viewer, editor):
        w.store.state.users[user] = UserStatus.ACTIVE
    ws = w.store.add_business({w.actor.user_id: "owner", viewer: "viewer", editor: "editor"})
    with pytest.raises(ForbiddenError):
        await w.capture.start(Actor(viewer, ws), E)
    assert w.store.denials[-1]["permission"] == "transaction.create"
    view = await w.capture.start(Actor(editor, ws), E, amount_text="5000")
    w.store.state.workspaces[ws].members[editor]["role"] = "viewer"
    with pytest.raises(ForbiddenError):
        await w.capture.choose_category(Actor(editor, ws), view.ref, view.categories[0].id)
    draft = next(iter(d for d in w.store.state.drafts.values() if d["user"] == editor))
    assert draft["status"].value == "expired"  # R-9
    w.store.state.workspaces[ws].status = "pending_deletion"
    with pytest.raises(ForbiddenError):
        await w.ledger.create_category(Actor(w.actor.user_id, ws), E, "X")
    await w.ledger.history(Actor(w.actor.user_id, ws), HistoryFilter())  # reads still work
    w.store.state.users[w.actor.user_id] = UserStatus.SUSPENDED
    with pytest.raises(ForbiddenError):
        await w.ledger.balance(w.actor)
    with pytest.raises(NotFoundError):
        await w.ledger.balance(Actor(uuid.uuid4()))


async def test_confirm_refuses_a_draft_whose_target_changed(w: World) -> None:
    saved = await w.save(E, "10rb", "Makanan")
    view = await w.capture.start_edit(w.actor, saved.id, 1, EditField.DESCRIPTION)  # type: ignore[attr-defined]
    await w.ledger.update_transaction(w.actor, saved.id, 1, TransactionChanges(description="y"))  # type: ignore[attr-defined]
    view = await w.capture.skip_description(w.actor, view.ref)
    with pytest.raises(ConflictError):
        await w.capture.confirm(w.actor, view.ref)
    status = next(d for d in w.store.state.drafts.values() if d["target"] == saved.id)["status"]  # type: ignore[attr-defined]
    assert status.value == "cancelled"
