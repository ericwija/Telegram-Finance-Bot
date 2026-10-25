"""``finbot-migrate``: the schema migration entry point (decision S10, docs/10 §3).

It is the only V2 entry point that reads ``finbot_owner`` credentials
(:mod:`finbot.migration_settings`); the bot never does. In Step 5 it becomes the command of the
one-shot ``migrate`` container, which receives the owner password secret and nothing else.

Commands::

    finbot-migrate check-config [--json]      validate settings offline (no connection)
    finbot-migrate upgrade [REVISION]         default: head
    finbot-migrate downgrade REVISION         dev/test/drill + FINBOT_MIGRATION_ALLOW_DOWNGRADE only
    finbot-migrate current                    show the database's revision
    finbot-migrate history                    list revisions (no connection)
    finbot-migrate sql upgrade|downgrade RANGE
                                              print guarded SQL offline (no connection, no password)
    finbot-migrate bootstrap-admin --telegram-user-id ID --display-name NAME
                                              ONE-TIME: create the first platform admin (Step 4B).
                                              Not a migration: never run by upgrade, never at
                                              start-up; refused once it has succeeded

Exit codes: 0 success, 1 unexpected error, 2 invalid configuration, 3 refused by policy/guard.
Output never contains secret values.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from finbot.config import ConfigurationError
from finbot.infra.db.migration_runtime import (
    GUARD_MESSAGE_PREFIX,
    SETTINGS_ATTRIBUTE,
    MigrationRefusedError,
)
from finbot.logging import configure_logging, scrub
from finbot.migration_settings import MigrationSettings, load_migration_settings

if TYPE_CHECKING:
    from alembic.config import Config

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_REFUSED = 3

#: src/finbot/adapters/cli/migrate.py -> repository root (editable install / source checkout).
DEFAULT_INI = Path(__file__).resolve().parents[4] / "alembic.ini"

_OFFLINE_COMMANDS = frozenset({"check-config", "history", "sql"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finbot-migrate", description=__doc__.split("\n")[0])
    parser.add_argument("--ini", type=Path, default=DEFAULT_INI, help="path to alembic.ini")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config", help="validate migration settings offline")
    check.add_argument("--json", action="store_true", help="print the redacted summary as JSON")
    up = sub.add_parser("upgrade", help="upgrade the database")
    up.add_argument("revision", nargs="?", default="head")
    down = sub.add_parser("downgrade", help="downgrade (dev/test/drill with opt-in only)")
    down.add_argument("revision")
    sub.add_parser("current", help="show the current revision")
    sub.add_parser("history", help="list revisions")
    sql = sub.add_parser("sql", help="print guarded SQL without connecting")
    sql.add_argument("direction", choices=("upgrade", "downgrade"))
    sql.add_argument("range", help="e.g. head, base:head, or 0002:0001 for a downgrade")
    boot = sub.add_parser(
        "bootstrap-admin",
        help="one-time: create the first platform admin (operator only; not a migration)",
    )
    boot.add_argument(
        "--telegram-user-id",
        type=_telegram_user_id,
        required=True,
        metavar="ID",
        help="numeric Telegram user ID of the operator who becomes the first platform admin",
    )
    boot.add_argument("--display-name", required=True, metavar="NAME")
    return parser


def _telegram_user_id(value: str) -> int:
    if not value.isdigit() or int(value) <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer Telegram user ID")
    return int(value)


def alembic_config(ini: Path, settings: MigrationSettings, out: TextIO) -> "Config":
    """An Alembic config that hands the validated settings to env.py."""
    from alembic.config import Config  # noqa: PLC0415 - keep alembic out of import time

    if not ini.is_file():
        raise ConfigurationError([f"alembic.ini not found at {ini} (use --ini)"])
    config = Config(str(ini), stdout=out, output_buffer=out)  # messages and offline SQL
    config.attributes[SETTINGS_ATTRIBUTE] = settings
    return config


def _dispatch(args: argparse.Namespace, settings: MigrationSettings, out: TextIO) -> None:
    from alembic import command  # noqa: PLC0415

    config = alembic_config(args.ini, settings, out)
    if args.command == "upgrade":
        command.upgrade(config, args.revision)
    elif args.command == "downgrade":
        command.downgrade(config, args.revision)
    elif args.command == "current":
        command.current(config)
    elif args.command == "history":
        command.history(config)
    elif args.command == "sql":
        if args.direction == "upgrade":
            command.upgrade(config, args.range, sql=True)
        else:
            command.downgrade(config, args.range, sql=True)


def _is_guard_refusal(exc: BaseException) -> bool:
    return GUARD_MESSAGE_PREFIX in str(exc)


def main(
    argv: list[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    """Run ``finbot-migrate``. Returns the process exit code."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = _parser().parse_args(argv)
    offline = args.command in _OFFLINE_COMMANDS
    try:
        settings = load_migration_settings(
            require_password=args.command == "check-config" or not offline
        )
    except ConfigurationError as exc:
        for problem in exc.problems:
            print(f"configuration error: {problem}", file=err)
        return EXIT_CONFIG

    if args.command == "check-config":
        summary = settings.redacted_summary()
        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True), file=out)
        else:
            for key, value in summary.items():
                print(f"{key}: {value}", file=out)
        return EXIT_OK

    downgrading = args.command == "downgrade" or getattr(args, "direction", "") == "downgrade"
    if downgrading and not settings.downgrade_permitted:
        print(
            f"refused: downgrade is not permitted in {settings.env} (S9). Downgrades run only in "
            "dev, test or drill with FINBOT_MIGRATION_ALLOW_DOWNGRADE=true; staging and "
            "production use forward fixes.",
            file=err,
        )
        return EXIT_REFUSED

    configure_logging(settings.log_level, env=settings.env, stream=err)
    if args.command == "bootstrap-admin":
        return _bootstrap_admin(args, settings, out, err)
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)  # start-up noise
    return _execute(args, settings, out, err)


