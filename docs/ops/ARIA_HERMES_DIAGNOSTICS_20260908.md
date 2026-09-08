# ARIA diagnostics exposed to Hermes — 2026-09-08

Current September8 handoff: [deployment/client state](CURRENT_DEPLOYMENT_20260908.md)
supersedes older model-status updates below. Hermes has gracefully reloaded and
reconnected to118ARIA tools; installed Pi/Hermes executor and compaction checks
pass. MTP stays ON at Ben's accepted risk; no more crash investigations or soaks.
Routine-start activation and one lifecycle check remain pending. Historical
failures and stopped experiments below are preserved, not current instructions.

Seven focused, read-only tools were added and verified through the installed
Hermes registry and the deployed, authenticated ARIA MCP launcher:

| Tool | Use |
| --- | --- |
| `operator_snapshot` | Concurrent readiness, exact-model route/admission, coding queue, retrieval and provider-availability checks; explicit partial failures. |
| `inference_backend` | Resolve the client's named model and inspect admission without generating tokens, starting a model or changing the default. |
| `inference_usage` | Bounded-window usage/cache totals, overall or by caller/model. |
| `inference_traces` | Content-free request timelines with context, queue delay, cache reuse, throughput and speculation metrics when available. |
| `benchmark_status` | Indexed benchmark run/metric inspection without raw logs, launch commands or benchmark execution. |
| `ralph_status` | Bounded run states, limits, usage and verification counts without approving or changing controller state. |
| `get_task` | Read one durable planning task before updating it; accepts the list's `id` alias. |

Existing fleet/shell controls, coding launch/results, projects/charters, alerts,
services/models, workflows and memory already had tools; no duplicate generic
API executor was added. The operator snapshot is a new bridge-level composition
of existing authenticated APIs, not a new listener or authentication boundary.

Descriptions now distinguish Corsair's RTX3090/Halo deployment from legitimate
R9700 devices on other hosts; explicit client routing from the global default;
shared serving capacity from dedicated slots; and disabled-search fallback from
hybrid retrieval. No search/embedding service was enabled.

New diagnostics use structured envelopes, bounded windows/IDs/results and
read-only MCP annotations. HTTP failures do not echo upstream bodies. Probe
failure is unknown, not healthy. A successful probe is not proof of idle state.
Source fingerprints now identify loaded code, including the concurrent
operations module, rather than reading a replacement file on every status call.

## Verification

- 62 scoped tests passed, latest run 2.07s; existing Pydantic deprecation only.
- Native Hermes Signal platform selection, tool_search and tool_describe passed
  for all seven additions; deferred full schemas remain enabled.
- Ten actual read-only calls passed through the deployed configured launcher,
  using the profile's resolved API credential. Includes three usage groupings.
- Contract `2026-09-08.2`; server SHA
  `6f88461228204c97fba9e3bd84cc3670d2474fc13f439a823e0ec28502cd74c2`;
  operations SHA `15eb2e205fce60ee8c1d57da1871bd0b9448184f558900fa24091422a2918854`.
- The separate rollout restarted Hermes; live gateway PID44731 reports
  118 registered/selected entries and no missing required tools. That is
  114 bridge tools plus four utilities, including 16 concurrent additions.
  This report does not claim all 114 were exercised or approve admin actions.
- No real Signal model turn was sent by this probe; `model_visible` was still
  null at observation. Actual discovery/dispatch and long-lived gateway
  registration are distinguished from model choice/tool use in a chat.

Redacted evidence: `/tmp/aria-hermes-exposure-20260908.4m70u1/deployed-registry.json`.
Failed initial parsing probes are retained alongside passing staged checks.
Verifier: `integrations/hermes/verify-aria-exposure.py`. Example invocation:

```sh
/Users/ben/Services/apps/hermes-agent/.venv/bin/python integrations/hermes/verify-aria-exposure.py \
  --expected-sha256 6f88461228204c97fba9e3bd84cc3670d2474fc13f439a823e0ec28502cd74c2 \
  --expected-operations-sha256 15eb2e205fce60ee8c1d57da1871bd0b9448184f558900fa24091422a2918854 \
  --model Qwen3.8-Flash-Next-CUDA-Halo-Candidate \
  --task-id 6aa0233942b6580c5f1742c1 --out /tmp/aria-diagnostics-fresh.json
```

Use a new output path. The verifier does not start inference or send messages.
Its fresh process resolves MCP credentials through Hermes secret scope; warning
messages about unpopulated global LLM/dashboard environment placeholders are
not inference authentication tests. No inference fallback occurs in this probe.

## Remaining deployment work

This diagnostic slice does not qualify the model or change Hermes/Pi defaults,
context limits or plugins. The model deployment task remains active. Its
two-hour mixed-workload soak started around15:28UTC after158/164base and
155/164HumanEval+ scores. Do not restart ARIA/model services or run competing
benchmarks until it ends. Shared rollout coordination is documented in
`ARIA_HERMES_EXPOSURE_HANDOFF_20260908.md`.

Ben's added goal is tracked by ARIA task`6aa0233942b6580c5f1742c1`. The thread
goal tracker permits only the already-active deployment goal, not a second one.
