"""PostgreSQL implementation of ``IdentityGateway`` (docs/09 §4.3), as ``finbot_app``.

``telegram_identities`` and ``onboarding_invites`` are never queried directly (the app has no
grants on them): only the ``SECURITY DEFINER`` functions ``resolve_principal`` and
``redeem_onboarding_invite`` are called. Each method is one transaction, so a failure inside
``redeem_onboarding_invite`` rolls back every row it wrote. The personal workspace is read with
``app.user_id`` set transaction-locally, so RLS (S5) applies as for any other request.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from finbot.domain.identity import PrincipalRecord, UserStatus, WorkspaceSummary

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class PgIdentityGateway:
    def __init__(self, engine: "AsyncEngine") -> None:
        self._engine = engine

    async def resolve_principal(self, telegram_user_id: int) -> PrincipalRecord | None:
        from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time

        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT user_id, status, is_platform_admin "
                        "FROM public.resolve_principal(:tg_user_id)"
                    ),
                    {"tg_user_id": telegram_user_id},
                )
            ).one_or_none()
        if row is None:
            return None
        return PrincipalRecord(row.user_id, UserStatus(row.status), row.is_platform_admin)

    async def redeem_onboarding_invite(
        self, token_hash: bytes, telegram_user_id: int, display_name: str
    ) -> UUID | None:
        from sqlalchemy import text  # noqa: PLC0415

        async with self._engine.begin() as conn:
            user_id = (
                await conn.execute(
                    text("SELECT public.redeem_onboarding_invite(:token_hash, :tg_user_id, :name)"),
                    {
                        "token_hash": token_hash,
                        "tg_user_id": telegram_user_id,
                        "name": display_name,
                    },
                )
            ).scalar_one()
        return user_id if isinstance(user_id, UUID) else None

    async def personal_workspace(self, user_id: UUID) -> WorkspaceSummary | None:
        from sqlalchemy import text  # noqa: PLC0415

        async with self._engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.user_id', :user_id, true)"),
                {"user_id": str(user_id)},
            )
            row = (
                await conn.execute(
                    text(
                        "SELECT id, name FROM public.workspaces "
                        "WHERE kind = 'personal' AND personal_owner_id = :user_id"
                    ),
                    {"user_id": user_id},
                )
            ).one_or_none()
        return None if row is None else WorkspaceSummary(row.id, row.name)
