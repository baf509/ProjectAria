"""
ARIA - Tests for the Poolside `pool` coding-agent backend

Covers command construction (start/resume), the credential-injection guard
(don't clobber a real `pool login` with the EMPTY placeholder), process
matching, and the exit-code-4 "real result, not a crash" carve-out.
"""

from __future__ import annotations

from unittest.mock import patch

from aria.agents.backends.base import StartParams
from aria.agents.backends.pool import TASK_FAILURE_EXIT_CODE, PoolBackend


@patch("aria.agents.backends.pool.settings")
def test_start_command_basic(mock_settings):
    mock_settings.pool_binary = "/usr/local/bin/pool"
    mock_settings.pool_api_url = "http://127.0.0.1:8102"
    mock_settings.pool_api_key = "EMPTY"
    mock_settings.pool_model = "laguna-s21-rocmfp4-strixkvspine-v4"

    backend = PoolBackend()
    params = StartParams(workspace="/tmp/w", prompt="do the thing")
    cmd = backend.start_command(params)

    assert cmd.argv == [
        "/usr/local/bin/pool",
        "exec",
        "--api-url", "http://127.0.0.1:8102",
        "--output", "json",
        "--unsafe-auto-allow",
        "--directory", "/tmp/w",
        "--prompt", "do the thing",
    ]
    assert cmd.cwd == "/tmp/w"


@patch("aria.agents.backends.pool.settings")
def test_resume_command_uses_continue_with_session_id(mock_settings):
    mock_settings.pool_binary = "/usr/local/bin/pool"
    mock_settings.pool_api_url = "http://127.0.0.1:8102"
    mock_settings.pool_api_key = "EMPTY"
    mock_settings.pool_model = "laguna-s21-rocmfp4-strixkvspine-v4"

    backend = PoolBackend()
    params = StartParams(workspace="/tmp/w", prompt="keep going")
    cmd = backend.resume_command("pool-run-id-123", params)

    assert "--continue" in cmd.argv
    assert cmd.argv[cmd.argv.index("--continue") + 1] == "pool-run-id-123"
    assert "--prompt" in cmd.argv
    assert cmd.argv[cmd.argv.index("--prompt") + 1] == "keep going"


@patch("aria.agents.backends.pool.settings")
def test_env_does_not_inject_empty_placeholder(mock_settings):
    """POOLSIDE_API_KEY=EMPTY must NOT be set -- it overrides
    ~/.config/poolside/credentials.json in pool's own credential hierarchy,
    which would clobber a real `pool login` and break --unsafe-auto-allow."""
    mock_settings.pool_api_key = "EMPTY"
    mock_settings.pool_model = "laguna-s21-rocmfp4-strixkvspine-v4"

    backend = PoolBackend()
    env = backend._env(StartParams(workspace="/tmp/w", prompt="x"))

    assert "POOLSIDE_API_KEY" not in env
    assert env["ARIA_MANAGED"] == "1"
    assert env["POOLSIDE_STANDALONE_MODEL"] == "laguna-s21-rocmfp4-strixkvspine-v4"


@patch("aria.agents.backends.pool.settings")
def test_env_injects_real_api_key(mock_settings):
    mock_settings.pool_api_key = "sk-real-credential"
    mock_settings.pool_model = "laguna-s21-rocmfp4-strixkvspine-v4"

    backend = PoolBackend()
    env = backend._env(StartParams(workspace="/tmp/w", prompt="x"))

    assert env["POOLSIDE_API_KEY"] == "sk-real-credential"


def test_matches_process_narrow():
    backend = PoolBackend()
    assert backend.matches_process("pool exec --api-url http://127.0.0.1:8102") is True
    # Deliberately narrow: must not match unrelated things containing "pool".
    assert backend.matches_process("poolside-webui --serve") is False
    assert backend.matches_process("python connection_pool.py") is False


class TestExpectedFailureExitCode:
    def test_task_failure_code_is_expected(self):
        backend = PoolBackend()
        assert backend.is_expected_failure_exit_code(TASK_FAILURE_EXIT_CODE) is True
        assert TASK_FAILURE_EXIT_CODE == 4

    def test_other_nonzero_codes_are_not_expected(self):
        backend = PoolBackend()
        assert backend.is_expected_failure_exit_code(1) is False
        assert backend.is_expected_failure_exit_code(137) is False

    def test_zero_is_not_a_failure_code(self):
        # Not meaningful to call with 0 in practice (caller only checks on
        # nonzero exit), but the method shouldn't misreport it either.
        backend = PoolBackend()
        assert backend.is_expected_failure_exit_code(0) is False
