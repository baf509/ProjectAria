# Documentation, source and deployment consistency review — September 8, 2026

The project is **partially consistent**. The current model/client deployment
notes largely match the running system, but production has only some of the
reviewed source changes. Several operational entry points still describe the
retired hardware, and health/identity checks can give misleading answers.

Review scope: canonical checkout at `52c814f`, the Mac service tree, installed
launchers/MCP/Hermes/Pi, authenticated ARIA GET endpoints, Mac launchd and
Tailscale Serve state, the Mongo Lima guest, and read-only Corsair SSH probes.
Observations were made September 8 EDT / September 9 UTC, including live API
responses at approximately 02:04–02:10 UTC. The checkout was initially clean.
No services, configuration, routing, credentials, or model lifecycle were changed.
This report is the only repository change.

Priority: P1 = address before relying on the affected operational surface;
P2 = material consistency/correctness issue. Findings describe verified behavior,
with limits stated where an action was intentionally not exercised.

Follow-up corrections and deployment status are tracked in
[CONSISTENCY_FIXES_20260908.md](CONSISTENCY_FIXES_20260908.md). Findings below
describe the original read-only snapshot.

## Findings

### 1. P1 — Production still offers retired Corsair loadouts

The current README and topology runbook retire Corsair's R9700 loadouts. Source
`api/aria/infrastructure/model_servers.py` explicitly sets `startable=False`,
`allow_force_start=False`, and `auto_route=False` for all three former choices:

- `Qwen3.8-Flash-Next-Hybrid-R9700-Halo`
- `Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K`
- `Qwen3.8-27B-R9700-Radiance`

The live `GET /api/v1/infrastructure/model-servers` returns **true for all three
flags on all three entries**. The Mac deployed registry lacks the retirement
changes. It also still describes Ridge as RTX 3090; canonical source records
the user-confirmed RTX 5090 replacement.

The live UI reports build `6aa61e0`, built `2026-09-08T12:45:46.825Z`.
Its served `/operate` JavaScript contains “Load Qwen dual resident”, “Load Flash
Next hybrid”, and the retired R9700 hybrid slug. The corresponding source
`ui/src/features/operate/Spine.tsx` instead offers the CUDA/Halo candidate.
This was checked against the actual served bundle, not inferred from its SHA.

**Impact:** operators are offered starts for hardware no longer installed. The
UI's loadout operations address obsolete services. Whether a particular start
would be refused downstream was not tested: no start/stop requests were sent.

**Action:** deploy the reviewed retirement guards and matching UI together,
including verification of the Corsair actuator's effective registry. Verify live
flags and served controls afterward. The source's pre-eviction autostart guard
is also absent in production, although production currently has
`LLM_PROXY_AUTOSTART=false`, limiting that path's immediate exposure.

### 2. P1 — Red's model API is published outside the ARIA gateway

The charter requires model traffic through ARIA. `LOCAL_INFERENCE_TOPOLOGY.md`
describes authenticated gateway consumption and loopback model endpoints.
However, Mac Tailscale Serve publishes TCP `:8094` directly to loopback `:8094`.
The installed `run-red-proxy` is a plain SSH forward from that listener to
Red's `127.0.0.1:8081`.

From Corsair, without credentials:

| GET request | HTTP status |
|---|---:|
| Mac tailnet `:8094/v1/models` | 200 |
| Mac tailnet `:8200/llm/v1/models` | 401 |
| Corsair loopback `:8131/v1/models` | 401 |

**Impact:** a reachable tailnet path exposes Red's backend catalogue without
ARIA authentication or gateway routing/accounting. This is private-tailnet
exposure, not evidence of public internet exposure. Completion authorization
was not tested, and tailnet ACL reachability beyond Corsair was not assessed.

Serve also publishes `:8092` and `:8099`; these additional surfaces need an
explicit documented purpose. Ridge was not queried through its wake-on-demand
proxy. The service behind `:8099` was not characterized.

**Action:** reconcile these publications with the gateway invariant. Remove
unneeded direct publications through the authorized network-change process, or
document and enforce an explicitly approved exception. No network changes were
made during this review.

### 3. P2 — The deployed identified-backend probe ignores the requested model

The same authenticated query produces different answers on the two mounts:

| GET request, with `?model=Qwen3.8-Flash-Next-CUDA-Halo-Candidate` | Result |
|---|---|
| `/llm/v1/backend` | Candidate; one-slot admission controlled |
| `/llm/v1-identified/backend` | Red; `requested_model=null`; admission uncontrolled |

The deployed `current_backend_identified()` does not accept or pass `model`.
Canonical `api/aria/api/routes/llm_proxy.py:1292` already fixes this.

