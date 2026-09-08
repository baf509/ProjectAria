# Hermes / Aria tool audit — 2026-09-08

Implementation followed this initial audit; see [deployment and validation](HERMES_OPERATIONS_20260908.md).

Aria already exposes most of the capabilities Hermes needs. The first priority
is ensuring the running Hermes gateway actually presents those capabilities to
each chat. A fresh runtime works; the reported Signal conversation does not
claim access. This audit did not change live configuration or restart the gateway.

## Verified observations

- The active Hermes service uses `/Users/ben/Services/data/hermes-home`, not
  the separate default `~/.hermes` profile. Its configured Aria MCP command is
  `/Users/ben/Services/apps/bin/run-aria-mcp`, with Aria enabled for Signal.
- Direct stdio initialization discovered **91 Aria tools in 2.47 seconds**.
  Read-only calls succeeded: `aria_health` 146 ms, `list_nodes` 48 ms, and
  `fleet_status` 223 ms. These are individual diagnostic samples, not a benchmark.
- The deployed bridge reports contract `2026-09-02.1`, SHA-256
  `a430192d0fbf787c4995d14466869b07021d89929cd8edbe3fa1b0c0c9581527`.
- A separate, freshly launched Hermes runtime using the active profile discovered
  **95 Aria entries**: 91 tools plus four resource/prompt utilities. Discovery
  took 3.73 seconds. Both `aria` and `mcp-aria` toolset names resolve correctly;
  the actual tool prefix is `mcp__aria__`.
- Building Signal's tool definitions in that fresh runtime included all 95
  entries in the underlying catalog. Native tool search was already `auto` and
  presented `tool_search`, `tool_describe`, and `tool_call` to the model. Full
  schemas are deferred. This check did not make an inference request.
- The pasted response came from a Signal session using
  `Red-Qwen3.8-27B-MXFP4`. That session recorded zero tool calls. The gateway
  process had been running since September 4; no live Aria MCP child process was
  visible outside the diagnostic probes.

These observations suggest missing or stale tool state in the running gateway
or conversation. They do **not** establish the original failure cause, nor prove
what tool definitions accompanied that particular model request. The model's
statement alone is not an inventory. Fresh-process validation is not validation
of the existing Signal session.

Hermes already provides `/reload-mcp` to reconnect servers and refresh cached
agents. Its implementation may ask for confirmation because refreshing tool
schemas invalidates the conversation's prompt cache. After reloading, verify an
actual read-only `fleet_status` call in that chat. A gateway restart is not the
first recovery step; follow the integration README's drain requirements if one
becomes necessary.

## Recommended work, in order

| Priority | Addition | Value and implementation scope |
| --- | --- | --- |
| 1 | Gateway tool readiness and recovery checks | Report enabled/discovered/selected/model-visible tool state separately. Detect a missing required Aria catalog, retain useful diagnostics, and exercise the existing reconnect/reload path. Keep schema refreshes at startup or explicit conversation refresh boundaries. Add this incident to the real model routing evaluations. |
| 2 | Coding recovery, review, and deadlines | Expose `resume_coding_session`, `review_coding_session`, `get_coding_review`, and `set_coding_deadline`. Existing API endpoints already implement these. They support “recover the interrupted job,” “run its checks,” and “stop after this time.” The review endpoint runs mechanical checks; it should not be described as a full independent semantic review. |
| 3 | Durable Ralph run access | Expose list/status/logs first, then typed run controls and the existing plan/approval flow. The deployed `/ralph/policy` is enabled and `/ralph/runs` responds successfully. These durable runs are distinct from the older, already exposed `set_coding_loop` prompt-nudging feature. Preserve the API's admin boundary for mutations. |
| 4 | Model performance and regression checks | Expose benchmark result inspection and bounded registered runs after restoring the backend. Live `/benchmarks/health` reports `available: false`; suites and targets return 503 because evalstack is unavailable at its configured path. Remote execution should remain on the model/benchmark data plane, with Aria coordinating it. Verify Radiance target support before promising a Qwen quick-test tool. |

Potential later work: an inference preparation plan that summarizes model
availability, wake/start actions, readiness, and effects on other running models
using the existing fleet and model APIs. This is a convenience composition;
model start/stop, GPU inspection, and machine sleep are already exposed.

## Avoid unnecessary additions

- Fleet status, shell discovery and input, coding launch/output/diff/stop,
  project tasks, memory search, model control, workflows, and alerts already
  have MCP tools.
- Tool search already exists and is configured. Verify its use by the deployed
  Qwen model instead of adding a second generic discovery/execution layer.
- Hermes already has cron functionality; add Aria schedule tools only where
  ownership of a durable Aria workflow requires them.
- Aria's coding watchdog already emits stall, deadline, and budget notifications.
  Reuse the existing alert relay rather than building another notification loop.
- Aria's 91 full tool schemas serialize to 70,738 characters. Preserve deferred
  schemas and concise results as the catalog grows; this is not a measured token
  count or the size of the current model-facing tool payload.
- Refresh stale bridge descriptions: model-control docstrings still cite the
  old Red Qwen3.6 / RTX 5090 deployment. Current fleet data should determine
  model identifiers and hardware.

## Evidence and source locations

- Bridge: `mcp/server.py`; deployed `/Users/ben/Services/apps/aria-mcp/server.py`.
- Aria API: `api/aria/api/routes/coding_sessions.py`, `ralph.py`, `benchmarks.py`.
- Existing integration and evaluation corpus: `integrations/hermes/`.
- Installed Hermes: `model_tools.py`, `tools/tool_search.py`, `tools/mcp_tool.py`,
  `hermes_cli/tools_config.py`, `gateway/run.py`, `gateway/slash_commands.py`
  under `/Users/ben/Services/apps/hermes-agent`.
- Redacted diagnostic evidence: `hermes-tool-audit-20260908.json` beside this file.
- Temporary probe scripts and fuller results: `/tmp/aria-hermes-tools-20260908/`.
- Hermes documentation: [MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)
  and [tool search](https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-search).

All production checks in this audit were read-only. No model servers or coding
sessions were launched, and no Signal messages were sent.
