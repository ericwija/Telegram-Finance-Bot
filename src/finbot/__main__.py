"""Command-line entry point (composition root).

Commands:

* ``finbot check-config [--json]``: load and validate settings from the environment (local
  secret files included) and print a redacted summary. Exit codes: 0 valid, 2 invalid.
  It never connects to PostgreSQL, Telegram, Gemini or any other network service. The runtime
  guards that need I/O (G-ENV-2 live database name, G-ENV-3 marker, G-ENV-6 poller lock,
  G-ENV-4 getMe) run in ``bot``.
* ``finbot bot [--dry-run] [--health-file PATH]`` (Steps 4C/4D): run the Telegram bot by long
  polling until SIGTERM/SIGINT, or with ``--dry-run`` build it offline and describe it (no
  Telegram or database connection). Exit codes: 0 stopped cleanly, 2 invalid configuration,
  3 refused by a start-up guard (including "poller lock held", AC-P1-04), 4 poller lock lost
  while polling.
* ``finbot healthcheck --health-file PATH [--max-age SECONDS]``: judge the bot's heartbeat
  file offline. Exit codes: 0 healthy, 1 unhealthy.
* ``finbot version``: print the package version.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from finbot import __version__
from finbot.config import ConfigurationError, load_settings

EXIT_OK = 0
EXIT_CONFIG_INVALID = 2
EXIT_REFUSED = 3
EXIT_LOCK_LOST = 4
EXIT_UNHEALTHY = 1


def _cmd_check_config(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        if args.json:
            json.dump({"valid": False, "problems": exc.problems}, out, indent=2)
            out.write("\n")
        else:
            err.write("configuration INVALID:\n")
            for problem in exc.problems:
                err.write(f"  - {problem}\n")
        return EXIT_CONFIG_INVALID

    summary = settings.redacted_summary()
    if args.json:
        json.dump({"valid": True, "settings": summary}, out, indent=2)
        out.write("\n")
    else:
        out.write("configuration OK (static checks only; no network connections made)\n")
        width = max(len(key) for key in summary)
        for key, value in summary.items():
            out.write(f"  {key.ljust(width)}  {value}\n")
    return EXIT_OK


def _cmd_version(_: argparse.Namespace, out: TextIO, __: TextIO) -> int:
    out.write(f"finbot {__version__}\n")
    return EXIT_OK


def _cmd_bot(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        err.write("configuration INVALID:\n")
        for problem in exc.problems:
            err.write(f"  - {problem}\n")
        return EXIT_CONFIG_INVALID

    from finbot.adapters.telegram import runtime  # noqa: PLC0415 - PTB only when needed
    from finbot.logging import configure_logging  # noqa: PLC0415

    if args.dry_run:
        runtime.dry_run(settings, out)
        return EXIT_OK
    configure_logging(settings.log_level, env=settings.env, stream=err)
    try:
        runtime.run_polling(settings, health_file=args.health_file)
    except runtime.BotStartupError as exc:
        err.write(f"refused: {exc}\n")
        return EXIT_REFUSED
    except runtime.PollerLockLostError as exc:
        err.write(f"stopped: {exc}\n")
        return EXIT_LOCK_LOST
    return EXIT_OK


def _cmd_healthcheck(args: argparse.Namespace, out: TextIO, _: TextIO) -> int:
    from finbot.infra.heartbeat import check_health  # noqa: PLC0415

    report = check_health(args.health_file, max_age=args.max_age)
    if args.json:
        json.dump({"healthy": report.healthy, "reason": report.reason, **report.details}, out)
        out.write("\n")
    else:
        out.write(f"{'healthy' if report.healthy else 'UNHEALTHY'}: {report.reason}\n")
    return EXIT_OK if report.healthy else EXIT_UNHEALTHY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finbot", description="Telegram-Finance-Bot V2")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-config", help="validate configuration without network access")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.set_defaults(handler=_cmd_check_config)

    bot = sub.add_parser("bot", help="run the Telegram bot (long polling)")
    bot.add_argument(
        "--dry-run",
        action="store_true",
        help="build the bot offline and describe it; contacts neither Telegram nor PostgreSQL",
    )
    bot.add_argument(
        "--health-file",
        type=Path,
        default=None,
        help="heartbeat file to rewrite while polling (read by `finbot healthcheck`)",
    )
    bot.set_defaults(handler=_cmd_bot)

    health = sub.add_parser("healthcheck", help="judge the bot heartbeat file (offline)")
    health.add_argument("--health-file", type=Path, required=True)
    health.add_argument(
        "--max-age", type=float, default=60.0, help="seconds a heartbeat stays fresh"
    )
    health.add_argument("--json", action="store_true", help="machine-readable output")
    health.set_defaults(handler=_cmd_healthcheck)

    version = sub.add_parser("version", help="print version")
    version.set_defaults(handler=_cmd_version)
    return parser


def main(
    argv: Sequence[str] | None = None, *, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)
    handler = args.handler
    return int(handler(args, out or sys.stdout, err or sys.stderr))


if __name__ == "__main__":
    sys.exit(main())
