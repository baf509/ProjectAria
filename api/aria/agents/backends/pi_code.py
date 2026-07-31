"""ARIA backend for the real upstream Pi coding-agent executable."""

from __future__ import annotations

from dataclasses import replace

from aria.agents.backends.base import CommandSpec, StartParams
from aria.agents.backends.claude_code import MANAGED_ENV
from aria.config import settings


class PiCodeBackend:
    """Launch Pi as an interactive coding TUI in ARIA's shell substrate.

    ARIA owns process supervision; Pi owns the agent loop, tools, context-file
    loading, and transcript. This is intentionally the same boundary as the
    Claude Code and Codex backends.
    """

    name = "pi-code"

    @staticmethod
    def _provider(provider: str | None) -> str | None:
        if not provider:
            return None
        setting_name = {
            "llamacpp": "pi_coding_provider_llamacpp",
            "agentic": "pi_coding_provider_agentic",
            "ridge": "pi_coding_provider_ridge",
        }.get(provider)
        if setting_name:
            return getattr(settings, setting_name) or provider
        return provider

    def _argv(self, params: StartParams) -> list[str]:
        argv = [settings.pi_coding_binary]
        provider = self._provider(params.provider)
        if provider:
            argv.extend(["--provider", provider])
        if params.model:
            argv.extend(["--model", params.model])
        if params.session_id:
            # Pi 0.83+ accepts an exact project session id, creating it on the
            # first launch and reopening it later. Reuse ARIA's UUID so the two
            # persistence layers have one stable identity.
            argv.extend(["--session-id", params.session_id])
        if params.append_system_prompt:
            argv.extend(["--append-system-prompt", params.append_system_prompt])
        # A positional initial message starts Pi in interactive mode. Do not use
        # -p/--print: the process must remain alive and drivable in its tmux
        # shell, exactly like a hand-run `pi` session.
        argv.append(params.prompt)
        return argv

    def start_command(self, params: StartParams) -> CommandSpec:
        return CommandSpec(
            argv=self._argv(params),
            cwd=params.workspace,
            env={**MANAGED_ENV, "PI_OFFLINE": "1"},
        )

    def resume_command(self, session_id: str, params: StartParams) -> CommandSpec:
        argv = self._argv(replace(params, session_id=session_id))
        return CommandSpec(
            argv=argv,
            cwd=params.workspace,
            env={**MANAGED_ENV, "PI_OFFLINE": "1"},
        )

    def matches_process(self, cmdline: str) -> bool:
        return "pi" in cmdline
