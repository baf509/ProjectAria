## ARIA is the shell and coding control plane

This Mac runs Hermes and Project ARIA; fleet machines connect through Tailscale.
Use ARIA MCP for shell discovery, creation, input, removal and delegated coding.
The API requires authentication: never probe it with bare curl or raw HTTP.
Use tool search for deferred tools. Report a missing bridge instead of bypassing it.

- Use `operator_snapshot` for readiness, queues and retrieval. Pass the client
  model to `inference_backend`: the default route does not identify Hermes.
  `inference_traces`/`inference_usage` provide latency/cache evidence,
  `benchmark_status` indexed results, and `loop_status` controller progress.
  Missing data is unknown.
- Start fleet questions with `fleet_status`; use `list_nodes` for placement.
  Working/blocked/done/idle describe activity, not connectivity.
- Use `create_shell` for an interactive watched shell: choose `claude`, `codex`,
  `pi` or `shell` and an absolute Mac workspace. Coding runs on the Mac;
  Corsair and Red are inference targets. Do not register tmux by hand.
- Use `create_coding_session` for a self-contained task with its full scope and
  absolute workspace. Honor explicit backend/model/host choices, otherwise use
  ARIA policy. Report its returned ID; retain the task until verified complete.
  Monitoring needs bounded observations and a registered follow-up job if the
  turn ends before the worker finishes.
- Use returned shell identifiers; short aliases must be unique. Monitor via
  `get_shell_screen`, `get_shell_events` and bounded `wait_for_shell_output`.
  Timeout is not completion. Send prompts with `send_shell_input(literal=true,
  append_enter=true)`; verify canonical name, byte count and hash. Delivery is
  not acceptance: observe an acknowledgment or action. Named keys such as `C-c`
  use `literal=false, append_enter=false`; never embed control bytes in JSON.
  `delete_shell(purge=false)` retains history.
- Red serves one model at a time. Read `red_model_status`; use `select_red_model`
  to wake/load/switch. Default: `qwen3.8-27b-paro-int5`; alternatives:
  `qwen3.8-27b`, `qwen-flash-next`, `qwen-paro` (PARO MXFP4).
  Only `status=ready` confirms success. Otherwise read status once and report
  unresolved; never retry start or use terminal/SSH/WoL fallbacks. Asleep means
  unreachable, not powered off. Loading does not change Hermes's own model.
  `force=true` requests explicit operator consent to interrupt active work.
- Use `host_temperatures`/`get_model_server` for hardware, `awareness_snapshot`
  for saved evidence, memory tools for recall/storage, and
  `research_status`/`get_research_report` for existing research.

Local inspection and explanations are fine. Repository mutations go through
ARIA; if it is unavailable, report that and stop rather than running invisibly.
