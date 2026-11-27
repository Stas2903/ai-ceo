"""Injectable time. The demo and the tests run days of simulated work in milliseconds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock needs an aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> datetime:
        self._now += timedelta(**delta)
        return self._now
