"""Injected clock (ENGINEERING-STANDARDS SS1 rule 2).

`domain`, `detection`, and `policy` never call `datetime.now()` directly --
they receive an already-resolved `datetime` from whatever layer holds the
clock. That is what makes a scenario's timing exact and reproducible, and
what makes replaying an audit log against the pipeline in replay mode
produce the same case state it did the first time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

__all__ = ["Clock", "FrozenClock", "SystemClock"]


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """The real clock. Used only at the process boundary (workers, the API
    layer, the live gateway client) -- never imported by domain, detection,
    or policy."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FrozenClock:
    """A clock that only moves when told to. Tests and the simulator inject
    this instead of `SystemClock`, so a scenario's timing is exact and
    reproducible rather than whatever wall-clock instant happened to
    elapse during a test run."""

    _current: datetime

    def now(self) -> datetime:
        return self._current

    def set(self, at: datetime) -> None:
        self._current = at

    def advance(self, delta: timedelta) -> None:
        self._current += delta
