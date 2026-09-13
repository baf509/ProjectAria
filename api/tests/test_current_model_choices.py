"""Operator catalog retirement must hold before remote forwarding or force."""
from unittest.mock import AsyncMock
import pytest
from aria.infrastructure import model_servers as ms
from aria.api.routes.infrastructure import _LIST_VIEW_FIELDS


def test_current_host_choices():
    visible = {s.slug for s in ms.REGISTRY if s.catalog_visible}
    # The auxiliary model joined 2026-09-10: Qwen3.5-9B Q8_0, CPU-only on
    # Corsair, for ARIA's background extraction/heartbeat calls. Startable and
    # onbox like the candidate, but CPU-only and outside automatic routing, so
    # it contends with nothing — measured at zero effect on the candidate.
    # NInfer-3090 joined 2026-09-13: Qwen3.8-27B on the RTX 3090, explicit
    # selection only and exclusive with the candidate (see test_ninfer_3090_registry).
    assert {s.slug for s in ms.REGISTRY if s.onbox and s.startable} == {
        'Qwen3.8-Flash-Next-CUDA-Halo-Candidate', 'Qwen3.5-9B-Aux-CPU', 'NInfer-3090-Qwen3.8-27B'}
    aux = ms._BY_SLUG['Qwen3.5-9B-Aux-CPU']
    # The properties that keep it from ever competing with the coding slot.
    assert aux.memory_pool == ms.POOL_HOST and not aux.gtt_resident
    assert aux.exclusive_with == ()
    assert not aux.auto_route, 'auxiliary model must be addressed by name, never auto-selected'
    # PARO joined 2026-09-10: weights downloaded and verified on Red, registered
    # so the checkpoint is a known option rather than an unrecorded directory.
    # Visible, but not startable — Red has no paroquant verb, unit or serve
    # script (see test_paro_is_a_known_red_option_that_cannot_silently_serve).
    assert {s.slug for s in ms.REGISTRY if s.host_machine == 'machine:red' and s.catalog_visible} == {
        'Red-Qwen3.8-27B-MXFP4', 'Red-Qwen3.8-Flash-Next-MXFP4', 'Red-Qwen3.8-27B-PARO-MXFP4'}
    assert {s.slug for s in ms.REGISTRY
            if s.host_machine == 'machine:red' and s.startable} == {
        'Red-Qwen3.8-27B-MXFP4', 'Red-Qwen3.8-Flash-Next-MXFP4'}
    assert 'gemma-4-e4b-Q4' not in visible
    assert {'catalog_visible', 'host_machine'} <= _LIST_VIEW_FIELDS


@pytest.mark.asyncio
@pytest.mark.parametrize('slug', ['gemma-4-e4b-Q4', 'Ling-3.0-flash-Q6_K'])
@pytest.mark.parametrize('force', [False, True])
async def test_retired_choice_cannot_reach_actuator(monkeypatch, slug, force):
    actuate = AsyncMock()
    monkeypatch.setattr(ms, '_corsair_forward_mode', lambda: True)
    monkeypatch.setattr(ms, '_corsair_actuate', actuate)
    with pytest.raises(ms.ModelServerSafetyError):
        await ms.ModelServerManager().start(slug, force=force)
    actuate.assert_not_awaited()
