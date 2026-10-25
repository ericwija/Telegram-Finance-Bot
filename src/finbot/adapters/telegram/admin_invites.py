"""Platform-admin invite commands in a private Telegram chat (docs/08 §3.3 "+ bot DM").

The acting admin is **always** the verified sender of the update (``effective_user.id`` from
Telegram), never an argument. The service and the database both require that account to be an
active platform admin. Command names (not in the docs/02 §12 map, which predates them):

* ``/undangan_baru [catatan]``: create a single-use, 7-day invite; the link is sent once
* ``/undangan_daftar [semua]``: list active invites (``semua``: all of them)
* ``/undangan_cabut <ID>``: revoke an unused invite

Replies are HTML; every interpolated value is escaped.
"""

import html
import uuid
from zoneinfo import ZoneInfo

from finbot.adapters.telegram import messages_id as msg
from finbot.domain.identity import (
    INVITE_VALIDITY_DAYS,
    InviteSummary,
    PlatformAdminRequiredError,
    RevokeOutcome,
    onboarding_link,
)
from finbot.services.platform_invites import PlatformInviteService

CREATE_COMMAND = "undangan_baru"
LIST_COMMAND = "undangan_daftar"
REVOKE_COMMAND = "undangan_cabut"
COMMANDS = (CREATE_COMMAND, LIST_COMMAND, REVOKE_COMMAND)


def command_argument(text: str) -> str:
    """Everything after the command word (``/cmd`` or ``/cmd@bot``), stripped."""
    _, _, rest = text.strip().partition(" ")
    return rest.strip()


def _refused(exc: PlatformAdminRequiredError) -> str:
    # FR-IDN-02: an unknown account learns nothing beyond the onboarding explanation.
    return msg.NOT_PERMITTED if exc.linked else msg.START_NEEDS_INVITE


async def create_invite(
    service: PlatformInviteService, sender_id: int, text: str, *, bot_username: str | None
) -> str:
    note = command_argument(text) or None
    try:
        created = await service.create(sender_id, note=note, channel="telegram")
    except PlatformAdminRequiredError as exc:
        return _refused(exc)
    except ValueError:
        return msg.INVITE_NOTE_TOO_LONG
    return msg.INVITE_CREATED.format(
        days=INVITE_VALIDITY_DAYS,
        invite_id=created.invite_id,
        link=html.escape(onboarding_link(created.token, bot_username)),
    )


def _render_item(invite: InviteSummary, tz: ZoneInfo) -> str:
    expires = invite.expires_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    note = f" — {html.escape(invite.note)}" if invite.note else ""
    return msg.INVITE_LIST_ITEM.format(
        invite_id=invite.id,
        status=msg.INVITE_STATUS[invite.status.value],
        expires=f"{expires} ({html.escape(tz.key)})",
        note=note,
    )


async def list_invites(
    service: PlatformInviteService, sender_id: int, text: str, *, timezone: str
) -> str:
    include_inactive = command_argument(text).lower() == "semua"
    try:
        invites = await service.list(sender_id, include_inactive=include_inactive)
    except PlatformAdminRequiredError as exc:
        return _refused(exc)
    if not invites:
        return msg.INVITE_LIST_EMPTY
    tz = ZoneInfo(timezone)
    lines = [msg.INVITE_LIST_HEADER.format(count=len(invites))]
    lines += [_render_item(invite, tz) for invite in invites]
    return "\n".join(lines)


async def revoke_invite(service: PlatformInviteService, sender_id: int, text: str) -> str:
    try:
        invite_id = uuid.UUID(command_argument(text))
    except ValueError:
        invite_id = None
    try:
        if invite_id is None:
            await service.require_admin(sender_id)  # no usage text for non-admins
            return msg.INVITE_REVOKE_USAGE
        outcome = await service.revoke(sender_id, invite_id, channel="telegram")
    except PlatformAdminRequiredError as exc:
        return _refused(exc)
    if outcome is RevokeOutcome.REVOKED:
        return msg.INVITE_REVOKED.format(invite_id=invite_id)
    return msg.INVITE_NOT_REVOKED[outcome.value]
