"""SlidingWindowLimiter and RecentUpdates (NFR-SEC-07, T17 / AT-TOK-04)."""

import pytest

from finbot.adapters.telegram.ratelimit import (
    BAD_STARTS_PER_MINUTE,
    UPDATES_PER_MINUTE,
    LimitDecision,
    RecentUpdates,
    SlidingWindowLimiter,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_documented_limits() -> None:
    assert (UPDATES_PER_MINUTE, BAD_STARTS_PER_MINUTE) == (30, 10)


def test_hits_within_the_limit_then_a_single_notice_per_window() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(3, 60, clock=clock)
    assert [limiter.hit(1).allowed for _ in range(3)] == [True, True, True]
    assert limiter.hit(1) == LimitDecision(allowed=False, notify=True)
    assert limiter.hit(1) == LimitDecision(allowed=False, notify=False)
    assert limiter.hit(2).allowed  # keys are independent
    clock.now = 59.9
    assert limiter.hit(1) == LimitDecision(allowed=False, notify=False)
    clock.now = 60.0  # all three hits (t=0) leave the window
    assert [limiter.hit(1).allowed for _ in range(3)] == [True, True, True]
    assert limiter.hit(1) == LimitDecision(allowed=False, notify=True)  # new window, new notice


def test_check_and_record_separate_counting_from_deciding() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 60, clock=clock)
    assert limiter.check(1).allowed
    limiter.record(1)
    assert limiter.check(1).allowed  # checking does not count
    limiter.record(1)
    assert limiter.check(1) == LimitDecision(allowed=False, notify=True)
    assert limiter.check(1) == LimitDecision(allowed=False, notify=False)
    clock.now = 61
    assert limiter.check(1).allowed


def test_memory_is_bounded_by_evicting_the_least_recent_keys() -> None:
    limiter = SlidingWindowLimiter(1, 60, clock=Clock(), max_keys=3)
    for key in (1, 2, 3):
        limiter.hit(key)
    limiter.hit(1)  # refresh key 1
    limiter.hit(4)  # evicts key 2
    assert not limiter.hit(1).allowed
    assert limiter.hit(2).allowed  # forgotten, so counted afresh


@pytest.mark.parametrize(("limit", "window", "keys"), [(0, 60, 1), (1, 0, 1), (1, 60, 0)])
def test_invalid_configuration(limit: int, window: float, keys: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        SlidingWindowLimiter(limit, window, max_keys=keys)


def test_recent_updates_remember_a_bounded_number_of_ids() -> None:
    seen = RecentUpdates(capacity=2)
    assert seen.first_time(1)
    assert not seen.first_time(1)
    assert seen.first_time(2)
    assert seen.first_time(3)  # evicts 1
    assert seen.first_time(1)
    with pytest.raises(ValueError, match="positive"):
        RecentUpdates(capacity=0)
