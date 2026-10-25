"""The PTB application with a fake Bot API and fake services (no network, no database)."""

import asyncio
import io
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from finbot.adapters.telegram import messages_id as msg
from finbot.adapters.telegram.bot import (
    BotGuards,
    BotServices,
    build_application,
    quiet_transport_loggers,
)
from finbot.adapters.telegram.ratelimit import RecentUpdates, SlidingWindowLimiter
from finbot.domain.identity import (
    InviteStatus,
    InviteSummary,
    OnboardingToken,
    PrincipalRecord,
    UserStatus,
)
from finbot.services.onboarding import OnboardingService
from finbot.services.platform_invites import PlatformInviteService
from tests.support.telegram import (
    BOT_TOKEN,
    BOT_USERNAME,
    FakeBotApi,
    added_to_group_update,
    message_update,
)
from tests.unit.test_onboarding_service import FakeGateway
from tests.unit.test_platform_invites import FakeIdentities, FakePlatform

USER = 1001
ADMIN = 2002
TOKEN = OnboardingToken("K" * 22)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    def __init__(self, *, admin: PrincipalRecord | None = None) -> None:
        self.api = FakeBotApi()
        self.clock = Clock()
        self.identities = FakeGateway()
        self.platform = FakePlatform()
        self.admin_identities = FakeIdentities(admin)
        self.guards = BotGuards(
            updates=SlidingWindowLimiter(30, clock=self.clock),
            bad_starts=SlidingWindowLimiter(10, clock=self.clock),
            seen=RecentUpdates(),
        )
        services = BotServices(
            onboarding=OnboardingService(self.identities),
            invites=PlatformInviteService(
                self.admin_identities, self.platform, token_factory=lambda: TOKEN
            ),
        )
        self.app = build_application(
            BOT_TOKEN, services, guards=self.guards, request=self.api, concurrent_updates=True
        )

    async def send(self, user_id: int, text: str, **kwargs: Any) -> None:
        await self.app.process_update(message_update(self.app, user_id, text, **kwargs))


@pytest_asyncio.fixture
async def bot() -> AsyncIterator[Harness]:
    harness = Harness()
    await harness.app.initialize()  # getMe through the fake API
    yield harness
    await harness.app.shutdown()


async def _admin_bot(principal: PrincipalRecord | None) -> Harness:
    harness = Harness(admin=principal)
    await harness.app.initialize()
    return harness


def _calls(gateway: FakeGateway, kind: str) -> int:
    return sum(1 for call in gateway.calls if call[0] == kind)


# ----------------------------------------------------------------------------- /start


@pytest.mark.asyncio
async def test_start_without_invite_explains_onboarding_in_html(bot: Harness) -> None:
    await bot.send(USER, "/start")
    [sent] = bot.api.sent(USER)
    assert sent["text"] == msg.START_NEEDS_INVITE
    assert sent["parse_mode"] == "HTML"
    assert sent["link_preview_options"] == {"is_disabled": True}
    assert _calls(bot.identities, "redeem") == 0


@pytest.mark.asyncio
async def test_deep_link_onboards_and_escapes_the_workspace_name(bot: Harness) -> None:
    bot.identities.redeem_result = uuid.uuid4()
    bot.identities.workspace = bot.identities.workspace.__class__(uuid.uuid4(), "<Ruang & Co>")
    await bot.send(USER, f"/start onb_{TOKEN.value}")
    [text] = bot.api.texts(USER)
    assert "&lt;Ruang &amp; Co&gt;" in text
    assert "<Ruang" not in text
    kind, (digest, tg, _) = bot.identities.calls[1]
    assert (kind, digest, tg) == ("redeem", TOKEN.digest(), USER)


@pytest.mark.asyncio
async def test_start_addressed_to_the_bot_by_name_works(bot: Harness) -> None:
    bot.identities.redeem_result = uuid.uuid4()
    await bot.send(USER, f"/start@{BOT_USERNAME} onb_{TOKEN.value}")
    assert _calls(bot.identities, "redeem") == 1


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
@pytest.mark.asyncio
async def test_group_messages_are_ignored_entirely(bot: Harness, chat_type: str) -> None:
    await bot.send(USER, f"/start onb_{TOKEN.value}", chat_type=chat_type, chat_id=-100)
    await bot.send(USER, "/undangan_baru", chat_type=chat_type, chat_id=-100)
    assert bot.api.sent() == []
    assert bot.identities.calls == []
    assert bot.platform.calls == []


@pytest.mark.asyncio
async def test_bot_leaves_a_group_it_is_added_to(bot: Harness) -> None:
    await bot.app.process_update(added_to_group_update(bot.app, -555))
    assert ("leaveChat", {"chat_id": -555}) in bot.api.calls
    assert bot.api.sent() == []


@pytest.mark.asyncio
async def test_messages_from_bots_are_ignored(bot: Harness) -> None:
    await bot.send(USER, "/start", is_bot=True)
    assert bot.api.sent() == []


