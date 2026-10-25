"""One-time first-platform-admin bootstrap, as ``finbot_owner`` (owner decision 2026-10-25).

Called only by ``finbot-migrate bootstrap-admin``: never by migrations, never at application
start-up, and never with the bot's credentials. In one transaction it checks that the database's
environment marker matches ``FINBOT_ENV`` (G-ENV-3) and that revision 0003 is installed, then
calls ``bootstrap_platform_admin``, which refuses if the permanent bootstrap marker exists.
"""

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

#: SQLSTATEs the bootstrap function raises on purpose (everything else is unexpected).
_REFUSAL_STATES = frozenset({"55000", "42501", "22023"})


class BootstrapRefusedError(RuntimeError):
    """The bootstrap was refused (already done, wrong environment, invalid input...)."""


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(
        getattr(orig, "__cause__", None), "sqlstate", None
    )


async def bootstrap_platform_admin(
    engine: "AsyncEngine", *, env: str, telegram_user_id: int, display_name: str
) -> UUID:
    from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time
    from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

    try:
        async with engine.begin() as conn:
            installed = (
                await conn.execute(
                    text(
                        "SELECT to_regprocedure("
                        "'public.bootstrap_platform_admin(bigint, text)') IS NOT NULL"
                    )
                )
            ).scalar_one()
            if not installed:
                raise BootstrapRefusedError(
                    "the schema is not at revision 0003 or later; run 'finbot-migrate upgrade' "
                    "first"
                )
            marker = (
                await conn.execute(
                    text("SELECT value FROM public.platform_meta WHERE key = 'environment'")
                )
            ).scalar_one_or_none()
            if marker != env:
                raise BootstrapRefusedError(
                    f"database environment marker is {marker!r}, not {env!r} (G-ENV-3)"
                )
            user_id = (
                await conn.execute(
                    text("SELECT public.bootstrap_platform_admin(:tg_user_id, :display_name)"),
                    {"tg_user_id": telegram_user_id, "display_name": display_name},
                )
            ).scalar_one()
    except DBAPIError as exc:
        if _sqlstate(exc) in _REFUSAL_STATES:
            message = str(getattr(exc, "orig", exc)).splitlines()[0]
            raise BootstrapRefusedError(message.split(": ", 1)[-1]) from None
        raise
    if not isinstance(user_id, UUID):  # pragma: no cover - the function returns uuid
        raise TypeError("bootstrap_platform_admin returned no ID")
    return user_id
