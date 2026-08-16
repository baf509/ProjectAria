# Local inference topology — the hardware gotchas

**Read before changing any `*_URL`, any llama-server flag, or adding a worker that
calls an LLM.** Everything here is a trap someone already walked into on this box.
Several cost a reboot or a day of silent wrong answers.

Last updated: 2026-08-15T23:14:42-04:00

> **This file no longer carries the topology tables.** It used to, and the copies
> drifted from their sources within days. Which model runs where, on what geometry,
> at what throughput, lives in:
>
> - `vault/ProjectAria/Planning/HOUSE_AGENT_ARCHITECTURE_20260815.md` §1.1 / §4.1 —
>   the layout decision and every measurement behind it.
> - `vault/infrastructure/Planning/MEMORY_BUDGET_POLICY_20260814.md` §7 — residency
>   and host-memory floors.
> - **Live truth, always:** `GET /api/v1/infrastructure/model-servers`,
>   `/model-servers/utilization`, `/model-servers/devices`,
>   `/api/v1/infrastructure/running`.
>
> A number copied into this file is a number that will disagree with the registry
> later. Read the endpoint.

> **Old section-number citations.** Running code cites this file by section:
> `api/aria/config.py:181` and `api/aria/agents/session.py:137` cite **§3.1** for the
> Ridge "one request at a time" rule — that is now *[Ridge is off-box](#ridge-is-off-box-and-behaves-differently)*.
> `api/aria/infrastructure/gpu_devices.py:9` cites this file for the device map —
> that is now *[Reading GPU memory truthfully](#reading-gpu-memory-truthfully)*.

---

## Routing: do not "fix" these back

**`/llm/v1` is pinned, on purpose.** `LLAMACPP_URL` / `AGENTIC_URL` /
Hermes's `base_url` all point at ARIA's own passthrough
(`http://localhost:8200/llm/v1`, `api/routes/llm_proxy.py`). That passthrough
would *auto*-resolve to the largest resident server — which is DS4 on the Halo —
but it is **pinned to Qwen on the R9700** and must stay that way:

```
GET /api/v1/infrastructure/llm-route
→ {"pinned":"Qwen3.8-27B-R9700-HIP", "reason":"pinned in ARIA (Qwen3.8-27B-R9700-HIP)"}
```

DS4 `:8108` is the coding agent's **single 131K slot**. Every background call that
took the auto-route evicted its warm prefix — 4.2 s warm turns into 39.5 s cold.
Unpinning re-breaks pi. If you need to change it, use `PUT /api/v1/infrastructure/llm-route`
or the `/operate` "Local model route" panel, and know what you are evicting.

**Do not pin a `*_URL` at a model port either.** That broke four times running
(qwen → laguna → chadrock → DS4): the big servers are mutually RAM-exclusive, so
the named port goes dark the moment another server starts, and `selfcheck` then
pages `llm (ConnectError)` every 10 minutes. `LLAMACPP_API_KEY` must equal `API_KEY`.

**Bind addresses are not uniform, and the asymmetry has caused repeated
misdiagnosis.** Verified live 2026-08-15:

| listener | bind | note |
|---|---|---|
| `:8080` Qwen (R9700) | `127.0.0.1` only | not reachable over the tailnet |
| `:8108` DS4 (Halo) | `127.0.0.1` only | not reachable over the tailnet |
| `:8092` `ridge-llama-proxy` | **`100.123.245.84` only** | `localhost:8092` is connection-refused *even though `ss` shows a listener* |
| `:8200` ARIA | `0.0.0.0` | the only thing that knows which of the above is up |

Check with `ss -ltnp`, and read the *address*, not just the port.

## Reading GPU memory truthfully

Two GPUs, two independent pools. **DRM enumeration is inverted from what you would
guess**, and three separate inversions bite:

| DRM card | PCI | device | pool | capacity |
|---|---|---|---|---|
| `card0` | `0000:c6:00.0` | Radeon AI PRO R9700 (**discrete**) | `r9700-vram` | 31.9 GiB VRAM |
| `card1` | `0000:c8:00.0` | Radeon 8060S / Strix Halo (**integrated**) | `halo-gtt` | 124 GiB GTT |

1. **`card0` is the discrete card.** Every historical read of
   `/sys/class/drm/card0/device/mem_info_gtt_used` — ARIA's start gate, `selfcheck.py`,
   and the old verification block in this very file — was reporting the dGPU's
   near-empty pool. Measured 2026-08-14: card0 GTT 0.22 GiB while card1 held 97.8 GiB.
   A gate reading that number would approve a second 100 GiB model onto a full box.
   Fixed in `api/aria/infrastructure/gpu_devices.py`, which classifies by VRAM size,
   not enumeration order.
2. **`Vulkan0` is the R9700; `Vulkan1` is the Halo.** Backwards from every reference
   guide, because those machines have no dGPU. Omitting `-dev` on the Halo deployment
   sends a 97 GiB model to a 32 GiB card and spills ~78 GiB over OCuLink — it still
   answers, at **1.65 tok/s**.
3. **`ROCm0`/`ROCm1` depend on the build.** The dual-arch HIP build enumerates both
   (`ROCm1` = Halo, `ROCm0` = R9700); a gfx1151-only build such as the sealed O5
   runtime sees only the Halo, so *its* `ROCm0` is a different card. Verify placement
   after any runtime change. **Never port a `-dev` flag across runtimes.**

### `docker stats` cannot see any of this

On this unified-memory APU, `-ngl 999` allocations are accounted to the kernel's
DRM/GTT manager, not to the container cgroup. Measured the night of the 2026-07-28
crash: `docker stats` showed **~5 GiB combined** for two servers while
`mem_info_gtt_used` showed **~97 GiB**.

**`docker run --memory` / compose `mem_limit` is a no-op safeguard for any
GPU-offloaded server.** It works only for a genuinely CPU-only service (confirmed on
`gemma-aux`, which does carry a real `mem_limit`). **The only ground truth for memory
pressure on this box is `mem_info_gtt_used` vs `mem_info_gtt_total` on `card1`** — not
`docker stats`, not `free -h`'s "used" column.

### Per-process residency: fdinfo, not KFD

`process_gpu_bytes()` reads amdgpu's per-fd `drm-resident-{gtt,vram}` from
`/proc/<pid>/fdinfo`, grouped by `drm-pdev`. This replaced a KFD-tree read that only
ever worked for HIP: **a RADV Vulkan server has no KFD entry at all**, so every Vulkan
deployment measured ~0 GiB while holding ~98. Live right now, DS4 measures
**98.96 GiB** by fdinfo while its cgroup sees nearly nothing.

### The ~1 GiB command-submission aperture

Separate from the 124 GiB GTT pool there is a **tiny (~1 GiB) dedicated VRAM aperture**
used for GPU command submission:
`/sys/class/drm/card1/device/mem_info_vis_vram_{used,total}`. It sits at **~78–96%
used essentially all the time, regardless of load** — structural, not a spike. Read
just now with one server idle: **993,316,864 / 1,073,741,824 = 92.5%**.

This is what actually ran out in the 2026-07-28 `vk::DeviceLostError` /
`radv/amdgpu: Not enough memory for command submission` crash — not the big pool.
**Any simultaneous command submission from two GPU-offloaded processes is a latent
risk on this hardware, independent of how much GTT is free.** The R9700 (card0) has
34.2 GiB visible via resizable BAR, so this is Halo-specific; a CPU-only helper can
never be a third contender for it.

The follow-up stress test (two concurrent ~64,500-token prompts, 2026-07-30) completed
cleanly and the failure has not reproduced — but the mitigation applied at the time
(`-ctxcp 32→10`, `--cache-ram 8192→2560`) was a hypothesis about this narrower
resource and was **never verified by repeat-crash testing**. Treat "does not reproduce"
as what it says.

## llama-server flags that are load-bearing

**`-fit off` is mandatory on every llama-server on this box.** Two distinct failures
if you omit it:

- On Vulkan (which reports the full 128 GB as free) the process deadlocks in
  "fitting params to device memory" and becomes an **unkillable D-state process
  holding the GPU**. SIGKILL does nothing. Recovery is a hard reboot.
- On the dGPU, a failed VRAM fit silently serves from host memory at **~0.4 tok/s**
  and eats the RAM the Halo model needs. So **check decode speed after a restart,
  not just `/health`.**

Present and required in `qwen-r9700/serve-rocmfp4.sh:35`, `ds4-halo-xxs/bench.sh:17`,
and `qwen3.8-27b/docker-compose.yml:85` (which says "REQUIRED here, not cosmetic").

**`-c` is PER SEQUENCE. Total KV = `-c` × `-np`.** Building on the inverted assumption
made the first start attempt request 57,065,472,000 bytes.

**Stop the unit before editing `-c`.** llama.cpp **segfaults on the KV-allocation
failure path** — `status=11/SEGV` after `failed to allocate DeepSeek4 compressed KV
cache buffer`. A bad `-c` does not fail cleanly, and `Restart=on-failure` retries it
in a loop; during one cutover an auto-restart of the *old* config raced an edit and
produced a confusing second failure.

**`--cache-reuse` is not set and should not be.** It is inert on sliding-window
models — the server refuses the flag and logs `cache_reuse is not supported by this
context`.

**Do not design around the parked prompt cache on DS4.** `--cache-ram`/`--cache-disk`
have **never restored once**: `loads=0` across every run, and the one time it was
needed it logged `forcing full prompt re-processing due to lack of cache data (likely
due to SWA or hybrid/recurrent memory)` at `n_swa = 128`. In-slot *context checkpoints*
do work (measured: 22,091 tokens restored in ~0.5 s). Re-check `loads=` before
revisiting.

**Never derive a KV bytes/token constant and build on it.** This has been wrong twice.
11 KiB/token (derived) → corrected to 6,880 B/token (measured off an OOM's exact
allocation) → that too corrected 2026-08-15 as specific to the sealed-O5 affine stack
and *wrong by an order of magnitude* on today's stacks (~90 KiB/token f16 DS4,
~34 KiB/token q4_0 Qwen). Measure the stack you are actually running.

## Model-specific KV: check the GGUF metadata first

**Gemma 4's KV cache is not comparable to a dense model's.** Its GGUF metadata carries
a per-layer `sliding_window_pattern`: **35 of 42 layers are window-capped at 512
tokens, and only 7 scale with `--ctx-size` at all**. So ~0.26 GiB at 8192 ctx and
~1.78 GiB at 65536 — not the 8× a dense model would cost. `gemma-4-e4b-Q4` is still a
registered server at `-c 65536` on that basis.

Check any new model's GGUF metadata for this before assuming its KV scales densely.
Also check **Hermes's context floor** before picking a small model's `--ctx-size`:
Hermes hard-rejects any configured model declaring below **64,000** tokens, on the
*declared* value, not the served `-c`. That is why gemma went 8192 → 65536.

**Gemma 4 also has a stochastic reasoning mode** that can silently consume an entire
`max_tokens` budget (`reasoning_content` preamble, empty `content`,
`finish_reason: length`) — disabled with `--reasoning off --reasoning-budget 0`.
Qwen has the same shape by design: budget generously and **treat empty `content` as a
failure**, because writing the empty result is exactly how DS4 once labelled every
memory with zero entities.

## Ridge is off-box and behaves differently

*(This is what `config.py:181` and `session.py:137` cite as "§3.1".)*

`RIDGE_URL=http://100.123.245.84:8092/v1` is corsair's `ridge-llama-proxy`, which
Wake-on-LANs the Ridge PC and holds the request while it boots. Behind it Ridge runs
**NInfer** (not llama.cpp). Registry slug `Ridge-Qwen3.8-27B`; as of 2026-08-15 it is
`startable=True` with `wake_command` / `remote_start_command` / `remote_stop_command` /
`sleep_command`, so ARIA can wake, start, stop and sleep it over ssh. `onbox=False`.
Current state reads `asleep`.

Agent `pi-coding-ridge` **thinks on Ridge but acts on corsair** — every
filesystem/shell tool call runs locally here. Ridge holds no repositories.

- **Cold path ~90 s.** Ridge sleeps after 30 min idle, so the orchestrator gives the
  **first** chunk its own budget (`ridge_timeout_seconds`, 420 s); the normal 60 s
  `stream_chunk_timeout_seconds` applies to every chunk after it. Without that split
  the turn died at 60 s with "LLM stream stalled" and persisted **nothing** — no
  assistant message at all.
- **One request at a time.** NInfer has no continuous batching, so concurrent callers
  queue. **Do not point background workers at it.**
- **Thinking is verbose** (~1k tokens before content). `max_tokens` below ~2000 returns
  EMPTY content with `finish_reason=length`.
- **Deliberately not health-probed.** A probe would either report DOWN while it is
  merely asleep, or wake a gaming PC every tick. See the comment in `health.py`.
- **NInfer and Ridge's Chatterbox TTS (`:8890`) are mutually exclusive** — 20.82 GiB of
  weights must be GPU-resident, so the TTS is disabled.
- Tuning record: `D:\ninfer\run-ninfer.bat` holds the measurement table. The shape
  worth keeping — disabling CUDA graphs bought **+57% context for ~2% throughput**
  (accepted); also disabling MTP would have reached 172032 ctx **at 141 tok/s, −46%**
  (measured and rejected). Absolute numbers are stale (NInfer 0.6.0 / Qwen3.8-27B now);
  the tradeoff shape is the record.

## Which background work actually costs tokens

`/usage/cost` returns **0** for local backends and is blind to this question, so this
was audited by reading each worker for LLM calls. Live flag values, verified
2026-08-15:

| worker | LLM? | state |
|---|---|---|
| shells extraction | yes — per watched shell, every 10 min once ≥20 new events | routed to `gemma-4-e4b-Q4` explicitly (`SHELLS_EXTRACTION_*`) |
| ambient task capture | yes — **every conversation turn** | on |
| weekly report | yes | on |
| `dream` (6 h) | yes | **ON** (`DREAM_ENABLED=true`) |
| `awareness` (sensors 2 min / analysis 30 min) | yes | **ON** (`AWARENESS_ENABLED=true`) |
| `shared_scan` | no LLM (deterministic emitters) | **ON** (`SHARED_SCAN_ENABLED=true`) |
| `shells_reap` | no | off (`shells_reap_enabled` default `False`, no `.env` override) |
| heartbeat | yes | off (`HEARTBEAT_ENABLED=false`) |
| ontology LLM extraction | yes | off (`ONTOLOGY_EXTRACTION_ENABLED=false`) |
| `selfcheck` (10 min), `projects harvest` (30 min), `shells snapshot`/`adopt`/`reconcile`, `coding watchdog`, `prune` | **no** — HTTP probes, tmux and Mongo only | leave alone |

⚠️ This table used to claim dream, awareness and `shared_scan` were **off** — they are
on. Re-check the flags before quoting this; that is one `grep` in `.env` plus the
defaults in `config.py`.

Background load is visible at `GET /api/v1/infrastructure/model-servers/utilization`.
**`saturated` is the field to watch** — `requests_deferred > 0` means a request lands
in whichever slot frees first rather than the one holding its prefix. `null` there
means *unknown* (server has no `--metrics`), not "fine". `declared_*` vs live is the
drift check for "unit edited, not restarted".

## Two schema/file gotchas that look like bugs and aren't

- **`db.alerts` uses `acked`, not `acknowledged`.** No `acknowledged` field exists.
  Querying the wrong name returns a scary "0 acked" that is purely your typo.
  (`notifications/service.py:558,593,595`.)
- **`~/.aria/HEARTBEAT.md` is an INPUT, not an output.**
  `heartbeat/service.py::_ensure_heartbeat_file()` writes it **once** from a template if
  missing and never again. Its old mtime marks creation, **not** a malfunction — a
  mid-session analysis got exactly this backwards. The real output path is
  `notify(source="heartbeat")` → `alerts`, and there were **0 heartbeat alerts in the
  entire 107-alert history**: it ran ~25×/day and never had anything to say, because the
  checklist is still the untouched default whose items duplicate the selfcheck worker.
  Re-enabling is only worth it with a checklist covering something not already monitored.

## An endpoint change is not done when the servers are up

Three instances of one failure mode, all found weeks late:

- Hermes's two cron jobs were hardcoded to `:8100` — a slot-proxy port retired long
  before — and **failed silently on every run**.
- `~/.hermes/skills/devops/systems-ops/references/aria-alert-diagnosis.md`, the skill a
  **diagnostic agent loads when an alert fires**, told it to curl three retired ports
  with wrong labels. A diagnostic agent following that produces confident false
  conclusions. **Skills are prompt-level instructions and drift silently — check them,
  not just config.**
- `~/.local/share/aria-mcp/server.py` was a hand-made *copy* of `mcp/server.py`, so
  "edit and restart" reloaded the old toolset. The drift was 19 tools deep (71 deployed
  vs 90 in repo) when found. Now a symlink.

**`.env`, cron jobs, skills, docs, and compose are five independently-stale places the
same endpoint can live in.** Audit all five.

## Quick verification

```bash
KEY=$(grep -E '^API_KEY=' /home/ben/Development/ProjectAria/.env | cut -d= -f2-)

# what is running, across BOTH registries (LLM + non-LLM)
curl -sH "X-API-Key: $KEY" localhost:8200/api/v1/infrastructure/running | jq

# where each model sits, its geometry, and declared-vs-live drift
curl -sH "X-API-Key: $KEY" localhost:8200/api/v1/infrastructure/model-servers | jq
curl -sH "X-API-Key: $KEY" localhost:8200/api/v1/infrastructure/model-servers/utilization | jq

# per-GPU pools — this is what the start gate reads, and it reads the RIGHT card
curl -sH "X-API-Key: $KEY" localhost:8200/api/v1/infrastructure/model-servers/devices | jq

# where /llm/v1 actually goes right now
curl -sH "X-API-Key: $KEY" localhost:8200/api/v1/infrastructure/llm-route | jq

# raw pressure, if you must read sysfs: card1 is the Halo, card0 is the dGPU
cat /sys/class/drm/card1/device/mem_info_gtt_{used,total}          # big pool
cat /sys/class/drm/card1/device/mem_info_vis_vram_{used,total}     # ~1 GiB aperture

# who is listening, and on WHICH address
ss -ltnp | grep llama-server
```

⚠️ **Start and stop model servers only through the registry**
(`POST /api/v1/infrastructure/model-servers/{slug}/start|stop`, `/operate`, TUI `g`,
or the MCP tools). A raw `docker start` / `systemctl start` / `serve.sh` checks none of
RAM exclusivity, per-pool fit, or port conflicts. Rule since 2026-07-29.
**Start the dGPU model FIRST** — it needs host RAM only transiently on its way to VRAM,
while the Halo model takes and holds ~100 GiB. Reversed, loading Qwen drove DS4's OOM
guard under its floor and killed it **17 MiB short**.

## Known-open defects (found 2026-08-15, not yet fixed)

Both are the "two mapping layers, both of which have drifted before" problem recurring.

1. **An ARIA-launched pi session takes a dead path.** `.env` has
   `PI_CODING_PROVIDER_LLAMACPP=ds4` / `_AGENTIC=ds4`; those name a provider inside
   *pi's own* `~/.pi/agent/models.json`, where `providers.ds4.baseUrl =
   http://127.0.0.1:18211/v1` — and **nothing listens on `:18211`**. Pi's own
   `defaultProvider` (`llama-cpp` → `:8108`) is fine, which is why this hides.
2. **`db.agents` `pi-coding.llm.model = "DS4-0731-UD-IQ3-XXS-Halo-DSpark"` is not a
   registry slug** (the registry has `DS4-0731-IQ3_XXS-Halo-Vulkan`). So
   `llm_route.match_requested()` cannot match it and the request silently falls through
   to the Qwen pin.

## Companion docs

- `vault/ProjectAria/Planning/HOUSE_AGENT_ARCHITECTURE_20260815.md` — current layout,
  geometry and throughput, and why.
- `vault/infrastructure/Planning/MEMORY_BUDGET_POLICY_20260814.md` §7 — residency and floors.
- `docs/ops/RETRIEVAL_CAPABILITIES.md` — the mongot/embeddings switches.
- `infrastructure/laguna/LAGUNA_TUNING_20260726.md` and
  `Development/Hermes/HERMES_TUNING_20260726.md` — historical server/client tuning; both
  describe retired servers, so read them for method, not for endpoints.