@pytest.mark.asyncio
async def test_duplicate_updates_are_processed_once(bot: Harness) -> None:
    bot.identities.redeem_result = uuid.uuid4()
    for _ in range(3):
        await bot.send(USER, f"/start onb_{TOKEN.value}", update_id=777)
    assert _calls(bot.identities, "redeem") == 1
    assert len(bot.api.sent(USER)) == 1


# ------------------------------------------------------------------------- rate limits


@pytest.mark.asyncio
async def test_updates_over_30_per_minute_are_dropped_with_a_single_notice(bot: Harness) -> None:
    for _ in range(33):
        await bot.send(USER, "/start")
    texts = bot.api.texts(USER)
    assert texts.count(msg.START_NEEDS_INVITE) == 30
    assert texts.count(msg.RATE_LIMITED) == 1
    assert _calls(bot.identities, "resolve") == 30  # no database work once limited
    await bot.send(USER + 1, "/start")  # other users are unaffected
    assert bot.api.texts(USER + 1) == [msg.START_NEEDS_INVITE]
    bot.clock.now += 61
    await bot.send(USER, "/start")
    assert bot.api.texts(USER)[-1] == msg.START_NEEDS_INVITE


@pytest.mark.asyncio
async def test_ten_bad_payloads_block_further_payloads_before_the_database(
    bot: Harness,
) -> None:
    bot.identities.redeem_result = None  # every invite refused
    for n in range(10):
        await bot.send(USER, f"/start onb_{'A' * 21}{n}")
    assert _calls(bot.identities, "redeem") == 10
    calls_before = len(bot.identities.calls)
    await bot.send(USER, f"/start onb_{TOKEN.value}")  # even a real invite now
    await bot.send(USER, "/start onb_malformed")
    assert len(bot.identities.calls) == calls_before
    texts = bot.api.texts(USER)
    assert texts[:10] == [msg.START_INVITE_REFUSED] * 10
    assert texts[10:] == [msg.RATE_LIMITED]  # one notice only
    await bot.send(USER, "/start")  # no payload: still answered
    assert bot.api.texts(USER)[-1] == msg.START_NEEDS_INVITE
    bot.clock.now += 61
    bot.identities.redeem_result = uuid.uuid4()
    await bot.send(USER, f"/start onb_{TOKEN.value}")
    assert bot.api.texts(USER)[-1].startswith("Selamat datang!")


@pytest.mark.asyncio
async def test_malformed_payloads_count_as_bad_without_reaching_the_database(
    bot: Harness,
) -> None:
    for n in range(10):
        await bot.send(USER, f"/start garbage{n}")
    assert _calls(bot.identities, "redeem") == 0
    await bot.send(USER, f"/start onb_{TOKEN.value}")
    assert bot.api.texts(USER)[-1] == msg.RATE_LIMITED


@pytest.mark.asyncio
async def test_the_limit_notice_does_not_reveal_whether_the_account_exists(bot: Harness) -> None:
    bot.identities.redeem_result = None
    for user in (USER, USER + 1):
        for n in range(11):
            await bot.send(user, f"/start onb_{'B' * 21}{n}")
    assert bot.api.texts(USER) == bot.api.texts(USER + 1)


# ----------------------------------------------------------------------- admin invites


def _principal(*, admin: bool = True, status: UserStatus = UserStatus.ACTIVE) -> PrincipalRecord:
    return PrincipalRecord(uuid.uuid4(), status, admin)


@pytest.mark.asyncio
async def test_admin_creates_an_invite_by_dm_as_the_verified_sender() -> None:
    principal = _principal()
    bot = await _admin_bot(principal)
    try:
        await bot.send(ADMIN, "/undangan_baru --as 999 untuk Siti")
        [text] = bot.api.texts(ADMIN)
        assert f"https://t.me/{BOT_USERNAME}?start=onb_{TOKEN.value}" in text
        # The actor is the authenticated sender; "--as 999" is only part of the note.
        assert bot.platform.calls == [
            ("create", principal.user_id, TOKEN.digest(), "--as 999 untuk Siti", "telegram")
        ]
    finally:
        await bot.app.shutdown()


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        (None, msg.START_NEEDS_INVITE),  # unknown account: onboarding explanation only
        (_principal(admin=False), msg.NOT_PERMITTED),
        (_principal(status=UserStatus.SUSPENDED), msg.NOT_PERMITTED),
    ],
)
@pytest.mark.asyncio
async def test_non_admins_cannot_use_invite_commands(
    principal: PrincipalRecord | None, expected: str
) -> None:
    bot = await _admin_bot(principal)
    try:
        invite_id = uuid.uuid4()
        for text in (
            "/undangan_baru",
            "/undangan_daftar semua",
            f"/undangan_cabut {invite_id}",
            "/undangan_cabut",
        ):
            await bot.send(ADMIN, text)
        assert bot.api.texts(ADMIN) == [expected] * 4
        assert bot.platform.calls == []
    finally:
        await bot.app.shutdown()


