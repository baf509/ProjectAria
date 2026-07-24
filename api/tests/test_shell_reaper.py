"""Tests for the idle-session reaper (COHERENCE_DESIGN.md C9)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.core.killswitch import Killswitch
from aria.shells.reaper import _save_done, SAVE_PROMPT, ShellReaperWorker


class TestSaveDoneDetection:
    TOKEN = "REAP_SAVED"

    def test_echoed_prompt_is_not_done(self):
        # The save prompt mentions the token mid-sentence and is echoed to the
        # pane — this must NOT count as the agent having finished.
        echoed = SAVE_PROMPT.format(token=self.TOKEN)
        assert _save_done(echoed, self.TOKEN) is False

    def test_agent_reply_on_its_own_line_is_done(self):
        screen = "…saved notes to vault/war-audio-game/Analysis/FOO.md\nREAP_SAVED\n$ "
        assert _save_done(screen, self.TOKEN) is True

    def test_token_with_surrounding_whitespace_is_done(self):
        assert _save_done("  REAP_SAVED  \n", self.TOKEN) is True

    def test_token_as_substring_of_word_is_not_done(self):
        assert _save_done("NOT_REAP_SAVED_YET is a variable", self.TOKEN) is False

    def test_empty_or_none_screen(self):
        assert _save_done(None, self.TOKEN) is False
        assert _save_done("", self.TOKEN) is False

    def test_prompt_then_reply_still_detects(self):
        # Both the echoed instruction AND the standalone reply are visible.
        screen = SAVE_PROMPT.format(token=self.TOKEN) + "\nREAP_SAVED\n"
        assert _save_done(screen, self.TOKEN) is True

    def test_prompt_contains_the_token_placeholder_filled(self):
        assert "REAP_SAVED" in SAVE_PROMPT.format(token="REAP_SAVED")


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
