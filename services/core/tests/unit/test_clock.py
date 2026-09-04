"""Clock is injected everywhere domain, detection, and policy need "now"
(ENGINEERING-STANDARDS SS1 rule 2) -- FrozenClock is what makes a test's
timing exact rather than whatever wall-clock instant the test happened to
run at."""

from datetime import UTC, datetime, timedelta

from recoup.platform.clock import Clock, FrozenClock, SystemClock


def test_system_clock_returns_a_timezone_aware_datetime() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None


def test_frozen_clock_returns_the_set_time_repeatedly() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    clock = FrozenClock(at)
    assert clock.now() == at
    assert clock.now() == at  # calling now() twice does not advance it


def test_frozen_clock_set_moves_to_an_arbitrary_time() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    later = datetime(2026, 6, 1, tzinfo=UTC)
    clock.set(later)
    assert clock.now() == later


def test_frozen_clock_advance_moves_forward_by_a_delta() -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(timedelta(hours=3))
    assert clock.now() == datetime(2026, 1, 1, 3, tzinfo=UTC)


def test_system_clock_and_frozen_clock_both_satisfy_the_clock_protocol() -> None:
    def _accepts_clock(clock: Clock) -> datetime:
        return clock.now()

    assert _accepts_clock(SystemClock()).tzinfo is not None
    assert _accepts_clock(FrozenClock(datetime(2026, 1, 1, tzinfo=UTC))) == datetime(
        2026, 1, 1, tzinfo=UTC
    )
