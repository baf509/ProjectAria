"""Tests for aria.core.resilience — retry and circuit breaker."""

import asyncio
from datetime import datetime, timedelta

import pytest

from aria.core.resilience import CircuitBreaker, retry_async


# ---------------------------------------------------------------------------
# retry_async
# ---------------------------------------------------------------------------

class TestRetryAsync:
    @pytest.mark.asyncio
    async def test_succeeds_first_try(self):
        call_count = 0

        async def op():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await retry_async(op, retries=3)
        assert result == "ok"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_failure(self):
        call_count = 0

        async def op():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("not yet")
            return "ok"

        result = await retry_async(op, retries=3, base_delay=0.01)
        assert result == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_retries(self):
        async def op():
            raise RuntimeError("always fails")

        with pytest.raises(RuntimeError, match="always fails"):
            await retry_async(op, retries=2, base_delay=0.01)

    @pytest.mark.asyncio
    async def test_only_catches_specified_exceptions(self):
        async def op():
            raise TypeError("wrong type")

        with pytest.raises(TypeError):
            await retry_async(
                op,
                retries=3,
                base_delay=0.01,
                exceptions=(ValueError,),
            )

    @pytest.mark.asyncio
    async def test_single_retry(self):
        call_count = 0

        async def op():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("fail once")
            return "recovered"

        result = await retry_async(op, retries=2, base_delay=0.01)
        assert result == "recovered"
        assert call_count == 2


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_initially_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        assert await cb.allow_request() is True

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=60)
        for _ in range(3):
            await cb.record_failure()
        assert await cb.allow_request() is False

    @pytest.mark.asyncio
    async def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        assert await cb.allow_request() is True

    @pytest.mark.asyncio
    async def test_resets_on_success(self):
        cb = CircuitBreaker(failure_threshold=3)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        assert cb.failure_count == 0
        assert cb.opened_at is None

    @pytest.mark.asyncio
    async def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0)
        await cb.record_failure()
        # With 0s timeout, should immediately allow half-open
        assert await cb.allow_request() is True

    @pytest.mark.asyncio
    async def test_call_success(self):
        cb = CircuitBreaker(failure_threshold=3)

        async def op():
            return "result"

        result = await cb.call(op)
        assert result == "result"
        assert cb.failure_count == 0

    @pytest.mark.asyncio
    async def test_call_failure_increments(self):
        cb = CircuitBreaker(failure_threshold=3)

        async def op():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await cb.call(op)
        assert cb.failure_count == 1

    @pytest.mark.asyncio
    async def test_call_rejects_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=300)
        await cb.record_failure()

        async def op():
            return "should not run"

        with pytest.raises(RuntimeError, match="Circuit is open"):
            await cb.call(op)
