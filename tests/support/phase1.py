"""Phase 1 schema fixtures: a small multi-tenant world seeded as finbot_owner.

World (all IDs random per test):

* ``b1`` business: ``u1`` owner (active), ``u4`` viewer (removed);
* ``b2`` business: ``u2`` owner, ``u1`` editor;
* ``p1`` personal workspace of ``u1``;
* ``u3`` has no membership anywhere;
* one audit event in each of b1 and b2, and one platform-level event (``workspace_id`` NULL).

Queries "as the app" run in one transaction with ``app.workspace_id`` / ``app.user_id`` set
transaction-locally (docs/09 §3), exactly as the Step 4 unit of work will.
"""

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class World:
    u1: str = field(default_factory=_new_id)
    u2: str = field(default_factory=_new_id)
    u3: str = field(default_factory=_new_id)
    u4: str = field(default_factory=_new_id)
    b1: str = field(default_factory=_new_id)
    b2: str = field(default_factory=_new_id)
    p1: str = field(default_factory=_new_id)


_SEED = (
    "INSERT INTO users (id, display_name) "
    "VALUES (:u1, 'U1'), (:u2, 'U2'), (:u3, 'U3'), (:u4, 'U4')",
    "INSERT INTO workspaces (id, kind, name) "
    "VALUES (:b1, 'business', 'B1'), (:b2, 'business', 'B2')",
    "INSERT INTO workspaces (id, kind, name, personal_owner_id) "
    "VALUES (:p1, 'personal', 'P1', :u1)",
    "INSERT INTO workspace_memberships (workspace_id, workspace_kind, user_id, role) VALUES "
    "(:b1, 'business', :u1, 'owner'), (:b2, 'business', :u2, 'owner'), "
    "(:b2, 'business', :u1, 'editor'), (:p1, 'personal', :u1, 'owner')",
    "INSERT INTO workspace_memberships "
    "(workspace_id, workspace_kind, user_id, role, status, ended_at) "
    "VALUES (:b1, 'business', :u4, 'viewer', 'removed', now())",
    "INSERT INTO audit_events (workspace_id, actor_user_id, actor_kind, channel, action) VALUES "
    "(:b1, :u1, 'user', 'telegram', 'workspace.created'), "
    "(:b2, :u2, 'user', 'telegram', 'workspace.created'), "
    "(NULL, NULL, 'system', 'job', 'backup.completed')",
)


async def seed(owner_engine: Any) -> World:
    """Insert the world as finbot_owner in one transaction (triggers and constraints apply)."""
    world = World()
    params = vars(world)
    async with owner_engine.begin() as conn:
        for statement in _SEED:
            names = {k: v for k, v in params.items() if f":{k}" in statement}
            await conn.execute(text(statement), names)
    return world


@asynccontextmanager
async def as_app(
    app_engine: Any, *, workspace: str | None = None, user: str | None = None
) -> AsyncIterator[Any]:
    """A finbot_app transaction with the given transaction-local context (committed on exit)."""
    async with app_engine.begin() as conn:
        await set_context(conn, workspace=workspace, user=user)
        yield conn


async def set_context(conn: Any, *, workspace: str | None, user: str | None) -> None:
    if workspace is not None:
        await conn.execute(
            text("SELECT set_config('app.workspace_id', :v, true)"), {"v": workspace}
        )
    if user is not None:
        await conn.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": user})


async def rows(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> list[Any]:
    return list((await conn.execute(text(sql), dict(params or {}))).all())


async def scalar(conn: Any, sql: str, params: Mapping[str, Any] | None = None) -> Any:
    return (await conn.execute(text(sql), dict(params or {}))).scalar()
