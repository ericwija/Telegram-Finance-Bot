"""Guided capture and edit conversations, persisted as drafts (FR-TXN-04..06, FR-TXN-08, -10).

The conversation state lives in PostgreSQL (NFR-AVL-05), never in process memory:

* a ``create`` draft walks type → amount → category → description → confirm (the date can be
  changed before confirming); an ``edit`` draft changes one field of one posted transaction;
* each user has at most one pending draft and one pending action per workspace (unique
  indexes); starting a new conversation cancels the previous one;
* buttons carry only a short draft reference. The draft itself is always the caller's own
  pending draft in the active workspace (RLS: ``user_id = app_user_id()``), and a reference
  that does not match it is stale and refused, so an old keyboard can never act on a newer
  draft (docs/09 L1);
* **confirm is idempotent** (FR-TXN-06, AT-TXN-IDEMP-01): the draft row is locked ``FOR
  UPDATE`` and moves ``pending → confirmed`` in the same transaction that writes the ledger,
  so a double tap or a Telegram retry stores exactly one transaction and the second press
  reports the first one;
* permission is checked when the draft is confirmed, with the membership of that moment
  (G-2, R-9): a member downgraded or removed meanwhile is refused, and their draft expires;
* drafts expire after 24 hours, category-name prompts after 30 minutes.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from finbot.authz.permissions import Permission
from finbot.authz.policy import ForbiddenError, authorize
from finbot.domain.finance import (
    DRAFT_TTL,
    PENDING_ACTION_TTL,
    TEXT_STEPS,
    CaptureState,
    CaptureStep,
    Category,
    ConflictError,
    DraftExpiredError,
    DraftPurpose,
    DraftRecord,
    DraftStatus,
    DraftView,
    EditField,
    EditState,
    FinanceError,
    InvalidInputError,
    NotFoundError,
    PendingAction,
    PendingKind,
    StepError,
    TransactionRecord,
    draft_ref,
    draft_state,
    parse_date,
)
from finbot.domain.ledger import TxnSource, TxnStatus, TxnType
from finbot.domain.money import InvalidAmountError, ParsedAmount, parse_amount
from finbot.ports.finance import FinanceSession
from finbot.services.finance import (
    Actor,
    FinanceServiceBase,
    WorkspaceInfo,
    create_transaction,
    update_transaction,
)
from finbot.services.ledger import create_category_in, rename_category_in

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    record: TransactionRecord
    purpose: DraftPurpose
    already: bool  # the draft had been confirmed before (double tap, retry)
    workspace: WorkspaceInfo


@dataclass(frozen=True, slots=True)
class CategoryPrompt:
    """A pending action is waiting for a category name."""

    action: PendingAction
    workspace: WorkspaceInfo
    category: Category | None  # the category being renamed


@dataclass(frozen=True, slots=True)
class CategoryDone:
    category: Category
    kind: PendingKind
    workspace: WorkspaceInfo


TextOutcome = DraftView | CategoryDone | None
DraftState = CaptureState | EditState
_Update = Callable[[FinanceSession, DraftState, date], Awaitable[DraftState]]


def _permission_for(state: DraftState) -> Permission:
    """A create draft needs transaction.create, an edit draft transaction.update (R-9)."""
    return (
        Permission.TRANSACTION_UPDATE
        if isinstance(state, EditState)
        else Permission.TRANSACTION_CREATE
    )


def _amount(text: str) -> ParsedAmount:
    try:
        return parse_amount(text)
    except InvalidAmountError as exc:
        raise InvalidInputError(f"amount_{exc.reason.value}") from None


class CaptureService(FinanceServiceBase):
    # -------------------------------------------------------------------- helpers

    async def _view(self, s: FinanceSession, draft_id: UUID, state: DraftState) -> DraftView:
        categories: tuple[Category, ...] = ()
        txn_type = state.txn_type
        if state.step is CaptureStep.CATEGORY and txn_type is not None:
            categories = tuple(await s.list_categories(txn_type))
        category = None
        if state.category_id is not None:
            category = await s.get_category(state.category_id)
        target = None
        if isinstance(state, EditState):
            draft = await s.get_draft(draft_id)
            if draft is not None and draft.target_transaction_id is not None:
                target = await s.get_transaction(draft.target_transaction_id)
        return DraftView(
            id=draft_id,
            ref=draft_ref(draft_id),
            state=state,
            workspace_name=s.ctx.workspace_name,
            workspace_kind=s.ctx.workspace_kind,
            currency=s.ctx.currency,
            categories=categories,
            category=category,
            target=target,
        )

    async def _pending(self, s: FinanceSession, ref: str | None) -> DraftRecord | None:
        """The caller's pending draft (locked), or None if missing, stale or expired.

        An expired draft is marked ``expired`` here; the caller must then raise
        :class:`DraftExpiredError` *after* the session commits.
        """
        draft = await s.pending_draft(for_update=True)
        if draft is None or (ref is not None and draft_ref(draft.id) != ref):
            return None
        if draft.expires_at <= self._clock():
            await s.set_draft_status(draft.id, DraftStatus.EXPIRED)
            logger.info("draft expired", extra={"event": "draft.expired"})
            return None
        return draft

    async def _start_draft(
        self,
        s: FinanceSession,
        purpose: DraftPurpose,
        state: DraftState,
        *,
        target: UUID | None = None,
        chat_id: int | None = None,
    ) -> DraftView:
        await s.lock_conversation()
        await s.cancel_pending_drafts()
        await s.cancel_pending_actions()
        draft_id = await s.insert_draft(
            purpose,
            state.to_json(),
            TxnSource.MANUAL,
            self._clock() + DRAFT_TTL,
            target_transaction_id=target,
            telegram_chat_id=chat_id,
        )
        return await self._view(s, draft_id, state)

    async def _step(self, actor: Actor, ref: str, update: _Update) -> DraftView:
        """Load the pending draft ``ref``, authorize for its purpose, apply ``update``, save."""
        try:
            async with self._session(actor, None, write=True) as s:
                draft = await self._pending(s, ref)
                if draft is not None:
                    state = draft_state(draft.proposal)
                    authorize(s.ctx, _permission_for(state))
                    state = await update(s, state, self._today(s.ctx))
                    await s.update_draft(draft.id, state.to_json())
                    return await self._view(s, draft.id, state)
        except ForbiddenError:
            await self._expire_after_denial(actor)
            raise
        raise DraftExpiredError("draft")

    # --------------------------------------------------------------------- create

    async def start(
        self,
        actor: Actor,
        txn_type: TxnType | None = None,
        *,
        amount_text: str | None = None,
        description: str | None = None,
        chat_id: int | None = None,
    ) -> DraftView:
        """Begin a new transaction draft (``/catat``, ``/pemasukan``, ``/pengeluaran``)."""
        amount = _amount(amount_text) if amount_text else None
        async with self._session(actor, Permission.TRANSACTION_CREATE, write=True) as s:
            state = CaptureState.start(self._today(s.ctx), txn_type, amount, description)
            view = await self._start_draft(s, DraftPurpose.CREATE, state, chat_id=chat_id)
        logger.info("draft started", extra={"event": "draft.created"})
        return view

    async def choose_type(self, actor: Actor, ref: str, txn_type: TxnType) -> DraftView:
        async def update(_: FinanceSession, state: DraftState, today: date) -> DraftState:
            if not isinstance(state, CaptureState):
                raise StepError("type")
            return state.with_type(txn_type)

        return await self._step(actor, ref, update)

    async def choose_category(self, actor: Actor, ref: str, category_id: UUID) -> DraftView:
        """The category button of a create or an edit draft (same workspace, same kind)."""

        async def update(s: FinanceSession, state: DraftState, today: date) -> DraftState:
            category = await s.get_category(category_id)
            if category is None:
                raise NotFoundError("category")  # also one of another workspace (G-3)
            return state.with_category(category)

        return await self._step(actor, ref, update)

    async def skip_description(self, actor: Actor, ref: str) -> DraftView:
        """No description (create) or remove the description (edit)."""

        async def update(_: FinanceSession, state: DraftState, today: date) -> DraftState:
            return state.with_description(None)

        return await self._step(actor, ref, update)

    async def ask_date(self, actor: Actor, ref: str) -> DraftView:
        async def update(_: FinanceSession, state: DraftState, today: date) -> DraftState:
            if not isinstance(state, CaptureState):
                raise StepError("date")
            return state.asking_date()

        return await self._step(actor, ref, update)

    async def current(self, actor: Actor) -> DraftView | CategoryPrompt | None:
        """The conversation in progress, if any (to re-render after an error)."""
        async with self._session(actor, None) as s:
            action = await s.pending_action()
            if action is not None and action.expires_at > self._clock():
                return await self._prompt(s, action)
            draft = await s.pending_draft()
            if draft is None or draft.expires_at <= self._clock():
                return None
            return await self._view(s, draft.id, draft_state(draft.proposal))

    # ------------------------------------------------------------------ text input

    async def input_text(self, actor: Actor, text: str) -> TextOutcome:
        """A plain message: the answer to the current text step, or None if none is waiting."""
        try:
            return await self._input_text(actor, text)
        except ForbiddenError:
            await self._expire_after_denial(actor)
            raise

    async def _input_text(self, actor: Actor, text: str) -> TextOutcome:
        expired = False
        async with self._session(actor, None, write=True) as s:
            today = self._today(s.ctx)
            action = await s.pending_action(for_update=True)
            if action is not None:
                if action.expires_at <= self._clock():
                    await s.set_pending_action_status(action.id, "expired")
                    expired = True
                else:
                    return await self._complete_action(s, action, text)
            else:
                draft = await s.pending_draft(for_update=True)
                if draft is None:
                    return None
                if draft.expires_at <= self._clock():
                    await s.set_draft_status(draft.id, DraftStatus.EXPIRED)
                    expired = True
                else:
                    state = draft_state(draft.proposal)
                    authorize(s.ctx, _permission_for(state))
                    if state.step not in TEXT_STEPS:
                        raise StepError(state.step.value)
                    state = self._apply_text(state, text, today)
                    await s.update_draft(draft.id, state.to_json())
                    return await self._view(s, draft.id, state)
        if expired:
            raise DraftExpiredError("conversation")
        return None

    @staticmethod
    def _apply_text(state: DraftState, text: str, today: date) -> DraftState:
        match state.step:
            case CaptureStep.AMOUNT:
                return state.with_amount(_amount(text))
            case CaptureStep.DESCRIPTION:
                return state.with_description(text)
            case CaptureStep.DATE:
                return state.with_date(parse_date(text, today), today)
            case _:  # pragma: no cover - filtered by TEXT_STEPS
                raise StepError(state.step.value)

    # -------------------------------------------------------------------- confirm

    async def confirm(self, actor: Actor, ref: str) -> ConfirmResult:
        failure: FinanceError | None = None
        try:
            async with self._session(actor, None, write=True) as s:
                today = self._today(s.ctx)
                draft = await self._pending(s, ref)
                if draft is None:
                    return await self._already_confirmed(s, ref)
                state = draft_state(draft.proposal)
                try:
                    if isinstance(state, EditState):
                        if draft.target_transaction_id is None:  # pragma: no cover - CHECK
                            raise ConflictError("edit_without_target")
                        record = await update_transaction(
                            s,
                            draft.target_transaction_id,
                            state.expected_version,
                            state.changes(),
                            today=today,
                        )
                    else:
                        txn_type, amount, category_id = state.complete()
                        record = await create_transaction(
                            s,
                            txn_type=txn_type,
                            amount=amount,
                            txn_date=state.txn_date,
                            category_id=category_id,
                            description=state.description,
                            source=TxnSource.MANUAL,
                            today=today,
                        )
                except (ConflictError, NotFoundError) as exc:
                    # The target changed or disappeared: this draft can never succeed.
                    await s.set_draft_status(draft.id, DraftStatus.CANCELLED)
                    failure = exc
                else:
                    await s.set_draft_status(draft.id, DraftStatus.CONFIRMED, record.id)
                    logger.info("draft confirmed", extra={"event": "draft.confirmed"})
                    return ConfirmResult(record, draft.purpose, False, WorkspaceInfo.of(s.ctx))
        except ForbiddenError:
            await self._expire_after_denial(actor)
            raise
        if failure is not None:
            raise failure
        raise AssertionError("unreachable")  # pragma: no cover

    async def _already_confirmed(self, s: FinanceSession, ref: str) -> ConfirmResult:
        draft = await s.find_draft_by_ref(ref)
        if draft is None or draft.status is not DraftStatus.CONFIRMED:
            raise DraftExpiredError("draft")
        if draft.confirmed_txn_id is None:  # pragma: no cover - CHECK constraint
            raise DraftExpiredError("draft")
        record = await s.get_transaction(draft.confirmed_txn_id)
        if record is None:  # pragma: no cover - FK
            raise DraftExpiredError("draft")
        return ConfirmResult(record, draft.purpose, True, WorkspaceInfo.of(s.ctx))

    async def _expire_after_denial(self, actor: Actor) -> None:
        """R-9 / docs/08 §6: a member who lost the permission loses their pending draft."""
        try:
            async with self._store.session(actor.user_id, actor.workspace_id, write=True) as s:
                draft = await s.pending_draft(for_update=True)
                if draft is not None:
                    await s.set_draft_status(draft.id, DraftStatus.EXPIRED)
        except NotFoundError:
            return  # no longer a member at all: nothing visible to expire

    async def cancel(self, actor: Actor, ref: str | None = None) -> bool:
        """Cancel the pending draft (``ref`` must match it if given) and any pending action."""
        async with self._session(actor, None, write=True) as s:
            cancelled = await s.cancel_pending_actions() > 0
            draft = await s.pending_draft(for_update=True)
            if draft is not None and (ref is None or draft_ref(draft.id) == ref):
                await s.set_draft_status(draft.id, DraftStatus.CANCELLED)
                cancelled = True
        if cancelled:
            logger.info("draft cancelled", extra={"event": "draft.cancelled"})
        return cancelled

    # ----------------------------------------------------------------------- edit

    async def start_edit(
        self, actor: Actor, transaction_id: UUID, expected_version: int, field: EditField
    ) -> DraftView:
        async with self._session(actor, Permission.TRANSACTION_UPDATE, write=True) as s:
            record = await s.get_transaction(transaction_id)
            if record is None:
                raise NotFoundError("transaction")
            if record.status is not TxnStatus.POSTED:
                raise ConflictError("voided")
            if record.version != expected_version:
                raise ConflictError("stale")
            state = EditState.start(field, record.version, record.type)
            return await self._start_draft(s, DraftPurpose.EDIT, state, target=record.id)

    # ------------------------------------------------------------- category names

    async def _prompt(self, s: FinanceSession, action: PendingAction) -> CategoryPrompt:
        category = None
        if action.kind is PendingKind.CATEGORY_RENAME:
            category = await s.get_category(UUID(action.payload["category_id"]))
        return CategoryPrompt(action, WorkspaceInfo.of(s.ctx), category)

    async def start_category_create(self, actor: Actor, kind: TxnType) -> CategoryPrompt:
        async with self._session(actor, Permission.CATEGORY_MANAGE, write=True) as s:
            if kind not in (TxnType.INCOME, TxnType.EXPENSE):
                raise InvalidInputError("category_kind")
            return await self._new_action(s, PendingKind.CATEGORY_CREATE, {"kind": kind.value})

    async def start_category_rename(self, actor: Actor, category_id: UUID) -> CategoryPrompt:
        async with self._session(actor, Permission.CATEGORY_MANAGE, write=True) as s:
            if await s.get_category(category_id) is None:
                raise NotFoundError("category")
            return await self._new_action(
                s, PendingKind.CATEGORY_RENAME, {"category_id": str(category_id)}
            )

    async def _new_action(
        self, s: FinanceSession, kind: PendingKind, payload: dict[str, str]
    ) -> CategoryPrompt:
        await s.lock_conversation()
        await s.cancel_pending_actions()
        await s.cancel_pending_drafts()
        expires = self._clock() + PENDING_ACTION_TTL
        action_id = await s.insert_pending_action(kind, payload, expires)
        return await self._prompt(s, PendingAction(action_id, kind, expires, payload))

    async def _complete_action(
        self, s: FinanceSession, action: PendingAction, text: str
    ) -> CategoryDone:
        if action.kind is PendingKind.CATEGORY_CREATE:
            category = await create_category_in(s, TxnType(action.payload["kind"]), text)
        else:
            category = await rename_category_in(s, UUID(action.payload["category_id"]), text)
        await s.set_pending_action_status(action.id, "completed")
        return CategoryDone(category, action.kind, WorkspaceInfo.of(s.ctx))
