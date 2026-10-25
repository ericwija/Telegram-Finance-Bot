"""The single-poller lock (G-ENV-6, NFR-AVL-06, AC-P1-04).

Only one Telegram long-polling process may run per environment. The bot takes a
**session-level** advisory lock on a dedicated connection before it contacts Telegram, and
holds that connection for its whole polling lifetime:

* ``pg_try_advisory_lock`` never waits. A second process gets ``false`` and refuses to start.
* The lock belongs to the database session, so it disappears when the connection does: a
  crashed or killed bot, a terminated backend or a lost network connection all release it
  without operator action, and a restarted bot can take it again.
* The connection runs in autocommit mode, so it is never "idle in transaction".
* :meth:`PollerLock.verify` asks ``pg_locks`` whether this very backend still holds the lock.
  The runtime calls it on every heartbeat tick and stops polling as soon as it fails.

The key uses the two-``int4`` form ``(class, hashtext('telegram-poller:<env>'))``. That key space
is disjoint from the one-``bigint`` keys used by migrations and invite redemption. Each
environment also has its own database (G-ENV-2), and advisory locks are per database.
"""

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = logging.getLogger(__name__)

#: Advisory-lock class for the poller ("fbpt" in ASCII; fits a signed int4).
POLLER_LOCK_CLASS = 0x66627074
#: Bounded wait for every query on the lock connection.
LOCK_QUERY_TIMEOUT = 5.0

_TRY_LOCK = (
    "SELECT pg_catalog.pg_try_advisory_lock(:cls, pg_catalog.hashtext(:key)), "
    "pg_catalog.pg_backend_pid()"
)
_HOLDS_LOCK = (
    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks WHERE locktype = 'advisory' "
    "AND granted AND pid = pg_catalog.pg_backend_pid() AND database = "
    "(SELECT oid FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database()) "
    "AND classid::bigint = :cls AND objid::bigint = "
    "(pg_catalog.hashtext(:key)::bigint & 4294967295) AND objsubid = 2)"
)
_UNLOCK = "SELECT pg_catalog.pg_advisory_unlock(:cls, pg_catalog.hashtext(:key))"


class PollerLockHeldError(RuntimeError):
    """Another process holds the poller lock for this environment."""


def lock_key(env: str) -> str:
    return f"telegram-poller:{env}"


class PollerLock:
    """A held poller lock and its dedicated connection. Create with :meth:`acquire`."""

    def __init__(self, conn: "AsyncConnection", env: str, backend_pid: int) -> None:
        self._conn: AsyncConnection | None = conn
        self._params: dict[str, Any] = {"cls": POLLER_LOCK_CLASS, "key": lock_key(env)}
        self.backend_pid = backend_pid

    @classmethod
    async def acquire(cls, engine: "AsyncEngine", env: str) -> Self:
        """Take the lock without waiting, or raise :class:`PollerLockHeldError`."""
        from sqlalchemy import text  # noqa: PLC0415 - lazy: no heavy imports at import time

        conn = await engine.connect()
        try:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            params = {"cls": POLLER_LOCK_CLASS, "key": lock_key(env)}
            row = (
                await asyncio.wait_for(conn.execute(text(_TRY_LOCK), params), LOCK_QUERY_TIMEOUT)
            ).one()
        except BaseException:
            await _close_quietly(conn)
            raise
        acquired, pid = bool(row[0]), int(row[1])
        if not acquired:
            await _close_quietly(conn)
            raise PollerLockHeldError(
                f"poller lock held by another process for environment {env!r} (G-ENV-6)"
            )
        return cls(conn, env, pid)

    @property
    def released(self) -> bool:
        return self._conn is None

    async def verify(self) -> bool:
        """Whether this session still holds the lock. Any error counts as "lost"."""
        from sqlalchemy import text  # noqa: PLC0415

        if self._conn is None:
            return False
        try:
            result = await asyncio.wait_for(
                self._conn.execute(text(_HOLDS_LOCK), self._params), LOCK_QUERY_TIMEOUT
            )
            return bool(result.scalar_one())
        except Exception as exc:  # noqa: BLE001 - connection loss in any form means "lost"
            logger.warning(
                "poller lock check failed",
                extra={"event": "poller_lock.check_failed", "error_type": type(exc).__name__},
            )
            return False

    async def release(self) -> None:
        """Unlock and close the connection. Idempotent; never raises on a dead connection."""
        from sqlalchemy import text  # noqa: PLC0415

        conn, self._conn = self._conn, None
        if conn is None:
            return
        # Closing the session releases the lock anyway, so a failed unlock is harmless.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.execute(text(_UNLOCK), self._params), LOCK_QUERY_TIMEOUT)
        await _close_quietly(conn)


async def _close_quietly(conn: "AsyncConnection") -> None:
    with contextlib.suppress(Exception):  # the connection is gone; nothing left to release
        await conn.close()
