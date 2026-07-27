"""Small async token bucket used to avoid flooding radios and Home Assistant."""

from __future__ import annotations

import asyncio
from time import monotonic


class TokenBucket:
    """Async token bucket."""

    def __init__(self, rate: float, capacity: int) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._rate = rate
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until tokens are available."""
        if tokens <= 0:
            return
        while True:
            async with self._lock:
                now = monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait_for = (tokens - self._tokens) / self._rate
            await asyncio.sleep(wait_for)

    def snapshot(self) -> dict[str, float]:
        """Return current bucket state."""
        return {
            "rate": self._rate,
            "capacity": self._capacity,
            "tokens": self._tokens,
        }
