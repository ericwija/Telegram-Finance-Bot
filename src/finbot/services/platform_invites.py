"""Platform-admin onboarding-invite management (FR-IDN-01, docs/08 §3.3).

Authorization comes first in every method: the acting Telegram account is resolved server-side
to its user, which must be an **active platform admin** (``platform.onboarding_invite.*``).
The database functions check the same again, so a bypass of this layer still fails closed.

A new token is 128 random bits (T17). Only its SHA-256 leaves this process; the plaintext is
returned once to the caller for delivery and is never logged or stored.
"""

import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from finbot.domain.identity import (
    InviteSummary,
    OnboardingToken,
    PlatformAdminRequiredError,
    RevokeOutcome,
    UserStatus,
)
from finbot.ports.identity import IdentityGateway
from finbot.ports.platform import PlatformAdminGateway

logger = logging.getLogger(__name__)

Channel = Literal["cli", "telegram"]
NOTE_MAX = 200


def new_onboarding_token() -> OnboardingToken:
    return OnboardingToken(secrets.token_urlsafe(16))


@dataclass(frozen=True, slots=True)
class CreatedInvite:
    invite_id: UUID
    token: OnboardingToken  # shown once, then discarded


class PlatformInviteService:
    def __init__(
        self,
        identities: IdentityGateway,
        platform: PlatformAdminGateway,
        *,
        token_factory: Callable[[], OnboardingToken] = new_onboarding_token,
    ) -> None:
        self._identities = identities
        self._platform = platform
        self._new_token = token_factory

    async def _authorize_admin(self, actor_telegram_user_id: int) -> UUID:
        principal = await self._identities.resolve_principal(actor_telegram_user_id)
        if (
            principal is None
            or principal.status is not UserStatus.ACTIVE
            or not principal.is_platform_admin
        ):
            logger.warning("platform admin required", extra={"event": "security.authz_denied"})
            raise PlatformAdminRequiredError("an active platform admin is required")
        return principal.user_id

    async def create(
        self, actor_telegram_user_id: int, *, note: str | None, channel: Channel
    ) -> CreatedInvite:
        admin = await self._authorize_admin(actor_telegram_user_id)
        cleaned = " ".join((note or "").split()) or None
        if cleaned is not None and len(cleaned) > NOTE_MAX:
            raise ValueError(f"note must be at most {NOTE_MAX} characters")
        token = self._new_token()
        invite_id = await self._platform.create_invite(admin, token.digest(), cleaned, channel)
        logger.info(
            "onboarding invite created",
            extra={"event": "onboarding_invite.created", "user_id": admin},
        )
        return CreatedInvite(invite_id, token)

    async def list(
        self, actor_telegram_user_id: int, *, include_inactive: bool
    ) -> list[InviteSummary]:
        admin = await self._authorize_admin(actor_telegram_user_id)
        return await self._platform.list_invites(admin, include_inactive=include_inactive)

    async def revoke(
        self, actor_telegram_user_id: int, invite_id: UUID, *, channel: Channel
    ) -> RevokeOutcome:
        admin = await self._authorize_admin(actor_telegram_user_id)
        outcome = await self._platform.revoke_invite(admin, invite_id, channel)
        logger.info(
            "onboarding invite revoke",
            extra={"event": "onboarding_invite.revoke", "outcome": outcome, "user_id": admin},
        )
        return outcome
