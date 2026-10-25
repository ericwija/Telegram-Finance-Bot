"""Guided capture and edit conversations, persisted as drafts (FR-TXN-04..06, FR-TXN-08, -10).

The conversation state lives in PostgreSQL (NFR-AVL-05), never in process memory:

* a ``create`` draft walks type → amount → category → description → confirm. Its account is
  resolved when it starts and stored in the draft (FR-ACC-05), and the confirmation shows it.
  Every field can be corrected from the confirmation (✏️ Ubah, FR-TXN-05), and every correction
  returns to the complete confirmation. An ``edit`` draft changes one field of one posted
  transaction;
* each user has at most one pending draft and one pending action (a category-name prompt) per
  workspace (unique indexes); starting a new conversation cancels the previous one;
* buttons carry only a short reference of the conversation they belong to. The conversation
  itself is always the caller's own pending one in the active workspace (RLS: ``user_id =
  app_user_id()``), and a reference that does not match it **exactly** is stale and refused, so
  an old keyboard can never act on (or cancel) a newer conversation (docs/09 L1);
* every conversation records the membership version it was started under. After any role or
  status change (a downgrade, a removal, a removal and rejoin) the version differs and the old
  conversation is expired instead of revived (R-9, Step 6B R03);
* **confirm is idempotent** (FR-TXN-06, AT-TXN-IDEMP-01): the draft row is locked ``FOR
  UPDATE`` and moves ``pending → confirmed`` in the same transaction that writes the ledger,
  so a double tap or a Telegram retry stores exactly one transaction. A replay reports the
  transaction only to a caller who still holds the permission, under the same membership,
  while the transaction is not voided; otherwise it is answered like an expired button;
* permission is checked when the conversation is used **or cancelled**, with the membership
  of that moment (G-2, R-9): a member who no longer holds the conversation's permission is
  refused (audited) and *that* conversation is invalidated (``expired``), never another one.
  The invalidation runs after the refused transaction rolled back, as a conditional update
  of the rejected conversation's own id (``pending`` → ``expired``), so a newer conversation
  started meanwhile is never touched (Step 6C, N01);
* drafts expire after 24 hours, category-name prompts after 30 minutes. An expired
  conversation is **persisted** as expired before the request is refused (the refusal is
  raised only after the transaction committed).
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Final
from uuid import UUID

from finbot.authz.permissions import Permission
from finbot.authz.policy import ForbiddenError, allowed, authorize
from finbot.domain.finance import (
    CATEGORY_PAGE_SIZE,
    DRAFT_TTL,
    PENDING_ACTION_TTL,
    TEXT_STEPS,
    CaptureState,
    CaptureStep,
    Category,
    ConflictError,
    DraftExpiredError,
    DraftField,
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
    validate_category_page,
)
from finbot.domain.ledger import TxnSource, TxnStatus, TxnType
from finbot.domain.money import InvalidAmountError, ParsedAmount, parse_amount
from finbot.ports.finance import FinanceSession, SessionMode
from finbot.services.finance import (
    Actor,
    FinanceServiceBase,
    WorkspaceInfo,
    create_transaction,
    update_transaction,
    visible,
)
from finbot.services.ledger import create_category_in, rename_category_in

logger = logging.getLogger(__name__)

#: Key of the membership version in ``drafts.proposal`` and ``pending_actions.payload``.
MEMBERSHIP_KEY: Final = "membership_version"


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

    @property
    def ref(self) -> str:
        return draft_ref(self.action.id)


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


def _same_membership(data: dict[str, Any], s: FinanceSession) -> bool:
    """A conversation without a recorded version (created before Step 6B) never matches."""
    return data.get(MEMBERSHIP_KEY) == s.ctx.membership_version


class ConversationForbiddenError(ForbiddenError):
    """The caller may no longer use (or cancel) this exact conversation. Audited like any
    refusal, with the conversation's entity type; the service then invalidates only it."""

    def __init__(
        self,
        denied: ForbiddenError,
        *,
        draft_id: UUID | None = None,
        action_id: UUID | None = None,
    ) -> None:
        super().__init__(
            denied.permission,
            denied.reason,
            denied.role,
            entity="category" if action_id is not None else "draft",
        )
        self.draft_id = draft_id
        self.action_id = action_id


