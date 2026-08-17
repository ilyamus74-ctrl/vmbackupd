"""Injectable clocks for runtime code."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from .models import utcnow


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return utcnow()


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self._current = current

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta | None = None, *, seconds: int = 0) -> datetime:
        self._current += delta if delta is not None else timedelta(seconds=seconds)
        return self._current
