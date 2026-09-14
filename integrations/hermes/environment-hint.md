## ARIA is the shell and coding control plane

This Mac runs Hermes and Project ARIA. Fleet machines connect through Tailscale.
Use ARIA's MCP tools as the authoritative path for shell discovery, creation,
input, removal, and delegated coding work. Never probe ARIA with bare curl or
raw HTTP: the API is authenticated. If a tool is not initially visible, use
tool search to load it by name. Report a missing MCP bridge rather than
bypassing API authentication.

- Use `operator_snapshot` for readiness/queues/retrieval. Pass the client model
  to `inference_backend`; the default route alone does not identify Hermes.
  `inference_traces`/`inference_usage` give cache and latency evidence,
  `benchmark_status` indexed results, `loop_status` controller progress.
  Missing data is unknown.
- Start fleet questions with `fleet_status`; use `list_nodes` when placement or
  connectivity matters. Treat semantic state (`working`, `blocked`, `done`,
  `idle`) separately from connectivity (`local`, `online`, `unreachable`).
- Use `create_shell` for a new interactive watched shell with its typed
  `profile` (`claude`, `codex`, `pi`, `shell`) and an explicit absolute Mac
  workspace. Agents run on this Mac; Corsair and Red are model targets.
  A request to work on a Corsair model does not place the agent on Corsair.
  Never register tmux by hand when `create_shell` can express the request.
- Use `create_coding_session` for a self-contained coding task, with the full
  task and absolute workspace. Honor an explicit backend/model/host request;
  otherwise let ARIA apply policy. It returns promptly, so report the session
  id. Preserve the accepted task until its requested outcome is verified.
  When monitoring is requested, use bounded observations and an actual
  registered follow-up job if the turn must end before the worker finishes.
- Use the shell identifier ARIA returns. Short aliases work only when unique;
  on ambiguity present the canonical matches rather than guessing.
- Monitor with `get_shell_screen`, `get_shell_events` and bounded
  `wait_for_shell_output`; timeout is not completion. For task prompts use
  `send_shell_input(literal=true, append_enter=true)` and inspect the returned
  canonical shell name, byte count and hash. This confirms terminal delivery,
  not worker acceptance: observe an acknowledgment or resulting action before
  claiming the worker started. Use named keys such as `C-c` with
  `literal=false, append_enter=false`; never send raw control bytes in JSON.
  `delete_shell(purge=false)` closes but keeps history.
- Red serves one model at a time. Read `red_model_status`; switch with
  `select_red_model` (`qwen-flash-next` or `qwen3.8-27b`), which wakes if
  needed and verifies readiness. Only `status=ready` is success; otherwise read
  status once and report it unresolved. Never fall back to terminal, SSH, WoL
  or polling, and never force-start past a refusal. `asleep` means unreachable,
  not powered off. Loading Red never changes Hermes's own model, and moves the
  default route only when Corsair is stopped: Corsair outranks Red.
  To interrupt work already running on Red, pass `force=true`: it asks the
  operator to confirm and proceeds only on accept.
- `host_temperatures`/`get_model_server` for hardware, `awareness_snapshot`
  for saved evidence, `list_memories`/`get_memory`/`store_memory`/
  `update_memory` for memory, `research_status`/`get_research_report` for
  existing research.

Local inspection and explanations are fine. Repository mutations must go
through ARIA. If ARIA is unavailable, say so and stop instead of silently
falling back to an invisible local coding loop.
