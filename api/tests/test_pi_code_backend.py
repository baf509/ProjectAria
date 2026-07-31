"""The pi-code backend must launch upstream Pi, not ARIA's orchestrator."""

from unittest.mock import patch

from aria.agents.backends.base import StartParams
from aria.agents.backends.pi_code import PiCodeBackend


def test_pi_code_starts_real_interactive_pi_with_explicit_provider_and_model():
    params = StartParams(
        workspace="/tmp/project",
        prompt="Fix the parser",
        provider="agentic",
        model="chadrockv2-qwen36-27b-fp6",
        append_system_prompt="Work incrementally.",
        session_id="aria-session-id",
    )
    with patch("aria.agents.backends.pi_code.settings.pi_coding_binary", "/usr/bin/pi"), \
         patch("aria.agents.backends.pi_code.settings.pi_coding_provider_agentic", "agentic"):
        cmd = PiCodeBackend().start_command(params)

    assert cmd.argv == [
        "/usr/bin/pi",
        "--provider", "agentic",
        "--model", "chadrockv2-qwen36-27b-fp6",
        "--session-id", "aria-session-id",
        "--append-system-prompt", "Work incrementally.",
        "Fix the parser",
    ]
    assert "-p" not in cmd.argv
    assert "--print" not in cmd.argv
    assert cmd.cwd == "/tmp/project"
    assert cmd.env["PI_OFFLINE"] == "1"


def test_pi_code_maps_aria_llamacpp_provider_to_pi_provider_name():
    params = StartParams(
        workspace="/tmp/project",
        prompt="Review this",
        provider="llamacpp",
        model="qwen35b-a3b-mtp",
    )
    with patch("aria.agents.backends.pi_code.settings.pi_coding_provider_llamacpp", "llama-cpp"):
        cmd = PiCodeBackend().start_command(params)
    assert cmd.argv[1:5] == ["--provider", "llama-cpp", "--model", "qwen35b-a3b-mtp"]


def test_pi_code_resume_uses_exact_aria_session_id_in_workspace():
    params = StartParams(workspace="/tmp/project", prompt="Continue fixing it")
    with patch("aria.agents.backends.pi_code.settings.pi_coding_binary", "pi"):
        cmd = PiCodeBackend().resume_command("aria-id-is-not-a-pi-id", params)
    assert cmd.argv[:3] == ["pi", "--session-id", "aria-id-is-not-a-pi-id"]
    assert cmd.cwd == "/tmp/project"
