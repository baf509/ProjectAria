# Local inference topology

Last reconciled: **2026-09-08**. See [current deployment and delivery status](CURRENT_DEPLOYMENT_20260908.md)
for the exact release, client checks, performance scope and verified activation.
Earlier R9700 benchmarks are historical evidence, not current launch instructions.

## Boundary and routing

The Mac owns ARIA, MongoDB, Hermes/Signal, credentials, managed shells and canonical
general projects. Corsair is the model/benchmark data plane. Consumers use the
authenticated Mac gateway:

- `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1`
- `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified` for Hermes and Pi

Raw model listeners remain loopback-only. Corsair's `:8131` is carried by the
restricted Mac SSH forward, not published as a public API. The raw endpoint also
requires its own backend credential; ARIA supplies it without forwarding client
credentials. An unauthenticated curl is expected to return 401, not prove failure.

| Host / deployment | Devices | Model endpoint / current role |
|---|---|---|
| Corsair Flash Next candidate | RTX 3090 FE CUDA + Strix Halo RADV Vulkan | Raw `127.0.0.1:8131`; explicit Hermes/Pi default |
| Red Qwen3.8-27B MXFP4 | Dual R9700 | Mac proxy `:8094`; distinct running alternative |
| Ridge | RTX 5090 (Ben-confirmed) | Mac proxy `:8092`; asleep at reconciliation, new-card readiness unverified |
| Mac Gemma | Mac native | Retained `:8104` configuration, intentionally stopped; not Hermes fallback |

No Corsair R9700 is installed. The old `:8080/:8120/:8121` Corsair loadouts
must not appear as live consumer options. Red's R9700 hardware is not retired.
Search/mongot remain disabled. DeepSeek assets may be retained for model
engineering; retention does not imply current serving eligibility.

## Current profile and lifecycle

`Qwen3.8-Flash-Next-CUDA-Halo-Candidate` uses the pinned author runtime plus the
Top-K argsort fallback, NVIDIA610.43.02, MTP Q4_K_M/depth3, q8_0 K/V, one
262144-token slot, batch2048/microbatch512, 32checkpoints and8GiB host cache.
`kv_unified=false`; CUDA capture OFF, independent graph reuse ON.
VRAM on the RTX3090 and Halo shared memory are separate pools. Never infer
placement from changing CUDA/Vulkan ordinal numbers; the launcher binds identities.

This exact profile is operator accepted, **not** a passed sustained-reliability
qualification. Known intermittent CUDA faults remain unresolved. No further soak
or crash investigation is scheduled. The old R9700's 1000+tok/s prefill is not
this deployment's measurement; current bounded samples are about443/57 at4K
and462/56 at8K prefill/decode tok/s.

Routine starts/stops use ARIA's restricted actuator. The active normal-start
wrapper verifies the exact accepted release hash and artifacts. Ben's API
activation and one normal ARIA stop/start are complete: the release-mode model
was ready at23:45:58UTC on September8. Backend auth and a bounded installed-Pi
tool-call check passed after restart. No boot-autostart, automatic fallback or global-route change
is implied by the explicit client default. Do not rerun historical upgrade,
stall-capture or experiment scripts.

Use authenticated ARIA `GET /infrastructure/model-servers/<slug>` and gateway
`GET /backend?model=<slug>` to inspect identity/geometry/admission. On Corsair,
`systemctl --user show flashnext-cuda-halo.service` supplies lifecycle evidence.
A listener, successful systemd start or token-speed sample alone is not readiness
or correctness qualification.

The forwarding launcher remains `scripts/macos/run-corsair-model-forwards`,
deployed under `/Users/ben/Services/apps/bin/`. Its launchd job is
`com.ben.devbox.corsair-forwards`. Only use an administrator-authorized restart
after checking admission; do not leave competing temporary forwards behind.

## Managed clients

Mac and Corsair Pi use the `aria` provider, the explicit candidate default and
the retained Red option. Both configs are authenticated/catalogue-checked at
256Kcontext/32Koutput with wired thinking controls, default thinking off and
compaction at 75% = 196608 (`reserveTokens=65536`,20K verbatim tail). Additional
unregistered machines are not covered by those checks. The idempotent helper
is `scripts/configure-pi-flashnext.py`; keep credentials private.

