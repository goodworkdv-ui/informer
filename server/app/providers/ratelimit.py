"""Ограничители частоты запросов к провайдерам (D-02, лимиты Finnhub и Twelve Data)."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone


class RateLimiter:
    """Скользящее окно: не более per_min запросов за 60 с и (опционально) per_day за сутки UTC."""

    def __init__(self, name: str, per_min: int, per_day: int | None = None):
        self.name = name
        self.per_min = per_min
        self.per_day = per_day
        self.window: deque[float] = deque()
        self.day = datetime.now(timezone.utc).date()
        self.day_count = 0
        self.lock = asyncio.Lock()
        self.last_ok: str | None = None
        self.last_error: str | None = None

    def _roll_day(self):
        d = datetime.now(timezone.utc).date()
        if d != self.day:
            self.day, self.day_count = d, 0

    def remaining_today(self) -> int | None:
        self._roll_day()
        return None if self.per_day is None else max(0, self.per_day - self.day_count)

    def remaining_minute(self) -> int:
        now = time.monotonic()
        while self.window and now - self.window[0] >= 60:
            self.window.popleft()
        return max(0, self.per_min - len(self.window))

    def day_exhausted(self) -> bool:
        r = self.remaining_today()
        return r is not None and r <= 0

    async def acquire(self) -> bool:
        """Ждёт свободного слота в минуте. Возвращает False, если исчерпан суточный лимит."""
        async with self.lock:
            if self.day_exhausted():
                return False
            while True:
                now = time.monotonic()
                while self.window and now - self.window[0] >= 60:
                    self.window.popleft()
                if len(self.window) < self.per_min:
                    self.window.append(now)
                    self.day_count += 1
                    return True
                await asyncio.sleep(60 - (now - self.window[0]) + 0.05)

    def status(self) -> dict:
        return {
            "name": self.name,
            "per_min": self.per_min,
            "left_minute": self.remaining_minute(),
            "per_day": self.per_day,
            "left_day": self.remaining_today(),
            "last_ok": self.last_ok,
            "last_error": self.last_error,
        }
