"""Tests for the awareness system: Observation, TriggerRule, TriggerEngine, AwarenessService."""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.awareness.base import Observation
from aria.awareness.triggers import TriggerRule, TriggerEngine

from tests.conftest import make_mock_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_obs(**overrides) -> Observation:
    defaults = dict(
        sensor="test_sensor",
        category="system",
        event_type="high_cpu",
        summary="CPU is at 95%",
        detail="Load average 8.2",
        severity="warning",
        tags=["infra"],
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Observation(**defaults)


def _make_mock_awareness_db():
    """Extend make_mock_db with awareness-specific collections."""
    db = make_mock_db()

    # observations collection
    obs_coll = MagicMock()
    obs_coll.find_one = AsyncMock(return_value=None)
    obs_coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
    obs_coll.insert_many = AsyncMock()
    obs_coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    obs_coll.create_index = AsyncMock()
    obs_cursor = MagicMock()
    obs_cursor.sort = MagicMock(return_value=obs_cursor)
    obs_cursor.limit = MagicMock(return_value=obs_cursor)
    obs_cursor.to_list = AsyncMock(return_value=[])
    obs_coll.find = MagicMock(return_value=obs_cursor)
    db.observations = obs_coll

    # awareness_summaries collection
    sum_coll = MagicMock()
    sum_coll.find_one = AsyncMock(return_value=None)
    sum_coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
    sum_coll.create_index = AsyncMock()
    db.awareness_summaries = sum_coll

    # awareness_triggers collection
    trig_coll = MagicMock()
    trig_coll.find_one = AsyncMock(return_value=None)
    trig_coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
    trig_coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    trig_coll.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    trig_cursor = MagicMock()
    trig_cursor.sort = MagicMock(return_value=trig_cursor)
    trig_cursor.limit = MagicMock(return_value=trig_cursor)
    trig_cursor.to_list = AsyncMock(return_value=[])
    trig_coll.find = MagicMock(return_value=trig_cursor)
    db.awareness_triggers = trig_coll

    # awareness_trigger_log collection
    log_coll = MagicMock()
    log_coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
    db.awareness_trigger_log = log_coll

    return db


# ===================================================================
# Observation tests
# ===================================================================

class TestObservation:

    def test_observation_to_doc(self):
        """All fields serialized correctly."""
        now = datetime.now(timezone.utc)
        obs = _make_obs(created_at=now)
        doc = obs.to_doc()

        assert doc["sensor"] == "test_sensor"
        assert doc["category"] == "system"
        assert doc["event_type"] == "high_cpu"
        assert doc["summary"] == "CPU is at 95%"
        assert doc["detail"] == "Load average 8.2"
        assert doc["severity"] == "warning"
        assert doc["tags"] == ["infra"]
        assert doc["created_at"] == now

    def test_observation_to_context_line(self):
        """Formats as [category/event_type] summary (Xm ago)."""
        obs = _make_obs(created_at=datetime.now(timezone.utc) - timedelta(minutes=5))
        line = obs.to_context_line()

        assert line.startswith("[system/high_cpu]")
        assert "CPU is at 95%" in line
        assert "5m ago" in line

    def test_observation_to_context_line_just_now(self):
        """< 60s shows 'just now'."""
        obs = _make_obs(created_at=datetime.now(timezone.utc) - timedelta(seconds=10))
        line = obs.to_context_line()

        assert "just now" in line

    def test_observation_to_context_line_hours(self):
        """> 3600s shows 'Xh ago'."""
        obs = _make_obs(created_at=datetime.now(timezone.utc) - timedelta(hours=3))
        line = obs.to_context_line()

        assert "3h ago" in line


# ===================================================================
# TriggerRule tests
# ===================================================================

class TestTriggerRule:

    def test_trigger_rule_matches_basic(self):
        """Category matches."""
        rule = TriggerRule(name="r1", category="system")
        assert rule.matches({"category": "system", "severity": "info"}) is True

    def test_trigger_rule_no_match_category(self):
        """Different category does not match."""
        rule = TriggerRule(name="r1", category="system")
        assert rule.matches({"category": "git", "severity": "info"}) is False

    def test_trigger_rule_matches_severity(self):
        """Severity filter works."""
        rule = TriggerRule(name="r1", category="system", severity="warning")
        assert rule.matches({"category": "system", "severity": "warning"}) is True
        assert rule.matches({"category": "system", "severity": "info"}) is False

    def test_trigger_rule_matches_content_pattern(self):
        """Regex pattern on detail."""
        rule = TriggerRule(name="r1", category="system", content_pattern=r"CPU.*90")
        assert rule.matches({
            "category": "system",
            "detail": "CPU usage is 90%",
        }) is True
        assert rule.matches({
            "category": "system",
            "detail": "Memory usage is 50%",
        }) is False

    def test_trigger_rule_no_match_disabled(self):
        """Disabled rule never matches."""
        rule = TriggerRule(name="r1", category="system", enabled=False)
        assert rule.matches({"category": "system"}) is False

    def test_trigger_rule_cooldown(self):
        """Rule within cooldown doesn't match."""
        rule = TriggerRule(name="r1", category="system", cooldown_seconds=600)
        rule.last_fired_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        # Still within 600s cooldown
        assert rule.matches({"category": "system"}) is False

        # Outside cooldown
        rule.last_fired_at = datetime.now(timezone.utc) - timedelta(seconds=700)
        assert rule.matches({"category": "system"}) is True

    def test_trigger_rule_to_doc_roundtrip(self):
        """to_doc -> from_doc preserves data."""
        original = TriggerRule(
            name="test_rule",
            category="git",
            severity="warning",
            content_pattern=r"uncommitted.*changes",
            action="notify",
            action_params={"message": "You have uncommitted changes"},
            cooldown_seconds=120,
            enabled=True,
        )
        doc = original.to_doc()
        restored = TriggerRule.from_doc(doc)

        assert restored.name == original.name
        assert restored.category == original.category
        assert restored.severity == original.severity
        assert restored.content_pattern == original.content_pattern
        assert restored.action == original.action
        assert restored.action_params == original.action_params
        assert restored.cooldown_seconds == original.cooldown_seconds
        assert restored.enabled == original.enabled


# ===================================================================
# TriggerEngine tests
# ===================================================================

class TestTriggerEngine:

    @pytest.mark.asyncio
    async def test_engine_evaluate_fires_matching(self):
        """evaluate fires matching rules."""
        db = _make_mock_awareness_db()
        engine = TriggerEngine(db)

        rule = TriggerRule(name="cpu_alert", category="system", action="notify")
        engine._rules["cpu_alert"] = rule

        with patch.object(engine, "_fire_action", new_callable=AsyncMock, return_value="notification_sent"):
            fired = await engine.evaluate({"category": "system", "severity": "warning"})

        assert len(fired) == 1
        assert fired[0]["rule"] == "cpu_alert"
        assert fired[0]["action"] == "notify"
        # Should have logged to trigger_log
        db.awareness_trigger_log.insert_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_engine_evaluate_no_match(self):
        """No rules match returns empty list."""
        db = _make_mock_awareness_db()
        engine = TriggerEngine(db)

        rule = TriggerRule(name="cpu_alert", category="system")
        engine._rules["cpu_alert"] = rule

        fired = await engine.evaluate({"category": "git", "severity": "info"})
        assert fired == []

    @pytest.mark.asyncio
    async def test_engine_add_rule(self):
        """Adds rule to DB and internal dict."""
        db = _make_mock_awareness_db()
        engine = TriggerEngine(db)

        rule = TriggerRule(name="new_rule", category="filesystem")
        result = await engine.add_rule(rule)

        assert result == "new_rule"
        assert "new_rule" in engine._rules
        db.awareness_triggers.update_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_engine_remove_rule(self):
        """Removes from DB and internal dict."""
        db = _make_mock_awareness_db()
        engine = TriggerEngine(db)
        engine._rules["old_rule"] = TriggerRule(name="old_rule", category="system")

        removed = await engine.remove_rule("old_rule")

        assert removed is True
        assert "old_rule" not in engine._rules
        db.awareness_triggers.delete_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_engine_list_rules(self):
        """Returns all rules as dicts."""
        db = _make_mock_awareness_db()
        engine = TriggerEngine(db)
        engine._rules["r1"] = TriggerRule(name="r1", category="system")
        engine._rules["r2"] = TriggerRule(name="r2", category="git")

        rules = engine.list_rules()

        assert len(rules) == 2
        names = {r["name"] for r in rules}
        assert names == {"r1", "r2"}


# ===================================================================
# AwarenessService tests
# ===================================================================

class TestAwarenessService:

    def _make_service(self, db=None):
        """Create an AwarenessService with mocked sensors."""
        db = db or _make_mock_awareness_db()
        with patch("aria.awareness.service.AwarenessService._init_sensors"):
            from aria.awareness.service import AwarenessService
            svc = AwarenessService(db)
        svc.sensors = []
        return svc

    def test_service_status(self):
        """Returns status dict with correct fields."""
        svc = self._make_service()
        status = svc.status()

        assert "enabled" in status
        assert "running" in status
        assert "sensors" in status
        assert "poll_interval_seconds" in status
        assert "analysis_interval_minutes" in status
        assert "observation_ttl_hours" in status
        assert "watch_dirs" in status
        assert isinstance(status["sensors"], list)

    @pytest.mark.asyncio
    async def test_run_poll_persists_observations(self):
        """Polls sensors and inserts observations to DB."""
        db = _make_mock_awareness_db()
        svc = self._make_service(db)

        obs = _make_obs()
        sensor = MagicMock()
        sensor.name = "test_sensor"
        sensor.poll = AsyncMock(return_value=[obs])
        svc.sensors = [sensor]

        await svc._run_poll()

        sensor.poll.assert_awaited_once()
        db.observations.insert_many.assert_awaited_once()
        docs = db.observations.insert_many.call_args[0][0]
        assert len(docs) == 1
        assert docs[0]["sensor"] == "test_sensor"

    @pytest.mark.asyncio
    async def test_get_recent_observations(self):
        """Queries DB with filters."""
        db = _make_mock_awareness_db()
        svc = self._make_service(db)

        fake_obs = [{"sensor": "git", "category": "git", "event_type": "changes", "summary": "3 files"}]
        db.observations.find.return_value.sort.return_value.to_list = AsyncMock(return_value=fake_obs)

        result = await svc.get_recent_observations(limit=5, category="git", severity="warning", hours=2.0)

        assert result == fake_obs
        # Verify the query filter includes category and severity
        call_args = db.observations.find.call_args[0][0]
        assert call_args["category"] == "git"
        assert call_args["severity"] == "warning"
        assert "created_at" in call_args

    @pytest.mark.asyncio
    async def test_get_context_lines(self):
        """Returns formatted observation lines."""
        db = _make_mock_awareness_db()
        svc = self._make_service(db)

        now = datetime.now(timezone.utc) - timedelta(minutes=2)
        fake_obs = [{
            "sensor": "system",
            "category": "system",
            "event_type": "high_cpu",
            "summary": "CPU at 90%",
            "severity": "warning",
            "created_at": now,
        }]
        db.observations.find.return_value.sort.return_value.to_list = AsyncMock(return_value=fake_obs)

        lines = await svc.get_context_lines(limit=5, hours=1.0)

        assert len(lines) == 1
        assert "[system/high_cpu]" in lines[0]
        assert "CPU at 90%" in lines[0]

    @pytest.mark.asyncio
    async def test_get_latest_summary(self):
        """Returns most recent summary."""
        db = _make_mock_awareness_db()
        svc = self._make_service(db)

        db.awareness_summaries.find_one = AsyncMock(return_value={
            "summary": "All systems normal",
            "created_at": datetime.now(timezone.utc),
        })

        result = await svc.get_latest_summary()
        assert result == "All systems normal"