Hermes uses the same identified candidate with256K/32K limits,196608 compaction,
the installed `aria-flashnext` medium/2048 default plugin and sixteen
thinking-off auxiliary routes. Its actual gateway has gracefully reloaded and
reconnected to118ARIA tools. Installed executor/compaction fixtures passed;
a personal Signal inference conversation was not used as a test. Red overrides
are preserved; Gemma is not an auxiliary fallback.

The legacy `pi-coding`/`pi-coding-ridge` database rows are compatibility launch
profiles, not extra Pi installations. Reconciliation migrates only retired
Flash contracts and does not overwrite explicit Red/custom choices. Mainstream
client calls use the gateway; control-plane operations use authenticated ARIA MCP.

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
were closed on 2026-08-29. The Mac forward job carries current candidate `:8131` plus optional
STT `:8003`, Context-1 `:8081` and retained DeepSeek engineering `:8107`.
These optional forwards do not imply a running backend. Retired R9700 ports
`:8080/:8120/:8121/:8122` are removed.


## Red fleet integration (2026-09-07)

`machine:red` represents the dual-boot PC; `red-linux` is its Linux node identity,
not a second physical machine. The seed records the X870 Taichi Creator, Ryzen
7 9800X3D, 96 GB installed RAM, two R9700s, separate Linux/Windows disks and
Tailscale identities. The node projection preserves that inventory.

The Mac API polls Red's restricted SSH `status` every 15 seconds in a background
observer. `/opt/red-r9700/status.py` verifies both GPU UUIDs and returns DRM
memory/load/temperature readings and the active container's context/sequence
limits. A failed read never renews the heartbeat. Expired observations are hidden
from live telemetry, and observation never wakes Red. No general node command
executor or Aria database/API credential is installed on Red.

Operate includes both Red VRAM pools and vLLM utilization/cache telemetry.
The aggregate 943,581-token cache is distinct from the 262,144 per-request limit
and eight-sequence scheduler limit. GPU allocations include reserved cache and
are not a measurement of active KV occupancy or process-exclusive memory.

`pi-coding-red` is the Red coding/escalation profile, with provider `aria`,
16,384 output tokens and 262,144 context. Coding still executes on the Mac.
The profile is available to both the escalation ladder and steward red tier.
Start Red through Operate before using it when asleep (global gateway autostart
remains disabled). Existing Flash Next client choices remain registered.

Qualified Red is eligible for automatic routing while resident. The stale
Flash Next route pin is cleared to auto; the operator-accepted CUDA/Halo candidate
remains excluded from model-omitted auto-selection. Explicit model names still require their named backend.
Registry benchmark metadata now refers to the 21:05 EDT BetterBench refresh:
195.6 weighted decode, 5108.3 prefill at 47,056 input tokens. Full conditions and
raw results live in CorsairModelHost/red-r9700/results/2026-09-07/.

## Red Linux network and power

Red Linux currently connects only over Wi-Fi (`wlp10s0`, RTL8922AE). The
Ethernet adapters have no carrier. ARIA's wake relay targets Wi-Fi MAC
`f8:3d:c6:88:54:92`; an Ethernet Wake-on-LAN flag does not prove Wi-Fi wake.
The Wi-Fi adapter supports magic-packet wake; its WoWLAN and PCI wake settings
are enabled, with Netplan/udev persistence. Sleep refuses unless Wi-Fi is
connected and wake is armed. A registered ARIA sleep/start cycle on 2026-09-08
resumed the same Linux boot over Wi-Fi in 30.13 seconds, before the four-minute
RTC recovery alarm, and returned the model to `ready`. The alarm was cleared.
Corsair's system wake relay is active and enabled. No automatic idle suspend
policy is enabled. See `CorsairModelHost/red-r9700/README.md` for the persistent
Netplan, udev and sleep-helper configuration; reboot persistence has not yet
been tested by rebooting Red.

The Mac's Red/Ridge model proxies are loopback-only; their direct tailnet
publications are removed. ARIA model start still uses the separate authenticated
Corsair wake relay. Tailnet `:8099` is the registered War Audio artifact server,
not an inference proxy.
