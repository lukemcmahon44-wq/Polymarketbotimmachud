"""Token-bucket rate limiter with exponential backoff."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class RateLimiter:
    """Token-bucket rate limiter with exponential backoff on 429s."""

    max_requests: int = 9000
    window_seconds: float = 10.0
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    backoff_multiplier: float = 2.0

    _tokens: float = field(init=False, default=0)
    _last_refill: float = field(init=False, default=0)
    _consecutive_429s: int = field(init=False, default=0)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._tokens = float(self.max_requests)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * (self.max_requests / self.window_seconds)
        self._tokens = min(float(self.max_requests), self._tokens + new_tokens)
        self._last_refill = now

    async def acquire(self) -> None:
        """Wait until a token is available."""
        async with self._lock:
            self._refill()
            if self._tokens < 1:
                wait_time = (1 - self._tokens) / (self.max_requests / self.window_seconds)
                logger.debug("rate_limiter.waiting", wait_seconds=round(wait_time, 3))
                await asyncio.sleep(wait_time)
                self._refill()
            self._tokens -= 1

    async def backoff_on_429(self) -> float:
        """Exponential backoff after a 429 response. Returns wait time."""
        self._consecutive_429s += 1
        wait = min(
            self.backoff_base * (self.backoff_multiplier ** (self._consecutive_429s - 1)),
            self.backoff_max,
        )
        logger.warning(
            "rate_limiter.backoff",
            consecutive_429s=self._consecutive_429s,
            wait_seconds=round(wait, 2),
        )
        await asyncio.sleep(wait)
        return wait

    def reset_backoff(self) -> None:
        """Reset backoff counter after a successful request."""
        self._consecutive_429s = 0
