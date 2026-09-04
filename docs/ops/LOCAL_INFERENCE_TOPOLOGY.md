# Local inference topology

Current operations guide for ARIA's model data plane. Historical benchmark and
DeepSeek-era measurements remain in dated vault analysis; they are not startup
instructions.

Last reconciled: **2026-09-04**.

## Boundary and routing

The Mac is the control plane and gateway. Corsair serves the primary models.
Consumers use:

- `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1`
- `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified` for pinned/identified consumers including Pi, Hermes local roles, and the ARIA steward

Raw Corsair model endpoints are loopback-only and reach the Mac through managed
SSH forwards. Never publish them directly or configure clients to call them.

| Deployment | Device/pool | Raw listener | Current role |
|---|---|---|---|
| Qwen3.8 Radiance | R9700, 32 GiB discrete VRAM | `127.0.0.1:8080` | dual-resident rollback option |
| Qwen3.8 Flash Next UD-Q4_K_XL | Strix Halo, 124 GiB shared/GTT | `127.0.0.1:8120` | dual-resident long-context rollback option, 1 slot × 256K with MTP/ngram speculative decoding |
| Qwen3.8 Flash Next UD-Q4_K_XL hybrid | R9700 dense/KV/MTP + Strix Halo experts | `127.0.0.1:8121` | boot-default 256K model for ARIA, Hermes, and Pi; replaces both resident servers |
| Gemma 4 E4B Q4 | Mac native | `127.0.0.1:8104` | auxiliary workers and side tasks |

The normal loadout is hybrid Flash Next on `:8121`; it is enabled at boot. The
dual-resident Radiance plus Halo-only Flash Next profile is retained as an
operator-selectable rollback. The hybrid unit conflicts with both dual-resident
services. Switch between these profiles through the dashboard loadout
controls so ARIA sequences the unload, start, and readiness checks.

The hybrid short-context profile was measured on 2026-09-03 at 64K total context:
layout 10, one slot, `-b 4096 -ub 2048`, q8_0 K/V, and MTP depth 3. On a fixed
4.3K-token prompt, warmed prefill was about 1,049 tok/s and decode 65.8 tok/s;
at 63K context, prefill was 634 tok/s and decode 34.9 tok/s. The former
`-b 2048 -ub 512`, depth-2 baseline measured about 552/59.5 tok/s. An early
four-slot throughput experiment reached roughly 70 aggregate tok/s, but that
configuration is retired: upstream issue #28286 demonstrates cross-request
content contamination with Qwen4exp MTP and `-np > 1`. The launcher now refuses
MTP with more than one slot; concurrency queues through ARIA until upstream has
an isolated per-sequence implementation and we requalify it.

The production profile is 1 x 256K with layout 0: every routed expert lives on
the Halo, leaving the R9700 room for the dense trunk, shared MTP head and the
262,144-token q8_0 KV cache. Unified KV and idle-slot caching are forced
explicitly, with a 16 GiB host-RAM prompt cache sized for roughly two complete
q8 context prefixes.
Live qualification on 2026-09-03 reported `n_ctx=262144`, about 29.4 GiB R9700
VRAM used, 73.0 GiB Halo GTT used, and about 40.7 GiB MemAvailable after load.
An identical second gateway request reused 57 of 61 prompt tokens.

The pinned hybrid runtime already contains the merged upstream Qwen4exp
long-context fixes through llama.cpp #28040. A matched 512-token workload test
of chained `ngram-mod,draft-mtp` reduced median decode from 61.02 to 53.64
tok/s, so production remains plain depth-3 MTP. The launcher rejects multiple
slots while MTP is active because upstream #28286 reports cross-request state
contamination; ARIA queues concurrency instead.

The hybrid systemd unit uses `KillMode=mixed`: SIGTERM goes only to the launch
guard, which owns and reaps llama-server's process group and allows one
30-second graceful GPU/mmap unwind. systemd keeps a 60-second whole-cgroup
SIGKILL ceiling. A controlled restart on 2026-09-03 stopped in about five
seconds with one cleanup signal and no orphan, second interrupt, or timeout.

DeepSeek V4 weights/runtimes may remain on Corsair for rollback, testing, and
model engineering. They are retained-but-inactive and are not default gateway
targets.

## Registry and observed truth

ARIA owns desired state. The backend readiness identity and Corsair process/unit
state own observed state. A shared open port is not model identity.

Routine starts/stops go through ARIA's restricted actuator. An authorized coding
or model-engineering agent may directly start a registered deployment for a
repair or test, but ARIA must observe, identify, record, and reconcile it.

The Flash registry slug still contains `2x256K` as a compatibility identifier,
but the reconciled registry reports the live 1 × 256K geometry, runtime
`8148b062e`, and MTP/ngram speculative mode. Continue comparing desired state
with backend identity and observed process state.

## Hardware facts that must not be guessed

- DRM `card0` is the R9700 discrete GPU; `card1` is the Strix Halo iGPU.
- The R9700 VRAM and Halo GTT are separate capacity pools, but loading a large
  checkpoint can still pressure host-wide memory.
