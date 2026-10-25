"""The PTB application end to end on a real PostgreSQL 18.6, with a fake Bot API (Step 4C).

Admin bootstrap (owner) → admin creates an invite by DM → a new user opens the deep link →
onboarded; repeats, concurrency, revoke and non-admin refusals, all through Telegram updates and
the finbot_app role. Runs only with FINBOT_TEST_PG_BACKEND set.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from finbot.adapters.telegram import messages_id as msg
from finbot.adapters.telegram.bot import BotServices, build_application
from finbot.infra.db.bootstrap import bootstrap_platform_admin
from finbot.infra.db.guards import EnvironmentMismatchError, require_environment_marker
from finbot.infra.db.identity import PgIdentityGateway
from finbot.infra.db.platform import PgPlatformGateway
from finbot.services.onboarding import OnboardingService
from finbot.services.platform_invites import PlatformInviteService
from tests.support.identity import counts
from tests.support.telegram import BOT_TOKEN, BOT_USERNAME, FakeBotApi, message_update

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ADMIN = 910_000_001
ALICE = 910_000_002
BOB = 910_000_003
LINK_RE = re.compile(rf"https://t\.me/{BOT_USERNAME}\?start=(onb_[A-Za-z0-9_-]+)")
ID_RE = re.compile(r"<code>([0-9a-f-]{36})</code>")


class Bot:
    def __init__(self, app_engine: Any) -> None:
        identities = PgIdentityGateway(app_engine)
        services = BotServices(
            onboarding=OnboardingService(identities),
            invites=PlatformInviteService(identities, PgPlatformGateway(app_engine)),
        )
        self.api = FakeBotApi()
        self.app = build_application(BOT_TOKEN, services, request=self.api, concurrent_updates=True)

    async def send(self, user: int, text: str, first_name: str = "Budi") -> str:
        before = len(self.api.texts(user))
        await self.app.process_update(message_update(self.app, user, text, first_name=first_name))
        texts = self.api.texts(user)
        assert len(texts) == before + 1, texts
        return texts[-1]

    async def new_invite(self) -> tuple[str, str]:
        reply = await self.send(ADMIN, "/undangan_baru untuk tamu")
        link, invite_id = LINK_RE.search(reply), ID_RE.search(reply)
        assert link is not None
        assert invite_id is not None
        return link.group(1), invite_id.group(1)


@pytest_asyncio.fixture
async def bot(migrated_database: str, owner_engine: Any, app_engine: Any) -> AsyncIterator[Bot]:
    await bootstrap_platform_admin(
        owner_engine, env="test", telegram_user_id=ADMIN, display_name="Operator"
    )
    harness = Bot(app_engine)
    await harness.app.initialize()
    yield harness
    await harness.app.shutdown()


async def test_admin_invite_by_dm_onboards_a_new_user_through_the_deep_link(
    bot: Bot, owner_engine: Any
) -> None:
    payload, invite_id = await bot.new_invite()
    assert (await bot.send(ALICE, f"/start {payload}", "Alice <3")).startswith("Selamat datang!")
    assert (await bot.send(ALICE, "/start")).startswith("Selamat datang kembali.")
    assert await bot.send(BOB, f"/start {payload}") == msg.START_INVITE_REFUSED  # single use
    listing = await bot.send(ADMIN, "/undangan_daftar semua")
    assert invite_id in listing
    assert "terpakai" in listing
    assert await bot.send(ADMIN, f"/undangan_cabut {invite_id}") == msg.INVITE_NOT_REVOKED["used"]
    async with owner_engine.connect() as conn:
        from sqlalchemy import text  # noqa: PLC0415

        name = (
            await conn.execute(
                text(
                    "SELECT u.display_name FROM users u JOIN telegram_identities t "
                    "ON t.user_id = u.id WHERE t.telegram_user_id = :tg"
                ),
                {"tg": ALICE},
            )
        ).scalar_one()
        channel = (
            await conn.execute(
                text("SELECT channel FROM audit_events WHERE action = 'onboarding_invite.created'")
            )
        ).scalar_one()
    assert name == "Alice <3"  # stored as text, escaped only when rendered
    assert channel == "telegram"


async def test_revoked_invite_cannot_be_used(bot: Bot) -> None:
    payload, invite_id = await bot.new_invite()
    assert await bot.send(ADMIN, f"/undangan_cabut {invite_id}") == msg.INVITE_REVOKED.format(
        invite_id=invite_id
    )
    assert await bot.send(ALICE, f"/start {payload}") == msg.START_INVITE_REFUSED


async def test_onboarded_non_admin_and_unknown_accounts_cannot_manage_invites(
    bot: Bot, owner_engine: Any
) -> None:
    payload, _ = await bot.new_invite()
    await bot.send(ALICE, f"/start {payload}")
    before = await counts(owner_engine)
    assert await bot.send(ALICE, "/undangan_baru") == msg.NOT_PERMITTED
    assert await bot.send(BOB, "/undangan_baru") == msg.START_NEEDS_INVITE
    assert await bot.send(ALICE, "/undangan_daftar") == msg.NOT_PERMITTED
    assert await counts(owner_engine) == before


async def test_concurrent_deep_link_presses_onboard_once(bot: Bot, owner_engine: Any) -> None:
    payload, _ = await bot.new_invite()
    before = await counts(owner_engine)
    await asyncio.gather(
        *(
            bot.app.process_update(message_update(bot.app, ALICE, f"/start {payload}"))
            for _ in range(4)
        )
    )
    replies = bot.api.texts(ALICE)
    assert len(replies) == 4
    assert all(r.startswith("Selamat datang") for r in replies)
    after = await counts(owner_engine)
    assert after["users"] == before["users"] + 1
    assert after["telegram_identities"] == before["telegram_identities"] + 1


async def test_environment_marker_guard(migrated_database: str, app_engine: Any) -> None:
    await require_environment_marker(app_engine, "test")
    with pytest.raises(EnvironmentMismatchError, match="G-ENV-3"):
        await require_environment_marker(app_engine, "production")
