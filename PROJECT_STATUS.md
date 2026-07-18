# ARIA Project Status

**Last Updated:** 2026-07-04
**Updated By:** Claude Code

This is a living "current state" page. For the full shipped history (what shipped, when), see `CHANGELOG.md`.

---

## Current Focus / Next Actions

Active branch: `feature/multi-runtime-fleet`.

0. **Shared Services (S1–S5) — implemented & partially live (2026-07-18).** Foundation for the Coherence + Ontology Memory plans (see `vault/ProjectAria/Design/SHARED_SERVICES_DESIGN.md`). **Live now:** S1 memory HTTP API (`/api/v1/memory/{recall,store}`), S5 native BSON vector storage (subtype 9; all 1245 memories migrated, recall verified over native vectors), S4 auth (existing global key), S3 review surface (`/api/v1/shared/review`), Phase 0 ground-truth correction. **Implemented, flag-gated OFF:** S2 scan/reconcile worker (`shared_scan_enabled=false` — set true to activate machine-scan → memory). Code in `api/aria/shared/`, `api/aria/api/routes/memory_api.py`, `api/aria/scripts/`; tests in `test_shared_services.py`. **Next (optional):** enable `shared_scan_enabled`; build the Ontology emitter on the S2 worker.

1. **Layer B2 — `aria-node` agent (implemented; live verification pending).** The fleet now spans machines: a remote node registers via `/api/v1/nodes/*`, captures its `claude-*` shells, and is driven back through the `shell_commands` queue via a host-aware `ShellService`; `start_coding_session(host=…)` + the watchdog/Ralph loop work over the wire. Code + unit tests are in (`api/aria/nodes/`, `api/aria/node/`, `test_nodes.py`); **next: restart `aria-api`, run `aria-node` on the MacBook, and verify end-to-end** (Mac shells in the fleet with `host=bens-macbook-air`, drive + a remote Ralph loop). See `MULTI_MACHINE_FLEET_DESIGN.md`.
2. **Integration testing with live services** — Signal, Research, and Workflow systems; Scheduler end-to-end against live MongoDB.
3. **Run the ABP migration** when ready to fully retire AgentBenchPlatform (utilities + cutover validation endpoint already exist).

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
