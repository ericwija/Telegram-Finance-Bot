"""Telegram account → acting user, for every finance update (FR-IDN-02, FR-IDN-06).

The Telegram sender ID is the only identity input; it is resolved through the narrow
``resolve_principal`` function each time (no caching, so a suspension applies at once). The
platform-admin flag is deliberately dropped: it grants nothing inside a workspace (docs/08 §7).
"""

from finbot.domain.identity import UserStatus
from finbot.ports.identity import IdentityGateway
from finbot.services.finance import Actor


class UnknownAccountError(LookupError):
    """The Telegram account is not onboarded (FR-IDN-02: only the onboarding explanation)."""


class AccountUnavailableError(PermissionError):
    """The user is suspended or anonymized (FR-IDN-06)."""


class PrincipalService:
    def __init__(self, identities: IdentityGateway) -> None:
        self._identities = identities

    async def actor(self, telegram_user_id: int, *, channel: str = "telegram") -> Actor:
        if telegram_user_id <= 0:
            raise UnknownAccountError("invalid Telegram user ID")
        principal = await self._identities.resolve_principal(telegram_user_id)
        if principal is None:
            raise UnknownAccountError("not onboarded")
        if principal.status is not UserStatus.ACTIVE:
            raise AccountUnavailableError("user is not active")
        return Actor(principal.user_id, None, channel)
