"""Tests for aria.scheduler.service — cron parsing, reminder parsing, schedule CRUD."""

from datetime import datetime, timedelta, timezone

import pytest

from aria.scheduler.service import SchedulerService


@pytest.fixture
def scheduler():
    """Create a SchedulerService with None dependencies for pure method testing."""
    svc = SchedulerService.__new__(SchedulerService)
    return svc


# ---------------------------------------------------------------------------
# Cron expression parsing
# ---------------------------------------------------------------------------

class TestComputeNextRun:
    def test_every_minutes(self, scheduler):
        before = datetime.now(timezone.utc)
        result = scheduler._compute_next_run("every 5m")
        after = datetime.now(timezone.utc)
        assert before + timedelta(minutes=5) <= result <= after + timedelta(minutes=5)

    def test_every_minutes_verbose(self, scheduler):
        result = scheduler._compute_next_run("every 10 minutes")
        expected_min = datetime.now(timezone.utc) + timedelta(minutes=9, seconds=59)
        assert result > expected_min

    def test_every_hours(self, scheduler):
        before = datetime.now(timezone.utc)
        result = scheduler._compute_next_run("every 2h")
        assert result >= before + timedelta(hours=2) - timedelta(seconds=1)

    def test_every_hours_verbose(self, scheduler):
        result = scheduler._compute_next_run("every 3 hours")
        expected_min = datetime.now(timezone.utc) + timedelta(hours=2, minutes=59)
        assert result > expected_min

    def test_hourly(self, scheduler):
        now = datetime.now(timezone.utc)
        result = scheduler._compute_next_run("hourly")
        expected = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        assert result == expected

    def test_daily(self, scheduler):
        result = scheduler._compute_next_run("daily 09:30")
        assert result.hour == 9
        assert result.minute == 30
        assert result > datetime.now(timezone.utc) - timedelta(seconds=1)

    def test_weekly_future_day(self, scheduler):
        # Pick a day that's definitely in the future
        now = datetime.now(timezone.utc)
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        future_day = days[(now.weekday() + 3) % 7]
        result = scheduler._compute_next_run(f"weekly {future_day} 10:00")
        assert result > now
        assert result.hour == 10
        assert result.minute == 0

    def test_min_1_minute(self, scheduler):
        """'every 0m' should be clamped to 1 minute."""
        before = datetime.now(timezone.utc)
        result = scheduler._compute_next_run("every 0m")
        assert result >= before + timedelta(minutes=1) - timedelta(seconds=1)

    def test_invalid_expression_raises(self, scheduler):
        with pytest.raises(ValueError, match="Unsupported cron expression"):
            scheduler._compute_next_run("at midnight")

    def test_invalid_day_name_raises(self, scheduler):
        with pytest.raises(ValueError, match="Unknown day name"):
            scheduler._compute_next_run("weekly notaday 10:00")


# ---------------------------------------------------------------------------
# Natural language reminder parsing
# ---------------------------------------------------------------------------

class TestParseReminder:
    @pytest.mark.asyncio
    async def test_remind_in_minutes(self, scheduler):
        result = await scheduler.parse_reminder("remind me to check the oven in 30 minutes")
        assert result is not None
        assert result["schedule_type"] == "once"
        assert result["action"] == "remind"
        assert "oven" in result["params"]["message"]
        assert result["run_at"] > datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_remind_in_hours(self, scheduler):
        result = await scheduler.parse_reminder("remind me to call mom in 2 hours")
        assert result is not None
        assert result["schedule_type"] == "once"
        assert result["run_at"] > datetime.now(timezone.utc) + timedelta(hours=1, minutes=59)

    @pytest.mark.asyncio
    async def test_remind_at_time(self, scheduler):
        result = await scheduler.parse_reminder("remind me to take a break at 15:00")
        assert result is not None
        assert result["schedule_type"] == "once"
        assert result["run_at"].hour == 15
        assert result["run_at"].minute == 0

    @pytest.mark.asyncio
    async def test_every_recurring(self, scheduler):
        result = await scheduler.parse_reminder("every 30 minutes check the build status")
        assert result is not None
        assert result["schedule_type"] == "recurring"
        assert result["cron_expr"] == "every 30m"
        assert "build status" in result["params"]["message"]

    @pytest.mark.asyncio
    async def test_unrecognized_returns_none(self, scheduler):
        result = await scheduler.parse_reminder("what's the weather like?")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_returns_none(self, scheduler):
        result = await scheduler.parse_reminder("")
        assert result is None
