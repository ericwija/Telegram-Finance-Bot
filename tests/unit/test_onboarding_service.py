"""OnboardingService and the /start adapter with a fake gateway (no database)."""

import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest

from finbot.adapters.telegram import messages_id as msg
from finbot.adapters.telegram.start import StartUpdate, handle_start, render, start_payload
from finbot.domain.identity import PrincipalRecord, UserStatus, WorkspaceSummary
from finbot.services.onboarding import (
    OnboardingError,
    OnboardingService,
    StartOutcome,
    StartRequest,
    StartResult,
)

TOKEN = "Q" * 22
TG = 4242


@dataclass
class FakeGateway:
    principal: PrincipalRecord | None = None
    redeem_result: UUID | None = None
    workspace: WorkspaceSummary | None = field(
        default_factory=lambda: WorkspaceSummary(uuid.uuid4(), "Pribadi")
    )
    calls: list[tuple[str, Any]] = field(default_factory=list)

    async def resolve_principal(self, telegram_user_id: int) -> PrincipalRecord | None:
        self.calls.append(("resolve", telegram_user_id))
        return self.principal

    async def redeem_onboarding_invite(
        self, token_hash: bytes, telegram_user_id: int, display_name: str
    ) -> UUID | None:
        self.calls.append(("redeem", (token_hash, telegram_user_id, display_name)))
        return self.redeem_result

    async def personal_workspace(self, user_id: UUID) -> WorkspaceSummary | None:
        self.calls.append(("workspace", user_id))
        return self.workspace


def _principal(status: UserStatus = UserStatus.ACTIVE) -> PrincipalRecord:
    return PrincipalRecord(uuid.uuid4(), status, False)


@pytest.mark.asyncio
async def test_unknown_account_without_token_needs_an_invite_and_writes_nothing() -> None:
    gateway = FakeGateway()
    result = await OnboardingService(gateway).start(StartRequest(TG, "Budi", None))
    assert result == StartResult(StartOutcome.NEEDS_INVITE)
    assert [c[0] for c in gateway.calls] == ["resolve"]


@pytest.mark.asyncio
async def test_valid_token_is_redeemed_by_hash_with_the_normalised_name() -> None:
    user = uuid.uuid4()
    gateway = FakeGateway(redeem_result=user)
    result = await OnboardingService(gateway).start(StartRequest(TG, "  Budi  ", f"onb_{TOKEN}"))
    assert result.outcome is StartOutcome.ONBOARDED
    assert result.workspace == gateway.workspace
    kind, (digest, tg, name) = gateway.calls[1]
    assert kind == "redeem"
    assert tg == TG
    assert name == "Budi"
    assert isinstance(digest, bytes)
    assert len(digest) == 32
    assert TOKEN.encode() not in digest
    assert gateway.calls[2] == ("workspace", user)


@pytest.mark.asyncio
async def test_refused_invite_is_one_generic_outcome() -> None:
    gateway = FakeGateway(redeem_result=None)
    result = await OnboardingService(gateway).start(StartRequest(TG, "Budi", f"onb_{TOKEN}"))
    assert result == StartResult(StartOutcome.INVITE_REFUSED)


@pytest.mark.parametrize("payload", [None, f"onb_{TOKEN}", "anything"])
@pytest.mark.asyncio
async def test_linked_account_is_welcomed_back_and_never_redeems(payload: str | None) -> None:
    principal = _principal()
    gateway = FakeGateway(principal=principal)
    result = await OnboardingService(gateway).start(StartRequest(TG, "Budi", payload))
    assert result.outcome is StartOutcome.WELCOME_BACK
    assert [c[0] for c in gateway.calls] == ["resolve", "workspace"]
    assert gateway.calls[1] == ("workspace", principal.user_id)


@pytest.mark.parametrize("status", [UserStatus.SUSPENDED, UserStatus.ANONYMIZED])
@pytest.mark.asyncio
async def test_inactive_user_is_refused_whatever_the_payload(status: UserStatus) -> None:
    gateway = FakeGateway(principal=_principal(status))
    result = await OnboardingService(gateway).start(StartRequest(TG, "Budi", f"onb_{TOKEN}"))
    assert result == StartResult(StartOutcome.ACCOUNT_UNAVAILABLE)
    assert [c[0] for c in gateway.calls] == ["resolve"]


@pytest.mark.asyncio
async def test_missing_personal_workspace_is_an_error_not_a_silent_success() -> None:
    gateway = FakeGateway(principal=_principal(), workspace=None)
    with pytest.raises(OnboardingError):
        await OnboardingService(gateway).start(StartRequest(TG, "Budi", None))


@pytest.mark.asyncio
async def test_non_positive_telegram_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        await OnboardingService(FakeGateway()).start(StartRequest(0, "Budi", None))


@pytest.mark.asyncio
async def test_service_logs_no_token_or_name(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    gateway = FakeGateway(redeem_result=uuid.uuid4())
    await OnboardingService(gateway).start(StartRequest(TG, "Rahasia", f"onb_{TOKEN}"))
    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert TOKEN not in text
    assert "Rahasia" not in text


# ------------------------------------------------------------------------------- adapter


@pytest.mark.parametrize(
    ("text", "payload"),
    [
        ("/start", None),
        ("/start   ", None),
        (f"/start onb_{TOKEN}", f"onb_{TOKEN}"),
        (f"/start@finbot_staging_bot onb_{TOKEN}", f"onb_{TOKEN}"),
        ("/help onb_x", None),
        ("/startx onb_x", None),
    ],
)
def test_start_payload_extraction(text: str, payload: str | None) -> None:
    assert start_payload(text) == payload


@pytest.mark.asyncio
async def test_group_chats_are_not_answered_or_processed() -> None:
    gateway = FakeGateway()
    for chat_type in ("group", "supergroup", "channel"):
        update = StartUpdate(TG, chat_type, "Budi", f"/start onb_{TOKEN}")
        assert await handle_start(OnboardingService(gateway), update) is None
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_private_start_renders_the_catalogue_message() -> None:
    reply = await handle_start(
        OnboardingService(FakeGateway()), StartUpdate(TG, "private", "Budi", "/start")
    )
    assert reply == msg.START_NEEDS_INVITE


def test_workspace_names_are_html_escaped() -> None:
    workspace = WorkspaceSummary(uuid.uuid4(), "<script>&")
    for outcome in (StartOutcome.ONBOARDED, StartOutcome.WELCOME_BACK):
        reply = render(StartResult(outcome, workspace))
        assert "&lt;script&gt;&amp;" in reply
        assert "<script>" not in reply


def test_every_outcome_renders() -> None:
    workspace = WorkspaceSummary(uuid.uuid4(), "Pribadi")
    for outcome in StartOutcome:
        assert render(StartResult(outcome, workspace))
    with pytest.raises(ValueError, match="needs a workspace"):
        render(StartResult(StartOutcome.ONBOARDED))