- Vulkan/ROCm device numbering depends on the runtime build. Verify placement
  after a runtime change; do not copy a `-dev` flag across runtimes.
- GPU-offloaded unified memory is visible in DRM/GTT accounting, not reliably in
  `docker stats` or only `free -h`.
- In this llama.cpp lineage, `-c` is the total context pool. Multiple slots
  divide that pool; they do not each receive another `-c` tokens.

## Verification

```bash
# Mac control plane and forwards
curl -fsS http://127.0.0.1:8200/api/v1/health
nc -z 127.0.0.1 8080
nc -z 127.0.0.1 8120
# Present as the boot-default hybrid forward.
nc -z 127.0.0.1 8121

# Corsair observed state
systemctl --user is-enabled qwen3.8-flash-next-hybrid.service
systemctl --user is-active qwen3.8-flash-next-hybrid.service
ss -ltn | rg '127.0.0.1:(8080|8120|8121)'
curl -fsS http://127.0.0.1:8121/v1/models
```

Use authenticated ARIA endpoints for the registry, model utilization, device
pools, and running infrastructure. Compare those results with the direct
readiness identity before declaring reconciliation complete.

## Pi policy

Pi has one provider (`aria`) and the Radiance, Halo-only Flash Next, and hybrid
Flash Next model entries. Hybrid is the default. All go through the identified
Mac gateway with an inference-only key. Fireworks, raw Corsair ports, and all
other models are forbidden in Pi configuration. The two physical managed Pi
installations are the Mac (`mac-agents`) and Corsair (`corsair-ai`); both were
live-tested against hybrid on 2026-09-03 and identify their gateway traffic as
`pi-coding-mac` and `pi-coding-corsair` respectively.

Both Flash Next entries advertise the native 262,144-token window and a
32,768-token generation budget. They declare reasoning support and map Pi's
off/low/medium/high controls into `chat_template_kwargs.enable_thinking` and
`reasoning_effort`. This mapping is load-bearing: the embedded model template
defaults to `xhigh` when those arguments are absent. Pi defaults to thinking
off for quick coding traffic, uses deterministic sampling, and auto-compacts at
about 95K tokens (`reserveTokens=167144`, with a 20K verbatim tail) so routine
sessions stay out of the measured deep-context latency band. ARIA's legacy
`pi-coding` and `pi-coding-ridge` database rows are compatibility launch
profiles, not additional Pi installations; startup reconciliation pins both to
the same hybrid model rather than maintaining a second source of routing truth.

## Gateway accounting

Every streaming and non-streaming completion through either gateway mount writes
a best-effort `db.usage` document with caller, resolved model, fresh input,
output, cache-read tokens, status and latency. Prompt and generated text are not
stored. Hermes and both managed Pi installations send explicit caller labels;
unidentified clients receive a bounded peer/user-agent fallback. Mongo logging
failure does not break inference.

Each request receives an opaque `X-Aria-Trace-ID`, forwarded to the backend and
returned to the caller. Its content-free trace joins routing, admission wait,
backend/first-chunk latency, context, cache reuse, prefill/decode throughput and
per-request MTP acceptance. `GET /api/v1/usage/traces` returns the recent bounded
projection used by the Usage dashboard. Clients may additionally send
`X-Aria-Conversation-ID` and `X-Aria-Session-ID`; these are correlation labels,
not authorization, and are character/length bounded before storage.

For chat requests the gateway fingerprints the actual forwarded system/tools
prefix plus reasoning-template controls. Only hashes, component byte counts,
tool count and a drift category are stored. The bounded in-process tracker
classifies `first_seen`, `stable`, timestamp-only, system, tools and reasoning
template changes; it never rewrites a prompt. A process restart intentionally
resets comparisons to `first_seen` rather than guessing from persisted text.

The gateway also owns admission for any backend whose registry geometry reports
exactly one slot. Hermes is interactive priority, Pi and other foreground
clients are normal priority, and callers explicitly labeled as evaluations,
benchmarks, maintenance, workers, or compaction are background priority. Every
30 seconds in queue promotes a request by one tier, so priority cannot starve
older work. Client cancellation removes a queued request; disconnecting a live
stream releases the slot. `/llm/v1/backend` exposes the active/queued counts,
and each usage row records priority, wait time, and queue depth at arrival.
ARIA's internal llama.cpp adapter labels its own stewardship, review, and
maintenance generations `aria-background`; otherwise the OpenAI SDK's generic
user-agent makes minute-long internal jobs indistinguishable from foreground
traffic.

This is the useful subset of NVIDIA PAIR's scheduling model for the current
fleet: one stable proxy endpoint, eligibility from observed model state, and
visible placement/wait telemetry. Cross-node least-loaded placement is deferred
until the same exact model exists on two independently eligible nodes; the
hybrid deployment is one process spanning both Corsair GPUs and cannot be
replicated onto the Mac by routing policy alone.

The Flash registry drift, duplicate Corsair Gemma listener, stale forward
mappings, Hermes/Pi gateway bypasses, and the steward's direct `:8080` route
were closed on 2026-08-29. The managed Mac forward job now carries only current
ports.