@pytest.mark.asyncio
async def test_admin_list_and_revoke_render_escaped_html() -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    bot = await _admin_bot(_principal())
    try:
        invite = InviteSummary(
            uuid.UUID(int=7),
            InviteStatus.ACTIVE,
            datetime(2026, 10, 25, tzinfo=UTC),
            datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
            "<b>Siti</b>",
            uuid.uuid4(),
            None,
        )
        bot.platform.invites = [invite]
        await bot.send(ADMIN, "/undangan_daftar semua")
        await bot.send(ADMIN, f"/undangan_cabut {invite.id}")
        await bot.send(ADMIN, "/undangan_cabut bukan-id")
        listing, revoked, usage = bot.api.texts(ADMIN)
        assert "&lt;b&gt;Siti&lt;/b&gt;" in listing
        assert "2026-11-01 10:00 (Asia/Jakarta)" in listing
        assert "aktif" in listing
        assert revoked == msg.INVITE_REVOKED.format(invite_id=invite.id)
        assert usage == msg.INVITE_REVOKE_USAGE
        assert bot.platform.calls[0] == ("list", bot.admin_identities.principal.user_id, True)  # type: ignore[union-attr]
    finally:
        await bot.app.shutdown()


# ------------------------------------------------------------------------------ errors


@pytest.mark.asyncio
async def test_telegram_api_errors_on_reply_are_contained(
    bot: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    bot.api.failures["sendMessage"] = (403, "Forbidden: bot was blocked by the user")
    caplog.set_level(logging.WARNING)
    await bot.send(USER, "/start")
    records = [r for r in caplog.records if getattr(r, "event", "") == "telegram.reply_failed"]
    assert [r.error_type for r in records] == ["Forbidden"]  # type: ignore[attr-defined]
    assert not [r for r in caplog.records if getattr(r, "event", "") == "telegram.handler_error"]


@pytest.mark.asyncio
async def test_unexpected_errors_get_a_generic_reply_and_a_type_only_log(
    bot: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(_: int) -> Any:
        raise RuntimeError(f"secret detail onb_{TOKEN.value}")

    bot.identities.resolve_principal = boom  # type: ignore[method-assign]
    quiet_transport_loggers()  # as in production: PTB debug logs may contain update contents
    caplog.set_level(logging.DEBUG)
    await bot.send(USER, f"/start onb_{TOKEN.value}")
    assert bot.api.texts(USER) == [msg.GENERIC_ERROR]
    [record] = [r for r in caplog.records if getattr(r, "event", "") == "telegram.handler_error"]
    assert record.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert TOKEN.value not in caplog.text
    assert "secret detail" not in caplog.text


@pytest.mark.asyncio
async def test_logs_never_contain_tokens_texts_or_the_bot_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    bot = await _admin_bot(_principal())
    try:
        quiet_transport_loggers()
        await bot.send(ADMIN, "/undangan_baru untuk Rahasia")
        bot.identities.redeem_result = uuid.uuid4()
        await bot.send(USER, f"/start onb_{TOKEN.value}", first_name="Rahasia")
    finally:
        await bot.app.shutdown()
    logged = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    for secret in (TOKEN.value, BOT_TOKEN, "Rahasia"):
        assert secret not in logged
    assert logging.getLogger("httpx").level == logging.WARNING


@pytest.mark.asyncio
async def test_concurrent_updates_from_many_users_are_all_answered(bot: Harness) -> None:
    await asyncio.gather(*(bot.send(USER + n, "/start") for n in range(20)))
    assert len(bot.api.sent()) == 20


# ------------------------------------------------------------------------------ runtime


def test_dry_run_builds_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from finbot.__main__ import main  # noqa: PLC0415

    monkeypatch.setenv("FINBOT_ENV", "dev")
    out = io.StringIO()
    assert main(["bot", "--dry-run"], out=out, err=io.StringIO()) == 0
    text = out.getvalue()
    assert "no Telegram or database connection" in text
    assert "/start, /undangan_baru, /undangan_cabut, /undangan_daftar" in text


@pytest.mark.asyncio
async def test_offline_transport_refuses_every_call() -> None:
    from finbot.adapters.telegram.runtime import OfflineRequest  # noqa: PLC0415

    with pytest.raises(RuntimeError, match="never contacted"):
        await OfflineRequest().do_request("https://example.invalid/getMe", "POST")


def test_running_without_a_token_is_refused_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finbot.__main__ import main  # noqa: PLC0415

    monkeypatch.setenv("FINBOT_ENV", "dev")
    err = io.StringIO()
    assert main(["bot"], out=io.StringIO(), err=err) == 3
    assert "FINBOT_TELEGRAM_TOKEN_FILE" in err.getvalue()
