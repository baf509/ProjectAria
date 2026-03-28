"""
ARIA - Simple Rate Limiting

Purpose: In-process request throttling for API protection.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from aria.config import settings


class RateLimiter:
    """Sliding-window in-memory rate limiter keyed by client identity."""

    def __init__(self):
        self._events: dict[str, deque[datetime]] = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, int]:
        if not settings.rate_limit_enabled:
            return True, settings.rate_limit_requests_per_window

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=settings.rate_limit_window_seconds)
        bucket = self._events[key]

        while bucket and bucket[0] < window_start:
            bucket.popleft()

        if len(bucket) >= settings.rate_limit_requests_per_window:
            return False, 0

        bucket.append(now)
        remaining = max(settings.rate_limit_requests_per_window - len(bucket), 0)

        # Prune stale keys to prevent unbounded memory growth
        if len(self._events) > 10000:
            stale_keys = [
                k for k, v in self._events.items()
                if not v or v[-1] < window_start
            ]
            for k in stale_keys:
                del self._events[k]

        return True, remaining
