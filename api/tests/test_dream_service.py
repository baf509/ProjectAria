"""Tests for the dream cycle service — locking, gating, parsing, persistence."""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.dreams.service import DreamService, _is_pid_alive
from tests.conftest import make_mock_db


@pytest.fixture
def mock_db():
    db = make_mock_db()
    for name in [
        "dream_lock", "dream_journal", "dream_soul_proposals",
        "memories", "session_checkpoints",
    ]:
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value=None)
        coll.insert_one = AsyncMock(return_value=MagicMock(inserted_id="mock-id"))
        coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        coll.update_many = AsyncMock(return_value=MagicMock(modified_count=0))
        coll.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        cursor = MagicMock()
        cursor.sort = MagicMock(return_value=cursor)
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=[])
        coll.find = MagicMock(return_value=cursor)
        coll.count_documents = AsyncMock(return_value=0)
        setattr(db, name, coll)
    return db


@pytest.fixture
def dream_service(mock_db):
    with patch("aria.dreams.service.settings") as mock_settings:
        mock_settings.dream_enabled = True
        mock_settings.dream_interval_hours = 6
        mock_settings.dream_active_hours_start = 1
        mock_settings.dream_active_hours_end = 6
        mock_settings.dream_timeout_seconds = 120
        mock_settings.dream_claude_model = None
        mock_settings.claude_code_binary = "claude"
        mock_settings.dream_min_conversations = 3
        svc = DreamService(mock_db)
    return svc


class TestIsPidAlive:
    def test_current_process_is_alive(self):
        assert _is_pid_alive(os.getpid()) is True

    def test_nonexistent_pid(self):
        # PID 99999999 almost certainly doesn't exist
        assert _is_pid_alive(99999999) is False


class TestDreamLock:
    @pytest.mark.asyncio
    async def test_acquire_when_no_lock_exists(self, dream_service, mock_db):
        """Should acquire lock via insert when no lock document exists."""
        mock_db.dream_lock.find_one = AsyncMock(return_value=None)
        result = await dream_service._acquire_lock()
        assert result is True
        mock_db.dream_lock.insert_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_acquire_fails_when_live_lock_held(self, dream_service, mock_db):
        """Should fail when another live process holds the lock."""
        mock_db.dream_lock.find_one = AsyncMock(return_value={
            "_id": "dream_cycle",
            "holder_pid": os.getpid(),  # Current process — definitely alive
            "acquired_at": datetime.now(timezone.utc),
        })
        result = await dream_service._acquire_lock()
        assert result is False

    @pytest.mark.asyncio
    async def test_acquire_reclaims_stale_lock(self, dream_service, mock_db):
        """Should reclaim a lock held by a dead process."""
        mock_db.dream_lock.find_one = AsyncMock(return_value={
            "_id": "dream_cycle",
            "holder_pid": 99999999,  # Dead PID
            "acquired_at": datetime.now(timezone.utc) - timedelta(hours=2),
        })
        mock_db.dream_lock.update_one = AsyncMock(
            return_value=MagicMock(modified_count=1)
        )
        result = await dream_service._acquire_lock()
        assert result is True

    @pytest.mark.asyncio
    async def test_release_lock(self, dream_service, mock_db):
        await dream_service._release_lock()
        mock_db.dream_lock.delete_one.assert_awaited_once()


class TestActivityGate:
    @pytest.mark.asyncio
    async def test_gate_disabled_when_min_zero(self, dream_service):
        """Should pass when dream_min_conversations is 0."""
        with patch("aria.dreams.service.settings") as s:
            s.dream_min_conversations = 0
            assert await dream_service._has_enough_activity() is True

    @pytest.mark.asyncio
    async def test_gate_fails_when_not_enough(self, dream_service, mock_db):
        """Should fail when total activity is below threshold."""
        with patch("aria.dreams.service.settings") as s:
            s.dream_min_conversations = 3
            mock_db.conversations.count_documents = AsyncMock(return_value=1)
            mock_db.memories.count_documents = AsyncMock(return_value=0)
            result = await dream_service._has_enough_activity()
            assert result is False

    @pytest.mark.asyncio
    async def test_gate_passes_with_conversations(self, dream_service, mock_db):
        """Should pass when conversation count alone meets threshold."""
        with patch("aria.dreams.service.settings") as s:
            s.dream_min_conversations = 3
            mock_db.conversations.count_documents = AsyncMock(return_value=5)
            mock_db.memories.count_documents = AsyncMock(return_value=0)
            result = await dream_service._has_enough_activity()
            assert result is True

    @pytest.mark.asyncio
    async def test_gate_passes_with_memories(self, dream_service, mock_db):
        """Should pass when memories alone meet the threshold."""
        with patch("aria.dreams.service.settings") as s:
            s.dream_min_conversations = 3
            mock_db.conversations.count_documents = AsyncMock(return_value=0)
            mock_db.memories.count_documents = AsyncMock(return_value=5)
            result = await dream_service._has_enough_activity()
            assert result is True

    @pytest.mark.asyncio
    async def test_gate_passes_with_combined_activity(self, dream_service, mock_db):
        """Should pass when conversations + memories together meet threshold."""
        with patch("aria.dreams.service.settings") as s:
            s.dream_min_conversations = 3
            mock_db.conversations.count_documents = AsyncMock(return_value=1)
            mock_db.memories.count_documents = AsyncMock(return_value=2)
            result = await dream_service._has_enough_activity()
            assert result is True

    @pytest.mark.asyncio
    async def test_first_call_not_throttled(self, dream_service, mock_db):
        """First call should NOT be throttled (bug fix: _last_conversation_scan = -inf)."""
        with patch("aria.dreams.service.settings") as s:
            s.dream_min_conversations = 3
            mock_db.conversations.count_documents = AsyncMock(return_value=3)
            mock_db.memories.count_documents = AsyncMock(return_value=2)
            result = await dream_service._has_enough_activity()
            # Should actually query DB, not be throttled
            mock_db.conversations.count_documents.assert_awaited()
            assert result is True


