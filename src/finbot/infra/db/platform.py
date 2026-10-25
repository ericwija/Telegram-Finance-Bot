"""PostgreSQL implementation of ``PlatformAdminGateway``, as ``finbot_app``.

Only the ``SECURITY DEFINER`` invite functions of revision 0003 are called, each in its own
transaction with ``app.user_id`` set transaction-locally to the acting admin. SQLSTATE 42501
(the function's admin check) becomes :class:`PlatformAdminRequiredError`.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from finbot.domain.identity import (
    InviteStatus,
    InviteSummary,
    PlatformAdminRequiredError,
    RevokeOutcome,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_INSUFFICIENT_PRIVILEGE = "42501"


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(
        getattr(orig, "__cause__", None), "sqlstate", None
    )


class PgPlatformGateway:
    def __init__(self, engine: "AsyncEngine") -> None:
        self._engine = engine

    async def _call(self, actor_user_id: UUID, sql: str, params: dict[str, Any]) -> Any:
        from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time
        from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

        try:
            async with self._engine.begin() as conn:
                await self._act_as(conn, actor_user_id)
                return (await conn.execute(text(sql), params)).all()
        except DBAPIError as exc:
            if _sqlstate(exc) == _INSUFFICIENT_PRIVILEGE:
                raise PlatformAdminRequiredError("an active platform admin is required") from None
            raise

    @staticmethod
    async def _act_as(conn: "AsyncConnection", actor_user_id: UUID) -> None:
        from sqlalchemy import text  # noqa: PLC0415

        await conn.execute(
            text("SELECT set_config('app.user_id', :user_id, true)"),
            {"user_id": str(actor_user_id)},
        )

    async def create_invite(
        self, actor_user_id: UUID, token_hash: bytes, note: str | None, channel: str
    ) -> UUID:
        rows = await self._call(
            actor_user_id,
            "SELECT public.create_onboarding_invite(:token_hash, :note, :channel)",
            {"token_hash": token_hash, "note": note, "channel": channel},
        )
        invite_id = rows[0][0]
        if not isinstance(invite_id, UUID):  # pragma: no cover - the function returns uuid
            raise TypeError("create_onboarding_invite returned no ID")
        return invite_id

    async def list_invites(
        self, actor_user_id: UUID, *, include_inactive: bool
    ) -> list[InviteSummary]:
        rows = await self._call(
            actor_user_id,
            "SELECT invite_id, status, created_at, expires_at, note, created_by, used_by "
            "FROM public.list_onboarding_invites(:include_inactive)",
            {"include_inactive": include_inactive},
        )
        return [
            InviteSummary(
                r.invite_id,
                InviteStatus(r.status),
                r.created_at,
                r.expires_at,
                r.note,
                r.created_by,
                r.used_by,
            )
            for r in rows
        ]

    async def revoke_invite(
        self, actor_user_id: UUID, invite_id: UUID, channel: str
    ) -> RevokeOutcome:
        rows = await self._call(
            actor_user_id,
            "SELECT public.revoke_onboarding_invite(:invite_id, :channel)",
            {"invite_id": invite_id, "channel": channel},
        )
        return RevokeOutcome(rows[0][0])
