"""``/start`` handling, independent of the Telegram framework (wired in at the composition root).

The adapter extracts only what the service needs from the update: the sender's Telegram user
ID, first name and the start payload. Only private chats are answered (T16); the workspace is
never taken from the update (docs/09 L1). Replies are HTML with every interpolation escaped.
"""

import html
from dataclasses import dataclass

from finbot.adapters.telegram import messages_id as msg
from finbot.services.onboarding import (
    OnboardingService,
    StartOutcome,
    StartRequest,
    StartResult,
)


@dataclass(frozen=True, slots=True)
class StartUpdate:
    """The fields of a Telegram message that ``/start`` uses."""

    telegram_user_id: int
    chat_type: str
    first_name: str | None
    text: str


def start_payload(text: str) -> str | None:
    """The deep-link payload of ``/start <payload>`` (also ``/start@bot <payload>``)."""
    command, _, rest = text.strip().partition(" ")
    if command != "/start" and not command.startswith("/start@"):
        return None
    payload = rest.strip()
    return payload or None


def render(result: StartResult) -> str:
    match result.outcome:
        case StartOutcome.ONBOARDED:
            return (
                msg.START_ONBOARDED.format(workspace=_workspace_name(result)) + msg.START_MENU_HINT
            )
        case StartOutcome.WELCOME_BACK:
            return (
                msg.START_WELCOME_BACK.format(workspace=_workspace_name(result))
                + msg.START_MENU_HINT
            )
        case StartOutcome.NEEDS_INVITE:
            return msg.START_NEEDS_INVITE
        case StartOutcome.INVITE_REFUSED:
            return msg.START_INVITE_REFUSED
        case StartOutcome.ACCOUNT_UNAVAILABLE:
            return msg.START_ACCOUNT_UNAVAILABLE


def _workspace_name(result: StartResult) -> str:
    if result.workspace is None:
        raise ValueError(f"{result.outcome} needs a workspace")
    return html.escape(result.workspace.name)


def start_request(update: StartUpdate) -> StartRequest | None:
    """The service request for a ``/start`` update, or None when it must not be answered."""
    if update.chat_type != "private":
        return None
    return StartRequest(
        telegram_user_id=update.telegram_user_id,
        first_name=update.first_name,
        payload=start_payload(update.text),
    )


async def handle_start(service: OnboardingService, update: StartUpdate) -> str | None:
    """The HTML reply to ``/start``, or None when the update must not be answered."""
    request = start_request(update)
    if request is None:
        return None
    return render(await service.start(request))
