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
