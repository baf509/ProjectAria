from aria.infrastructure import model_servers as ms
from aria.infrastructure.red_observer import MODELS


def test_promoted_identity_and_restricted_controls():
    slug = 'Red-Qwen3.8-27B-PARO-INT5'
    spec = ms._BY_SLUG[slug]
    assert spec.startable and spec.catalog_visible and spec.auto_route
    assert not spec.allow_force_start
    assert spec.port == 8094 and spec.remote_model_id == 'red-qwen3.8-27b-paro-int5-isolated'
    assert spec.remote_start_command[-1] == 'start-paro-int5'
    assert spec.remote_stop_command[-1] == 'stop-paro-int5'
    assert spec.remote_model_id == MODELS[slug]
    assert not ms._BY_SLUG['Red-Qwen3.8-27B-MXFP4'].auto_route
    for old in ('Red-Qwen3.8-27B-MXFP4', 'Red-Qwen3.8-Flash-Next-MXFP4', 'Red-Qwen3.8-27B-PARO-MXFP4'):
        assert old in spec.exclusive_with and slug in ms._BY_SLUG[old].exclusive_with
