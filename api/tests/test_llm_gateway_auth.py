"""The coding-agent key is useful for inference and nothing else."""

from aria.main import _api_key_authorized, _is_llm_gateway_path


def test_gateway_path_matching_is_segment_bounded():
    assert _is_llm_gateway_path("/llm/v1/chat/completions")
    assert _is_llm_gateway_path("/llm/v1-identified/models")
    assert not _is_llm_gateway_path("/llm/v10/chat/completions")
    assert not _is_llm_gateway_path("/api/v1/todos")


def test_inference_key_cannot_access_control_plane(monkeypatch):
    monkeypatch.setattr("aria.main.settings.api_key", "admin-secret")
    monkeypatch.setattr("aria.main.settings.llm_gateway_api_key", "inference-secret")

    assert _api_key_authorized("/llm/v1/chat/completions", "inference-secret")
    assert _api_key_authorized("/llm/v1-identified/models", "inference-secret")
    assert not _api_key_authorized("/api/v1/todos", "inference-secret")
    assert _api_key_authorized("/api/v1/todos", "admin-secret")
