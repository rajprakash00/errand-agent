"""Injectable clocks. Every timestamp in the system is timezone-aware."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Clock(Protocol):
    """The only source of time in the kernel."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall clock in the product timezone."""

    def now(self) -> datetime:
        return datetime.now(tz=IST)


class FakeClock:
    """Deterministic clock for tests and eval scenarios."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware start")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: float = 0, minutes: float = 0, days: float = 0) -> None:
        self._now = self._now + timedelta(seconds=seconds, minutes=minutes, days=days)

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware value")
        self._now = value


class DemoClock:
    """Wall clock with a movable offset for deterministic demo faults.

    The web demo runs on real time until a scenario needs time to pass, such as
    the hold-expiry flow. ``advance`` shifts the clock forward without freezing
    it, so the rest of the page keeps showing live timestamps.
    """

    def __init__(self, base: Clock | None = None) -> None:
        self._base: Clock = base or SystemClock()
        self._offset = timedelta(0)

    def now(self) -> datetime:
        return self._base.now() + self._offset

    def advance(self, *, seconds: float = 0, minutes: float = 0, days: float = 0) -> None:
        self._offset += timedelta(seconds=seconds, minutes=minutes, days=days)
