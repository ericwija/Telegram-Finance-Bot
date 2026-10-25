"""Finance-core fixtures: users and workspaces seeded as finbot_owner, services as finbot_app.

Users are created the way onboarding stores them (user, Telegram identity, personal workspace
"Pribadi", owner membership); business workspaces and memberships are inserted directly because
their management flows (FR-WSP-02, FR-MEM-*) are not built yet. All finance data is then
written through the real services as ``finbot_app``, so RLS, grants and triggers apply.
"""

import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from finbot.domain.finance import DraftField
from finbot.infra.db.finance import PgFinanceStore
from finbot.services.capture import CaptureService
from finbot.services.finance import Actor
from finbot.services.ledger import LedgerService

#: A fixed instant: 25 October 2026, 12:00 in Jakarta.
NOW = datetime(2026, 10, 25, 5, 0, tzinfo=UTC)
_tg_ids = itertools.count(920_000_001)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@dataclass(frozen=True)
class Person:
    user_id: uuid.UUID
    personal_ws: uuid.UUID
    telegram_id: int

    def actor(self, workspace: uuid.UUID | None = None) -> Actor:
        return Actor(self.user_id, workspace)


async def person(owner_engine: Any, name: str = "Budi", *, platform_admin: bool = False) -> Person:
    user_id, workspace_id, telegram_id = uuid.uuid4(), uuid.uuid4(), next(_tg_ids)
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, display_name, is_platform_admin) "
                "VALUES (:id, :name, :admin)"
            ),
            {"id": user_id, "name": name, "admin": platform_admin},
        )
        await conn.execute(
            text("INSERT INTO telegram_identities (user_id, telegram_user_id) VALUES (:u, :tg)"),
            {"u": user_id, "tg": telegram_id},
        )
        await conn.execute(
            text(
                "INSERT INTO workspaces (id, kind, name, personal_owner_id) "
                "VALUES (:ws, 'personal', 'Pribadi', :u)"
            ),
            {"ws": workspace_id, "u": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO workspace_memberships (workspace_id, workspace_kind, user_id, role) "
                "VALUES (:ws, 'personal', :u, 'owner')"
            ),
            {"ws": workspace_id, "u": user_id},
        )
    return Person(user_id, workspace_id, telegram_id)


async def business(
    owner_engine: Any, owner: Person, members: dict[Person, str] | None = None, name: str = "Toko"
) -> uuid.UUID:
    workspace_id = uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO workspaces (id, kind, name) VALUES (:ws, 'business', :name)"),
            {"ws": workspace_id, "name": name},
        )
        for member, role in [(owner, "owner"), *(members or {}).items()]:
            await conn.execute(
                text(
                    "INSERT INTO workspace_memberships "
                    "(workspace_id, workspace_kind, user_id, role) "
                    "VALUES (:ws, 'business', :u, :role)"
                ),
                {"ws": workspace_id, "u": member.user_id, "role": role},
            )
    return workspace_id


async def set_role(owner_engine: Any, workspace: uuid.UUID, member: Person, role: str) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE workspace_memberships SET role = :role "
                "WHERE workspace_id = :ws AND user_id = :u"
            ),
            {"role": role, "ws": workspace, "u": member.user_id},
        )


async def remove_member(owner_engine: Any, workspace: uuid.UUID, member: Person) -> None:
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed', ended_at = now() "
                "WHERE workspace_id = :ws AND user_id = :u"
            ),
            {"ws": workspace, "u": member.user_id},
        )


async def owner_scalar(owner_engine: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
    async with owner_engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar()


async def owner_rows(
    owner_engine: Any, sql: str, params: dict[str, Any] | None = None
) -> list[Any]:
    async with owner_engine.connect() as conn:
        return list((await conn.execute(text(sql), params or {})).all())


@dataclass
class Finance:
    store: PgFinanceStore
    ledger: LedgerService
    capture: CaptureService
    clock: Clock


def finance(app_engine: Any, clock: Clock | None = None) -> Finance:
    clock = clock or Clock()
    store = PgFinanceStore(app_engine)
    return Finance(
        store,
        LedgerService(store, clock=clock),
        CaptureService(store, clock=clock),
        clock,
    )


async def record(
    f: Finance,
    actor: Actor,
    txn_type: Any,
    amount: str,
    category: str,
    *,
    description: str | None = "-",
    date_text: str | None = None,
) -> Any:
    """Create one transaction through the full capture flow; returns the ConfirmResult."""
    view = await f.capture.start(actor, txn_type, amount_text=amount)
    match = next(c for c in view.categories if c.name == category)
    view = await f.capture.choose_category(actor, view.ref, match.id)
    if description is None:
        view = await f.capture.skip_description(actor, view.ref)
    else:
        view = await f.capture.input_text(actor, description)  # type: ignore[assignment]
    if date_text is not None:
        view = await f.capture.revise(actor, view.ref, DraftField.DATE)
        view = await f.capture.input_text(actor, date_text)  # type: ignore[assignment]
    return await f.capture.confirm(actor, view.ref)
