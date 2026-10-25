"""Start-up guards that need the database (docs/04 §8.3).

G-ENV-2: the bot refuses to serve unless ``current_database()`` on the live connection equals
the configured ``FINBOT_DB_NAME``, which configuration already pinned to ``finbot_<env>``
(``finbot_prod`` for production). This catches a proxy, a service alias or a connection default
that lands on another database.

G-ENV-3: the bot refuses to serve a database whose ``platform_meta`` environment marker differs
from ``FINBOT_ENV`` (for example a staging bot pointed at a production database). Runs as
``finbot_app``, which may only read that table.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class EnvironmentMismatchError(RuntimeError):
    """The database belongs to another environment, or has no marker."""


class DatabaseNameMismatchError(EnvironmentMismatchError):
    """The live connection is to another database than the configured one (G-ENV-2)."""


async def require_database_name(engine: "AsyncEngine", expected: str) -> None:
    from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time

    async with engine.connect() as conn:
        actual = (await conn.execute(text("SELECT pg_catalog.current_database()"))).scalar_one()
    if actual != expected:
        raise DatabaseNameMismatchError(
            f"connected to database {actual!r}, expected {expected!r} (G-ENV-2)"
        )


async def check_database(engine: "AsyncEngine", *, env: str, db_name: str) -> None:
    """G-ENV-2 then G-ENV-3, before anything else touches the database."""
    await require_database_name(engine, db_name)
    await require_environment_marker(engine, env)


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
