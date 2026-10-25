"""Start-up guards that need the database (docs/04 §8.3).

G-ENV-2: the bot refuses to serve unless ``current_database()`` on the live connection equals
the configured ``FINBOT_DB_NAME``, which configuration already pinned to ``finbot_<env>``
(``finbot_prod`` for production). This catches a proxy, a service alias or a connection default
that lands on another database.

G-ENV-3: the bot refuses to serve a database whose ``platform_meta`` environment marker differs
from ``FINBOT_ENV`` (for example a staging bot pointed at a production database). Runs as
``finbot_app``, which may only read that table.

G-ENV-7 (Step 6B): the bot refuses to serve a database whose schema revision
(``alembic_version``) is not one this release supports. A database that is behind (for example
still at ``0003``, before the ledger tables) or ahead, or whose revision cannot be read, is
refused before polling starts and before the heartbeat reports healthy. The bot never migrates:
the operator runs ``finbot-migrate`` (docs/10 §13). The revision is read through
``public.app_schema_revision()`` (since revision ``0005``; ``finbot_app`` has no privilege on
``alembic_version`` itself); on an older schema that function does not exist, which is
reported the same way.
"""

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class EnvironmentMismatchError(RuntimeError):
    """The database belongs to another environment, or has no marker."""


class DatabaseNameMismatchError(EnvironmentMismatchError):
    """The live connection is to another database than the configured one (G-ENV-2)."""


class SchemaMismatchError(EnvironmentMismatchError):
    """The database schema revision is not one this release supports (G-ENV-7)."""


#: Schema revisions this release runs against. A release that adds a revision adds it here; an
#: expand-only revision may be listed next to its predecessor while both are compatible.
SUPPORTED_SCHEMA_REVISIONS: Final = frozenset({"0006"})
#: undefined_function (before 0005), insufficient_privilege, undefined_table.
_UNREADABLE: Final = frozenset({"42883", "42501", "42P01"})


async def require_database_name(engine: "AsyncEngine", expected: str) -> None:
    from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time

    async with engine.connect() as conn:
        actual = (await conn.execute(text("SELECT pg_catalog.current_database()"))).scalar_one()
    if actual != expected:
        raise DatabaseNameMismatchError(
            f"connected to database {actual!r}, expected {expected!r} (G-ENV-2)"
        )


async def check_database(engine: "AsyncEngine", *, env: str, db_name: str) -> None:
    """G-ENV-2, G-ENV-3 then G-ENV-7, before anything else touches the database."""
    await require_database_name(engine, db_name)
    await require_environment_marker(engine, env)
    await require_schema_revision(engine)


async def require_schema_revision(
    engine: "AsyncEngine", supported: frozenset[str] = SUPPORTED_SCHEMA_REVISIONS
) -> None:
    from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time
    from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

    expected = ", ".join(sorted(supported))
    try:
        async with engine.connect() as conn:
            revision = (
                await conn.execute(text("SELECT public.app_schema_revision()"))
            ).scalar_one()
    except DBAPIError as exc:
        orig = getattr(exc, "orig", None)
        state = getattr(orig, "sqlstate", None) or getattr(
            getattr(orig, "__cause__", None), "sqlstate", None
        )
        if state not in _UNREADABLE:
            raise
        raise SchemaMismatchError(
            f"database schema revision unreadable; expected {expected}: the operator must "
            "apply the migrations first (G-ENV-7, docs/10 §13)"
        ) from None
    if revision not in supported:
        raise SchemaMismatchError(
            f"database schema revision is {revision!r}, this release supports {expected}: "
            "the operator must migrate (or deploy the matching release) first (G-ENV-7)"
        )


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
