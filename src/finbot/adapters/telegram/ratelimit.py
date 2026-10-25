"""In-process rate limits and duplicate-update detection for the Telegram adapter.

* NFR-SEC-07: at most 30 updates per minute per Telegram user; updates over the limit are
  dropped, and the user gets a single notice per window.
* T17 / AT-TOK-04: after 10 bad ``/start`` payloads (refused or malformed invites) within a
  minute, further ``/start`` payloads from that account are refused without touching the
  database. The reply is the same notice whether or not the account or the invite exists.

State lives in memory and is bounded (least-recently-seen keys are evicted). It is correct for
the single polling process that the poller lock guarantees (docs/06 Step 4, AC-P1-04). No
``await`` happens inside these methods, so concurrent handlers cannot interleave within a check.
"""

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

UPDATES_PER_MINUTE = 30
BAD_STARTS_PER_MINUTE = 10
_WINDOW_SECONDS = 60.0
_MAX_KEYS = 10_000


@dataclass(frozen=True, slots=True)
class LimitDecision:
    allowed: bool
    notify: bool = False  # True only for the first refusal in a window ("single notice")


@dataclass(slots=True)
class _Bucket:
    hits: deque[float] = field(default_factory=deque)
    notified_until: float = 0.0


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float = _WINDOW_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = _MAX_KEYS,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("limit, window and max_keys must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[int, _Bucket] = OrderedDict()

    def _bucket(self, key: int, now: float) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket()
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        while bucket.hits and bucket.hits[0] <= now - self._window:
            bucket.hits.popleft()
        return bucket

    def _refuse(self, bucket: _Bucket, now: float) -> LimitDecision:
        notify = now >= bucket.notified_until
        if notify:
            bucket.notified_until = bucket.hits[0] + self._window
        return LimitDecision(allowed=False, notify=notify)

    def hit(self, key: int) -> LimitDecision:
        """Count one event for ``key`` and say whether it is within the limit."""
        now = self._clock()
        bucket = self._bucket(key, now)
        if len(bucket.hits) >= self._limit:
            return self._refuse(bucket, now)
        bucket.hits.append(now)
        return LimitDecision(allowed=True)

    def check(self, key: int) -> LimitDecision:
        """Whether ``key`` is currently over the limit, without counting an event."""
        now = self._clock()
        bucket = self._bucket(key, now)
        if len(bucket.hits) >= self._limit:
            return self._refuse(bucket, now)
        return LimitDecision(allowed=True)

    def record(self, key: int) -> None:
        """Count one event without deciding anything (e.g. a refused invite)."""
        now = self._clock()
        self._bucket(key, now).hits.append(now)


class RecentUpdates:
    """Remembers recent ``update_id`` values so a redelivered update is processed once."""

    def __init__(self, capacity: int = _MAX_KEYS) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._seen: OrderedDict[int, None] = OrderedDict()

    def first_time(self, update_id: int) -> bool:
        if update_id in self._seen:
            return False
        self._seen[update_id] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True
