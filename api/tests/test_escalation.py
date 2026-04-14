"""Tests for the escalation protocol — severity routing, re-escalation, stale detection."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.notifications.escalation import (
    Escalation,
    EscalationManager,
    Severity,
)
from tests.conftest import make_mock_db


@pytest.fixture
def mock_db():
    db = make_mock_db()
    # Add escalation-specific collections
    for name in ["escalations", "estop", "estop_log"]:
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value=None)
        coll.find_one_and_update = AsyncMock(return_value=None)
        coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
        coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[])
        coll.find = MagicMock(return_value=cursor)
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[])
        agg_cursor.__aiter__ = lambda self: self
        agg_cursor.__anext__ = AsyncMock(side_effect=StopAsyncIteration)
        coll.aggregate = MagicMock(return_value=agg_cursor)
        setattr(db, name, coll)
    return db


@pytest.fixture
def escalation_manager(mock_db):
    notification = MagicMock()
    notification.notify = AsyncMock()
    return EscalationManager(mock_db, notification)


class TestSeverityEnum:
    def test_ordering(self):
        assert Severity.LOW < Severity.MEDIUM < Severity.HIGH < Severity.CRITICAL

    def test_int_values(self):
        assert Severity.LOW == 0
        assert Severity.CRITICAL == 3


class TestEscalation:
    def test_to_dict_roundtrip(self):
        now = datetime.now(timezone.utc)
        esc = Escalation(
            source="test",
            severity=Severity.HIGH,
            description="Something broke",
            created_at=now,
        )
        d = esc.to_dict()
        assert d["severity"] == 2
        assert d["severity_name"] == "HIGH"
        assert d["source"] == "test"

        restored = Escalation.from_dict(d)
        assert restored.severity == Severity.HIGH
        assert restored.source == "test"
        assert restored.description == "Something broke"


class TestEscalationManager:
    @pytest.mark.asyncio
    async def test_escalate_creates_and_routes(self, escalation_manager, mock_db):
        esc = await escalation_manager.escalate(
            source="watchdog",
            severity=Severity.HIGH,
            description="Agent stuck",
        )
        assert esc.severity == Severity.HIGH
        assert esc.status == "open"
        mock_db.escalations.insert_one.assert_awaited_once()
        # HIGH routes through notify channel
        escalation_manager.notification_service.notify.assert_awaited()

    @pytest.mark.asyncio
    async def test_escalate_low_does_not_notify(self, escalation_manager):
        await escalation_manager.escalate(
            source="info",
            severity=Severity.LOW,
            description="Minor thing",
        )
        escalation_manager.notification_service.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve(self, escalation_manager, mock_db):
        resolved_doc = {
            "_id": "esc-1",
            "source": "test",
            "severity": 1,
            "description": "test",
            "status": "resolved",
            "resolution": "fixed it",
            "created_at": datetime.now(timezone.utc),
            "resolved_at": datetime.now(timezone.utc),
            "re_escalation_count": 0,
            "metadata": {},
        }
        mock_db.escalations.find_one_and_update = AsyncMock(return_value=resolved_doc)

        result = await escalation_manager.resolve("esc-1", "fixed it")
        assert result is not None
        assert result.status == "resolved"

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_returns_none(self, escalation_manager, mock_db):
        mock_db.escalations.find_one_and_update = AsyncMock(return_value=None)
        result = await escalation_manager.resolve("nope", "n/a")
        assert result is None

    @pytest.mark.asyncio
    async def test_re_escalation_caps_at_critical(self, escalation_manager, mock_db):
        """Re-escalating a CRITICAL item should stay at CRITICAL, not crash."""
        stale_doc = {
            "_id": "esc-critical",
            "source": "test",
            "severity": Severity.CRITICAL.value,
            "description": "critical issue",
            "status": "open",
            "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "re_escalation_count": 0,
            "metadata": {},
        }

        # check_stale iterates each severity level and queries by severity value.
        # Return the doc only for the CRITICAL query, empty for others.
        def make_cursor(query, **kwargs):
            cursor = MagicMock()
            if query.get("severity") == Severity.CRITICAL.value:
                cursor.to_list = AsyncMock(return_value=[stale_doc])
            else:
                cursor.to_list = AsyncMock(return_value=[])
            return cursor

        mock_db.escalations.find = MagicMock(side_effect=make_cursor)

        re_escalated = await escalation_manager.check_stale()
        # Should not raise ValueError — the bug fix caps at CRITICAL
        critical_results = [e for e in re_escalated if e.escalation_id == "esc-critical"]
        assert len(critical_results) == 1
        assert critical_results[0].severity == Severity.CRITICAL


class TestSeverityRouting:
    @pytest.mark.asyncio
    async def test_critical_uses_all_channels(self, escalation_manager):
        await escalation_manager.escalate(
            source="system",
            severity=Severity.CRITICAL,
            description="Everything is on fire",
        )
        # CRITICAL routes through notify AND notify_all
        calls = escalation_manager.notification_service.notify.await_args_list
        assert len(calls) >= 2
        event_types = [c.kwargs.get("event_type") or c[1].get("event_type", "") for c in calls]
        assert any("critical" in et.lower() for et in event_types)

    @pytest.mark.asyncio
    async def test_medium_no_notification(self, escalation_manager):
        await escalation_manager.escalate(
            source="info",
            severity=Severity.MEDIUM,
            description="Moderate issue",
        )
        # MEDIUM routes to log+db only, no notify
        escalation_manager.notification_service.notify.assert_not_awaited()