**Impact:** a diagnostic aimed at Hermes/Pi's explicit candidate reports the
model-omitted default and the wrong admission state. This does **not** establish
that explicitly named completion requests are misrouted; the defect is in the
inspection endpoint.

**Action:** deploy the existing fix and verify identical model-specific answers
through both gateway mounts.

### 4. P2 — Intentional service shutdowns still produce health alarms

`RETRIEVAL_CAPABILITIES.md` records the requested embeddings/TTS shutdown.
Live capability state confirms embeddings and search disabled, retrieval mode
`fallback`; launchd confirms embeddings/TTS stopped, and both disable markers
exist. The Mongo guest has `devbox-mongod` healthy and `devbox-mongot` exited.

Nevertheless:

- `GET /api/v1/health` returns `status=degraded`, `embeddings=unreachable`.
  Both source and deployed `api/aria/api/routes/health.py:84` unconditionally
  probe embeddings; the overall result requires embeddings to be connected.
- `GET /api/v1/infrastructure/services/shared-tts` returns
  `expected_state=always_up`, `healthy=false`. Both copies of
  `api/aria/infrastructure/services.py:247` retain that expectation despite
  the deliberate disable marker.

**Impact:** monitoring reports an intentional operating state as a failure,
contradicting the documented nonincident semantics and encouraging unnecessary
repair/start attempts. API startup readiness itself correctly reports ready.

**Action:** make the general health endpoint honor persisted capabilities and
represent deliberately disabled TTS explicitly in service health. Preserve the
requested shutdown; restarting dependencies is not the correction.

### 5. P2 — Current agent instructions and backlog contradict the current charter

`CLAUDE.md:80` calls the retired R9700 hybrid the boot default, describes Gemma
as the auxiliary worker, and assigns the old DRM identities. Its Pi invariant
at line 114 requires retired choices and omits the current CUDA/Halo candidate.
Its read-first links point at superseded vault notes; its verification commands
probe obsolete services/ports.

`docs/ops/WEB_UI.md:64` still documents the old pair of loadout buttons and
R9700 boot default. This happens to describe the stale production UI but
contradicts the current hardware and canonical UI source.

`BACKLOG.md` says it contains only current open work, yet instructs further
R9700/Halo qualification and preservation of `:8121` as control. The current
deployment note explicitly records the different accepted runtime and the end
of further crash investigation/soaks.

**Impact:** an agent following the repository's own operational instructions
can undo the intended client configuration or pursue obsolete repair/testing.
The earlier documentation reconciliation did not cover these entry points.

**Action:** reconcile the guide, current backlog and UI runbook with the charter
and actual rollout status. Preserve old measurements as clearly historical
records, and distinguish source-ready changes from deployed behavior.

### 6. P2 — The advertised boot verifier can succeed when its checks fail

README describes `scripts/aria-boot-check` as verifying process state, the
deployed MCP contract and node heartbeats. At lines 127–153, overall `ok` is
based only on API readiness and the optional shell canary. Launchd, MCP and
node lookup results do not affect success.

An isolated in-process simulation returned **exit 0 / `ok=true`** with all
launchd checks false, MCP file check false and node request HTTP 503. No live
dependencies or shells were touched by this simulation.

Additionally, the MCP check only hashes an existing file; it does not compare
an expected contract or perform discovery. Node checking accepts HTTP 200
without validating heartbeat freshness or required nodes.

**Impact:** a green boot result does not establish the readiness promised in
the documentation.

**Action:** aggregate required checks into the exit status, validate the stated
MCP/heartbeat contract, and add failure-path tests. Existing tests cover waiting
and stable hashing, not the overall failure result.

### 7. P2 — A production node agent runs from the development checkout

README and the agent guide describe source and production as separate. This is
true for API/UI, but the installed launcher
`/Users/ben/Services/apps/bin/run-aria-node` sets its project to
`/Users/ben/Development/Infrastructure/ProjectAria`, changes into `api/`, and
executes `python -m aria.node` there.

The running launchd node process, PID 335, has that source API directory as its
actual working directory. Its logical node ID is `mac-agents`; the live node
registry advertises coding and command execution capabilities.

**Impact:** node restarts load development checkout code outside the documented
service-tree deployment boundary. This is not a claim of hot reload: already
imported Python modules need not change immediately after a source edit.

**Action:** either deploy this node component into a versioned service tree or
explicitly document and control this source-backed production exception.

## Confirmed agreement

