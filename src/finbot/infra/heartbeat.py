"""The bot's heartbeat file and the ``finbot healthcheck`` probe (docs/04 §5.8, §8.2).

While polling, the bot rewrites a small JSON file on every lock check (default every 10 s):
its state, environment, process ID, whether it holds the poller lock and is polling, and the
UTC time of the write. ``finbot healthcheck`` reads it and reports healthy only when the state
is ``ready`` and the file is fresh, so a hung event loop, a lost lock or a stopped bot all turn
unhealthy.

The file holds no token, credential, invite link, user ID or message content. It is written
atomically (temporary file plus rename), so the probe never reads half a file.
"""

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Default freshness limit for the probe (six missed 10 s ticks).
DEFAULT_MAX_AGE_SECONDS = 60.0
_FILE_MODE = 0o640


class BotState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Writes the heartbeat file. ``path=None`` writes nothing (local runs)."""

    path: Path | None
    env: str

    def write(self, state: BotState, *, lock_held: bool, polling: bool) -> None:
        if self.path is None:
            return
        body = {
            "state": state.value,
            "env": self.env,
            "pid": os.getpid(),
            "lock_held": lock_held,
            "polling": polling,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        directory = self.path.parent
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=directory)
        try:
            os.fchmod(fd, _FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


@dataclass(frozen=True, slots=True)
class HealthReport:
    healthy: bool
    reason: str
    details: dict[str, Any]


def check_health(
    path: Path, *, max_age: float = DEFAULT_MAX_AGE_SECONDS, now: datetime | None = None
) -> HealthReport:
    """Read the heartbeat file and judge it. Never raises for a missing or corrupt file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(data["updated_at"])
        state = str(data["state"])
    except FileNotFoundError:
        return HealthReport(False, "no heartbeat file", {})
    except (OSError, ValueError, KeyError, TypeError):
        return HealthReport(False, "unreadable heartbeat file", {})
    details = {
        key: data.get(key) for key in ("state", "env", "pid", "lock_held", "polling", "updated_at")
    }
    age = ((now or datetime.now(UTC)) - updated).total_seconds()
    details["age_seconds"] = round(age, 1)
    if state != BotState.READY:
        return HealthReport(False, f"bot state is {state}", details)
    if not (data.get("lock_held") and data.get("polling")):
        return HealthReport(False, "not holding the poller lock or not polling", details)
    if age > max_age:
        return HealthReport(False, f"heartbeat is {age:.0f}s old (limit {max_age:.0f}s)", details)
    return HealthReport(True, "ready", details)
