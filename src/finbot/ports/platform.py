"""Platform-admin port: onboarding-invite management (docs/08 §3.3, docs/09 §4.3).

Implemented by ``finbot.infra.db.platform``. Every call acts as ``actor_user_id`` (set as
``app.user_id``), and the database refuses it unless that user is an active platform admin.
"""

from typing import Protocol
from uuid import UUID

from finbot.domain.identity import InviteSummary, RevokeOutcome


class PlatformAdminGateway(Protocol):
    async def create_invite(
        self, actor_user_id: UUID, token_hash: bytes, note: str | None, channel: str
    ) -> UUID: ...

    async def list_invites(
        self, actor_user_id: UUID, *, include_inactive: bool
    ) -> list[InviteSummary]: ...

    async def revoke_invite(
        self, actor_user_id: UUID, invite_id: UUID, channel: str
    ) -> RevokeOutcome: ...
