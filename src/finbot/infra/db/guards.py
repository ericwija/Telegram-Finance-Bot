"""Start-up guards that need the database (docs/04 §8.3).

G-ENV-3: the bot refuses to serve a database whose ``platform_meta`` environment marker differs
from ``FINBOT_ENV`` (for example a staging bot pointed at a production database). Runs as
``finbot_app``, which may only read that table.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class EnvironmentMismatchError(RuntimeError):
    """The database belongs to another environment, or has no marker."""


async def require_environment_marker(engine: "AsyncEngine", env: str) -> None:
    from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time

    async with engine.connect() as conn:
        marker = (
            await conn.execute(
                text("SELECT value FROM public.platform_meta WHERE key = 'environment'")
            )
        ).scalar_one_or_none()
    if marker != env:
        raise EnvironmentMismatchError(
            f"database environment marker is {marker!r}, expected {env!r} (G-ENV-3)"
        )
