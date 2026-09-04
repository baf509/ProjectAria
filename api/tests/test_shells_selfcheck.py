"""Tests for aria.shells.selfcheck — specifically that a deliberately-disabled
backend is not reported as an incident."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aria.shells import selfcheck


class _EmptyCursor:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeCollection:
    def find(self, *args, **kwargs):
        return _EmptyCursor()


class _FakeDB:
    def __init__(self):
        self.shell_extraction_state = _FakeCollection()

    async def command(self, *_a, **_kw):
        return {"ok": 1}

    def __getattr__(self, _name):
        return _FakeCollection()


async def _run(pool_enabled: bool) -> list[str]:
    with patch.object(selfcheck.settings, "pool_enabled", pool_enabled), \
         patch.object(selfcheck, "_check_http", AsyncMock(return_value=(True, "HTTP 200"))), \
         patch.object(selfcheck, "_check_gtt", lambda: (True, "ok")):
        checks = await selfcheck.run_checks(_FakeDB())
    return [c["name"] for c in checks]


@pytest.mark.asyncio
async def test_chadrock_not_probed_when_pool_disabled():
    """Ben shut chadrock down on purpose (pool_enabled=false). Probing it
    anyway reported DEGRADED every tick, and each alert woke the Hermes
    alert-triage cron into spawning a diagnostic agent for a non-incident."""
    assert "chadrock" not in await _run(pool_enabled=False)


@pytest.mark.asyncio
async def test_chadrock_probed_when_pool_enabled():
    """The monitoring must come back the moment the server is legitimately
    in service again — this check exists because a chadrock crash used to
    page nobody at all."""
    assert "chadrock" in await _run(pool_enabled=True)


# ---------------------------------------------------------------------------
# GPU memory: a full card is FIT, not pressure
#
# Raised by Ben 2026-08-17, looking at a "Needs You" alert reading
# "gpu_memory (Strix Halo GTT: 82%; R9700 VRAM: 93%)" and asking what he was
# supposed to do with it. Nothing — that is both models resident and serving,
# i.e. the machine working. The check used to fail any pool over 90%, and
# Qwen3.8-27B-Radiance occupies 29 of the R9700's 31.9 GiB by design, so it
# paged permanently for the intended configuration.
# ---------------------------------------------------------------------------


def _pools(halo_pct=82, vram_pct=93, spilling=False):
    return [
        {
            "pool": "halo-gtt",
            "label": "Strix Halo GTT (card1)",
            "used_gib": 124.0 * halo_pct / 100,
            "total_gib": 124.0,
            "spilling": False,
        },
        {
            "pool": "r9700-vram",
            "label": "R9700 VRAM (card0)",
            "used_gib": 31.9 * vram_pct / 100,
            "total_gib": 31.9,
            "spilling": spilling,
        },
    ]


def test_a_card_full_of_its_own_model_is_not_an_alert():
    """93% VRAM is what "the model is loaded" looks like on a 32 GiB card."""
    with patch.object(selfcheck.sys, "platform", "linux"), patch.object(
        selfcheck.gpu_devices, "pool_snapshot", lambda: _pools()
    ):
        ok, detail = selfcheck._check_gtt()
    assert ok is True, f"paged on the designed state: {detail}"
    # The numbers still travel, as context rather than as an incident.
    assert "93%" in detail and "82%" in detail


def test_even_a_completely_full_pool_is_not_by_itself_an_alert():
    """Headroom is enforced by the start gate, which refuses a launch that will
    not fit — and does so while the operator is trying to do something."""
    with patch.object(selfcheck.sys, "platform", "linux"), patch.object(
        selfcheck.gpu_devices,
        "pool_snapshot",
        lambda: _pools(halo_pct=99, vram_pct=99),
    ):
        ok, _ = selfcheck._check_gtt()
    assert ok is True


def test_spilling_is_the_condition_that_pages():
    """A dGPU model consuming system RAM is the one documented coupling between
    the two pools — and unlike a full card, it is actionable."""
    with patch.object(selfcheck.sys, "platform", "linux"), patch.object(
        selfcheck.gpu_devices, "pool_snapshot", lambda: _pools(spilling=True)
    ):
        ok, detail = selfcheck._check_gtt()
    assert ok is False
    assert "SPILLING" in detail
    assert "competing with the Halo" in detail


def test_unreadable_pools_still_fail():
    with patch.object(selfcheck.sys, "platform", "linux"), patch.object(
        selfcheck.gpu_devices, "pool_snapshot", lambda: []
    ):
        ok, detail = selfcheck._check_gtt()
    assert ok is False
    assert "unreadable" in detail


def test_gpu_pool_probe_is_not_an_incident_on_darwin_control_plane():
    with patch.object(selfcheck.sys, "platform", "darwin"), patch.object(
        selfcheck.gpu_devices,
        "pool_snapshot",
        side_effect=AssertionError("Darwin must not read Linux DRM sysfs"),
    ):
        ok, detail = selfcheck._check_gtt()
    assert ok is True
    assert "not applicable" in detail


# ---------------------------------------------------------------------------
# Vault readability (2026-08-19)
#
# The livesync bridge reads the vault from a container as a different uid. One
# file it cannot read kills its `corsair-files` peer at startup with EACCES and
# stops disk->phone sync for the entire vault -- while the container stays up,
# so every container-level check reports healthy. It ran undetected for two
# days. This probe tests the CAUSE, so it fires before the peer next trips.
# ---------------------------------------------------------------------------

class TestVaultReadableProbe:
    def test_all_readable_passes(self, tmp_path):
        from aria.shells.selfcheck import _check_vault_readable

        (tmp_path / "Planning").mkdir()
        note = tmp_path / "Planning" / "STEWARD_PLAN.md"
        note.write_text("approval: pending\n")
        note.chmod(0o644)

        result = _check_vault_readable(str(tmp_path))
        assert result["ok"] is True
        assert result["name"] == "vault"

    def test_one_unreadable_file_fails_the_check(self, tmp_path):
        from aria.shells.selfcheck import _check_vault_readable

        (tmp_path / "Planning").mkdir()
        good = tmp_path / "Planning" / "CHARTER.md"
        good.write_text("autonomy: 1\n")
        good.chmod(0o644)
        bad = tmp_path / "Planning" / "STEWARD_PLAN.md"
        bad.write_text("approval: approved\n")
        bad.chmod(0o000)

        result = _check_vault_readable(str(tmp_path))
        assert result["ok"] is False
        assert "STEWARD_PLAN.md" in result["detail"]
        assert "WHOLE vault" in result["detail"]

    def test_owner_readable_0600_file_passes_after_account_unification(self, tmp_path):
        from aria.shells.selfcheck import _check_vault_readable

        note = tmp_path / "private-note.md"
        note.write_text("readable by its owner\n")
        note.chmod(0o600)

        assert _check_vault_readable(str(tmp_path))["ok"] is True

    def test_git_internals_are_not_offenders(self, tmp_path):
        """The vault's own backup repo is skipped by the bridge and has
        legitimately varied modes; flagging it would make the check cry wolf."""
        from aria.shells.selfcheck import _check_vault_readable

        objects = tmp_path / ".git" / "objects" / "ab"
        objects.mkdir(parents=True)
        blob = objects / "cdef"
        blob.write_text("x")
        blob.chmod(0o600)

        assert _check_vault_readable(str(tmp_path))["ok"] is True

    def test_missing_vault_is_skipped_not_failed(self, tmp_path):
        """A box with no vault must not page forever."""
        from aria.shells.selfcheck import _check_vault_readable

        result = _check_vault_readable(str(tmp_path / "nope"))
        assert result["ok"] is True
        assert "skipped" in result["detail"]


class TestBridgeIsAControlChannel:
    def test_the_bridge_pages_when_down(self):
        """It carries Ben's approval edits back to ARIA (D10). An on_demand
        classification means a stopped bridge never reaches the alert cron."""
        from aria.infrastructure import services

        spec = next(s for s in services.REGISTRY if s.slug == "obsidian-livesync-bridge")
        assert spec.expected_state == "always_up"
        assert spec.needs_review is False
        assert services.is_healthy("stopped", spec.expected_state) is False
