"""Onboarding fixtures: platform admins and invites seeded as finbot_owner (no real tokens).

Invite creation by a platform admin (FR-IDN-01, CLI) is not implemented yet, so tests insert
invites directly as the owner, exactly as the schema stores them: only ``sha256(token)``.
"""

import hashlib
import secrets
import uuid
from typing import Any

from sqlalchemy import text

from finbot.infra.db.identity import PgIdentityGateway
from finbot.services.onboarding import OnboardingService

COUNTED_TABLES = (
    "users",
    "telegram_identities",
    "workspaces",
    "workspace_memberships",
    "audit_events",
)


def new_token() -> str:
    """A fresh 128-bit URL-safe token (22 characters), like the future invite CLI."""
    return secrets.token_urlsafe(16)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


async def create_admin(owner_engine: Any) -> str:
    admin = str(uuid.uuid4())
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, display_name, is_platform_admin) "
                "VALUES (:id, 'Admin', true)"
            ),
            {"id": admin},
        )
    return admin


async def create_invite(
    owner_engine: Any, admin: str, *, expires_in: int = 7 * 86400, revoked: bool = False
) -> str:
    """Insert an invite and return its plaintext token (kept only by the test)."""
    token = new_token()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO onboarding_invites (token_hash, created_by, expires_at, revoked_at) "
                "VALUES (:hash, :admin, now() + make_interval(secs => :secs), "
                "CASE WHEN :revoked THEN now() END)"
            ),
            {"hash": token_hash(token), "admin": admin, "secs": expires_in, "revoked": revoked},
        )
    return token


async def counts(owner_engine: Any) -> dict[str, int]:
    async with owner_engine.connect() as conn:
        return {
            table: (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            for table in COUNTED_TABLES
        }


async def invite_state(owner_engine: Any, token: str) -> tuple[Any, Any]:
    """(used_at, used_by) of the invite for ``token``."""
    async with owner_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT used_at, used_by FROM onboarding_invites WHERE token_hash = :h"),
                {"h": token_hash(token)},
            )
        ).one()
    return row.used_at, row.used_by


def service(app_engine: Any) -> OnboardingService:
    return OnboardingService(PgIdentityGateway(app_engine))
