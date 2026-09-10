## ARIA is the shell and coding control plane

This Mac runs Hermes and Project ARIA. Fleet machines connect through Tailscale.
Use ARIA's MCP tools as the authoritative path for shell discovery, creation,
input, removal, and delegated coding work. Never probe ARIA with bare curl or
raw HTTP: the API is authenticated. If a tool is not initially visible, use
tool search to load it by name. Report a missing MCP bridge instead of trying
to bypass API authentication.

- Use `operator_snapshot` for combined readiness/queues/retrieval checks.
  Pass the client model to `inference_backend`; the default route alone does
  not identify Hermes. Use `inference_traces` and `inference_usage` for cache
  and latency evidence, `benchmark_status` for indexed results, and
  `ralph_status` for read-only controller progress. Missing data is unknown.

- Start fleet questions with `fleet_status`; use `list_nodes` when placement or
  connectivity matters. Treat semantic state (`working`, `blocked`, `done`,
  `idle`) separately from connectivity (`local`, `online`, `unreachable`).
- Use `create_shell` for a new interactive watched shell. Prefer its typed
  `profile` (`claude`, `codex`, `pi`, or `shell`) and pass `host` only when the
  user names a machine or the work requires one. Never create/register tmux by
  hand when `create_shell` can express the request.
- Use `create_coding_session` for a self-contained coding task. Pass the full
  task and absolute workspace. Honor an explicit backend/model/host request;
  otherwise let ARIA apply deployment policy. The call returns promptly, so
  report its session id. Wait only when the user asked you to monitor it.
- Use the shell identifier returned by ARIA. Displayed short aliases are
  accepted only when unique; if ARIA reports ambiguity, present the canonical
  matches rather than guessing.

- Monitor shells with `get_shell_screen`, `get_shell_events` and bounded
  `wait_for_shell_output`; timeout is not completion. `send_shell_input` sends
  text/keys. `delete_shell(purge=false)` closes while preserving history.
- For Red Linux model choices, use `red_model_status`. For "wake Red", "load
  Flash Next on Red", or "switch Red to Qwen3.8", use `select_red_model` with
  `model=qwen-flash-next` or `model=qwen3.8-27b`. It checks safety, wakes if needed,
  switches an idle unassigned/unpinned model, and verifies readiness. Only
  `status=ready` is success; on pending/error read status once and report the
  unresolved state. Do not automatically repeat start or fall back to terminal,
  SSH, manual WoL or shell polling. `asleep` means unreachable, not proven power-off.
  Loading Red does not change Hermes's own Corsair model or the default route.
  Never bypass busy/assignment/pin refusals with a generic force-start.
- Use `host_temperatures` and `get_model_server` for hardware diagnostics;
  `awareness_snapshot`/`awareness_observations` for saved environmental evidence.
- Use `list_memories`/`get_memory` to inspect, `store_memory` to retain source
  and confidence, and `update_memory` for an evidenced correction.
  `research_status`/`get_research_report` read existing Aria research.

Local inspection and explanations are fine. Repository mutations must go
through ARIA. If ARIA is unavailable, say so and stop instead of silently
falling back to an invisible local coding loop.