class CaptureService(FinanceServiceBase):
    # -------------------------------------------------------------------- helpers

    @staticmethod
    def _proposal(s: FinanceSession, state: DraftState) -> dict[str, Any]:
        return {**state.to_json(), MEMBERSHIP_KEY: s.ctx.membership_version}

    async def _view(
        self, s: FinanceSession, draft: DraftRecord | UUID, state: DraftState, page: int = 0
    ) -> DraftView:
        draft_id = draft if isinstance(draft, UUID) else draft.id
        categories: tuple[Category, ...] = ()
        has_next = False
        if state.step is CaptureStep.CATEGORY and state.txn_type is not None:
            rows = await s.list_categories(
                state.txn_type, limit=CATEGORY_PAGE_SIZE + 1, offset=page * CATEGORY_PAGE_SIZE
            )
            categories, has_next = tuple(rows[:CATEGORY_PAGE_SIZE]), len(rows) > CATEGORY_PAGE_SIZE
        category = None
        if state.category_id is not None:
            category = await s.get_category(state.category_id)
        target = account = None
        if isinstance(state, EditState):
            record = draft if isinstance(draft, DraftRecord) else await s.get_draft(draft_id)
            if record is not None and record.target_transaction_id is not None:
                target = visible(s, await s.get_transaction(record.target_transaction_id))
        else:
            account_id = state.account_id or s.ctx.default_account_id
            account = None if account_id is None else await s.get_account(account_id)
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
            account=account,
            category_page=page,
            categories_has_next=has_next,
        )

    async def _pending(self, s: FinanceSession, ref: str | None) -> tuple[DraftRecord | None, bool]:
        """(the caller's pending draft, locked and authorized; whether it just expired).

        A draft whose reference differs is stale (None, False) and is left alone. A draft past
        its expiry is marked ``expired`` (None, True). Otherwise the caller must hold the
        draft's permission now (ForbiddenError, audited; R-9), and a draft started under an
        earlier membership (an upgrade, a removal and rejoin) is marked ``expired`` too. The
        caller must let the session commit and raise only afterwards.
        """
        draft = await s.pending_draft(for_update=True)
        if draft is None or (ref is not None and draft_ref(draft.id) != ref):
            return None, False
        if draft.expires_at > self._clock():
            try:
                authorize(s.ctx, _permission_for(draft_state(draft.proposal)))
            except ForbiddenError as exc:
                raise ConversationForbiddenError(exc, draft_id=draft.id) from None
            if _same_membership(draft.proposal, s):
                return draft, False
        await s.set_draft_status(draft.id, DraftStatus.EXPIRED)
        logger.info("draft expired", extra={"event": "draft.expired"})
        return None, True

    async def _pending_action(
        self, s: FinanceSession, ref: str | None = None
    ) -> tuple[PendingAction | None, bool]:
        """As :meth:`_pending`, for the caller's pending category-name prompt."""
        action = await s.pending_action(for_update=True)
        if action is None or (ref is not None and draft_ref(action.id) != ref):
            return None, False
        if action.expires_at > self._clock():
            try:
                authorize(s.ctx, Permission.CATEGORY_MANAGE)
            except ForbiddenError as exc:
                raise ConversationForbiddenError(exc, action_id=action.id) from None
            if _same_membership(action.payload, s):
                return action, False
        await s.set_pending_action_status(action.id, "expired")
        return None, True

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
            self._proposal(s, state),
            TxnSource.MANUAL,
            self._clock() + DRAFT_TTL,
            target_transaction_id=target,
            telegram_chat_id=chat_id,
        )
        return await self._view(s, draft_id, state)

    async def _step(self, actor: Actor, ref: str, update: _Update, page: int = 0) -> DraftView:
        """Load the pending draft ``ref``, authorize for its purpose, apply ``update``, save."""
        try:
            async with self._session(actor, None, write=True, entity="draft") as s:
                draft, _ = await self._pending(s, ref)
                if draft is not None:
                    state = await update(s, draft_state(draft.proposal), self._today(s.ctx))
                    await s.update_draft(draft.id, self._proposal(s, state))
                    return await self._view(s, draft, state, page)
        except ForbiddenError as exc:
            await self._invalidate(actor, exc)
            raise
        raise DraftExpiredError("draft")  # stale, expired (now persisted) or gone

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
        async with self._session(
            actor, Permission.TRANSACTION_CREATE, write=True, entity="draft"
        ) as s:
            state = CaptureState.start(
                self._today(s.ctx),
                txn_type,
                amount,
                description,
                account_id=s.ctx.default_account_id,
            )
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

    async def category_page(self, actor: Actor, ref: str, page: int) -> DraftView:
        """Another page of the category step (every category is reachable, F06)."""
        validate_category_page(page)

        async def update(_: FinanceSession, state: DraftState, today: date) -> DraftState:
            if state.step is not CaptureStep.CATEGORY:
                raise StepError(state.step.value)
            return state

        return await self._step(actor, ref, update, page)

    async def skip_description(self, actor: Actor, ref: str) -> DraftView:
        """No description (create) or remove the description (edit)."""

        async def update(_: FinanceSession, state: DraftState, today: date) -> DraftState:
            return state.with_description(None)

        return await self._step(actor, ref, update)

    async def review(self, actor: Actor, ref: str) -> DraftView:
        """✏️ Ubah: the confirmation of a complete create draft with one button per field
        (the persisted REVIEW step, so it survives a restart)."""

        async def update(_: FinanceSession, state: DraftState, today: date) -> DraftState:
            if not isinstance(state, CaptureState):
                raise StepError(state.step.value)
            return state.review()

        return await self._step(actor, ref, update)

    async def revise(self, actor: Actor, ref: str, draft_field: DraftField) -> DraftView:
        """Open one field of a complete create draft for correction (FR-TXN-05)."""

        async def update(_: FinanceSession, state: DraftState, today: date) -> DraftState:
            if not isinstance(state, CaptureState):
                raise StepError(state.step.value)
            return state.revise(draft_field)

        return await self._step(actor, ref, update)

    async def back(self, actor: Actor, ref: str) -> DraftView:
        """Leave a correction unchanged and show the confirmation again."""

        async def update(_: FinanceSession, state: DraftState, today: date) -> DraftState:
            if not isinstance(state, CaptureState):
                raise StepError(state.step.value)
            return state.back()

        return await self._step(actor, ref, update)

    async def current(self, actor: Actor) -> DraftView | CategoryPrompt | None:
        """The conversation in progress, if any and still usable by the caller (read only)."""
        async with self._session(actor, None) as s:
            action = await s.pending_action()
            if (
                action is not None
                and action.expires_at > self._clock()
                and _same_membership(action.payload, s)
            ):
                if not allowed(s.ctx, Permission.CATEGORY_MANAGE):
                    return None
                return await self._prompt(s, action)
            draft = await s.pending_draft()
            if (
                draft is None
                or draft.expires_at <= self._clock()
                or not _same_membership(draft.proposal, s)
            ):
                return None
            state = draft_state(draft.proposal)
            if not allowed(s.ctx, _permission_for(state)):
                return None
            return await self._view(s, draft, state)

    # ------------------------------------------------------------------ text input

    async def input_text(self, actor: Actor, text: str) -> TextOutcome:
        """A plain message: the answer to the current text step, or None if none is waiting."""
        try:
            return await self._input_text(actor, text)
        except ForbiddenError as exc:
            await self._invalidate(actor, exc)
            raise

    async def _input_text(self, actor: Actor, text: str) -> TextOutcome:
        async with self._session(actor, None, write=True, entity="draft") as s:
            today = self._today(s.ctx)
            action, expired = await self._pending_action(s)
            if action is not None:
                return await self._complete_action(s, action, text)
            if not expired:
                draft, expired = await self._pending(s, None)
                if draft is not None:
                    state = draft_state(draft.proposal)
                    if state.step not in TEXT_STEPS:
                        raise StepError(state.step.value)
                    state = self._apply_text(state, text, today)
                    await s.update_draft(draft.id, self._proposal(s, state))
                    return await self._view(s, draft, state)
        if expired:  # persisted above; refused only now that it is committed
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
            async with self._session(actor, None, write=True, entity="transaction") as s:
                today = self._today(s.ctx)
                draft, expired = await self._pending(s, ref)
                if draft is None:
                    replay = DraftExpiredError("draft") if expired else await self._replay(s, ref)
                    if isinstance(replay, ConfirmResult):
                        return replay
                    failure = replay  # raised after the commit (an expiry must persist)
                else:
                    try:
                        record = await self._apply(s, draft, draft_state(draft.proposal), today)
                    except (ConflictError, NotFoundError) as exc:
                        # The target changed or disappeared: this draft can never succeed.
                        await s.set_draft_status(draft.id, DraftStatus.CANCELLED)
                        failure = exc
                    else:
                        await s.set_draft_status(draft.id, DraftStatus.CONFIRMED, record.id)
                        logger.info(
                            "draft confirmed",
                            extra={"event": "draft.confirmed", "request_id": actor.request_id},
                        )
                        return ConfirmResult(record, draft.purpose, False, WorkspaceInfo.of(s.ctx))
        except ForbiddenError as exc:
            await self._invalidate(actor, exc)
            raise
        if failure is None:  # pragma: no cover - every branch above returns or sets it
            raise AssertionError("unreachable")
        raise failure

    @staticmethod
    async def _apply(
        s: FinanceSession, draft: DraftRecord, state: DraftState, today: date
    ) -> TransactionRecord:
        if isinstance(state, EditState):
            if draft.target_transaction_id is None:  # pragma: no cover - CHECK constraint
                raise ConflictError("edit_without_target")
            return await update_transaction(
                s, draft.target_transaction_id, state.expected_version, state.changes(), today=today
            )
        txn_type, amount, category_id = state.complete()
        return await create_transaction(
            s,
            txn_type=txn_type,
            amount=amount,
            txn_date=state.txn_date,
            category_id=category_id,
            description=state.description,
            source=TxnSource.MANUAL,
            today=today,
            account_id=state.account_id,
        )

    async def _replay(self, s: FinanceSession, ref: str) -> ConfirmResult | FinanceError:
        """A second ✅ of an already confirmed draft (double tap, retry, restart).

        Re-authorized now: the caller must still hold the draft's permission (refused and
        audited otherwise) and the same membership; a voided result is not shown. Anything
        else is answered like an expired button, without details.
        """
        draft = await s.find_draft_by_ref(ref)
        if (
            draft is None
            or draft.status is not DraftStatus.CONFIRMED
            or draft.confirmed_txn_id is None
        ):
            return DraftExpiredError("draft")
        authorize(s.ctx, _permission_for(draft_state(draft.proposal)))
        authorize(s.ctx, Permission.TRANSACTION_VIEW)
        if not _same_membership(draft.proposal, s):
            return DraftExpiredError("draft")
        record = await s.get_transaction(draft.confirmed_txn_id)
        if record is None or record.status is not TxnStatus.POSTED:
            return DraftExpiredError("draft")
        return ConfirmResult(record, draft.purpose, True, WorkspaceInfo.of(s.ctx))

    async def _invalidate(self, actor: Actor, denied: ForbiddenError) -> None:
        """R-9 / docs/08 §6: the conversation the caller was refused is invalidated.

        Only the refused conversation, identified by its id, and only while it is still
        pending: if another request replaced it in the meantime, nothing changes (N01). A
        refusal that is not about a conversation (e.g. starting one) invalidates nothing.
        """
        if not isinstance(denied, ConversationForbiddenError):
            return
        try:
            async with self._store.session(
                actor.user_id,
                actor.workspace_id,
                mode=SessionMode.WRITE,
                request_id=actor.request_id,
            ) as s:
                if denied.draft_id is not None:
                    await s.expire_draft_if_pending(denied.draft_id)
                if denied.action_id is not None:
                    await s.expire_action_if_pending(denied.action_id)
        except NotFoundError:
            return  # no longer a member at all: nothing visible to expire

    async def cancel(self, actor: Actor, ref: str | None = None) -> bool:
        """``/batal`` (``ref`` None): cancel the conversation in progress, whatever it is.

        A ❌ button (``ref`` given) cancels only the conversation it belongs to: the pending
        draft or category prompt whose reference is exactly ``ref``; a stale ❌ changes nothing
        (F08). Cancelling is validated exactly like continuing (F01, Step 6C): the conversation
        must not be expired, the caller must still hold its permission in a workspace whose
        status allows it, and its membership version must be current. A refused cancel raises
        ForbiddenError (audited) and the conversation is *invalidated* (``expired``), never
        ``cancelled``; an expired or out-of-epoch one is marked ``expired`` and False is
        returned.
        """
        cancelled = False
        try:
            async with self._session(actor, None, write=True, entity="draft") as s:
                await s.lock_conversation()
                draft, _ = await self._pending(s, ref)
                if draft is not None:
                    await s.set_draft_status(draft.id, DraftStatus.CANCELLED)
                    cancelled = True
                action, _ = await self._pending_action(s, ref)
                if action is not None:
                    await s.set_pending_action_status(action.id, "cancelled")
                    cancelled = True
        except ForbiddenError as exc:
            await self._invalidate(actor, exc)
            raise
        if cancelled:
            logger.info(
                "draft cancelled",
                extra={"event": "draft.cancelled", "request_id": actor.request_id},
            )
        return cancelled

    # ----------------------------------------------------------------------- edit

    async def start_edit(
        self, actor: Actor, transaction_id: UUID, expected_version: int, field: EditField
    ) -> DraftView:
        async with self._session(
            actor, Permission.TRANSACTION_UPDATE, write=True, entity="transaction"
        ) as s:
            record = visible(s, await s.get_transaction(transaction_id))
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
        if kind not in (TxnType.INCOME, TxnType.EXPENSE):
            raise InvalidInputError("category_kind")
        async with self._session(
            actor, Permission.CATEGORY_MANAGE, write=True, entity="category"
        ) as s:
            return await self._new_action(s, PendingKind.CATEGORY_CREATE, {"kind": kind.value})

    async def start_category_rename(self, actor: Actor, category_id: UUID) -> CategoryPrompt:
        async with self._session(
            actor, Permission.CATEGORY_MANAGE, write=True, entity="category"
        ) as s:
            if await s.get_category(category_id) is None:
                raise NotFoundError("category")
            return await self._new_action(
                s, PendingKind.CATEGORY_RENAME, {"category_id": str(category_id)}
            )

    async def _new_action(
        self, s: FinanceSession, kind: PendingKind, payload: dict[str, Any]
    ) -> CategoryPrompt:
        await s.lock_conversation()
        await s.cancel_pending_actions()
        await s.cancel_pending_drafts()
        expires = self._clock() + PENDING_ACTION_TTL
        payload = {**payload, MEMBERSHIP_KEY: s.ctx.membership_version}
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