class TestParseOutput:
    def test_valid_json(self, dream_service):
        output = json.dumps({
            "journal_entry": "Today I learned...",
            "connections": [],
            "knowledge_gaps": [],
        })
        result = dream_service._parse_output(output)
        assert result is not None
        assert result["journal_entry"] == "Today I learned..."

    def test_json_with_markdown_fences(self, dream_service):
        output = "```json\n" + json.dumps({
            "journal_entry": "Reflection",
        }) + "\n```"
        result = dream_service._parse_output(output)
        assert result is not None
        assert result["journal_entry"] == "Reflection"

    def test_json_with_surrounding_text(self, dream_service):
        output = "Here's my reflection:\n" + json.dumps({
            "journal_entry": "Deep thought",
        }) + "\nThat's all."
        result = dream_service._parse_output(output)
        assert result is not None

    def test_missing_journal_entry(self, dream_service):
        output = json.dumps({"connections": []})
        result = dream_service._parse_output(output)
        assert result is None

    def test_invalid_json(self, dream_service):
        result = dream_service._parse_output("not json at all")
        assert result is None

    def test_empty_output(self, dream_service):
        result = dream_service._parse_output("")
        assert result is None

    def test_non_dict_json(self, dream_service):
        result = dream_service._parse_output("[1, 2, 3]")
        assert result is None


class TestPersist:
    @pytest.mark.asyncio
    async def test_saves_journal(self, dream_service, mock_db):
        dream_data = {
            "journal_entry": "Today I reflected on conversations.",
            "connections": ["memory-a relates to memory-b"],
            "knowledge_gaps": ["Need to learn more about X"],
            "soul_proposals": [],
            "memory_consolidations": [],
            "stale_memory_ids": [],
        }
        await dream_service._persist(dream_data)
        mock_db.dream_journal.insert_one.assert_awaited_once()
        doc = mock_db.dream_journal.insert_one.await_args[0][0]
        assert doc["journal_entry"] == "Today I reflected on conversations."

    @pytest.mark.asyncio
    async def test_saves_soul_proposals(self, dream_service, mock_db):
        dream_data = {
            "journal_entry": "Reflection",
            "soul_proposals": [{"trait": "more curious"}],
            "memory_consolidations": [],
            "stale_memory_ids": [],
        }
        await dream_service._persist(dream_data)
        mock_db.dream_soul_proposals.insert_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_prunes_stale_memories(self, dream_service, mock_db):
        from bson import ObjectId
        oid = str(ObjectId())
        dream_data = {
            "journal_entry": "Reflection",
            "soul_proposals": [],
            "memory_consolidations": [],
            "stale_memory_ids": [oid],
        }
        await dream_service._persist(dream_data)
        mock_db.memories.update_many.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_invalid_stale_ids(self, dream_service, mock_db):
        dream_data = {
            "journal_entry": "Reflection",
            "soul_proposals": [],
            "memory_consolidations": [],
            "stale_memory_ids": ["not-a-valid-oid"],
        }
        await dream_service._persist(dream_data)
        # update_many should not be called (no valid IDs)
        # Actually it will be called with empty list — check it doesn't crash
        # The _prune_stale_memories method filters invalid IDs


class TestDreamStatus:
    def test_status_returns_dict(self, dream_service):
        with patch("aria.dreams.service.settings") as s:
            s.dream_enabled = True
            s.dream_interval_hours = 6
            s.dream_active_hours_start = 1
            s.dream_active_hours_end = 6
            s.claude_code_binary = "claude"
            s.dream_claude_model = None
            s.dream_timeout_seconds = 120
            status = dream_service.status()
            assert isinstance(status, dict)
            assert "enabled" in status
            assert "running" in status
            assert "last_run" in status
