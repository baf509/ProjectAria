"""Operator catalog retirement must hold before remote forwarding or force."""
from unittest.mock import AsyncMock
import pytest
from aria.infrastructure import model_servers as ms
from aria.api.routes.infrastructure import _LIST_VIEW_FIELDS


def test_current_host_choices():
    visible = {s.slug for s in ms.REGISTRY if s.catalog_visible}
    assert {s.slug for s in ms.REGISTRY if s.onbox and s.startable} == {
        'Qwen3.8-Flash-Next-CUDA-Halo-Candidate'}
    assert {s.slug for s in ms.REGISTRY if s.host_machine == 'machine:red' and s.catalog_visible} == {
        'Red-Qwen3.8-27B-MXFP4', 'Red-Qwen3.8-Flash-Next-MXFP4'}
    assert 'gemma-4-e4b-Q4' not in visible
    assert {'catalog_visible', 'host_machine'} <= _LIST_VIEW_FIELDS


@pytest.mark.asyncio
@pytest.mark.parametrize('slug', ['gemma-4-e4b-Q4', 'context1-Q4', 'DS4-0731-ROCmFPX-Affine-Quality'])
@pytest.mark.parametrize('force', [False, True])
async def test_retired_choice_cannot_reach_actuator(monkeypatch, slug, force):
    actuate = AsyncMock()
    monkeypatch.setattr(ms, '_corsair_forward_mode', lambda: True)
    monkeypatch.setattr(ms, '_corsair_actuate', actuate)
    with pytest.raises(ms.ModelServerSafetyError):
        await ms.ModelServerManager().start(slug, force=force)
    actuate.assert_not_awaited()
