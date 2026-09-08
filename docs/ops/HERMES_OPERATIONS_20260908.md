# Aria operations in Hermes — 2026-09-08

Implemented and registered in the active Mac Hermes profile. The deployed MCP
catalog contains **114 Aria tools**, plus four Hermes resource/prompt utilities.
Full schemas remain deferred behind Hermes's native tool search.

## New operations

| Area | Tools |
| --- | --- |
| Coding recovery and checks | `resume_coding_session`, `review_coding_session`, `get_coding_review`, `set_coding_deadline` |
| Durable Ralph inspection | `ralph_policy`, `get_ralph_run`, `get_ralph_logs` |
| Durable Ralph operations | `create_ralph_run`, `update_ralph_plan`, `approve_ralph_run`, `extend_ralph_limits`, `control_ralph_run` |
| Benchmarks | `benchmark_catalog`, `start_benchmark`, `get_benchmark_run`, `cancel_benchmark` |

These 16 operation tools live in `mcp/operations.py`, loaded by `mcp/server.py`.
They coexist with the separately added inspection tools (`operator_snapshot`,
inference diagnostics, `get_task`, `ralph_status`, `benchmark_status`).

Ralph uses its existing registered project policies, verifier IDs, limits and
version checks. Creating a draft does not approve or execute it. Admin operations
present the concrete operation and current plan through MCP consent, which Hermes
routes to its existing approval interface. Declined requests do not mutate state;
changes during approval are rejected. The admin credential is read from a private
service file after consent and is never a tool argument or model-visible value.

Coding resume continues from the workspace checkpoint. Review runs the workspace's
detected tests/lint; it is a mechanical review and does not authorize merging.

## Readiness and recovery

The Aria-owned `aria-readiness` Hermes plugin observes the actual MCP connection,
registry and platform tool selection. At model request time it separately records
whether the request contains direct Aria tools or all three native discovery tools.
An unobserved request schema stays unknown rather than being reported as present.

The healthy path does not call Aria over the network. A missing enabled connection
uses Hermes's existing reconnect/discovery implementation, with a background retry
no more than once per minute. Hermes retains ownership of agent tool refresh at
turn boundaries. The plugin never rewrites a cached system prompt or enables an
intentionally disabled server.

Gateway evidence: `/Users/ben/Services/data/hermes-home/aria-tools-ready.json`.
Per-process evidence: `aria-readiness/<pid>.json` under that same profile. Probe/CLI
processes cannot overwrite the gateway's report. Timestamps describe observations,
not a continuously polled health guarantee.

The active profile is `/Users/ben/Services/data/hermes-home`; the separate default
`~/.hermes` profile is not the Signal gateway. `/reload-mcp` remains the manual
recovery command for a chat with stale tool state.

## Mac benchmark harness

At the user's request, evalstack was copied from Corsair to
`/Users/ben/Development/benchmark-tooling/evalstack`, excluding its remote venv and
historical results. A separate Mac venv contains its core dependencies. GPU
inference stays on the registered remote machines and goes through Aria.

The active target configuration contains `red-radiance` and `flash-next`, using
current Aria model IDs. Historical Corsair targets are preserved in
`configs/targets.corsair-source-20260908.yaml`; they are not offered as active Mac
targets. There are no model lifecycle commands in the active benchmark catalog.
Performance and the built-in agents suite are available; optional suites advertise
missing runner dependencies and refuse to launch until those are installed.

Runs have a hard runtime limit (default 300 seconds; maximum 3600) enforced by an
independent supervisor. Cancellation and completion evidence survive API restarts.
Duplicate IDs and path traversal are refused. Cancellation preserves results and
does not stop pre-existing model endpoints. The bound-model conflict guard remains
active whenever the harness catalog contains model lifecycle commands.

Streaming measurements were corrected so every repetition contributes timings.
Token usage comes from the endpoint, never the number of SSE chunks. Prefill is
input tokens divided by observed TTFT, including network and queueing overhead;
decode is a client estimate after the first token. Unknown cache reuse remains
unknown. Metrics returned through Aria retain their measurement source.

Reproducible Mac configuration, dependency lock and streaming patch are under
`integrations/benchmarks/`.

## Validation

Final checks passed: 87 Aria/Hermes test cases, three harness streaming tests,
and three live model-routing scenarios.

- Real MCP schemas and stdio consent round trips against an isolated HTTP fixture,
  covering approval acceptance/decline and scoped credential headers.
- Coding operation routing, invalid IDs/deadlines, stale Ralph versions, bounded
  benchmark arguments, missing dependencies, process timeout/cancellation, and
  recovery of completion records after an API restart.
- Native tool-search scoring now follows the first operation actually invoked;
  searching or describing a tool alone cannot pass an execution test.
- Three live Radiance model-routing scenarios passed using Hermes's Signal tool
  definitions: fleet discovery, Ralph status, and benchmark options. The model used
  `tool_search`/`tool_describe`/`tool_call` correctly. These were isolated model
  probes, not messages sent to Signal.
- Live `start_benchmark` through Hermes's MCP dispatcher completed run
  `aria-1788881151`. At three repetitions per prompt size, headline client estimates
  were **207.12 decode tokens/s** at the shortest prompt and **5,276.12 prefill
  tokens/s** at the longest prompt. This was a short integration benchmark, not the
  earlier BetterBench workload.
- Live cancellation of run `aria-1788881645` succeeded through the MCP dispatcher
  and the independent supervisor recorded `cancelled`.

Hermes was restarted through its drain-aware SIGUSR1 path after checking for active
work and pending approvals. Aria was gracefully refreshed for the benchmark API.
No production Ralph runs were started, approved or modified by validation.

Deployment backups are in `/Users/ben/Services/backups/aria-hermes-tools-20260908`.
The bridge deployment now requires **both** `server.py` and `operations.py`;
`tool_contract_status` reports the loaded hash of each. Copying just `server.py`
is insufficient. Test evidence is summarized in `hermes-operations-20260908.json`.
