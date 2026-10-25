"""PlatformInviteService, invite links and the finbot-admin / bootstrap CLIs (no database)."""

import argparse
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from finbot.adapters.cli import admin, migrate
from finbot.domain.identity import (
    InviteStatus,
    InviteSummary,
    OnboardingToken,
    PlatformAdminRequiredError,
    PrincipalRecord,
    RevokeOutcome,
    UserStatus,
    onboarding_link,
    parse_start_payload,
)
from finbot.services.platform_invites import PlatformInviteService, new_onboarding_token

ADMIN_TG = 77
TOKEN = OnboardingToken("T" * 22)


@dataclass
class FakeIdentities:
    principal: PrincipalRecord | None

    async def resolve_principal(self, telegram_user_id: int) -> PrincipalRecord | None:
        return self.principal

    async def redeem_onboarding_invite(self, *_: Any) -> UUID | None:  # pragma: no cover
        raise AssertionError("not used")

    async def personal_workspace(self, user_id: UUID) -> Any:  # pragma: no cover
        raise AssertionError("not used")


@dataclass
class FakePlatform:
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    revoke_outcome: RevokeOutcome = RevokeOutcome.REVOKED
    invites: list[InviteSummary] = field(default_factory=list)

    async def create_invite(
        self, actor_user_id: UUID, token_hash: bytes, note: str | None, channel: str
    ) -> UUID:
        self.calls.append(("create", actor_user_id, token_hash, note, channel))
        return uuid.UUID(int=1)

    async def list_invites(
        self, actor_user_id: UUID, *, include_inactive: bool
    ) -> list[InviteSummary]:
        self.calls.append(("list", actor_user_id, include_inactive))
        return self.invites

    async def revoke_invite(
        self, actor_user_id: UUID, invite_id: UUID, channel: str
    ) -> RevokeOutcome:
        self.calls.append(("revoke", actor_user_id, invite_id, channel))
        return self.revoke_outcome


def _admin(*, status: UserStatus = UserStatus.ACTIVE, is_admin: bool = True) -> PrincipalRecord:
    return PrincipalRecord(uuid.uuid4(), status, is_admin)


def _service(principal: PrincipalRecord | None, platform: FakePlatform) -> PlatformInviteService:
    return PlatformInviteService(FakeIdentities(principal), platform, token_factory=lambda: TOKEN)


# ------------------------------------------------------------------------------ service


def test_new_tokens_are_128_bit_and_parse_as_onboarding_payloads() -> None:
    tokens = {new_onboarding_token().value for _ in range(50)}
    assert len(tokens) == 50
    for value in tokens:
        assert len(value) == 22
        assert parse_start_payload(f"onb_{value}") == OnboardingToken(value)


@pytest.mark.asyncio
async def test_create_sends_only_the_digest_as_the_admin() -> None:
    principal = _admin()
    platform = FakePlatform()
    created = await _service(principal, platform).create(ADMIN_TG, note="  a  b ", channel="cli")
    assert created.token == TOKEN
    assert platform.calls == [("create", principal.user_id, TOKEN.digest(), "a b", "cli")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal",
    [
        None,
        _admin(is_admin=False),
        _admin(status=UserStatus.SUSPENDED),
        _admin(status=UserStatus.ANONYMIZED),
    ],
)
async def test_non_admins_are_refused_before_the_database_is_called(
    principal: PrincipalRecord | None,
) -> None:
    platform = FakePlatform()
    service = _service(principal, platform)
    with pytest.raises(PlatformAdminRequiredError):
        await service.create(ADMIN_TG, note=None, channel="cli")
    with pytest.raises(PlatformAdminRequiredError):
        await service.list(ADMIN_TG, include_inactive=False)
    with pytest.raises(PlatformAdminRequiredError):
        await service.revoke(ADMIN_TG, uuid.uuid4(), channel="cli")
    assert platform.calls == []


@pytest.mark.asyncio
async def test_overlong_notes_are_rejected() -> None:
    with pytest.raises(ValueError, match="200"):
        await _service(_admin(), FakePlatform()).create(ADMIN_TG, note="x" * 201, channel="cli")


@pytest.mark.asyncio
async def test_service_never_logs_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    await _service(_admin(), FakePlatform()).create(ADMIN_TG, note="for Siti", channel="cli")
    logged = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert TOKEN.value not in logged
    assert "Siti" not in logged


def test_links() -> None:
    assert onboarding_link(TOKEN, "finbot_staging_bot") == (
        f"https://t.me/finbot_staging_bot?start=onb_{TOKEN.value}"
    )
    assert onboarding_link(TOKEN, "@finbot_staging_bot").startswith("https://t.me/finbot_staging")
    assert onboarding_link(TOKEN, None) == f"/start onb_{TOKEN.value}"
    with pytest.raises(ValueError, match="bot username"):
        onboarding_link(TOKEN, "bad/name")


