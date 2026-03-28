"""
ARIA - Resilience Utilities

Purpose: Retry helpers and circuit breaker implementation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Retry an async operation with exponential backoff."""
    attempt = 0
    last_error: Exception | None = None

    while attempt < retries:
        try:
            return await operation()
        except exceptions as exc:
            last_error = exc
            attempt += 1
            if attempt >= retries:
                break
            await asyncio.sleep(min(max_delay, base_delay * (2 ** (attempt - 1))))

    if last_error is None:
        raise RuntimeError("retry_async: exhausted retries with no captured error")
    raise last_error


@dataclass
class CircuitBreaker:
    """Minimal async-safe circuit breaker for unstable external services."""

    failure_threshold: int = 5
    recovery_timeout_seconds: int = 30
    failure_count: int = 0
    opened_at: datetime | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def allow_request(self) -> bool:
        async with self._lock:
            if self.opened_at is None:
                return True

            if datetime.now(timezone.utc) - self.opened_at >= timedelta(seconds=self.recovery_timeout_seconds):
                return True

            return False

    async def record_success(self) -> None:
        async with self._lock:
            self.failure_count = 0
            self.opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold:
                self.opened_at = datetime.now(timezone.utc)

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        if not await self.allow_request():
            raise RuntimeError("Circuit is open")

        try:
            result = await operation()
        except Exception:
            await self.record_failure()
            raise

        await self.record_success()
        return result
