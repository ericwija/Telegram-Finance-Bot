"""Connection targets for PostgreSQL via SQLAlchemy 2.x async + asyncpg (docs/04 §7).

The password is never placed in a URL string, where it could leak through ``repr``, logs or
error messages: the URL carries only the driver, user and database, and host, port and password
travel in asyncpg's ``connect_args``. A host starting with ``/`` is a Unix-socket directory
(user-space test clusters).

Nothing here connects at import time; SQLAlchemy is imported only when an engine is created.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from finbot.migration_settings import MigrationSettings

if TYPE_CHECKING:
    from sqlalchemy.engine import URL
    from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    """Where and as whom to connect. ``password`` is excluded from ``repr``."""

    host: str
    port: int
    database: str
    user: str
    password: SecretStr = field(repr=False)
    application_name: str = "finbot"

    def url(self) -> "URL":
        """A SQLAlchemy URL without host, port or password."""
        from sqlalchemy.engine import URL  # noqa: PLC0415 - lazy: no heavy imports at import time

        return URL.create("postgresql+asyncpg", username=self.user, database=self.database)

    def connect_args(self) -> dict[str, Any]:
        """asyncpg ``connect()`` keyword arguments, including the password."""
        return {
            "host": self.host,
            "port": self.port,
            "password": self.password.get_secret_value(),
            "server_settings": {"application_name": self.application_name},
        }


def owner_target(settings: MigrationSettings) -> ConnectionTarget:
    """The ``finbot_owner`` target for migrations (requires the owner password file)."""
    return ConnectionTarget(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_owner_user,
        password=settings.require_owner_password(),
        application_name="finbot-migrate",
    )


def create_engine(target: ConnectionTarget) -> "AsyncEngine":
    """An async engine without pooling (one-shot tools and tests)."""
    from sqlalchemy.ext.asyncio import create_async_engine  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    return create_async_engine(target.url(), poolclass=NullPool, connect_args=target.connect_args())
