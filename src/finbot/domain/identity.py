"""Identity values for onboarding (FR-IDN-01..05, T17): pure, no I/O.

* An onboarding deep link is ``/start onb_<token>``. Telegram allows only ``A-Z a-z 0-9 _ -``
  and at most 64 characters in a start parameter; tokens are URL-safe base64 of at least 16
  random bytes (128 bits, T17), so a well-formed token has 22 to 60 characters.
* Only ``sha256(token)`` is ever stored or sent to the database (docs/05 §3).
* The display name defaults to the Telegram first name (FR-IDN-05) and must fit
  ``users.display_name`` (1 to 64 printable characters).
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

ONBOARDING_PREFIX = "onb_"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22,60}")
DISPLAY_NAME_MAX = 64
#: Used when Telegram gives no usable first name. Internal fallback, not a user-facing message.
FALLBACK_DISPLAY_NAME = "Pengguna"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ANONYMIZED = "anonymized"


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    """What ``resolve_principal`` returns for a linked Telegram account."""

    user_id: UUID
    status: UserStatus
    is_platform_admin: bool


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class OnboardingToken:
    """A syntactically valid onboarding token. Its value never leaves the process."""

    value: str

    def digest(self) -> bytes:
        return hashlib.sha256(self.value.encode("ascii")).digest()

    def __repr__(self) -> str:
        return "OnboardingToken(<redacted>)"


def parse_start_payload(payload: str | None) -> OnboardingToken | None:
    """The onboarding token in a ``/start`` payload, or None for anything else."""
    if payload is None or not payload.startswith(ONBOARDING_PREFIX):
        return None
    token = payload.removeprefix(ONBOARDING_PREFIX)
    if not _TOKEN_RE.fullmatch(token):
        return None
    return OnboardingToken(token)


def normalize_display_name(first_name: str | None) -> str:
    """A valid ``users.display_name`` from a Telegram first name (FR-IDN-05)."""
    text = unicodedata.normalize("NFC", first_name or "")
    # Drop control and format characters (Cc, Cf), collapse whitespace.
    cleaned = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    cleaned = " ".join(cleaned.split())[:DISPLAY_NAME_MAX].strip()
    return cleaned or FALLBACK_DISPLAY_NAME


# ------------------------------------------------------------ platform: onboarding invites

#: FR-IDN-01: invites are single use and expire after 7 days (fixed in the database function).
INVITE_VALIDITY_DAYS = 7
_BOT_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


class PlatformAdminRequiredError(PermissionError):
    """The acting user is not an active platform admin (docs/08 §3.3).

    ``linked`` says whether the Telegram account belongs to any user at all, so that an unknown
    account can be answered with the onboarding explanation only (FR-IDN-02).
    """

    def __init__(
        self, message: str = "an active platform admin is required", *, linked: bool = True
    ):
        super().__init__(message)
        self.linked = linked


class InviteStatus(StrEnum):
    ACTIVE = "active"
    USED = "used"
    REVOKED = "revoked"
    EXPIRED = "expired"


class RevokeOutcome(StrEnum):
    """``revoked``, or why nothing changed."""

    REVOKED = "revoked"
    NOT_FOUND = "not_found"
    USED = "used"
    ALREADY_REVOKED = "already_revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class InviteSummary:
    """Invite metadata for listings. Never contains the token or its hash."""

    id: UUID
    status: InviteStatus
    created_at: datetime
    expires_at: datetime
    note: str | None
    created_by: UUID
    used_by: UUID | None


def onboarding_payload(token: OnboardingToken) -> str:
    return f"{ONBOARDING_PREFIX}{token.value}"


def onboarding_link(token: OnboardingToken, bot_username: str | None) -> str:
    """``https://t.me/<bot>?start=onb_<token>``, or the bare ``/start`` command without a bot."""
    if bot_username is None:
        return f"/start {onboarding_payload(token)}"
    name = bot_username.removeprefix("@")
    if not _BOT_USERNAME_RE.fullmatch(name):
        raise ValueError("invalid bot username")
    return f"https://t.me/{name}?start={onboarding_payload(token)}"
