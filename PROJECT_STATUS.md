# ARIA Project Status

**Last Updated:** 2026-07-28
**Updated By:** Claude Code

This is a living "current state" page. For the full shipped history (what shipped, when), see `CHANGELOG.md`.

---

## Current Focus / Next Actions

00. **Local inference topology: two-server split, then a same-day routing correction (2026-07-28).** The shared `laguna` server was retired and split into two single-consumer servers to stop KV-cache eviction fights: `chadrock` (:8102, Laguna S 2.1) and `qwen3.6-35b-a3b` (:8103, renamed from `qwen-hermes`, Qwen3.6-35B-A3B-MTP). The split's dead-port fix-up initially repointed ARIA's own chat agent, `pi-coding`, and Search Agent at chadrock — silently recreating the exact "asymmetric consumers on one shared server" problem the split existed to prevent, since chadrock is meant to be the `pool` CLI's dedicated server only. **Corrected same day:** ARIA chat + Search Agent now share `qwen3.6-35b-a3b` with Hermes; `pi-coding` (both the chat-only tool and any bare `backend="pi-code"` session) moved to `backend=ridge` — Ridge is now the only backend any pi-coding-family agent runs on. Search Agent is currently **paused** (`enabled=false`) to reduce load on the shared server while its decode-speed profile is being watched. Full detail: `docs/ops/LOCAL_INFERENCE_TOPOLOGY.md`. **Done — no follow-up**, beyond watching qwen3.6-35b-a3b for contention symptoms now that ARIA chat shares it with Hermes.