# ------------------------------------------------------------------------ finbot-admin


def _parse(*argv: str) -> argparse.Namespace:
    return admin._parser().parse_args(list(argv))


async def _run(
    argv: list[str], principal: PrincipalRecord | None, platform: FakePlatform
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = await admin.run(
        _parse(*argv), _service(principal, platform), out, err, bot_username="finbot_bot"
    )
    return code, out.getvalue(), err.getvalue()


@pytest.mark.asyncio
async def test_admin_create_prints_the_link_once_on_stdout_only() -> None:
    code, out, err = await _run(["invite", "create", "--as", "77"], _admin(), FakePlatform())
    assert code == admin.EXIT_OK
    assert err == ""
    assert out.count(TOKEN.value) == 1
    assert f"https://t.me/finbot_bot?start=onb_{TOKEN.value}" in out
    assert "expires in 7 days" in out


@pytest.mark.asyncio
async def test_admin_list_prints_metadata_without_tokens() -> None:
    now = datetime(2026, 10, 25, tzinfo=UTC)
    invite = InviteSummary(
        uuid.UUID(int=5),
        InviteStatus.ACTIVE,
        now,
        now + timedelta(days=7),
        "Budi",
        uuid.UUID(int=9),
        None,
    )
    platform = FakePlatform(invites=[invite])
    code, out, _ = await _run(["invite", "list", "--as", "77", "--all"], _admin(), platform)
    assert code == admin.EXIT_OK
    assert out.splitlines() == [
        "invite_id\tstatus\tcreated_at\texpires_at\tnote",
        f"{invite.id}\tactive\t{now.isoformat()}\t{invite.expires_at.isoformat()}\tBudi",
    ]
    assert platform.calls[0][2] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (RevokeOutcome.REVOKED, admin.EXIT_OK),
        (RevokeOutcome.USED, admin.EXIT_REFUSED),
        (RevokeOutcome.NOT_FOUND, admin.EXIT_REFUSED),
        (RevokeOutcome.ALREADY_REVOKED, admin.EXIT_REFUSED),
    ],
)
async def test_admin_revoke_exit_codes(outcome: RevokeOutcome, code: int) -> None:
    invite_id = str(uuid.uuid4())
    result = await _run(
        ["invite", "revoke", "--as", "77", invite_id],
        _admin(),
        FakePlatform(revoke_outcome=outcome),
    )
    assert result[0] == code
    if code != admin.EXIT_OK:
        assert str(outcome) in result[2]


@pytest.mark.asyncio
async def test_admin_refuses_non_admins_with_exit_3() -> None:
    code, out, err = await _run(["invite", "create", "--as", "77"], None, FakePlatform())
    assert code == admin.EXIT_REFUSED
    assert out == ""
    assert "not linked to an active platform admin" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["invite", "create"],  # --as is required: no default actor
        ["invite", "create", "--as", "0"],
        ["invite", "create", "--as", "-5"],
        ["invite", "create", "--as", "abc"],
        ["invite", "revoke", "--as", "77", "not-a-uuid"],
    ],
)
def test_admin_argument_validation(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _parse(*argv)
    assert excinfo.value.code == 2


def test_admin_main_reports_configuration_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINBOT_ENV", raising=False)
    err = io.StringIO()
    assert admin.main(["invite", "list", "--as", "77"], out=io.StringIO(), err=err) == 2
    assert "configuration error" in err.getvalue()


def test_admin_main_needs_the_app_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINBOT_ENV", "test")
    err = io.StringIO()
    assert admin.main(["invite", "list", "--as", "77"], out=io.StringIO(), err=err) == 2
    assert "FINBOT_DB_APP_PASSWORD_FILE" in err.getvalue()


# ------------------------------------------------------------- finbot-migrate bootstrap


@pytest.mark.parametrize(
    "argv",
    [
        ["bootstrap-admin"],
        ["bootstrap-admin", "--display-name", "Op"],  # no default Telegram ID
        ["bootstrap-admin", "--telegram-user-id", "123"],  # no default name
        ["bootstrap-admin", "--telegram-user-id", "0", "--display-name", "Op"],
        ["bootstrap-admin", "--telegram-user-id", "12x", "--display-name", "Op"],
    ],
)
def test_bootstrap_requires_explicit_valid_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        migrate._parser().parse_args(argv)
    assert excinfo.value.code == 2


def test_bootstrap_needs_the_owner_password_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINBOT_ENV", "test")
    monkeypatch.delenv("FINBOT_DB_OWNER_PASSWORD_FILE", raising=False)
    err = io.StringIO()
    code = migrate.main(
        ["bootstrap-admin", "--telegram-user-id", "5", "--display-name", "Op"],
        out=io.StringIO(),
        err=err,
    )
    assert code == migrate.EXIT_CONFIG
    assert "FINBOT_DB_OWNER_PASSWORD_FILE" in err.getvalue()
