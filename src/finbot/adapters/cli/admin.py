"""``finbot-admin``: platform-admin operations on the host (docs/04 §2, docs/08 §3.3).

It uses the **application** settings and the ``finbot_app`` role, exactly like the bot (run it
in the bot container), never owner credentials. Every command names the acting admin with
``--as <telegram user id>``; that account must be linked to an active platform admin, which the
service and the database both check. The first admin is created once with
``finbot-migrate bootstrap-admin`` (owner role), not here.

Commands::

    finbot-admin invite create --as ID [--note TEXT]   prints the one-time link (stdout only)
    finbot-admin invite list   --as ID [--all]         metadata only, never tokens
    finbot-admin invite revoke --as ID INVITE_ID

Exit codes: 0 success, 1 unexpected error, 2 invalid configuration, 3 refused (not an active
platform admin, or the invite cannot be revoked). The invite link is the only secret ever
printed, once, by ``invite create``; it is never logged and cannot be shown again.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from typing import TextIO

from finbot.config import ConfigurationError, load_settings
from finbot.domain.identity import (
    INVITE_VALIDITY_DAYS,
    PlatformAdminRequiredError,
    RevokeOutcome,
    onboarding_link,
)
from finbot.logging import configure_logging, scrub
from finbot.services.platform_invites import PlatformInviteService

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_REFUSED = 3


def _telegram_user_id(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer Telegram user ID")
    return int(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finbot-admin", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="area", required=True)
    invite = sub.add_parser("invite", help="onboarding invites (FR-IDN-01)")
    actions = invite.add_subparsers(dest="action", required=True)

    def with_actor(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--as",
            dest="actor",
            type=_telegram_user_id,
            required=True,
            metavar="ID",
            help="Telegram user ID of the acting platform admin",
        )
        return p

    create = with_actor(actions.add_parser("create", help="create a single-use 7-day invite"))
    create.add_argument("--note", help="who the invite is for (stored; max 200 characters)")
    listing = with_actor(actions.add_parser("list", help="list invites (active by default)"))
    listing.add_argument("--all", action="store_true", help="include used, revoked, expired")
    revoke = with_actor(actions.add_parser("revoke", help="revoke an unused invite"))
    revoke.add_argument("invite_id", type=uuid.UUID, metavar="INVITE_ID")
    return parser


async def run(
    args: argparse.Namespace,
    service: PlatformInviteService,
    out: TextIO,
    err: TextIO,
    *,
    bot_username: str | None,
) -> int:
    """Execute one parsed command against ``service`` (no configuration or engine here)."""
    try:
        if args.action == "create":
            created = await service.create(args.actor, note=args.note, channel="cli")
            print(
                f"invite {created.invite_id} created: single use, expires in "
                f"{INVITE_VALIDITY_DAYS} days.\n"
                "Deliver this link privately to one person. It is not stored and cannot be "
                "shown again:",
                file=out,
            )
            print(onboarding_link(created.token, bot_username), file=out)
        elif args.action == "list":
            invites = await service.list(args.actor, include_inactive=args.all)
            print("invite_id\tstatus\tcreated_at\texpires_at\tnote", file=out)
            for i in invites:
                print(
                    f"{i.id}\t{i.status}\t{i.created_at.isoformat()}\t"
                    f"{i.expires_at.isoformat()}\t{i.note or ''}",
                    file=out,
                )
        else:
            outcome = await service.revoke(args.actor, args.invite_id, channel="cli")
            if outcome is not RevokeOutcome.REVOKED:
                print(f"refused: invite not revoked ({outcome})", file=err)
                return EXIT_REFUSED
            print(f"invite {args.invite_id} revoked", file=out)
    except PlatformAdminRequiredError:
        print("refused: --as is not linked to an active platform admin", file=err)
        return EXIT_REFUSED
    except ValueError as exc:
        print(f"refused: {scrub(str(exc))}", file=err)
        return EXIT_REFUSED
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = _parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        for problem in exc.problems:
            print(f"configuration error: {problem}", file=err)
        return EXIT_CONFIG
    if settings.db_app_password is None:
        print("configuration error: FINBOT_DB_APP_PASSWORD_FILE is required", file=err)
        return EXIT_CONFIG
    configure_logging(settings.log_level, env=settings.env, stream=err)

    from finbot.infra.db.connection import app_target, create_engine  # noqa: PLC0415
    from finbot.infra.db.identity import PgIdentityGateway  # noqa: PLC0415
    from finbot.infra.db.platform import PgPlatformGateway  # noqa: PLC0415

    async def execute() -> int:
        engine = create_engine(app_target(settings, application_name="finbot-admin"))
        try:
            service = PlatformInviteService(PgIdentityGateway(engine), PgPlatformGateway(engine))
            return await run(
                args, service, out, err, bot_username=settings.telegram_expected_bot_username
            )
        finally:
            await engine.dispose()

    try:
        return asyncio.run(execute())
    except Exception as exc:  # noqa: BLE001 - report every failure without a traceback
        print(f"error: {type(exc).__name__}: {scrub(str(exc))}", file=err)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
