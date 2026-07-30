"""Tests for the idle-session reaper (COHERENCE_DESIGN.md C9)."""
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.core.killswitch import Killswitch
from aria.shells.reaper import _said_done, SAVE_PROMPT, ShellReaperWorker

# SAVE_PROMPT now takes both `token` and `handoff` placeholders.
_FMT = dict(token="REAP_SAVED", handoff="/tmp/ws/HANDOFF.md")


class TestSaveDoneDetection:
    TOKEN = "REAP_SAVED"

    def test_echoed_prompt_is_not_done(self):
        # The save prompt mentions the token mid-sentence and is echoed to the
        # pane — this must NOT count as the agent having finished.
        echoed = SAVE_PROMPT.format(**_FMT)
        assert _said_done(echoed, self.TOKEN) is False

    def test_agent_reply_on_its_own_line_is_done(self):
        screen = "…saved notes to vault/war-audio-game/Analysis/FOO.md\nREAP_SAVED\n$ "
        assert _said_done(screen, self.TOKEN) is True

    def test_token_with_surrounding_whitespace_is_done(self):
        assert _said_done("  REAP_SAVED  \n", self.TOKEN) is True

    def test_token_as_substring_of_word_is_not_done(self):
        assert _said_done("NOT_REAP_SAVED_YET is a variable", self.TOKEN) is False

    def test_empty_or_none_screen(self):
        assert _said_done(None, self.TOKEN) is False
        assert _said_done("", self.TOKEN) is False

    def test_prompt_then_reply_still_detects(self):
        # Both the echoed instruction AND the standalone reply are visible.
        screen = SAVE_PROMPT.format(**_FMT) + "\nREAP_SAVED\n"
        assert _said_done(screen, self.TOKEN) is True

    def test_prompt_contains_the_token_placeholder_filled(self):
        assert "REAP_SAVED" in SAVE_PROMPT.format(**_FMT)


class TestVerifyHandoff:
    """The independent file-mtime check -- a self-reported done token alone
    must never be enough to reap (same lesson as C1's verification gate)."""

    def _worker(self):
        return ShellReaperWorker.__new__(ShellReaperWorker)

    def test_missing_file_is_not_verified(self):
        worker = self._worker()
        prompted_at = datetime.now(timezone.utc)
        assert worker._verify_handoff("/nonexistent/HANDOFF.md", prompted_at) is False

    def test_file_modified_after_prompt_is_verified(self):
        worker = self._worker()
        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            prompted_at = datetime.now(timezone.utc) - timedelta(seconds=5)
            os.utime(f.name, None)  # mtime = now, after prompted_at
            assert worker._verify_handoff(f.name, prompted_at) is True

    def test_stale_file_from_before_the_prompt_is_not_verified(self):
        """A HANDOFF.md that already existed before this save prompt was sent
        (e.g. from a previous, unrelated save) must not count -- otherwise a
        session that never actually saved anything this time gets reaped on
        an old file's coattails."""
        worker = self._worker()
        with tempfile.NamedTemporaryFile(suffix=".md") as f:
            old_time = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
            os.utime(f.name, (old_time, old_time))
            prompted_at = datetime.now(timezone.utc)  # after the file's mtime
            assert worker._verify_handoff(f.name, prompted_at) is False

    def test_handoff_path_uses_project_dir(self):
        worker = self._worker()
        shell = MagicMock(project_dir="/home/ben/Development/war-audio-game")
        assert worker._handoff_path(shell) == "/home/ben/Development/war-audio-game/HANDOFF.md"

    def test_handoff_path_falls_back_to_home_without_project_dir(self):
        worker = self._worker()
        shell = MagicMock(project_dir="")
        assert worker._handoff_path(shell) == os.path.join(os.path.expanduser("~"), "HANDOFF.md")