| Area | Observed evidence |
|---|---|
| Mac control plane | API/UI/Hermes/Signal launchd jobs running as `ben`; API ready and Mongo connected |
| UI publication | Corsair reaches Mac TCP UI and HTTPS UI (308 redirects); API readiness returns 200 |
| Corsair hardware/runtime | RTX 3090, 24 GiB, driver 610.43.02; candidate service active, PID 2151525; raw listener loopback `:8131` |
| Accepted image | Running image matches `sha256:56e11bba0ea44f6ee57f1832492a5dbe11a4d1bcc4325840ba145c4432011d1c` |
| Candidate readiness | Authenticated backend health OK; model identity matches; `/props` reports 262144 context and one slot |
| Lifecycle policy | Candidate systemd unit static with `Restart=no`; container restart policy `no`; live registry points to `serve-release.sh` |
| Automatic route | No pin; model-omitted route selects Red; candidate has `auto_route=false`, as documented |
| Pi | Mac and Corsair select provider `aria`, candidate default, Red alternative, identified gateway, thinking off; 167144 reserve and 20000 recent tokens |
| Hermes engine | Clean upstream `main` at `c3ce41645cb08f39e7dd5739dcfd72527096896f`; home/application symlinks match runbook |
| Hermes integration | Installed launchers and separate `aria-mcp` server/operations files byte-match source; active gateway receipt PID 1560 reports 118 registered/selected tools and none required missing |
| Hermes model controls | Candidate context 262144/output 32768; explicit 95000 compression cap; scoped reasoning plugin; sixteen candidate auxiliary routes with thinking off; empty fallback lists |
| Intentional shutdowns | Embeddings, TTS, Gemma stopped with markers; search disabled and mongot container exited |
| Nodes | Mac, Corsair, `mac-agents` and Red observations online with recent heartbeat timestamps |
| Vault projections | Desktop/service charter and current architecture/orientation notes byte-match; charter hash matches the prior reconciliation receipt |

The Lima job named `lima-mongot` being active is not evidence that mongot search
is enabled: the job supervises the VM, while the mongot container is stopped.
Historical unit files under `systemd/` are explicitly labeled historical and
were not counted as deployment drift simply because they retain old paths.

## Deployment traceability and remaining documentation gaps

A byte comparison of tracked source files found 683 identical, 31 different and
49 absent in the Mac ProjectAria service tree. These counts include tests/docs,
so absence alone is not an operational defect. In particular, MCP and Hermes
integrations intentionally deploy elsewhere and were checked at their actual
installed locations. The consequential API/UI differences are listed above.

The forwarding launcher byte-matches source but still forwards retired/unused
ports `8080`, `8081`, `8107`, `8120`, `8121`, `8122` alongside current `8131` and
STT `8003`. The topology runbook's claim that it carries “only current ports”
needs qualification. A listening forward does not establish a healthy backend;
Corsair had no listener on the checked old `8080/8120/8121` ports or `8003`.

The README's “Live services” table groups embeddings/TTS with running Mongo
without identifying their stopped state. The retrieval runbook is more accurate.
Mongo's registry port remains 27017 while the Mac's actual tunnel is 27018;
documentation/metadata should distinguish guest service ports from host access.

`make ui-deploy` deliberately fails, and `WEB_UI.md` describes layout and checks
without supplying a complete atomic deployment/recovery procedure. BACKLOG
already records this gap. The partial rollout demonstrates why a manifest and
repeatable API/UI/node deployment procedure would be valuable.

Root `docker-compose.yml` and `scripts/setup.sh` retain Linux-era setup guidance
without the strong historical header used in `systemd/README.md`. They should
be labeled/scoped so they are not mistaken for the Mac production installer.

## Validation limits and proposed order

This was an operational consistency review, not a complete code/security audit
or reliability qualification. No model generations, Signal messages, shell
canaries, restarts, wakes, deployments or network changes were performed. No
new soak was run. Ridge hardware/readiness was not independently inspected;
the RTX 5090 remains user-confirmed inventory. Red was observed through ARIA
and its already-running proxy. NAS CouchDB replication and backup restoration
were not tested. Remote Git publication was not reverified. The active Hermes
tool count comes from its process-specific receipt rather than a new chat.

The full test suite was not run: pytest was unavailable in both the default
Python and the inspected production API environment. No dependencies were
installed into production. Evidence consists of source/deployed comparisons,
read-only runtime probes and the isolated standard-library boot-check simulation.

Recommended order: reconcile model publication boundaries; deploy the existing
retirement/UI and identified-probe fixes with explicit acceptance checks; correct
health semantics and boot verification; then reconcile the agent guide/backlog
and document or eliminate the source-backed node deployment exception.
