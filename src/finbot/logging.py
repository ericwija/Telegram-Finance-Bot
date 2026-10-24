"""Structured JSON logging with redaction.

Specification: NFR-OBS-01 and NFR-PRV-01 (docs/03), threat T12 (docs/09 §5).

What reaches the log output:

* One JSON object per line with ``ts`` (UTC), ``level``, ``logger`` and ``message``, plus extra
  fields from :data:`ALLOWED_EXTRA_FIELDS` only. Any other ``extra=`` key (for example
  ``description`` or ``text``) is **dropped**, and only its *name* is listed in ``dropped_fields``.
* ``%``-style arguments that are plain ``str`` values are replaced by ``[REDACTED]``, because
  free text may be user content. Numbers, booleans, ``None``, UUIDs, enums and strings wrapped
  in :func:`safe` are kept. Log event names and IDs, never user text or amounts.
* The final message, string extras and exception text are scrubbed of token-like values,
  credentials in URLs, ``password=...`` pairs, rupiah amounts and long digit runs.
* A non-string message object (dict, model, Telegram update) is never rendered, only its type
  name. Exceptions are rendered as ``Type: scrubbed message``.

Importing this module configures nothing. Call :func:`configure_logging` from the entry point.
"""

import json
import logging
import re
import sys
import traceback
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import IO, Any, Final

REDACTED: Final = "[REDACTED]"

#: Extra fields allowed in log records (NFR-OBS-01). Everything else is dropped.
ALLOWED_EXTRA_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "request_id",
        "user_id",
        "workspace_id",
        "event",
        "duration_ms",
        "outcome",
        "env",
        "component",
        "error_type",
        "count",
    }
)

_RESERVED_ATTRS: Final[frozenset[str]] = frozenset(
    {*logging.makeLogRecord({}).__dict__, "message", "asctime", "taskName"}
)

_SCRUB_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED:private-key]",
    ),
    (re.compile(r"\b\d{5,15}:[A-Za-z0-9_-]{30,}"), "[REDACTED:telegram-token]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED:api-key]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:@/\s]+):[^@/\s]+@"), r"\1:[REDACTED]@"),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization)"
            r"([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)\brp\.?\s?\d[\d.,]*"), "[REDACTED:amount]"),
    (re.compile(r"(?i)\b\d+(?:[.,]\d+)*\s?(?:rb|ribu|jt|juta|k)\b"), "[REDACTED:amount]"),
    (re.compile(r"\b\d{9,}\b"), "[REDACTED:number]"),
)


class Safe(str):
    """A string vetted as non-sensitive (command name, enum value, ...). See :func:`safe`."""

    __slots__ = ()


def safe(value: str) -> Safe:
    """Mark ``value`` as safe to interpolate into a log message unchanged."""
    return Safe(value)


def scrub(text: str) -> str:
    """Mask secrets, credentials, amounts and long digit runs in ``text``."""
    for pattern, replacement in _SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _safe_arg(value: object) -> object:
    if isinstance(value, Safe | Enum | uuid.UUID | bool | int | float) or value is None:
        return value
    if isinstance(value, str) and _is_uuid(value):
        return value
    return REDACTED


def _render_message(record: logging.LogRecord) -> str:
    if isinstance(record.msg, BaseException):
        return scrub(f"{type(record.msg).__name__}: {record.msg}")
    if not isinstance(record.msg, str):
        # Arbitrary objects (dicts, models, Telegram updates) may carry user data: never render.
        return f"[{type(record.msg).__name__} object redacted]"
    msg = record.msg
    args = record.args
    if args:
        try:
            if isinstance(args, Mapping):
                msg = msg % {key: _safe_arg(value) for key, value in args.items()}
            else:
                msg = msg % tuple(_safe_arg(value) for value in args)
        except (TypeError, ValueError, KeyError):
            msg = f"{msg} [unformattable log arguments]"
    return scrub(msg)


def _safe_extra_value(value: object) -> object:
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, uuid.UUID | Enum):
        return str(value.value if isinstance(value, Enum) else value)
    if isinstance(value, str):
        return value if _is_uuid(value) else scrub(value)
    return scrub(str(value))


class JsonFormatter(logging.Formatter):
    """Format records as single-line JSON with redaction. See the module docstring."""

    def __init__(self, static_fields: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self._static = {k: v for k, v in (static_fields or {}).items() if v is not None}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": _render_message(record),
            **self._static,
        }
        dropped: list[str] = []
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            if key in ALLOWED_EXTRA_FIELDS:
                payload[key] = _safe_extra_value(value)
            else:
                dropped.append(key)
        if dropped:
            payload["dropped_fields"] = sorted(dropped)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            payload["exception"] = {
                "type": type(exc).__name__,
                "message": scrub(str(exc)),
                "traceback": scrub("".join(traceback.format_tb(exc.__traceback__))),
            }
        if record.stack_info:
            payload["stack"] = scrub(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_HANDLER_MARK: Final = "_finbot_handler"


def configure_logging(
    level: str = "INFO", *, env: str | None = None, stream: IO[str] | None = None
) -> logging.Handler:
    """Install the JSON handler on the root logger, replacing any previously installed by us.

    Call once from the entry point. Repeated calls are idempotent (used by tests).
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARK, False):
            root.removeHandler(existing)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter({"env": env} if env else None))
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    return handler
