# Local inference topology (updated 2026-07-28 — two-server split, corrected same day, then a crash + a third server that evening)

Operational runbook for how ARIA reaches a model, after the day this host became
**local-only**. Companion docs: `infrastructure/laguna/LAGUNA_TUNING_20260726.md`
(server tuning + benchmarks) and `Development/Hermes/HERMES_TUNING_20260726.md`
(Hermes client side).

> **Same-day correction:** the split below originally repointed BOTH
> `LLAMACPP_URL` and `AGENTIC_URL` at `chadrock` (:8102) as a quick fix for the
> dead-laguna-port bug in §4 — but chadrock is the `pool` CLI's dedicated
> `--parallel 1` server, so that silently put ARIA's own chat agent and Search
> Agent back into the exact "asymmetric consumers on one server" problem this
> split exists to prevent. Corrected: `LLAMACPP_URL`/`AGENTIC_URL` now point at
> `qwen3.6-35b-a3b` (renamed from `qwen-hermes` — it's no longer Hermes-only
> either), and `pi-coding` moved off the local box entirely onto
> `backend=ridge` — **Ridge is now the only backend any pi-coding-family agent
> runs on.** Sections below are updated to match; where a section still
> narrates the original same-day incident as history, it says so.

> This is an **agent-operational** doc, so it lives in the repo per the
> `project-docs` routing rule. Design-level consequences were written into
> `vault/ProjectAria/Design/COHERENCE_DESIGN.md` §5 — entries 11–13 (2026-07-26,
> now partly superseded) and entries 14–23 (2026-07-28, the two-server split).

---

## 1. There are TWO model servers (split 2026-07-28)

Hermes and the coding agents no longer share a model. They never share a KV
cache, which is the entire point.

