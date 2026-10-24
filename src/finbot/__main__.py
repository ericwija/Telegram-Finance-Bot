"""Command-line entry point (composition root).

Phase 1 Step 2 provides only offline commands:

* ``finbot check-config [--json]``: load and validate settings from the environment (local
  secret files included) and print a redacted summary. Exit codes: 0 valid, 2 invalid.
  It never connects to PostgreSQL, Telegram, Gemini or any other network service. The runtime
  guards that need I/O (G-ENV-2 database name, G-ENV-3 marker, G-ENV-4 getMe, G-ENV-6 lock)
  arrive in Step 4.
* ``finbot version``: print the package version.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TextIO

from finbot import __version__
from finbot.config import ConfigurationError, load_settings

EXIT_OK = 0
EXIT_CONFIG_INVALID = 2


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finbot", description="Telegram-Finance-Bot V2")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-config", help="validate configuration without network access")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.set_defaults(handler=_cmd_check_config)

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