0. **Coding-agent security + correctness fixes (2026-07-27/28).** Added a `ridge` LLM backend and `pi-coding-ridge` (a second, distinct pi-coding agent — inference on Ridge's RTX 3090, tools execute locally on corsair) and a `pool` backend (Poolside's own coding CLI, run against the local Laguna weights it's matched to). Found and fixed along the way: `subagent_profile` resolution was conflating the coding-session backend vocabulary (`claude_code`/`codex`/`pi-code`/`pool`) with the LLM-adapter vocabulary (`llamacpp`/`ridge`/…), which broke invoking `pi-coding-ridge` entirely (`ValueError: Unknown coding backend: ridge`) before it ever spawned. Added OS-level sandboxing (`bwrap`) for the in-process pi-code/pi-coding-ridge shell tool — network cut, credential dirs kernel-masked, fails closed if `bwrap` is missing — since those two backends run local, less-safety-trained models with real filesystem/shell access. Also added an `enabled` flag to the agent schema (used to pause Search Agent above) and disclosed a previously-silent backend swap (private conversations were forced onto `llamacpp` with no notice to the caller). 909 tests pass.

1. **Complexity routing — LIVE on the automated spawn path (2026-07-24).** Unpinned coding tasks spawned via `start_session()` (Hermes/MCP/TUI create) are classified into a tier and run on that tier's model — planning/design → Opus 4.8, scoped work → Sonnet 5, research → Sonnet 5; Sonnet is the floor, quota fallback → `pool` (local, was `pi-code`+`agentic` until the 2026-07-28 routing correction above). Code in `api/aria/agents/routing.py`, `api/aria/api/routes/routing.py`. **Desk-path auto-routing was tried and deliberately dropped (see CLAUDE.md → Desk path):** Claude Code is one-model-per-session, so the interactive REPL has no task to classify and can't be re-routed mid-session. **Done — no follow-up.**

2. **Pi-Flow parity — implemented (2026-07-25).** A global concurrency limiter + queue for coding sub-agents (`coding_max_concurrent_sessions`, `queued` state, `GET /coding/sessions/concurrency`), a `wait_for_session` join primitive, fan-out workflow actions (`parallel`/`map`/`code_session await`/`synthesize` with nested `{{steps.N.results.M}}` interpolation), prompt-cache metrics (`cache_hit_rate` on `/usage/*`), and declarative specialist profiles (`subagent_profile`). **Done — no follow-up.**

3. **Shared Services (S1–S5) — implemented & partially live (2026-07-18).** Foundation for the Coherence + Ontology Memory plans (see `vault/ProjectAria/Design/SHARED_SERVICES_DESIGN.md`). **Live now:** S1 memory HTTP API (`/api/v1/memory/{recall,store}`), S5 native BSON vector storage, S4 auth, S3 review surface (`/api/v1/shared/review`). **Implemented, flag-gated OFF:** S2 scan/reconcile worker (`shared_scan_enabled=false`). **Next (optional):** enable `shared_scan_enabled`; build the Ontology emitter on the S2 worker.

4. **Layer B2 — `aria-node` agent (implemented; live verification pending).** The fleet now spans machines: a remote node registers via `/api/v1/nodes/*`, captures its `claude-*` shells, and is driven back through the `shell_commands` queue via a host-aware `ShellService`; `start_coding_session(host=…)` + the watchdog/Ralph loop work over the wire. **Next: run `aria-node` on the MacBook and verify end-to-end** (Mac shells in the fleet, drive + a remote Ralph loop). See `MULTI_MACHINE_FLEET_DESIGN.md`.
5. **Integration testing with live services** — Signal, Research, and Workflow systems; Scheduler end-to-end against live MongoDB.
6. **Run the ABP migration** when ready to fully retire AgentBenchPlatform (utilities + cutover validation endpoint already exist).

See `BACKLOG.md` for the uncommitted product/research backlog, and `docs/archive/IMPLEMENTATION_PLAN.md` for the historical phase breakdown.

---

## Phase → Status Summary

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Foundation (API, LLM, Conversations) | COMPLETE |
| 2 | Memory System (short-term, long-term, embeddings) | COMPLETE |
| 3 | Tools & MCP (built-in tools, MCP client) | COMPLETE |
| 4 | Cloud LLM adapters (Anthropic, OpenAI, OpenRouter, fallback) | COMPLETE |
| 5 | Web UI (Next.js) | COMPLETE |
| 6 | Desktop widget + llama.cpp ROCm + voice I/O | COMPLETE |
| 7 | Signal integration | COMPLETE |
| 8 | Hardening (tokens, summaries, resilience, migrations, usage) | COMPLETE |
| 9 | Mode system (agent switching, keywords) | COMPLETE |
| 10 | Memory categories (extraction types) | COMPLETE |
| 11 | Research system (recursive web research) | COMPLETE |
| 12 | Coding sessions (subprocess, watchdog, review) | COMPLETE |
| 13 | Task runner (background tasks, recovery) | COMPLETE |
| 14 | Scheduler (cron tasks, reminders) | COMPLETE |
| 15 | Widget enhancements (mode switcher, voice, quick actions) | COMPLETE |
| 16 | Web UI dashboard (7-tab management console) | COMPLETE |
| 17 | CLI enhancements (20+ command groups) | COMPLETE |
| 18 | Workflow engine (multi-step orchestration) | COMPLETE |
| 19 | Security (audit, rate limiting, API auth) | COMPLETE |
| 20 | Retire ABP (migration, cutover validation) | COMPLETE |
| — | Agent safety subsystems (budget guard, checkpoints, e-stop, mail) | COMPLETE |
| — | Multi-runtime fleet, cost/health, pinning, search, routines, PWA, backups, computer-use | COMPLETE |
| — | Ralph loop (keep-a-session-going) + `aria tui` launcher | COMPLETE |
| — | Multi-machine cockpit — Layer A (remote cockpit) | COMPLETE |
| — | Multi-machine fleet — **Layer B2 (`aria-node` agent)** | IN PROGRESS |

Everything shipped through 2026-07-04 is COMPLETE; Layer B2 is the single in-progress item.

---

## Recent Decisions

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-28 | Ridge is the only backend any pi-coding-family agent runs on; laguna/chadrock backs none of them | Chadrock is reserved for `pool` only (single-consumer, vendor-validated `--parallel 1` profile); putting pi-coding there too recreated the shared-server contention the two-server split was meant to fix |
| 2026-07-28 | Search Agent paused (`enabled=false`), not deleted | It shares `qwen3.6-35b-a3b` with Hermes and ARIA chat; pausing removes a consumer while decode-perf on that server is still being watched, without losing the config if it's needed again |
| 2026-07-28 | Sandbox the shell tool with `bwrap`, enforced at the subprocess-spawn layer | Local models (laguna, qwen) now have real filesystem/shell access; OS-level containment (network cut, credential dirs kernel-masked) survives an allowlist bug in a way a Python string check can't, and can't be disabled by the model itself |
| 2026-07-23 | Sonnet 5 is the floor for routing; sub-Sonnet only on quota exhaustion | Cheap models cost more in rework than they save; the fallback exists to keep working when the subscription is out, not to save money |
| 2026-07-23 | Route inside `start_session()`, not per-caller | One chokepoint means Hermes, `/code`, the TUI, autostart and remote-node sessions all inherit routing with no per-surface work |
| 2026-07-23 | Detect-then-degrade for quota, not prediction | There is no API for Claude subscription quota; pane output is the only available signal |
| 2026-07-04 | One brain, many hands (Layer B2 is API-mediated + pull-based) | Keep memory/watchdog/Ralph loop centralized on corsair; remote hands register in rather than run a second brain |
| 2026-07-04 | Ship Layer A (remote cockpit) first | The TUI is already a thin pure-HTTP client, so a cross-machine cockpit is near-free ahead of the deeper node work |
| 2025-12-06 | Use official Anthropic and OpenAI SDKs | Better reliability and maintenance than a custom implementation |
| 2025-12-06 | Implement the fallback chain in the orchestrator | Automatic failover to cloud LLMs when local fails |
| 2025-12-06 | Tool sandboxing for filesystem; stdio transport for MCP | Limit file ops to allowed paths; simplest/most-compatible MCP transport |
| 2025-11-29 | MongoDB 8.2 + mongot instead of Atlas | Self-hosted vector search without an Atlas subscription |
| 2025-11-29 | Hybrid search with RRF fusion; LLM-based, background memory extraction | Best of lexical + semantic; more flexible than rules; don't block chat |

---

## Known Issues

| Issue | Severity | Status |
|-------|----------|--------|
| Some route files use `datetime.now(datetime.UTC)` (class-level) — works in Python 3.13+ but needs a shim in 3.12 | Low | Mitigated (test shim in place) |
