"""Identity port: the onboarding and principal-resolution database functions (docs/09 §4.3).

Implemented by ``finbot.infra.db.identity``. Each call is one database transaction.
"""

from typing import Protocol
from uuid import UUID

from finbot.domain.identity import PrincipalRecord, WorkspaceSummary


class IdentityGateway(Protocol):
    async def resolve_principal(self, telegram_user_id: int) -> PrincipalRecord | None:
        """The linked user for a Telegram account, or None if it has no active identity."""
        ...

    async def redeem_onboarding_invite(
        self, token_hash: bytes, telegram_user_id: int, display_name: str
    ) -> UUID | None:
        """Onboard atomically; the (new or existing) user ID, or None if the invite is refused."""
        ...

    async def personal_workspace(self, user_id: UUID) -> WorkspaceSummary | None:
        """The user's personal workspace, read as that user (RLS applies)."""
        ...
