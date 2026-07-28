"""
ARIA - Poolside `pool` CLI Backend

Purpose: run Poolside's own coding agent against the locally hosted Laguna
model, rather than against Poolside's cloud.

WHY THIS BACKEND
----------------
laguna serves Laguna-S-2.1. `pool` is Poolside's agent for that model family
(its own docs launch it as `ollama launch pool --model laguna-xs.2`), so its
system prompt, tool-calling format and chat template are matched to these
weights in a way a generic harness is not.

STANDALONE MODE
---------------
`pool exec --api-url <openai-compatible>` bypasses Poolside's cloud. The model
is NOT a CLI flag -- it comes from POOLSIDE_STANDALONE_MODEL -- and
POOLSIDE_API_KEY must be set to something or pool falls back to
credentials.json and tries to authenticate.

Point `pool_api_url` at the slot-proxy port for the coding slot, not at :8095
directly, so requests carry the id_slot that keeps this agent's prefix pinned.

EXIT CODES (from `pool exec --help`)
  0  task completed
  4  task ran but pool could not complete it
  *  unexpected error
Only the third class is an infrastructure failure; 4 is a real result and must
not be reported as a crash.
"""

from __future__ import annotations

from aria.agents.backends.base import CommandSpec, StartParams
from aria.config import settings

# Same marker the other CLI backends use: tells rc-file wrappers that ARIA
# launched this process, so they don't route back into ARIA recursively.
MANAGED_ENV = {"ARIA_MANAGED": "1"}

# `pool exec` exit code for "ran fine, could not finish the task".
TASK_FAILURE_EXIT_CODE = 4


class PoolBackend:
    name = "pool"

    def _env(self, params: StartParams) -> dict[str, str]:
        env = dict(MANAGED_ENV)
        env["POOLSIDE_API_KEY"] = settings.pool_api_key
        model = params.model or settings.pool_model
        if model:
            env["POOLSIDE_STANDALONE_MODEL"] = model
        return env

    def _common(self) -> list[str]:
        return [
            settings.pool_binary,
            "exec",
            "--api-url", settings.pool_api_url,
            # NLJSON, one object per line -- parseable for incremental progress.
            "--output", "json",
            # Non-interactive: without this, pool blocks awaiting approval.
            "--unsafe-auto-allow",
        ]

    def start_command(self, params: StartParams) -> CommandSpec:
        argv = self._common() + [
            "--directory", params.workspace,
            "--prompt", params.prompt,
        ]
        return CommandSpec(argv=argv, cwd=params.workspace, env=self._env(params))

    def resume_command(self, session_id: str, params: StartParams) -> CommandSpec:
        # `--continue` takes pool's own Run ID. ARIA stores it when the start
        # command reports it, so this is pool's run id, not ARIA's session id.
        argv = self._common() + [
            "--directory", params.workspace,
            "--continue", session_id,
            "--prompt", params.prompt,
        ]
        return CommandSpec(argv=argv, cwd=params.workspace, env=self._env(params))

    def matches_process(self, cmdline: str) -> bool:
        # Deliberately narrow: a bare "pool" substring matches "poolside",
        # "connection_pool", and any path containing it.
        return "pool exec" in cmdline
