"""Tests for session checkpointing — data model and resume prompt building."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.agents.checkpoint import (
    SessionCheckpoint,
    build_resume_prompt,
    find_resumable_checkpoint,
    read_checkpoint,
    write_checkpoint,
)
from tests.conftest import make_mock_db


class TestSessionCheckpoint:
    def test_to_dict_roundtrip(self):
        now = datetime.now(timezone.utc)
        cp = SessionCheckpoint(
            session_id="s1",
            workspace="/home/user/project",
            branch="feature-x",
            last_commit="abc1234",
            modified_files=["src/main.py", "README.md"],
            current_step="Writing tests",
            notes="Halfway through",
            timestamp=now,
        )
        d = cp.to_dict()
        assert d["session_id"] == "s1"
        assert d["branch"] == "feature-x"
        assert len(d["modified_files"]) == 2

        restored = SessionCheckpoint.from_dict(d)
        assert restored.session_id == "s1"
        assert restored.workspace == "/home/user/project"
        assert restored.branch == "feature-x"
        assert restored.modified_files == ["src/main.py", "README.md"]

    def test_defaults(self):
        cp = SessionCheckpoint(session_id="s1", workspace="/tmp")
        assert cp.branch is None
        assert cp.modified_files == []
        assert cp.notes is None
        assert cp.timestamp is not None


class TestBuildResumePrompt:
    def test_includes_original_prompt(self):
        cp = SessionCheckpoint(session_id="s1", workspace="/tmp")
        prompt = build_resume_prompt(cp, "Fix the login bug")
        assert "Fix the login bug" in prompt

    def test_includes_branch(self):
        cp = SessionCheckpoint(session_id="s1", workspace="/tmp", branch="fix-login")
        prompt = build_resume_prompt(cp, "task")
        assert "fix-login" in prompt

    def test_includes_commit(self):
        cp = SessionCheckpoint(session_id="s1", workspace="/tmp", last_commit="abc1234")
        prompt = build_resume_prompt(cp, "task")
        assert "abc1234" in prompt

    def test_includes_modified_files(self):
        cp = SessionCheckpoint(
            session_id="s1", workspace="/tmp",
            modified_files=["src/auth.py", "tests/test_auth.py"],
        )
        prompt = build_resume_prompt(cp, "task")
        assert "src/auth.py" in prompt
        assert "tests/test_auth.py" in prompt

    def test_includes_notes(self):
        cp = SessionCheckpoint(session_id="s1", workspace="/tmp", notes="Tests failing")
        prompt = build_resume_prompt(cp, "task")
        assert "Tests failing" in prompt

    def test_includes_resume_instructions(self):
        cp = SessionCheckpoint(session_id="s1", workspace="/tmp")
        prompt = build_resume_prompt(cp, "task")
        assert "resuming" in prompt.lower()
        assert "git status" in prompt.lower()


class TestWriteCheckpoint:
    @pytest.mark.asyncio
    async def test_write_upserts(self):
        db = make_mock_db()
        coll = MagicMock()
        coll.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        db.session_checkpoints = coll

        cp = await write_checkpoint(
            db, "s1", "/nonexistent/path",
            current_step="testing",
            notes="test note",
        )
        assert cp.session_id == "s1"
        assert cp.notes == "test note"
        coll.update_one.assert_awaited_once()
        # Should upsert
        call_kwargs = coll.update_one.await_args
        assert call_kwargs[1].get("upsert") is True or call_kwargs[0][2] is True


class TestReadCheckpoint:
    @pytest.mark.asyncio
    async def test_read_existing(self):
        db = make_mock_db()
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value={
            "session_id": "s1",
            "workspace": "/tmp",
            "branch": "main",
        })
        db.session_checkpoints = coll

        cp = await read_checkpoint(db, "s1")
        assert cp is not None
        assert cp.session_id == "s1"

    @pytest.mark.asyncio
    async def test_read_nonexistent(self):
        db = make_mock_db()
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value=None)
        db.session_checkpoints = coll

        cp = await read_checkpoint(db, "nope")
        assert cp is None


class TestFindResumableCheckpoint:
    @pytest.mark.asyncio
    async def test_find_by_workspace(self):
        db = make_mock_db()
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value={
            "session_id": "s1",
            "workspace": "/home/user/project",
            "branch": "feature-x",
        })
        db.session_checkpoints = coll

        cp = await find_resumable_checkpoint(db, "/home/user/project")
        assert cp is not None
        assert cp.workspace == "/home/user/project"

    @pytest.mark.asyncio
    async def test_find_none(self):
        db = make_mock_db()
        coll = MagicMock()
        coll.find_one = AsyncMock(return_value=None)
        db.session_checkpoints = coll

        cp = await find_resumable_checkpoint(db, "/no/checkpoint/here")
        assert cp is None
