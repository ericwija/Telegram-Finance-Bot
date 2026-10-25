"""Telegram ``/start`` onboarding (FR-IDN-01, -02, -04, -07; D6 documented default (a)).

This runs before a principal exists, so there is no ``authorize()`` call: the only operations
are the two narrow database functions behind ``IdentityGateway``. Rules:

* a linked Telegram account is never onboarded again: ``/start`` shows its personal workspace
  (FR-IDN-07), with or without a payload, and leaves any invite untouched;
* a suspended or anonymized user is refused (FR-IDN-06), whatever the payload;
* an unknown account without a well-formed ``onb_`` token gets only the onboarding explanation
  (FR-IDN-02), and the database is not written;
* an unknown account with a token is onboarded by ``redeem_onboarding_invite`` in one
  transaction, or refused with one generic outcome (unknown, used, revoked and expired are not
  distinguished to the user; the database audits the reason).
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from finbot.domain.identity import (
    UserStatus,
    WorkspaceSummary,
    normalize_display_name,
    parse_start_payload,
)
from finbot.ports.identity import IdentityGateway

logger = logging.getLogger(__name__)


class StartOutcome(StrEnum):
    ONBOARDED = "onboarded"
    WELCOME_BACK = "welcome_back"
    NEEDS_INVITE = "needs_invite"
    INVITE_REFUSED = "invite_refused"
    ACCOUNT_UNAVAILABLE = "account_unavailable"


@dataclass(frozen=True, slots=True)
class StartRequest:
    telegram_user_id: int
    first_name: str | None
    payload: str | None


@dataclass(frozen=True, slots=True)
class StartResult:
    outcome: StartOutcome
    workspace: WorkspaceSummary | None = None


class OnboardingError(RuntimeError):
    """The database reported an inconsistent onboarding state (never expected)."""


class OnboardingService:
    def __init__(self, identities: IdentityGateway) -> None:
        self._identities = identities

    async def start(self, request: StartRequest) -> StartResult:
        if request.telegram_user_id <= 0:
            raise ValueError("telegram_user_id must be positive")
        principal = await self._identities.resolve_principal(request.telegram_user_id)
        if principal is not None:
            if principal.status is not UserStatus.ACTIVE:
                return StartResult(StartOutcome.ACCOUNT_UNAVAILABLE)
            return StartResult(
                StartOutcome.WELCOME_BACK,
                await self._personal_workspace(principal.user_id),
            )

        token = parse_start_payload(request.payload)
        if token is None:
            return StartResult(StartOutcome.NEEDS_INVITE)

        user_id = await self._identities.redeem_onboarding_invite(
            token.digest(), request.telegram_user_id, normalize_display_name(request.first_name)
        )
        if user_id is None:
            logger.info("onboarding invite refused", extra={"event": "onboarding.refused"})
            return StartResult(StartOutcome.INVITE_REFUSED)
        # A concurrent /start may have onboarded this account first; the function then returned
        # that user. Either way the account is now linked and active.
        logger.info("user onboarded", extra={"event": "onboarding.completed"})
        return StartResult(StartOutcome.ONBOARDED, await self._personal_workspace(user_id))

    async def _personal_workspace(self, user_id: UUID) -> WorkspaceSummary:
        workspace = await self._identities.personal_workspace(user_id)
        if workspace is None:  # FR-IDN-04 guarantees one; the database enforces it
            raise OnboardingError("linked user has no personal workspace")
        return workspace