| endpoint | model | consumer | measured |
|---|---|---|---|
| `:8102` **chadrock** | Laguna S 2.1 ROCmFP4 (Vulkan), `-c 131072` (unchanged, kept at max — see §10) | **pool CLI → ProjectAria** only — genuinely only, as of the same-day correction above | decode 36.03 t/s, 66.8 GiB; **unmonitored until §10** |
| `:8103` **qwen3.6-35b-a3b** (renamed from `qwen-hermes`) | Qwen3.6-35B-A3B-MTP ROCmFP4 (Vulkan), `-c` trimmed **131072 → 100000** same evening (§10) | **Hermes main chat only** as of §10 — ARIA's default chat agent (`aria`) and Search Agent are both **disabled** (`enabled=false`), so despite `LLAMACPP_URL`/`AGENTIC_URL` still pointing here, neither sends real traffic today; Hermes's auxiliary tasks + 2 cron jobs moved off to `gemma-aux` (below) the same evening | decode 64–68 t/s measured 2026-07-28 (below the model card's 78–90 t/s floor — open question, not yet root-caused), prefill 840–940 t/s @ 7–19K context measured clean/uncontended (the "140.1" figure above was very likely taken under real Hermes slot contention, not a clean single request — not directly comparable). **Crashed once, §10** |
| `:8104` **gemma-aux** (new, 2026-07-28 evening) | Gemma 4 E4B, Q4_0 GGUF, **CPU-only** (`-ngl 0`) | Hermes's ~16 "auxiliary" side-tasks (title generation, compression, curator, approval, triage_specifier, mcp, etc.) + both cron jobs (alert triage, stock scanner) | see §10 for why CPU, the reasoning-mode gotcha, and real KV-cache sizing |
| `:8095` laguna | Laguna S 2.1 Q4_K_M (HIP) | — | **STOPPED**, incumbent, one command back |
| `:8092` qwen-chat / `:8093` qwen-agentic | — | — | retired, down for days |

`qwen3.6-35b-a3b` is now genuinely **single-consumer** (Hermes main chat) as of
this evening — the auxiliary-task sharing this table used to describe was
itself a smaller-scale repeat of the "asymmetric consumers on one server"
problem the two-server split was built to prevent (different prompt shapes:
Hermes's ~30K stable tool-schema prefix vs. 16 short, varied side-task
prompts), and got fixed the same way: split it onto its own server rather
than keep tuning the shared pool. See §10 for the full incident and why
"single consumer per model" turned out not to be the whole story on this
hardware.

**"~89.4 GiB, ~30 GB free" (the original split-day estimate above) does not
hold as a static number — do not treat it as a budget.** Real usage is
whatever the two GPU-offloaded models' *combined current context* costs, and
that's read from `/sys/class/drm/card0/device/mem_info_gtt_used` /
`_gtt_total`, not from a fixed baseline. See §10.

**Why split.** Every hard problem measured on 2026-07-27/28 came from one cause:
asymmetric consumers sharing a single unified KV pool. Hermes holds a ~30K stable
tool-schema prefix and is latency-sensitive; coding agents grow past 100K. They
evicted each other, and no amount of slot pinning, `--cache-ram` or checkpoint
tuning fixed it. Two servers retire the whole class.

> ⚠️ **`-fit off` is mandatory on every llama-server on this box.** Without it the
> process deadlocks in "fitting params to device memory" on Vulkan (which reports
> the full 128 GB as free) and becomes an **unkillable D-state process holding the
> GPU** — SIGKILL does nothing, and recovery is a hard reboot.

Restore the incumbent if you need it:
```
cd ~/Development/infrastructure/laguna && \
  LAGUNA_SLOTS="-np 3 --kv-unified" docker compose up -d laguna
```

---

## 2. Slot topology — RETIRED

`laguna-slot-proxy` is **stopped and disabled**. Ports 8096–8100 no longer listen.

It existed to stop consumers evicting each other inside one shared pool. With one
consumer per server there is nothing to pin against, so the proxy is dead weight.
All 20 references to it in `~/.hermes/config.yaml` were repointed to `:8103`;
their upstream (`:8095`) is stopped, so every one of them was broken.

Slot counts now:

| server | slots | why |
|---|---|---|
| `:8102` chadrock | `--parallel 1` | single consumer; the vendor-validated profile |
| `:8103` qwen3.6-35b-a3b (renamed from qwen-hermes) | `--parallel 2 --kv-unified` | Hermes main + auxiliary tools, which previously used a *separate* laguna slot so they would not evict Hermes's prefix. Since the same-day correction above, ARIA's own chat agent + Search Agent also share these 2 slots. |

Measured: slots cost ~2% decode each and ~0.15 GiB for 4→8. Do not add them
without a consumer that needs one.

---

## 3. Which agent uses which server

> Updated 2026-07-28 (same-day correction, supersedes the first split): `pool`
> is the ONLY consumer of `:8102` chadrock. `aria` and `search-agent` are
> **both disabled** (`enabled=false`) as of later the same day — their
> `db.agents` rows still resolve to `:8103` qwen3.6-35b-a3b, but neither sends
> real traffic today, so qwen's only active consumer is Hermes. Both
> pi-coding-family agents (`pi-coding` chat tool and `pi-coding-ridge` coding
> session) run on Ridge — laguna no longer backs anything named "pi-coding".
> The `:8097`/`:8095` slot references below are historical (pre-split,
> laguna-slot-proxy era).

`db.agents`:

| slug | backend | resolves to |
|---|---|---|
| `aria` | `llamacpp` | `:8103` qwen3.6-35b-a3b (was chadrock `:8102` for part of 2026-07-28; corrected same day) |
| `pi-coding` | `ridge` | Ridge's RTX 3090 (was `agentic` → chadrock/laguna; corrected 2026-07-28 — see §3.1) |
| `pi-coding-ridge` | `ridge` | `:8092` → **Ridge's RTX 3090** (see §3.1) |
| `search-agent` | `llamacpp` | `:8103` qwen3.6-35b-a3b — **was `context1`**, retired and down; repointed 2026-07-26, then moved off chadrock same-day as `aria` above |

### 3.1 `ridge` — the one backend that is NOT on this box (2026-07-27)

`RIDGE_URL=http://100.123.245.84:8092/v1` is corsair's `ridge-llama-proxy`, which
Wake-on-LANs the Ridge PC and holds the request while it boots. Behind it, Ridge
runs **NInfer** (not llama.cpp) serving Qwen3.6-35B-A3B at ~259 tok/s with a
**147456-token context**. That context came from disabling CUDA graphs, measured
at ~2% throughput cost for +57% context; disabling MTP as well would reach 172032
but at 141 tok/s (-46%), so it was rejected. `D:\ninfer\run-ninfer.bat` carries
the full measurement table — read it before retuning.

Agent `pi-coding-ridge` **thinks on Ridge but acts on corsair** — every
filesystem/shell tool call executes locally here. Ridge holds no repositories.

Things that bite:

- **Cold path ~90s.** Ridge sleeps after 30 min idle. The orchestrator therefore
  gives the **first** chunk its own budget (`ridge_timeout_seconds`, 420s); the
  normal 60s `stream_chunk_timeout_seconds` applies to every chunk after it.
  Without that split the turn died at 60s with "LLM stream stalled" and persisted
  **nothing** — no assistant message at all.
- **One request at a time.** NInfer has no continuous batching, so concurrent
  ridge callers queue. Do not point background workers at it.
- **Thinking is verbose** (~1k tokens before content). `max_tokens` below ~2000
  returns EMPTY content with `finish_reason=length`.
- **Not health-probed.** A probe would either report DOWN while it is merely
  asleep, or wake a gaming PC every tick. See the comment in `health.py`.
- **NInfer vs Chatterbox TTS is exclusive** — 20.82 GiB of weights must be
  GPU-resident, so Ridge's TTS (`:8890`) is disabled. See
  `infrastructure/endpoints.env`.

`_start_pi_code_session()` resolves `db.agents` slug `pi-code` **or**
`pi-coding` — we have `pi-coding`, on `ridge` (corrected 2026-07-28; was
`agentic` → chadrock/laguna). So a bare `backend="pi-code"` session with no
`subagent_profile` now runs on Ridge, same as `pi-coding-ridge` explicitly —
they resolve to the same backend/model, differing only in that
`pi-coding-ridge`'s system prompt documents the filesystem/shell tools and the
wake-on-demand behavior explicitly.

### Hermes must spin pi-coding through ARIA
`~/.hermes/config.yaml` `agent.environment_hint` now distinguishes:
- `backend="claude_code"` — default for anything non-trivial.
- `backend="pi"` — **only** on explicit request for the local model. Runs ARIA's
  own agentic loop on Ridge (corrected 2026-07-28; was laguna) and still
  inherits the watchdog, e-stop and concurrency limiter.

"Use the local model" means `backend="pi"` **through `create_coding_session`** —
never a coding loop inside Hermes.

---

## 4. Backends that were silently failing

Both were pointed at a credit-exhausted OpenRouter account and erroring on every
call. Repointed to laguna in `.env`:

| setting | was | now |
|---|---|---|
| `PLANNING_AMBIENT_BACKEND` | `openrouter` / `deepseek-v4-flash` | `llamacpp` / `laguna-s-2.1` |
| `HEARTBEAT_BACKEND` | `openrouter` / `deepseek-v4-flash` | `llamacpp` / `laguna-s-2.1` |

`planning_ambient_capture_enabled` is **true**, so ambient task extraction fires
on **every conversation turn** — and it feeds the `tasks` collection that C3's
Linear reconciliation consumes. It had been failing for as long as the credits
had been gone.

`OPENROUTER_API_KEY` is commented out; `GET /api/v1/health` now reports
`available (llamacpp, agentic)`.

**Design consequence:** there is no cloud fallback anywhere. The "cheap fast
cloud model for the hot path" escape hatch that some designs assume **no longer
exists** — new components must budget for laguna's ~204 t/s prefill /
~18–23 t/s decode.

---

## 5. Which background work actually costs tokens

Audited by reading each worker for LLM calls, because `/usage/cost` returns **0**
for local backends and is blind to this question. The laguna server log is the
only place background load is visible.

**Costs tokens:**
- **shells extraction** — every 10 min *per watched shell*, LLM call once a shell
  has ≥ `shells_extraction_min_events` (20) new events. Legitimate (it's how
  long-term memory gets built); the fix was routing, not frequency.
- **ambient task capture** — every conversation turn.
- weekly report.
- **Hermes's ~16 auxiliary side-tasks + 2 cron jobs** — as of 2026-07-28 evening
  these run on `gemma-aux` (`:8104`, CPU-only), **not** qwen. Moving them off
  qwen doesn't change that they cost tokens, just which server pays for them —
  noted here so "what's calling qwen" audits aren't fooled by their absence.

**Costs nothing — verified, leave alone:** `selfcheck` (10 min, HTTP probes
only), `projects harvest` (30 min, deterministic), `shells snapshot`/`adopt`/
`reconcile`/`coding watchdog` (15–120 s, tmux + Mongo only), `prune` (6 h).

**Already off, and expensive if enabled:** `dream` (6 h), `awareness` (sensors
2 min + analysis 30 min), `shared_scan`, `shells_reap`.

**Disabled 2026-07-26:** the heartbeat. See §7.

---

## 6. The alert relay is a single unmonitored hop

ARIA enqueues alerts; **Hermes owns the only relay to a human.** That relay was
broken from **2026-06-29 to 2026-07-26** — 91 failures in the final 14 days — and
nothing detected it. ARIA kept enqueuing correctly; the alerts just never
arrived.

Every component that reports to a human inherits this hop. A relay-liveness check
("has any alert been delivered in N hours?") belongs in the selfcheck worker
before more components depend on the channel. Flagged in `COHERENCE_DESIGN.md` §6.

Measured alert rate: **49 in 30 days ≈ 1.6/day** (30 `agent_task_done`, 10
`degraded`, 5 weekly report, 4 `recovered`). The Hermes triage cron was cut from
`*/10` → hourly on that basis.

> **Schema gotcha:** the `alerts` collection uses **`acked`**, not
> `acknowledged`. Querying the wrong field returns a scary "0 acked" that looks
> like a bug and isn't.

---

## 7. Heartbeat — read this before "fixing" it

`~/.aria/HEARTBEAT.md` is an **input, not an output**.
`heartbeat/service.py::_ensure_heartbeat_file()` writes it **once** from a
template if missing and never again; the file's own comment says *"Edit this file
to customize what ARIA monitors."* Its old mtime marks creation, **not** a
malfunction — a mid-session analysis got this backwards.

The real output path is `notification_service.notify(source="heartbeat")` →
`alerts`. Measured there: **0 heartbeat alerts in the entire 107-alert history**,
and over 7 days **114 "Heartbeat OK" + 63 "Heartbeat LLM call failed" + 0
"Heartbeat alert"**.

So it isn't broken — it ran ~25×/day and never had anything to say, because the
checklist is still the untouched default whose items ("scheduled tasks failed",
"missed reminders") duplicate the selfcheck worker and the triage cron.
`HEARTBEAT_ENABLED=false`. Re-enabling is only worthwhile with a checklist
covering something not already monitored.

---

## 8. Stale-endpoint hazard in skills

`~/.hermes/skills/devops/systems-ops/references/aria-alert-diagnosis.md` — the
skill the **diagnostic agent loads when an alert fires** — instructed it to curl
`:8081`, `:8092`, `:8093` and mislabelled them ("openrouter proxy", "context").
All three are retired. A diagnostic agent following that would produce confident
false conclusions. Corrected 2026-07-26.

**Check skills, not just config, when endpoints move.** Skills are prompt-level
instructions to agents and drift silently.

---

## 9. Quick verification

```bash
curl -s localhost:8102/health                          # chadrock (pool CLI only)
curl -s localhost:8103/health                           # qwen3.6-35b-a3b (Hermes main chat only)
curl -s localhost:8104/health                           # gemma-aux (Hermes auxiliary + cron)
curl -s localhost:8200/api/v1/health                     # expect available (llamacpp, agentic, ridge)
systemctl --user is-active aria-api hermes-gateway
docker logs qwen3.6-35b-a3b 2>&1 | grep 'selected slot by id' | tail -4   # pinning working
bash infrastructure/scripts/health                       # no dead-qwen probe
cat /sys/class/drm/card0/device/mem_info_gtt_used /sys/class/drm/card0/device/mem_info_gtt_total
                                                          # real GPU memory pressure — see §10, NOT docker stats
```

Backups: `ProjectAria/.env.bak-openrouter-20260726`, and the `~/.hermes/*.bak-*`
set listed in the Hermes doc.

---

## 10. The 2026-07-28 evening crash: GPU command-submission contention, docker's blind spot, and a third server

**What happened.** `:8103` (then `qwen-hermes`) crashed — `vk::DeviceLostError`,
`radv/amdgpu: Not enough memory for command submission` — mid-prompt-processing
on a long-context turn, at the same moment chadrock was deep in its own
87K–95K token coding session. Crash backtrace: inside a checkpoint-restore copy
(`llama_io_write_device` / `state_seq_get_data`). `restart: "no"` (deliberate,
see §1's `-fit off` warning) meant it just stayed down until Hermes's next
real request failed after 3 retries.

**Root cause — a different resource than the one already ruled out.**
`COHERENCE_DESIGN.md` §5 #19 already measured that `--cache-ram`/`-ctxcp` don't
move *total* GTT memory growth — that finding stands, this isn't a
contradiction of it. What actually ran out is a **separate, tiny (~1 GiB)
dedicated VRAM aperture** used for GPU command submission
(`/sys/class/drm/card0/device/mem_info_vis_vram_{total,used}` — NOT the ~124 GiB
GTT pool). It sits at **~78–96% used essentially all the time regardless of
load** (structural, not a spike) — any simultaneous command submission from
two GPU-offloaded processes is a latent risk on this hardware, independent of
how much of the large GTT pool is free.

**Mitigation applied (qwen only, unverified by repeat-crash testing — a
hypothesis about this narrower resource, not a proven fix):**
```diff
-ctxcp 32 → 10
--cache-ram 8192 → 2560
```
Chadrock's own checkpoint config was **not** touched — it needs max context,
by explicit decision.

**⚠️ The bigger finding: docker/cgroup memory limits do not see GPU-offloaded
memory on this hardware.** Confirmed empirically the night of the crash:
`docker stats` showed **~5 GiB combined** for chadrock+qwen while
`mem_info_gtt_used` showed **~97 GiB**. `-ngl 999` GPU-offloaded allocations on
this unified-memory Strix Halo APU are accounted to the kernel's DRM/GTT
memory manager, not to the container's cgroup. **`docker run --memory` /
compose `mem_limit` is a no-op safeguard for chadrock or qwen** — it only
works for a genuinely CPU-only service (confirmed on gemma-aux, which does
carry a real `mem_limit: 6g`). **The only ground truth for real memory
pressure on this box is `mem_info_gtt_used` vs `mem_info_gtt_total`** — not
`docker stats`, not `free -h`'s "used" column.

**Fixes landed:**
1. **qwen's `-ctxcp`/`--cache-ram` shrunk** (above).
2. **qwen's `-c` trimmed 131072 → 100000** — ~24% cut to its own KV-cache
   ceiling (~10.25 → ~7.8 GiB max), freeing real headroom for chadrock. Modest,
   honestly sized — not a structural fix by itself.
3. **Hermes's ~16 auxiliary tasks + 2 cron jobs moved to a new third server**,
   `gemma-aux` (`:8104`, Gemma 4 E4B Q4_0, CPU-only) — the same
   asymmetric-consumers pattern the two-server split fixed, recurring at
   smaller scale *inside* Hermes's own qwen usage. CPU-only doesn't buy
   separate memory headroom here (same GTT pool either way) but does keep it
   out of the ~1 GiB VRAM command-submission aperture above — it can never be
   a third contender for *that* specific resource. Two things worth knowing if
   you add another small model to this box:
   - Gemma 4 has a real hybrid local/global attention pattern (its GGUF
     metadata carries a per-layer `sliding_window_pattern` — 35 of 42 layers
     window-capped at 512 tokens, only 7 scale with `--ctx-size` at all) that
     makes its KV cache far cheaper than a dense model at the same context:
     ~0.26 GiB computed at 8192 ctx, ~1.78 GiB at 65536 — not the 8× a dense
     model would cost. (`--ctx-size` was in fact raised 8192→65536 the same
     evening: Hermes hard-rejects any configured model below a 64,000-token
     context floor regardless of what a given task actually sends — "ARIA
     alert triage" started failing with "Model gemma-4-e4b-it has a context
     window of 8,192 tokens, which is below the minimum 64,000 required by
     Hermes Agent." `mem_limit` bumped 6g→8g to match.) Check any future
     model's GGUF metadata for this before assuming its KV cache scales like
     chadrock/qwen's (dense, no such pattern) — and check Hermes's context
     floor before picking a small model's `--ctx-size` at all.
   - Gemma 4 has a **stochastic reasoning mode** that can silently consume an
     entire `max_tokens` budget (`reasoning_content` preamble, empty `content`,
     `finish_reason: length`) — disabled with `--reasoning off
     --reasoning-budget 0`, same as chadrock already carries.
4. **Chadrock had zero automated health monitoring until this incident**,
   despite the identical crash risk and identical `restart: "no"` policy as
   qwen. `api/aria/shells/selfcheck.py` (the code that actually pages via
   Signal) only ever probed `llamacpp_url`. Added a `pool_api_url` check.
5. **New `gpu_memory` selfcheck check** — reads the real GTT figures above,
   alerts >90%. First automated signal this box has had for either model
   server's crash risk before a human notices via a failed reply.
6. **A third, unrelated stale-config bug found in the same pass:** Hermes's
   two cron jobs (hourly alert triage, daily stock scanner) were hardcoded to
   `:8100` — the slot-proxy port retired in §2 — silently failing on every run
   since. Fixed alongside the auxiliary-task repoint. Same lesson as §6/§8:
   a topology change isn't done until every consumer's config is audited —
   `.env`, cron jobs, skills, docs, and compose are five independently-stale
   places the same endpoint can live in.

**Design-level writeup:** `COHERENCE_DESIGN.md` §5 #24–28.
