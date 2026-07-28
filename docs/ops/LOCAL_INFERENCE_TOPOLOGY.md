# Local inference topology (2026-07-26)

Operational runbook for how ARIA reaches a model, after the day this host became
**local-only**. Companion docs: `infrastructure/laguna/LAGUNA_TUNING_20260726.md`
(server tuning + benchmarks) and `Development/Hermes/HERMES_TUNING_20260726.md`
(Hermes client side).

> This is an **agent-operational** doc, so it lives in the repo per the
> `project-docs` routing rule. Design-level consequences were written into
> `vault/ProjectAria/Design/COHERENCE_DESIGN.md` §5 (entries 11–13) and §6.

---

## 1. There is exactly one model server

**laguna** (Laguna S 2.1 Q4_K_M) on `:8095` is the only local model. Everything
else you may remember is retired:

| endpoint | status |
|---|---|
| `:8095` laguna | **live, resident, 8 slots** |
| `:8092` qwen-chat | **retired** — profile-gated, mutually exclusive with laguna |
| `:8093` qwen-agentic | **retired** — same |
| `:8081` context-1 | **retired** — `CONTEXT1_ENABLED=false` |
| openrouter / fireworks | **removed** — credits exhausted / key retired |

> ⚠️ **`:8092` lies.** `ridge-llama-proxy` binds it on the **tailnet IP only**
> (`100.123.245.84:8092`), so `curl localhost:8092` is connection-refused even
> though `ss -ltnp` shows a listener. This has caused misdiagnosis more than
> once. `:8093` has nothing at all.

qwen is **retired, not deleted** — `docker compose --profile qwen up -d` brings
it back, but you must `systemctl --user stop ridge-llama-proxy` first (it holds
`:8092`) and stop laguna (they cannot both be resident: ~85 GiB + ~61 GiB on a
124 GiB box).

---

## 2. Slot topology — which port ARIA should use

laguna runs `-np 8 -kvu` (8 slots, each addressing the full 262144, one **shared**
KV pool). `laguna-slot-proxy` (systemd user unit,
`infrastructure/wake-proxies/laguna-slot-proxy.py`) injects llama.cpp's `id_slot`
**by listen port**, bypassing the server's LCP/LRU heuristic:

| port | slot | consumer |
|---|---|---|
| 8096 | 0 | Hermes main |
| **8097** | **1** | **ARIA orchestrator** ← `LLAMACPP_URL` |
| 8098 | 2 | Hermes auxiliary |
| 8100 | 3 | Hermes cron |
| `:8095` | 4–7 | **pi-code sessions** ← `AGENTIC_URL` (unpinned, deliberate) |

**Why ARIA is pinned:** `LLAMACPP_URL` used to be `:8095` direct, so ARIA's
background workers — memory extraction (every 10 min per watched shell) and
ambient task capture (**every conversation turn**) — were placed by the heuristic
and could land on slot 0, **evicting Hermes's warm tool-schema prefix** and
forcing a ~150 s cold re-prefill on the next Signal message.

**Why pi-code is deliberately NOT pinned:** four concurrent sessions
(`coding_max_concurrent_sessions=4`) can't share one pinned port, and llama.cpp's
selector prefers an **unused** slot (`t_last = -1`) over one holding a prefix — so
fresh sessions land on 4–7 by themselves and cannot displace a pinned consumer.
Pinning them properly would need per-session port assignment in the session
manager; the current arrangement gets the same outcome for free.

**Rule for any new ARIA worker:** if its prompt shape differs from an existing
consumer's, give it its own slot. Extra slots are **memory-neutral** (8 slots cost
the same 85.3 GiB as 4), so this is cheap. Sharing a slot with a different prompt
shape thrashes both.

---

## 3. Every agent that uses a local model uses laguna

`db.agents`:

| slug | backend | resolves to |
|---|---|---|
| `aria` | `llamacpp` | `:8097` (slot 1) |
| `pi-coding` | `agentic` | `:8095` (slots 4–7) |
| `pi-coding-ridge` | `ridge` | `:8092` → **Ridge's RTX 3090** (see §3.1) |
| `search-agent` | `llamacpp` | `:8097` — **was `context1`**, which is retired and down; repointed 2026-07-26 so it is functional again |

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
`pi-coding` — we have `pi-coding`, on `agentic` → laguna. So pi-code sessions run
on laguna today with no further wiring.

### Hermes must spin pi-coding through ARIA
`~/.hermes/config.yaml` `agent.environment_hint` now distinguishes:
- `backend="claude_code"` — default for anything non-trivial.
- `backend="pi"` — **only** on explicit request for the local model. Runs ARIA's
  own agentic loop on laguna and still inherits the watchdog, e-stop and
  concurrency limiter.

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
curl -s localhost:8095/health                       # laguna
docker logs laguna 2>&1 | grep n_slots | head -1    # expect n_slots = 8, kv_unified = true
curl -s localhost:8200/api/v1/health                # expect available (llamacpp, agentic)
systemctl --user is-active laguna-slot-proxy aria-api hermes-gateway
docker logs laguna 2>&1 | grep 'selected slot by id' | tail -4   # pinning working
bash infrastructure/scripts/health                  # no dead-qwen probe
```

Backups: `ProjectAria/.env.bak-openrouter-20260726`, and the `~/.hermes/*.bak-*`
set listed in the Hermes doc.