def _bootstrap_admin(
    args: argparse.Namespace, settings: MigrationSettings, out: TextIO, err: TextIO
) -> int:
    """Separate from the Alembic commands: one owner transaction, no schema change."""
    import asyncio  # noqa: PLC0415

    from finbot.infra.db.bootstrap import (  # noqa: PLC0415
        BootstrapRefusedError,
        bootstrap_platform_admin,
    )
    from finbot.infra.db.connection import create_engine, owner_target  # noqa: PLC0415

    async def run() -> object:
        engine = create_engine(owner_target(settings))
        try:
            return await bootstrap_platform_admin(
                engine,
                env=settings.env,
                telegram_user_id=args.telegram_user_id,
                display_name=args.display_name,
            )
        finally:
            await engine.dispose()

    try:
        user_id = asyncio.run(run())
    except BootstrapRefusedError as exc:
        print(f"refused: {scrub(str(exc))}", file=err)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - report every failure without a traceback
        print(f"error: {type(exc).__name__}: {scrub(str(exc))}", file=err)
        return EXIT_ERROR
    print(f"platform admin bootstrapped: user {user_id} (this command cannot run again)", file=out)
    return EXIT_OK


def _execute(
    args: argparse.Namespace, settings: MigrationSettings, out: TextIO, err: TextIO
) -> int:
    """Run the Alembic command and map every failure to an exit code without secrets."""
    try:
        _dispatch(args, settings, out)
    except ConfigurationError as exc:
        for problem in exc.problems:
            print(f"configuration error: {problem}", file=err)
        return EXIT_CONFIG
    except MigrationRefusedError as exc:
        print(f"refused: {scrub(str(exc))}", file=err)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 - report every failure without a traceback
        if _is_guard_refusal(exc):
            message = str(exc)
            reason = message[message.index(GUARD_MESSAGE_PREFIX) :].splitlines()[0]
            print(f"refused: {scrub(reason)}", file=err)
            return EXIT_REFUSED
        print(f"error: {type(exc).__name__}: {scrub(str(exc))}", file=err)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