class TestConfirmedSaveRequiresBothSignals:
    """Neither the token nor the file alone is sufficient -- both must agree,
    and the reaper must NEVER reap on an unconfirmed save (skip + alert
    instead), no matter how long it waits."""

    @pytest.mark.asyncio
    async def test_token_without_file_is_not_confirmed(self):
        worker = ShellReaperWorker.__new__(ShellReaperWorker)
        worker._stop = MagicMock()
        worker._stop.wait = AsyncMock(side_effect=__import__("asyncio").TimeoutError())
        worker.svc = MagicMock()
        worker.svc.current_screen = AsyncMock(return_value="work done\nREAP_SAVED\n")
        with patch("aria.shells.reaper.settings") as mock_settings:
            mock_settings.shells_reap_save_timeout_minutes = 0  # single poll, then timeout
            mock_settings.shells_reap_done_token = "REAP_SAVED"
            confirmed = await worker._await_confirmed_save(
                "shell-1", "/nonexistent/HANDOFF.md", datetime.now(timezone.utc)
            )
        assert confirmed is False

    @pytest.mark.asyncio
    async def test_file_without_token_is_not_confirmed(self):
        worker = ShellReaperWorker.__new__(ShellReaperWorker)
        worker._stop = MagicMock()
        worker._stop.wait = AsyncMock(side_effect=__import__("asyncio").TimeoutError())
        worker.svc = MagicMock()
        worker.svc.current_screen = AsyncMock(return_value="still working...\n")
        with tempfile.NamedTemporaryFile(suffix=".md") as f, \
             patch("aria.shells.reaper.settings") as mock_settings:
            mock_settings.shells_reap_save_timeout_minutes = 0
            mock_settings.shells_reap_done_token = "REAP_SAVED"
            prompted_at = datetime.now(timezone.utc) - timedelta(seconds=5)
            os.utime(f.name, None)
            confirmed = await worker._await_confirmed_save("shell-1", f.name, prompted_at)
        assert confirmed is False

    @pytest.mark.asyncio
    async def test_both_signals_present_is_confirmed(self):
        worker = ShellReaperWorker.__new__(ShellReaperWorker)
        worker._stop = MagicMock()
        worker._stop.wait = AsyncMock(side_effect=__import__("asyncio").TimeoutError())
        worker.svc = MagicMock()
        worker.svc.current_screen = AsyncMock(return_value="saved it\nREAP_SAVED\n")
        with tempfile.NamedTemporaryFile(suffix=".md") as f, \
             patch("aria.shells.reaper.settings") as mock_settings:
            mock_settings.shells_reap_save_timeout_minutes = 1
            mock_settings.shells_reap_done_token = "REAP_SAVED"
            prompted_at = datetime.now(timezone.utc) - timedelta(seconds=5)
            os.utime(f.name, None)
            confirmed = await worker._await_confirmed_save("shell-1", f.name, prompted_at)
        assert confirmed is True


class TestCandidatesUniversalScope:
    """As of 2026-07-30, ANY idle watched shell is a candidate -- not just
    ones backed by an ARIA coding_sessions record."""

    @pytest.mark.asyncio
    async def test_hand_run_shell_with_no_coding_session_is_a_candidate(self):
        worker = ShellReaperWorker.__new__(ShellReaperWorker)
        old_shell = MagicMock(
            name="claude-ProjectAria",
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=10),
            tags=[],
        )
        old_shell.name = "claude-ProjectAria"  # MagicMock(name=...) doesn't set .name normally
        worker.svc = MagicMock()
        worker.svc.list_shells = AsyncMock(return_value=[old_shell])
        worker.db = MagicMock()
        worker.db.coding_sessions.find_one = AsyncMock(return_value=None)  # no coding session
        with patch("aria.shells.reaper.settings") as mock_settings:
            mock_settings.shells_reap_idle_days = 7
            mock_settings.shells_reap_protected_tag = "keep"
            candidates = await worker._candidates()
        assert len(candidates) == 1
        assert candidates[0]["shell"] is old_shell
        assert candidates[0]["coding_session"] is None

    @pytest.mark.asyncio
    async def test_protected_tag_excludes_a_shell(self):
        worker = ShellReaperWorker.__new__(ShellReaperWorker)
        tagged_shell = MagicMock(
            last_activity_at=datetime.now(timezone.utc) - timedelta(days=10),
            tags=["keep"],
        )
        worker.svc = MagicMock()
        worker.svc.list_shells = AsyncMock(return_value=[tagged_shell])
        worker.db = MagicMock()
        with patch("aria.shells.reaper.settings") as mock_settings:
            mock_settings.shells_reap_idle_days = 7
            mock_settings.shells_reap_protected_tag = "keep"
            candidates = await worker._candidates()
        assert candidates == []


class TestSafetyGate:
    """`is_active` is a @property on Killswitch — calling it raised
    `'bool' object is not callable`, which `_safety_ok`'s fail-closed except
    swallowed into "skip this tick". The reaper never reaped anything.
    """

    @pytest.mark.asyncio
    async def test_inactive_safety_lets_the_reaper_run(self):
        ks = Killswitch()
        estop = MagicMock(is_active=AsyncMock(return_value=False))
        worker = ShellReaperWorker.__new__(ShellReaperWorker)
        worker.db = MagicMock()
        with patch("aria.api.deps.get_killswitch", return_value=ks), patch(
            "aria.api.deps.resolve_estop_manager", new=AsyncMock(return_value=estop)
        ):
            assert await worker._safety_ok() is True

    @pytest.mark.asyncio
    async def test_engaged_killswitch_or_estop_skips_the_tick(self):
        estop = MagicMock(is_active=AsyncMock(return_value=False))
        worker = ShellReaperWorker.__new__(ShellReaperWorker)
        worker.db = MagicMock()

        ks = Killswitch()
        ks._active = True
        with patch("aria.api.deps.get_killswitch", return_value=ks), patch(
            "aria.api.deps.resolve_estop_manager", new=AsyncMock(return_value=estop)
        ):
            assert await worker._safety_ok() is False

        ks._active = False
        estop.is_active = AsyncMock(return_value=True)
        with patch("aria.api.deps.get_killswitch", return_value=ks), patch(
            "aria.api.deps.resolve_estop_manager", new=AsyncMock(return_value=estop)
        ):
            assert await worker._safety_ok() is False
