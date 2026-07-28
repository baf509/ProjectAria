# ARIA Changelog

All notable changes to ARIA will be documented in this file.

## [2026-07-28] - Coding-session inspection tools for Hermes; qwen crash root-caused and mitigated; chadrock/GPU-memory monitoring closed

### Added
- **MCP coding-session inspection tools** (`mcp/server.py`): `get_coding_session`
  (structured single-session status/error/result_summary), `wait_for_coding_session`
  (blocks up to a timeout for a terminal state — the `wait_for_session` join
  primitive, previously only reachable from inside workflow fan-out, now callable
  directly), `get_coding_diff` (working-tree diff). New `GET
  /coding/sessions/{id}/wait` route (`routes/coding_sessions.py`) backs the second.
  Lets Hermes join/inspect a spawned coding sub-agent by id instead of polling raw
  terminal output or telling the human to run a CLI command themselves.
- **`gemma-aux`** (`infrastructure/gemma-aux/`, `:8104`) — a third local model
  server, Gemma 4 E4B (Q4_0 GGUF), CPU-only. Takes Hermes's ~16 "auxiliary"
  side-tasks (title generation, compression, curator, approval, triage_specifier,
  mcp, etc.) and both cron jobs (alert triage, stock scanner) off qwen entirely.
  Ships with `--reasoning off --reasoning-budget 0` (Gemma 4's reasoning mode
  fires stochastically and can silently consume an entire `max_tokens` budget —
  confirmed live, fixed before rollout) and `--kv-unified` (without it, `-c 8192`
  with `--parallel 2` silently halves to 4096 usable tokens per request).
- **`gpu_memory` and `chadrock` checks in `selfcheck.py`.** Chadrock had zero
  automated health monitoring despite carrying the same crash risk as qwen (both
  `restart: "no"` by design); added a `pool_api_url` probe. `gpu_memory` reads
  `/sys/class/drm/card0/device/mem_info_gtt_{used,total}` and alerts >90% — the
  real ground-truth signal for GPU memory pressure on this hardware (see Fixed).

### Fixed
- **qwen (`:8103`) crashed** (`vk::DeviceLostError`, "Not enough memory for
  command submission") under GPU command-submission contention with chadrock
  during simultaneous long-context checkpoint activity. Root cause: a tiny
  (~1 GiB) dedicated VRAM aperture used for GPU command submission — not the
  large GTT/system-RAM pool — sits near-permanently full on this hardware.
  Mitigated (unverified by repeat-crash testing): qwen's `-ctxcp` 32→10,
  `--cache-ram` 8192→2560; chadrock's own config untouched (kept at max context
  by design). qwen's `-c` also trimmed 131072→100000 to free real GTT headroom.
- **Docker/cgroup memory limits do not see GPU-offloaded memory on this
  unified-memory (Strix Halo) box** — confirmed empirically: `docker stats`
  showed ~5 GiB combined for chadrock+qwen while real GTT usage was ~97 GiB.
  `mem_limit` is a no-op safeguard for any GPU-offloaded (`-ngl 999`) server here;
  it only works for a genuinely CPU-only one (gemma-aux). Documented so it isn't
  rediscovered the hard way.
- **Hermes's two cron jobs** (`~/.hermes/cron/jobs.json`) were hardcoded to a
  slot-proxy port retired the same day as the two-server split — silently
  failing to connect on every run since. Repointed alongside the auxiliary-task
  move to gemma-aux.

### Notes
- Full incident writeup: `docs/ops/LOCAL_INFERENCE_TOPOLOGY.md` §10.
- Design-level consequences: `vault/ProjectAria/Design/COHERENCE_DESIGN.md` §5
  entries 24–28.
- `PROJECT_STATUS.md` item 000 tracks the one open follow-up: the checkpoint
  mitigation is a plausible hypothesis about the VRAM-aperture mechanism, not a
  proven fix — watch for a recurrence.

---

## [2026-07-25] - Pi-Flow parity: fan-out workflows, concurrency limiter, cache metrics

Closes the gaps between ARIA and [pi-flow](https://github.com/kky42/pi-flow) (multi-agent
orchestration). Plan: `vault/ProjectAria/Planning/PiFlow_Parity_Plan.md`.

### Added
- **Global concurrency limiter + queue for coding sub-agents** (`agents/session.py`).
  A session holds a "slot" while running; spawns beyond `coding_max_concurrent_sessions`
  (default 4; 0 = unbounded) sit in a new `queued` state and launch as slots free.
  `coding_queue_max` (default 64) hard-caps the wait queue (fail loud beyond it).
  CV-guarded, idempotent set-based slot release across every finalize path; the fast
  path (free slot → inline launch) is unchanged. Gauge at `GET /coding/sessions/concurrency`
  and merged into MCP `fleet_status`. Applies to CLI/shell/subprocess/remote **and**
  pi-code substrates.
- **`wait_for_session(session_id, timeout)`** — the join primitive: polls a session to a
  terminal state (restart-safe) and attaches the `TASK_DONE` result summary from the mailbox.
- **Fan-out workflow orchestration** (`workflows/engine.py`). New actions on top of the
  linear DAG: `parallel` (concurrent explicit sub-steps, bounded by `max_concurrent`),
  `map` (one `template` over a list, with `{{item}}`/`{{index}}` scope), `code_session`
  with `await:true` (join a spawned sub-agent and capture its result_summary), and
  `synthesize` (reduce prior results into one answer via an agent turn, optional
  backend/model — e.g. merge on Opus). Sub-step results nest under the group as
  `results`/`records`, addressable via `{{steps.N.results.M.path}}` (dotted paths now walk
  lists too). Exposed to Hermes as MCP `list_workflows`/`create_workflow`/`run_workflow`/
  `get_workflow_status`.
- **Prompt-cache metrics.** Anthropic adapter now captures `cache_read`/`cache_write`
  tokens; persisted by `UsageRepo.record` and aggregated into a weighted
  `cache_hit_rate` on `/usage/summary` and `/usage/by-model` (Pi-Flow `cacheHitRate` parity).
- **Declarative specialist profiles.** `start_coding_session(subagent_profile=<slug>)`
  resolves a `db.agents` row and applies its backend/model + `system_prompt` (role
  preamble); an explicit backend/model still wins. Threaded through the REST model + MCP
  `create_coding_session`.

### Notes
- Slot semantics: a slot is held while a session is actively running (has a live watch
  task), so long-lived Ralph-loop sessions occupy a slot for their whole life — size the
  cap accordingly. Visible-tmux sessions (which have no completion watcher) hold a slot
  until stopped. Queued sessions are lost on an `aria-api` restart (rare edge).
- 873 tests pass; new coverage in `test_coding_concurrency.py`, extended
  `test_workflow_engine.py`, `test_usage_repo.py`. After deploying, restart
  `aria-api` (limiter + engine) and `hermes-gateway.service` (new MCP tools).

## [2026-07-24] - Desk-path auto-routing reverted; routing is spawn-path only

### Changed
- **Complexity routing no longer intercepts the desk `claude` command.** Claude Code is one-model-per-session — you pick the model at launch or with `/model`, and no hook can swap it per prompt — so the sit-down interactive REPL has no single task to classify and can't be dynamically re-routed. The desk wrapper also fit the primary habit poorly: `claude --dangerously-skip-permissions` starts with a flag, which the wrapper bailed on, so routing never ran for it. Reverted `~/.bashrc` on corsair: `claude()` is once again just the saved-state per-directory shell attach (the aria-shells workflow that was working well). **Routing is unchanged on the automated spawn path** (`start_session()` — Hermes/MCP/TUI), where one task really is one session.
- The desk-path scripts (`scripts/aria-claude.sh`, `aria-route-task`, `aria-desk-install-mac`) are kept in the repo but no longer sourced. `aria-route-task` remains a useful manual client for `POST /api/v1/routing/classify`.

### Notes
- **MacBook still needs one manual edit:** the installer had added the wrapper to `~/.zshrc`. Remove the `. ~/.config/aria/aria-claude.sh` line there so bare `claude` is the real binary again (the export line is harmless to keep).

## [2026-07-23] - Complexity routing actually reaches its callers

### Fixed
- **`backend="claude_code"` silently disabled routing.** `start_session()` only routed when *both* `backend` and `model` were unset, so any caller naming the backend — which Hermes does as belt-and-suspenders, and the `route-coding-to-aria.py` hook text encourages — got the default model instead of the tier's. Since Hermes is the main non-desk spawner, this was most of the traffic. Routing now treats an explicit `model` as the only unconditional pin; an explicit backend suppresses it only when the router wouldn't have chosen that backend itself (`routing.is_routable_backend`: `codex`/`pi-code` = a real pin, `claude_code` = agreement). Verified live: `backend=claude_code` + a design prompt → `claude-opus-4-8`; `backend=codex` → unrouted, as before.

### Added
- **The desk wrapper is live on corsair.** `scripts/aria-claude.sh` was complete but had never been sourced, so `claude "<task>"` still launched on the default model. `~/.bashrc` already defined its own `claude()` (attach/spawn the persisted per-directory shell), so rather than clobber it, that function is renamed `_aria_claude_dir_session` and the routing wrapper — sourced after it — delegates the no-task case back via the new `ARIA_CLAUDE_BARE_COMMAND` hook. Net: `claude` alone behaves exactly as before, `claude "<task>"` routes, and `claude --no-aria …` (the old escape hatch, now also honoured by the routing wrapper) skips both. Backup at `~/.bashrc.bak-20260723-aria-routing`.
  - **`scripts/aria-desk-install-mac`**: emits a self-contained installer (both scripts + the API key embedded as heredocs) to be piped into `sh` from the Mac — `ssh ben@corsair-ai 'bash Development/ProjectAria/scripts/aria-desk-install-mac' | sh`. No scp, no repo checkout, and no long command line on the Mac side, because a long line pasted into a terminal can be split mid-command — and a split `scp remote:a remote:b` is a silent remote-to-remote overwrite (it clobbered `scripts/aria-route-task` once; recovered from the session transcript). Idempotent, and refuses to touch `~/.zshrc` if it already defines its own `claude()`.

## [2026-07-23] - Backend cleanup: context-1 off, Fireworks key removed

### Changed
- **context-1 is now explicitly disabled** via `context1_enabled` (`CONTEXT1_ENABLED=false` in `.env`). The container isn't part of the normal stack, so probing it produced a permanent DEGRADED. With the flag off, `is_backend_available("context1")` reports "disabled", the Search Agent tool isn't registered, and `/health/services` skips `:8081` entirely.
- **`FIREWORKS_API_KEY` removed from `.env`** — it had started returning 401 on every call. No agent was on Fireworks any more (`db.agents` has ARIA on `llamacpp` and Pi Coding Agent on `agentic`, both pointed at the local `laguna` server on `:8095`), so nothing regressed. The adapter and the `fireworks`/`glm` aliases stay; re-add a key to reactivate.
- **Routing's quota fallback now points at the local open-weights server** (`coding_routing_fallback_llm` `fireworks` → `agentic`, model → `default`) instead of Fireworks GLM 5.2, so the fallback still works with no key and no spend.

### Fixed
- **`/health/services` counted a rejected credential as healthy.** The probe used `ok = status_code < 500`, so Fireworks' `401` reported green while every call was failing. `401`/`403` are now unhealthy, and disabled/unconfigured backends are omitted from the result rather than counted against `healthy` (so the ratio reflects only what is meant to be up).

### Notes
- Cleared two zombie coding sessions (`1ad4d84d…` from 07-07, `c0d521dd…` from 07-22) left in `status: running` with no surviving pane. The watchdog had been logging `stuck: idle` for both every ~6s ever since — that was the entire content of the journal. There is still **no reaper for coding sessions whose substrate has vanished** (the C9 reaper covers idle *shells*).
- Complexity routing verified end-to-end for the first time: an unpinned `POST /api/v1/coding/sessions` with a design prompt spawned a real Claude Code session on `claude-opus-4-8` with the verdict persisted on the session doc.

## [2026-07-23] - Fix: restarting aria-api destroyed every watched shell

### Fixed
- **The tmux server lived inside `aria-api.service`'s cgroup**, so systemd's default `KillMode=control-group` killed it — and every watched `claude-*` session with it — on each `systemctl --user restart aria-api`. A tmux server inherits the cgroup of the client that first spawns it, so whenever ARIA's adopt/spawn path beat a login shell to it, the server was owned by the API. Symptom: you never got dropped back into an existing thread, because there was no session left to reattach to; the `claude()` wrapper correctly found nothing live and spawned a fresh one.
  - Confirmed against the journal: the stop at `2026-07-21 23:10:15` froze ~10 sessions' `last_activity_at` at `23:10:17` (`claude-emu_fleet_monitor`, `claude-strategy-tenets`, `claude-macbook-pro`, `claude-scenarios`, `claude-sm8550`, `claude-lieutenant-lab`, `claude-campaigns`, `claude-Emulation`, `claude-emuDeviceConfig`); the stop at `21:52:51` on 07-23 took `claude-coding-{699f58fc,645c3e45,0e43dfc0}` and `claude-Development` at `:51`–`:54`.

### Added
- **`aria-tmux.service`** (`scripts/systemd/aria-tmux.service` + `scripts/aria-tmux-server`): a dedicated unit that owns the tmux server, ordered `Before=aria-api.service`. `Type=oneshot` + `RemainAfterExit=yes` — the server daemonises out of `ExecStart`, and RemainAfterExit is what stops systemd tearing the cgroup down behind it. aria-api now only ever connects as a *client*.
  - `exit-empty off` is load-bearing: at the tmux default (`on`) the server exits when its last session is destroyed, and the next caller would rebuild it in *their* cgroup — straight back into the bug.
- **`scripts/aria-claude-launch`**: resume-aware launch shim, now the default `shells_claude_launch_command`. When the workdir already has Claude Code history (`~/.claude/projects/<munged cwd>/*.jsonl`) it launches `claude --continue`, so a respawned session picks up its thread instead of coming back empty. Falls back to a fresh thread if `--continue` fails within 5s (corrupt history) rather than leaving a dead pane. Coding sub-agents are unaffected — they pass an explicit `launch_command`, which takes precedence.
- **`scripts/aria-tmux-cutover`**: one-time takeover (tmux can't migrate sessions between servers, so the old server must die once). Refuses to run inside tmux, since `tmux kill-server` would kill the pane running it partway through and leave aria-api stopped.

### Notes
- **Requires the one-time cutover** to take effect; it costs every currently-live session one last time. Run it detached:
  `systemd-run --user --collect --unit=aria-tmux-cutover /home/ben/Development/ProjectAria/scripts/aria-tmux-cutover`
- Pane processes were never the problem — tmux 3.4 already puts each pane in its own transient `tmux-spawn-*.scope`. Only the *server* was captured.
- Unrelated bug noticed while cleaning up: `DELETE /api/v1/shells/{name}` 500s when the tmux session is already gone (`tmux kill-session failed: can't find session`) instead of just marking the row stopped. `?purge=true` works. Not fixed here.

## [2026-07-23] - Complexity routing: coding tasks pick their own model

### Added
- **`ComplexityRouter`** (`api/aria/agents/routing.py`): classifies a coding task into a tier and picks the backend/model. Three stages, cheap-first — (1) a heuristic prefilter for unambiguous phrasing, (2) a Sonnet-class judge returning strict JSON, (3) an availability filter that demotes while the Claude quota is cooling down. Never raises: any failure degrades to the standard tier.
  - `deep` (planning / design / strategy / trade-offs) → `claude-opus-4-8`
  - `standard` (scoped implementation, bug fixes, tests) → `claude-sonnet-5`
  - `light` (research / information gathering) → `claude-sonnet-5`, and on the desk path the judge may **answer inline** so trivial lookups never spawn a session at all.
  - Sonnet is the floor for normal routing. The sub-Sonnet fallback (`pi-code` / Fireworks GLM 5.2) is reachable *only* via the quota cooldown.
- **`POST /api/v1/routing/classify`** (`api/aria/api/routes/routing.py`) plus `GET/POST/DELETE /api/v1/routing/availability[/cooldown]` — a thin cross-machine surface so a client on any host can ask "what model does this need?" without a local venv. Inherits the global X-API-Key auth.
- **Desk-path wrapper** (`scripts/aria-claude.sh` + `scripts/aria-route-task`): source the `.sh` from `~/.bashrc` (corsair) or `~/.zshrc` (MacBook) and typing `claude "<task>"` routes the task, then launches Claude Code on the chosen model inside a `claude-*` tmux session — which both hosts already auto-adopt into the fleet (tmux hook on corsair, `aria-node`'s capture loop on the MacBook). Identical on both; only the resolved API URL differs (same `~/.config/aria/hosts` convention as the TUI). `aria-route-task` is pure stdlib Python 3, no venv needed.
- **Quota cooldown state** (`model_availability` collection): ARIA can't query the Claude subscription quota — there's no API for it — so the watchdog records a cooldown when it sees rate-limit/quota text in a `claude_code` session's pane output, and the router demotes new sessions until it expires. Detect-then-degrade, not prediction.
- Tests: `tests/test_complexity_routing.py` (52 tests).

### Changed
- `CodingSessionManager.start_session()` routes when **both** `backend` and `model` are unset; an explicit pin always wins. Every existing caller — Hermes MCP `create_coding_session`, `/code`, the TUI, the coding-mode autostart — inherits routing for free, including sessions on remote nodes (the chosen `--model` is already part of the launch string shipped to `aria-node`).
- Coding session docs and `GET /api/v1/coding/sessions` now carry `routing: {tier, why, confidence, source, judge_model, decided_at}` — so every surface can show *why* a session is on the model it's on. `None` means the caller pinned it.
- Coding backends now set `ARIA_MANAGED=1` in `CommandSpec.env` (`agents/backends/claude_code.py`, `codex.py`). The shell substrate launches agents under `bash -lc`, which sources the user's rc files — where the desk wrapper defines a `claude` function. Without the flag an ARIA-spawned session would re-enter the wrapper and recursively spawn more sessions.

### Notes
- The judge transport is configurable (`coding_routing_judge_transport`): `api` (one small Anthropic call, sub-second, fractions of a cent — right for the interactive desk path) or `cli` (`claude -p` via `ClaudeRunner`, burns the subscription instead of API tokens but costs several seconds of CLI startup — right for background/Hermes-initiated routing). Default `api`.
- **Requires an `aria-api` restart** for the new routes to serve.
- A shell you started by hand *outside* the wrapper still can't be routed — ARIA sees it only after `claude` is already running. Adoption and fleet visibility work as before; only the model choice is missed.

## [2026-07-18] - Shared Services (S1–S5) - Foundation for the Coherence & Ontology plans

### Added
- **S1 — Memory HTTP API** (`api/aria/api/routes/memory_api.py`): `POST /api/v1/memory/recall` and `POST /api/v1/memory/store` — a minimal cross-machine surface wrapping `LongTermMemory` (embeds server-side). Inherits the global X-API-Key auth.
- **S2 — Scan/Reconcile worker substrate** (`api/aria/shared/scan.py`): one periodic worker (`ScanReconcileWorker`) observes live machine state (`docker ps`, `systemctl`, `ss`) and feeds pluggable emitters. Ships with `MachineScanMemoryEmitter` (Coherence C2: machine-change → memory). Flag-gated OFF by default (`shared_scan_enabled`).
- **S3 — Freshness/ownership convention** (`api/aria/shared/ownership.py` `merge_owned`) + **review surface** (`api/aria/shared/review.py`, `GET/POST /api/v1/shared/review`): worker writes only worker-owned fields; human-curated conflicts are flagged for review, never clobbered.
- **S5 — Native vector storage**: embeddings now stored as MongoDB's native BSON vector (Binary **subtype 9**, float32) via `Binary.from_vector`. Backfill script `aria/scripts/migrate_embeddings_vector_subtype9.py` (idempotent; migrated 1245 existing docs, no re-embedding). Vector-search failures now log loudly instead of silently degrading recall to lexical-only.
- Tests: `tests/test_shared_services.py` (9 tests).

### Changed
- `memory/long_term.py`: `embedding_to_binary`/`binary_to_embedding` use subtype 9, with backward-compatible decode of legacy subtype-0 docs.
- **S4 — Auth:** confirmed the existing global `api_key_middleware` already protects all non-health endpoints; the new memory/store + shared routes inherit it (no separate write-auth needed).
- Design docs (`SHARED_SERVICES_DESIGN.md`, `ONTOLOGY_MEMORY_DESIGN.md`, `COHERENCE_DESIGN.md`) now live in the Obsidian vault under `ProjectAria/Design/`.

Format:
```
## [Date] - Phase X - [Summary]

### Added
- New features

### Changed
- Changes to existing features

### Fixed
- Bug fixes

### Removed
- Removed features

### Notes
- Important notes for future work
```

## [2026-07-07] - Multi-machine fleet: deployed live + robustness fixes

### Added
- **Deployed the fleet across machines.** `aria-node` runs as a persistent launchd agent on the MacBook Pro (`bens-macbook-pro`), registered with corsair over the tailnet. Verified live: capture + drive of Mac tmux shells, and a **Claude Code session in a Mac project driven end-to-end from corsair** (send instruction → the Mac's claude submits + responds → screen returns). This is the "talk to the MCP → drive a Claude Code instance on the Mac" workflow.

### Fixed
- **Reliable TUI submit** (`shells/tmux.py`): send the text and the submit `Enter` as separate `send-keys` calls — combined, TUIs using bracketed paste (Claude Code) absorb the Enter, so the text lands in the input box but never submits.
- **Node keepalive** (`aria/node/agent.py`): re-assert a live-but-idle shell as active each cycle (throttled) so an idle session never gets stuck `stopped` centrally and refuses input; plus **register-with-retry** so the node never crash-loops if the API is momentarily unreachable.
- **Remote `send_input` tolerates status flap** (`shells/service.py`): the owning node is authoritative on liveness, so remote input dispatches regardless of the cached (occasionally-flapping) `stopped` status; only local shells honor the stopped row.

## [2026-07-06] - Multi-machine fleet: Layer B2 (`aria-node` agent)

### Added
- **The fleet now spans machines.** A remote `aria-node` agent registers its host with the central brain, captures its local `claude-*` tmux shells (pushing events + snapshots over the API), and long-polls a command queue to be driven back — so a MacBook's shells and coding sessions appear in, and are drivable from, the one corsair fleet. One brain (corsair Mongo/memory), many hands.
  - **Central:** `api/aria/nodes/` (`NodeService`, `models`, `commands` queue) + `POST/GET /api/v1/nodes/*` (register, heartbeat, events, snapshot, command long-poll + result, list); `nodes` + `shell_commands` collections (with a TTL) in migrations; `local_node_id` + node timeouts in config; `get_node_service` DI.
  - **Host-aware `ShellService`:** `register_shell`/`insert_events_batch` accept a `host` override; `send_input`/`current_screen`/`session_alive`/`kill_shell` dispatch by the shell's host — **local → tmux (unchanged, zero regression); remote → the node command queue** (`_dispatch`/`_remote_command`). The reconciler skips remote shells. Reads (`fleet_overview`, scrollback) were already host-agnostic.
  - **Remote coding sessions + Ralph over the wire:** `start_coding_session(host=<node>)` runs the session on that node (it creates the `claude-coding-*` shell locally); `host`/`node_id` on `coding_sessions`; the watchdog + **Ralph loop drive remote sessions for free** through the host-aware manager. MCP gains `list_nodes` + `create_coding_session(host=…)`; the TUI fleet HOST column now shows remote coding sessions.
  - **The node agent** (`aria/node/`, run via `python -m aria.node` / `scripts/aria-node`, + a macOS launchd plist): outbound-only, depends on httpx + the local tmux driver (no Mongo), so it runs on a MacBook against corsair over the tailnet with the shared `X-API-Key`.
  - Tests: `api/tests/test_nodes.py` (command queue, NodeService ingest/registry, ShellService local-vs-remote dispatch, remote-session routing, node-agent handlers). Full suite 755 green. Live loopback verification pending an `aria-api` restart (loads the new routes/migrations).

## [2026-07-04] - Telegram removal + documentation streamline

### Removed
- **Telegram integration deleted as dead code.** After the 2026-06 move to the Hermes/MCP alert queue, the Telegram bot was disused. Removed `api/aria/telegram/` (bot + handler), the `/api/v1/telegram` routes, `get_telegram_handler` + startup/shutdown wiring in `main.py`/`deps.py`, all `telegram_*` settings in `config.py`, the `set_telegram_bot` no-op in `NotificationService`, and the wizard's Telegram section. Full suite green (732). Signal is unaffected.

### Changed
- **Documentation streamlined** to reduce overlap/drift (from a 15-doc root):
  - **Archived** 5 historical artifacts to `docs/archive/` (with an index): `IMPLEMENTATION_PLAN.md` (Signal-centric ABP→ARIA plan), `SHELLS_DESIGN.md` + `SHELLS_CHAT_TRANSCRIPT.md` + `ARIA_SHELLS_MERGE_PLAN.md` (post-merge shells design/history), `REVIEW_SUMMARY.md` (superseded review snapshot).
  - **Merged** `IDEAS.md` + `FUTURE_INVESTIGATIONS.md` → one **`BACKLOG.md`** (shipped items — Dream Cycle, Ambient Awareness, proactive behavior — pruned).
  - **Slimmed** `PROJECT_STATUS.md` (612→~90 lines) to a living status page; `CHANGELOG.md` is now the single source of shipped history. Rewrote `GETTING_STARTED.md` (~580→~330) correct + non-duplicative (native-systemd API, three-server LLM topology, no `:8080`/`:8000`).
  - **Scrubbed** stale facts: `:8000`→`:8200` (ui/README, SPEC, wizard), Signal/Telegram-as-live-channel lines (README, ARCHITECTURE), Pi-Coding-on-llama.cpp framing (ARCHITECTURE), + a living-truth banner on `SPECIFICATION.md`.
  - Added a **Documentation Map** (source-of-truth per doc) to `README.md`.

## [2026-07-04] - Multi-machine cockpit: design doc + Layer A (remote cockpit)

### Added
- **`MULTI_MACHINE_FLEET_DESIGN.md`** — design doc for making the TUI a cross-machine cockpit and letting the fleet span corsair-ai (the brain) and the MacBook (iOS/Xcode work). Two layers: **A** remote cockpit (native macOS TUI build + `aria tui --host` profiles + a fleet HOST column — the TUI is already a thin pure-HTTP client, so this is near-free), and **B2** an API-mediated, pull-based `aria-node` agent that registers the Mac's tmux shells/coding sessions into the single corsair brain and is driven back through a `shell_commands` queue via a host-aware `ShellService._dispatch`. Principle: one brain (corsair Mongo/memory), many hands; the watchdog + Ralph loop inherit remote sessions for free.
- **Layer A — remote cockpit (implemented).** The Go TUI can now run natively on any machine and point at a remote ARIA over the tailnet:
  - `tui/Makefile` with `build` / `build-darwin` (Apple-Silicon, `CGO_ENABLED=0`) / `build-linux` / `install` — verified the macOS cross-compile produces a working `Mach-O arm64` binary with no code changes.
  - **Host profiles**: `aria-tui --host <name|host:port|url>` resolves against a dependency-free `~/.config/aria/hosts` file (`$ARIA_HOSTS` override); precedence is flag → `ARIA_API_URL` env/.env → `default` profile → `http://localhost:8200`. `aria tui` (CLI) forwards extra args through to the binary. Covered by `tui/main_test.go`.
  - **`.env` fallback chain** in `tui/main.go`: `$ARIA_ENV` → `~/.config/aria/env` → the repo `.env`, so a MacBook needs no repo checkout to configure the cockpit.
  - **Fleet HOST column**: `host` added to `ShellOverviewItem` + `/shells/overview` (from the already-stamped `Shell.host`) and to the Go `Shell` struct; the TUI fleet view renders a compact HOST column (blank for coding sessions until Layer B2 adds session host).

## [2026-07-03] - Ralph loop (keep-a-session-going) + `aria tui` launcher

### Added
- **Ralph loop — opt-in, per-session "keep it going".** A coding session carrying a `loop_config` is nudged forward by the watchdog whenever it idles at its prompt (re-checking killswitch/e-stop **every nudge**) until it emits the done token (`coding_loop_done_regex`, default `RALPH_DONE`) or hits `max_nudges`/`deadline_minutes`. Absent `loop_config` = one-shot (unchanged; not default). Reuses the existing safety net (killswitch, e-stop, spend-cap, context-budget) and, because it drives through `session_manager.send_input`, works for any substrate.
  - New: `POST /api/v1/coding/sessions/{id}/loop {enabled, …overrides}` toggle; `loop_config` + bookkeeping on the `coding_sessions` doc + `set_loop_config()` (`agents/session.py`); `_maybe_nudge`/`_end_loop` in `agents/watchdog.py`; `loop_enabled` on `CodingSessionResponse`; `coding_loop_*` settings in `config.py`.
  - Surfaces: MCP `set_coding_loop` tool + `create_coding_session(loop=true)`; `start_coding_session` `loop` param; TUI toggle with `l` on both the **session** screen and the **fleet** screen (`⟳` marks looping sessions).
  - Tests: `api/tests/test_coding_loop.py` (normalize/toggle + watchdog nudge/done/cap/deadline/e-stop/killswitch/debounce).
- **`aria tui`** — the Python CLI now launches the Go TUI (resolves the binary from `$ARIA_TUI_BIN` / the repo `tui/` dir / PATH; `--build` to rebuild; extra args pass through).

## [2026-06-28] - Multi-runtime fleet, cost/health, model pinning, search, routines, PWA, backups, and computer-use

### Added
- **qwen-agentic addressable as backend `agentic`** — a 3rd coresident local llama.cpp server (Qwen3.6-27B, `:8093`, `agentic_url`) is now reachable as backend `"agentic"`. Local topology is now `llamacpp`/qwen-chat (`:8092`), `agentic`/qwen-agentic (`:8093`), `context1` (`:8081`) — all coresident on the APU. Aliases: `agentic`/`qwen-agentic`, plus `fireworks`/`glm` for Fireworks GLM 5.2.
- **pi-code as a first-class coding-session backend** — `start_coding_session(backend="pi-code", llm=<fireworks|agentic|llamacpp>, model=<id>)` runs ARIA's own agentic loop as a supervised, spawnable coding session alongside `claude_code` and `codex`. The LLM/model is **pinned** for the session and it inherits the watchdog + e-stop/killswitch gates. The three deployable runtimes are now Claude Code, pi-code+GLM (Fireworks), and pi-code+local-qwen — one unified `start_coding_session` path. Orchestrator default model is Fireworks GLM 5.2.
- **Cost accounting + spend circuit-breaker** — new `llm/pricing.py` price table (local backends + "default" = $0; cloud models priced; unknown cloud → conservative default). New endpoints `GET /usage/cost`, `/usage/by-session`, `/usage/by-conversation`; `/usage/by-model` now includes cost. Usage records now carry `backend` + `session_id`. A spend circuit-breaker (`spend_cap_usd_per_hour`, 0=off) trips the global e-stop (and escalates) when the last hour's priced spend exceeds the cap; the rate-limit watchdog also now watches fireworks.
- **`GET /health/services`** — concurrently probes all backing services (mongod, mongot, qwen-chat, qwen-agentic, context-1, embeddings, tts, stt, fireworks) returning per-service `{name, ok, latency_ms, detail}`. context-1/fireworks also added to the deep `/health` and `/health/llm` coverage.
- **Model pinning + routing** — `/model <backend> [<model-id>]` pins a conversation to a specific model (ids with `/` preserved); `/model` shows the current pin; `/model auto` unpins. An explicit pin is now **strict/fallback-free**. `/route <task>` applies an advisory heuristic suggestion as a pin you can override.
- **TUI fleet/health/search screens** — Fleet view (`f`: all coding sessions + watched shells with backend/model, status, idle, tokens + $cost), Health view (`h`: per-service status from `/health/services`), Search view (`s`: runs the search agent). Binary rebuilt.
- **Search agent easily invokable** — `/search <query>` (runs the context-1 search agent inline, shows ranked documents), `aria search <query>` CLI command, and the TUI search screen.
- **Scheduled autonomous routines** — the scheduler gained an `autopilot` action: schedule an autonomous goal (e.g. "every morning, triage my repos") that decomposes + executes via the autopilot service on a local-time cadence.
- **`browse_page` tool** — a built-in tool that fetches a URL and returns readable text (title + main content), following redirects (allowlisted).
- **Full computer-use via Playwright MCP** — ARIA drives a real headless browser (23 `browser_*` tools: navigate, accessibility snapshot, click/type by ref, screenshot, evaluate JS, tabs, network). Enabled via `tool_allowed_prefixes` (`browser_*` passes the allowlist) and a `"*"` wildcard in agent `enabled_tools` (one `browser_*` entry enables the family; the ARIA + pi-code agents have it). See the README "Computer Use (Playwright)" setup section.
- **Installable PWA** — the Next.js web UI now ships a manifest, service worker (network-first, bypasses `/api` + SSE), icons, and is installable.
- **Automated backups** — `scripts/aria-backup.sh` (mongodump of the `aria` DB via the mongod container + SOUL/journals/skills, with rotation), installed as a daily systemd user timer (`aria-backup.timer` @ 03:30). Restore is documented via mongorestore.
- **`/forget <query>`** — removes the single best-matching long-term memory (reviewable, one at a time).

### Changed
- The default ARIA orchestrator model remains Fireworks GLM 5.2, but conversations can now be pinned to any backend/model strictly (no fallback) via `/model`.
- `/models` (a.k.a. `/backends`) now lists `agentic` (qwen-agentic) alongside the other backends.
- The rate-limit / spend watchdog now also covers the `fireworks` backend.

### Notes
- Conversation branching/export already existed; this batch adds `/forget` for single-memory removal.

## [2026-06-26] - Absorb aria-shells; GLM 5.2; unified sub-agents; self-healing alerts

### Added
- **Fireworks LLM backend** (`llm/fireworks.py`) serving **GLM 5.2**; ARIA orchestrator + Pi Coding Agent switched to it (hard-pinned). `FIREWORKS_API_KEY` / `fireworks_base_url` in config.
- **Watched-shells / fleet subsystem** absorbed from the standalone `aria-shells` service (`api/aria/shells/`): auto-adopt via tmux hook + `ShellAdoptWorker`, capture, snapshot, extraction, `prune`, `selfcheck`, weekly `report`, project `harvest`. `fleet_overview`/`current_screen`/`send_input(wait_ms)`; routes under `/api/v1/shells`.
- **MCP server** (`mcp/server.py`) exposing ~31 tools to the Hermes agent — fleet, chat (ARIA orchestrator), conversations, agents, memory, coding sub-agents, projects/tasks, alerts.
- **Planning** projects fed by both the LLM `TaskExtractor` and the deterministic `ProjectHarvestWorker`; `/api/v1/todos` + `/projects/{id|slug}` (slug-or-id).
- **Alert queue** (`/api/v1/alerts`) + **Hermes self-healing triage**: on each alert Hermes spawns a diagnostic coding sub-agent, relays a root-cause + proposed fix to Signal, and applies on `APPLY` (verified end-to-end).
- **TUI** shows watched shells as a labeled sidebar group (with awaiting-input highlighting).
- Local LLMs **context-1** (`:8081`) and qwen **27B** (`:8093`) deployed alongside **qwen-chat 35B-A3B** (`:8092`) via `infrastructure/qwen-rocmfp4/`.

### Changed
- **ARIA is the single always-on service on `:8200`** (took over the port from the retired `aria-shells`; systemd drop-in). UI/CLI/TUI default to `:8200`.
- **Coding sub-agents run on the watched-shell substrate** (`coding_use_shell_substrate`): each is an interactive `claude-coding-*` shell — unified with the fleet, drivable via the same tools, visible in the TUI/MCP. Watchdog/checkpoint/review overlay retained.
- `llamacpp_url` → `:8092` (qwen-chat); `context1_url` → `:8081`. The old single llama.cpp on `:8080` retired (compose `legacy` profile).
- Notifications: ARIA no longer sends Signal/Telegram; `selfcheck` alerts **once per state-transition** (not hourly).

### Fixed
- `NotificationService.notify()` drops `coding:*` / `task` lifecycle events — they were polluting the alert queue and creating a triage feedback loop.
- TUI default port `:8000`→`:8200` and dotenv path; dashboard panel height clamp (header no longer scrolls off).
- Tolerant memory-extraction parser; per-call llama.cpp timeout; shells ANSI hardening.

### Removed
- ~187 GB of unused docker images + build cache (old llama.cpp variants).

### Notes
- `aria-shells` repo retained as reference only; its service is decommissioned.

## [2026-06-24] - Remove iOS client; ARIA is Linux-only

### Removed
- **`ios/` directory** — the native SwiftUI/iPadOS client (AriaMobile + AriaKit)
  is gone. ARIA is now exclusively a service that runs on a Linux machine,
  accessed via the Web UI, TUI, CLI, desktop widget, Signal, Telegram, and the
  REST API.
- **APNs push delivery** (`api/aria/shells/apns.py`) and all `apns_*` /
  `shells_apns_enabled` settings in `config.py` — Apple Push was only used by
  the iOS client.
- **Device registration API** (`api/aria/api/routes/devices.py`,
  `POST /api/v1/devices` and friends) plus its router registration in
  `main.py` and the `test_devices_routes.py` suite — device tokens existed
  solely to target APNs.

### Changed
- `shells/notifier.py` no longer attempts APNs delivery; idle-prompt alerts
  continue to route through Signal/Telegram via `NotificationService`.
- Dropped incidental iOS references from code comments
  (`core/orchestrator.py`, `shells/capture.py`), the task-extraction prompt,
  a planning unit test, and the project docs (README already had none).

### Notes
- The watched-shells subsystem itself is unchanged and fully retained — it is a
  Linux/tmux feature consumed by the Web UI dashboard, TUI, and CLI.
- The 2026-04-18 entry below is kept as historical record.

## [2026-04-18] - Native iOS / iPadOS client + shells/devices API

### Added
- **`ios/` subfolder** — native SwiftUI app targeting iOS 17+, Swift 6, Xcode 26.
  Project is generated via XcodeGen (`ios/project.yml`). See `ios/README.md`.
  - **AriaKit** local SPM package: `Sendable` Codable models, `URLSession`-based
    `AriaClient` with optional `X-API-Key`, `AsyncThrowingStream` SSE parser,
    Keychain helper. Typed API clients: Shells, Conversations, Memories,
    Health, Devices.
  - **AriaMobile** app target: TabView root on iPhone, `NavigationSplitView`
    on iPad. Full shells coverage — list/filter/search, create sheet, SwiftTerm
    live ANSI terminal on detail with 2000-event backfill + SSE tail + auto
    reconnect, input bar + key accessory bar (Esc/Tab/arrows/⏎/Ctrl-?/yes/no),
    kill session, edit tags, 3s snapshot view, noise filter toggle. Also
    chat with streaming SSE replies + steer, memory search (debounced
    hybrid search).
  - **SwiftTerm** SPM dependency for real VT100 rendering from day one.
- **`POST /api/v1/shells`** — create a detached tmux session and register it
  as a watched shell. Body: `{name, workdir?, launch_claude=true}`. Name is
  prefixed with the configured shells prefix (`claude-`) if not already.
  Launches Claude Code in the new session by default.
- **`DELETE /api/v1/shells/{name}`** — kill a tmux session and mark its
  shell row stopped.
- `ShellService.create_shell` / `kill_shell` and `TmuxClient.new_session`
  wrap these operations; existing tmux hooks (`session-created`) continue
  to wire up pipe-pane capture for new sessions.
- **`POST /api/v1/devices`** / **`DELETE /api/v1/devices/{token}`** — APNs
  device-token registration for mobile push. Stored in `devices` collection.
- **APNs idle alerts** (feature-flagged) — `IdleNotifier` now fans out to
  `send_apns_alert` when `shells_apns_enabled=true`. Transport itself is a
  stub (`api/aria/shells/apns.py`) with clear config-check + logging; flip
  the flag and drop in `httpx[http2]`/`aioapns` when ready to deliver.

### Changed
- `api/aria/main.py` registers the new `devices` router.
- `api/aria/config.py` gains `shells_apns_enabled`, `apns_team_id`,
  `apns_key_id`, `apns_bundle_id`, `apns_auth_key_path`, `apns_use_sandbox`.

### Notes
- No change needed to the existing shells SSE payload: `ShellEvent` was
  already serialized via `model_dump_json()` which includes `text_raw`, so
  SwiftTerm gets raw ANSI from the existing `/shells/{name}/stream` route.
- Route tests: `tests/test_shells_routes.py` (+6 cases) and
  `tests/test_devices_routes.py` (+5 cases), all green.

## [2026-04-15] - Local Agentic Search (Chroma context-1)

### Added
- **context-1 LLM backend** (`api/aria/llm/context1.py`) — new "context1" backend
  registered in `llm/manager.py`, pointing at a second llama.cpp instance that
  serves the chromadb/context-1 20B GGUF
  (https://huggingface.co/ryancook/chromadb-context-1-gguf). Configured via
  `context1_url`, `context1_model`, `context1_max_iterations`, `context1_max_docs`,
  and `context1_fs_allowed_roots` in `config.py`.
- **Search Agent tool** (`api/aria/tools/builtin/search_agent.py`) — agentic
  observe/reason/act retrieval loop driven by context-1. Exposes six tools to
  the model: `memory_search` (hybrid vector+BM25 over `memories`), `web_search`
  + `web_read` (existing search provider + WebTool), `fs_grep` + `fs_read`
  (ripgrep + bounded file reads over allowed roots), `prune`, and `finalize`.
  Returns a ranked list of documents with stable `mem:`/`web:`/`file:` ids.
  Registered at startup when the context1 backend is available.
- **Search Agent profile** (`slug=search-agent`, seeded in `db/migrations.py`) —
  named agent profile with `mode_category=research`, `backend=context1`, and
  `enabled_tools=[search_agent, web, filesystem, deep_think]`.
- **Research service integration** (`api/aria/research/service.py`) — when the
  research run's backend is `context1`, the branch loop now gathers sources via
  `search_agent` (over memory + web + local files) instead of the web-only
  provider path. Falls back to the web provider if the tool errors.
- **Infrastructure service** (`infrastructure/docker-compose.yml`) — new
  `llamacpp-context1` service (profile `context1`) on port 8081 that reuses the
  existing ROCm llama.cpp build to serve the context-1 GGUF from
  `${LLAMACPP_MODELS_DIR}/context-1/`.

### Notes
- The GGUF must be downloaded once into `infrastructure/models/llm/context-1/`
  (e.g. `chromadb-context-1-Q4_K_M.gguf`). Start the service with
  `docker compose --profile context1 up -d llamacpp-context1`.
- The model does not have upstream tool-calling templates documented; the
  service launches with `--jinja` so llama.cpp's OpenAI-compatible tool-call
  path is active. Adjust `CONTEXT1_ARGS` if the template needs tuning.

## [2026-04-13] - Agent Safety Subsystems & Escalation

### Added
- **Context Budget Guard** (`api/aria/agents/budget_guard.py`) — monitors coding
  sessions for context-window exhaustion via heuristic signals (provider limit
  messages, Claude Code compaction notices, latency spikes, explicit mentions).
  Thresholds: WARN 75%, SOFT 85% (checkpoint + notify), HARD 92% (checkpoint +
  stop + suggest resume).
- **Session Checkpointing** (`api/aria/agents/checkpoint.py`) — persists coding
  session state (current task, modified files, branch, last commit, notes) to
  MongoDB so crashed agents can be resumed with full context.
- **Emergency Stop / Rate-Limit Watchdog** (`api/aria/agents/estop.py`) —
  MongoDB-backed global estop that freezes all agent activity on API rate
  limits or critical errors. Visible across processes, persists across restarts,
  auto-thaws when clear.
- **Inter-Agent Mail Protocol** (`api/aria/agents/mail.py`) — structured
  agent-to-agent messages (`TASK_DONE`, `HANDOFF`, `RESULT`, `ERROR`,
  `CHECKPOINT`) stored in MongoDB and polled by the orchestrator.
- **Tmux Agent Backend** (`api/aria/agents/backends/tmux.py`) — spawns coding
  agents in visible, color-coded tmux panes in a dedicated `aria-agents`
  session so the user can watch multiple agents work in parallel.
- **Escalation Protocol** (`api/aria/notifications/escalation.py`) — severity
  routing (CRITICAL/HIGH/MEDIUM/LOW) with auto-resolution attempts before user
  notification and auto-re-escalation of stale items.
- Broad test coverage across new and existing subsystems: agent mail, autopilot,
  awareness, budget guard, builtin tools, checkpoint, coding session, context
  builder, db models, dream service, embeddings, escalation, estop, killswitch,
  llm manager, mcp, orchestrator, orchestrator tool loop, short-term memory,
  steering, usage repo, watchdog.

### Changed
- Expanded `agents/watchdog.py` and `agents/session.py` to integrate budget
  guard, checkpoint, estop, and mail signals.
- Extended `dreams/service.py` and `prompts/dream_reflection.md` with deeper
  reflection flow.
- Wired new safety dependencies through `api/deps.py`, `main.py`, and DB
  migrations (`db/migrations.py`).

### Notes
- Inspired by Gas Town's context-budget-guard, checkpoint, rate-limit-watchdog,
  mail, and tiered escalation patterns.

---

## [2026-02-16] - Phase 6 - Voice I/O (TTS + STT)

### Added
- TTS microservice (`tts/`) running Qwen3-TTS 0.6B CustomVoice model on CPU
- `POST /v1/tts/synthesize` endpoint for speech synthesis (returns WAV audio)
- `GET /v1/tts/speakers` endpoint listing 9 available voice speakers
- `GET /v1/tts/health` endpoint for TTS service health checks
- API proxy routes forwarding TTS requests to the microservice
- Widget play button on assistant messages for reading responses aloud
- Docker Compose `tts` service on port 8002
- `TTS_URL` configuration in `.env.example` and API settings
- STT microservice (`stt/`) running whisper-large-v3-turbo via faster-whisper on CPU (int8)
- `POST /v1/stt/transcribe` endpoint accepting audio file upload, returns transcribed text
- `GET /v1/stt/health` endpoint for STT service health checks
- API proxy routes forwarding STT requests to the microservice
- Widget mic button now functional: click to record, click again to stop and transcribe
- Recording indicator (pulsing red) and transcribing state on mic button
- Docker Compose `stt` service on port 8003
- `STT_URL` configuration in `.env.example` and API settings

---

## [2025-12-28] - Phase 2 - Fix Memory Extraction Background Tasks

### Fixed
- **Memory extraction now uses FastAPI BackgroundTasks instead of asyncio.create_task()**
  - Orchestrator now accepts `background_tasks` parameter from API routes
  - Proper lifecycle management for background memory extraction tasks
  - Prevents memory extraction tasks from being cancelled prematurely
  - Fallback to asyncio.create_task for non-HTTP contexts (CLI, tests)
  - Location: `api/aria/core/orchestrator.py:280-309`

- **Manual memory extraction API now uses agent's LLM configuration**
  - Fixed `/api/v1/memories/extract/{conversation_id}` endpoint
  - Previously used hardcoded defaults (ollama/llama3.2:latest)
  - Now correctly looks up and uses the agent's configured LLM backend and model
  - Ensures extraction works with OpenRouter, Anthropic, OpenAI, etc.
  - Location: `api/aria/api/routes/memories.py:232-251`

- **Message sending routes now pass BackgroundTasks to orchestrator**
  - Updated `/api/v1/conversations/{id}/messages` endpoint
  - Passes BackgroundTasks to orchestrator for proper memory extraction scheduling
  - Works for both streaming and non-streaming modes
  - Location: `api/aria/api/routes/conversations.py:154-196`

### Changed
- Added logging to memory extraction with `[MEMORY EXTRACTION]` prefix for easier debugging
- Memory extraction errors now print to logs for troubleshooting

### Notes
- **Action Required**: Restart API container to apply these fixes
- These fixes resolve the issue where memories were not being automatically extracted from conversations
- Memory extraction will now work reliably with any LLM backend (Ollama, OpenRouter, Anthropic, OpenAI)

---

## [2025-12-27] - Phase 5 - Fix Missing UI API Client

### Fixed
- Created missing `ui/src/lib/api-client.ts` file
  - Implements complete API client for ARIA API
  - Methods for health check, conversations, agents, memories, tools, MCP
  - Streaming message support with Server-Sent Events
  - TypeScript types integration
- Resolves UI build errors ("Module not found: Can't resolve '@/lib/api-client'")

### Added
- Complete API client implementation with:
  - Health check endpoint
  - Conversation CRUD and streaming
  - Agent management
  - Memory operations and search
  - Tool listing and execution
  - MCP server management

### Notes
- This file was referenced in UI code but was missing from the repository
- Phase 5 Web UI can now build successfully

---

## [2025-12-27] - Infrastructure - MongoDB Community Search Update

### Changed
- Updated MongoDB Community Search (mongot) to version 0.55.0 (from 0.53.1)
  - `docker-compose.yml` - Updated mongot image version
  - `SPECIFICATION.md` - Updated documentation to reflect latest version
- Latest mongot version provides improved performance and bug fixes

### Notes
- Version 0.55.0 is the latest stable release of MongoDB Community Search
- No breaking changes from 0.53.1 to 0.55.0
- Existing mongot data volumes remain compatible

---

## [2025-12-27] - Phase 4 - OpenRouter Health Check Fix

### Fixed
- Added OpenRouter to health check endpoint (`api/aria/api/routes/health.py`)
  - OpenRouter now included in `/api/v1/health/llm` status checks
  - Completes OpenRouter integration (was missing from health check backends list)

---

## [2025-12-25] - Phase 4 - OpenRouter Support

### Added
- OpenRouter adapter (`api/aria/llm/openrouter.py`)
  - OpenAI-compatible API for unified access to multiple LLM providers
  - Supports models from OpenAI, Anthropic, Google, Meta, and more
  - Streaming support with proper message formatting
  - Tool use (function calling) support
  - Optional HTTP-Referer and X-Title headers for app rankings
  - Uses OpenAI SDK with custom base URL (https://openrouter.ai/api/v1)
- OpenRouter configuration
  - `OPENROUTER_API_KEY` environment variable
  - Added to `.env.example` with other API keys
  - Configuration in `api/aria/config.py`
  - Docker compose environment variable pass-through

### Changed
- Updated LLM manager (`api/aria/llm/manager.py`)
  - Added "openrouter" backend support
  - Backend availability check for OpenRouter
  - Error messages for missing OpenRouter API key
- Updated documentation
  - `CLAUDE.md` - Added OpenRouter to adapter list and configuration
  - `SPECIFICATION.md` - Added OpenRouter to cloud API options
  - `README.md` - Updated LLM agnostic description
  - Added API key security section to CLAUDE.md

### Notes
- OpenRouter reuses the OpenAI SDK (no additional dependencies)
- Model names in OpenRouter use provider prefixes (e.g., "openai/gpt-4")
- API keys stored in `.env` file (git-ignored for security)
- OpenRouter provides cost-effective access to multiple providers through one API

---

## [2025-12-25] - Documentation & Configuration - CLAUDE.md and Embedding Standardization

### Added
- `CLAUDE.md` - Comprehensive guide for Claude Code sessions
  - Quick start instructions (PROJECT_STATUS.md, CHANGELOG.md, SPECIFICATION.md)
  - Architecture overview (core flow, memory system, tools, LLM adapters)
  - MongoDB 8.2 + mongot setup details
  - Development commands (Docker, API, UI, CLI, Database)
  - Code patterns and conventions
  - Key files reference organized by layer
  - Configuration examples
  - Important gotchas and best practices
  - Current phase summary

### Changed
- **Embedding configuration standardized across entire codebase**
  - Model: Changed from `qwen3:8b` to `qwen3-embedding:0.6b`
  - Dimensions: Changed from 4096 to 1024
  - Updated files:
    - `.env.example` - Default embedding model and dimensions
    - `api/aria/config.py` - Configuration class defaults
    - `SPECIFICATION.md` - All architecture diagrams, code examples, and index definitions
    - `README.md` - Key design decisions
    - `CLAUDE.md` - All embedding references
- Improved clarity in embedding configuration
  - Comment: "Using qwen3-embedding:0.6b model with 1024-dimensional embeddings for optimal balance of quality and performance"
  - Note: docker-compose.yml and scripts/init-mongo.js were already correct at 1024 dims

### Fixed
- Configuration inconsistencies between environment files, code defaults, and documentation
- All vector search index definitions now consistently use 1024 dimensions
- All embedding service references now use correct model name

### Notes
- This standardization ensures vector search works correctly with actual embedding dimensions
- MongoDB vector index in `scripts/init-mongo.js` already used 1024 dims
- Docker compose defaults already used `qwen3-embedding:0.6b`
- Changes align runtime configuration with actual deployed setup

---

## [2025-12-06] - Phase 4 - Cloud LLM Adapters

### Added
- Anthropic/Claude adapter (`api/aria/llm/anthropic.py`)
  - Streaming support with proper message formatting
  - Tool use (function calling) support
  - Support for all Claude 3 models (Opus, Sonnet, Haiku)
  - System prompt handling
  - Error handling with proper error messages
- OpenAI adapter (`api/aria/llm/openai.py`)
  - Streaming support with chunk accumulation
  - Function calling support
  - Support for GPT-4, GPT-4 Turbo, GPT-3.5
  - Proper message role handling (including tool role)
  - Error handling
- LLM backend availability check (`api/aria/llm/manager.py`)
  - Check if API keys are configured
  - Verify SDK packages are installed
  - Helpful error messages for missing configuration
- LLM health endpoint (`api/aria/api/routes/health.py`)
  - `GET /api/v1/health/llm` - Check all LLM backend status
  - Returns availability and reason for each backend

### Changed
- Updated LLM manager (`api/aria/llm/manager.py`)
  - Register Anthropic adapter when API key present
  - Register OpenAI adapter when API key present
  - Lazy import of cloud adapters to avoid import errors
  - Validate API keys before creating adapters
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Added `_get_llm_with_fallback()` method
  - Automatic fallback to cloud LLMs on error
  - User notification when fallback is used
  - Support for configurable fallback conditions
- Updated configuration
  - API keys already present in config (ANTHROPIC_API_KEY, OPENAI_API_KEY)
  - Already in `.env.example` file

### Notes
- Cloud LLM packages (anthropic, openai) already in requirements.txt
- API keys must be set in environment variables
- Fallback chain configured per agent in agent.fallback_chain
- Fallback conditions: on_error, on_context_overflow (future)
- Cloud LLMs automatically used when primary LLM fails

---

## [2025-12-06] - Phase 3 - Tools & MCP

### Added
- Tool infrastructure (`api/aria/tools/`)
  - BaseTool abstract class with parameter validation
  - ToolRouter for tool registration and execution
  - ToolDefinition and ToolParameter models
  - ToolResult with status tracking and metadata
- Built-in tools (`api/aria/tools/builtin/`)
  - Filesystem tool: read/write files, list directories, manage files
  - Shell tool: execute commands with timeout and sandboxing
  - Web tool: HTTP GET requests with size limits
- MCP (Model Context Protocol) integration (`api/aria/tools/mcp/`)
  - MCP client with JSON-RPC 2.0 over stdio
  - MCP manager for multi-server lifecycle management
  - MCPToolWrapper for BaseTool compatibility
  - Server health tracking and tool registration
- Tool management API routes (`api/aria/api/routes/tools.py`)
  - List tools (with type filtering)
  - Get tool details
  - Execute tools directly
  - MCP server CRUD (add, remove, list)
  - List tools per MCP server
  - Tool statistics endpoint
- CLI tool commands
  - `aria tools list` - List available tools
  - `aria tools info <name>` - Show tool details
  - `aria tools execute <name> <args>` - Execute a tool
  - `aria mcp list` - List MCP servers
  - `aria mcp add <id> <command>` - Add MCP server
  - `aria mcp remove <id>` - Remove MCP server
  - `aria mcp tools <id>` - List server's tools

### Changed
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Added tool_router parameter
  - Tool definitions passed to LLM when tools enabled
  - Handle tool calls from LLM responses
  - Execute tools and save results to conversation
  - Tool results included in streaming response
- Updated main app (`api/aria/main.py`)
  - Initialize built-in tools on startup
  - Register tools with tool router
  - Shutdown MCP servers on app shutdown
  - Added tools routes
- Updated API dependencies (`api/aria/api/deps.py`)
  - Added get_tool_router() dependency
  - Added get_mcp_manager() dependency
  - Pass tool_router to orchestrator
- Updated CLI with tool and MCP management commands
- Updated `PROJECT_STATUS.md` to Phase 3

### Notes
- Tools must be explicitly enabled per agent (capabilities.tools_enabled)
- Agents can specify which tools they can use (enabled_tools list)
- Filesystem tool is sandboxed to allowed paths (default: user home)
- Shell commands can be filtered with allow/deny lists
- MCP servers communicate via stdio transport
- Tool execution has configurable timeout (default: 5 minutes)

---

## [2025-11-29] - Phase 2 - Memory System

### Added
- Embedding service (`api/aria/memory/embeddings.py`)
  - Ollama embeddings using Qwen3-8b (4096 dimensions)
  - Voyage AI fallback provider
  - Batch embedding support
- Short-term memory (`api/aria/memory/short_term.py`)
  - Current conversation context retrieval
  - Recent conversations context
  - Token budget management
- Long-term memory (`api/aria/memory/long_term.py`)
  - Vector search using MongoDB Atlas Vector Search
  - Lexical search using MongoDB Atlas Search (BM25)
  - Hybrid search with Reciprocal Rank Fusion (RRF)
  - Memory CRUD operations with automatic embedding generation
  - Access tracking and statistics
- Memory extraction pipeline (`api/aria/memory/extraction.py`)
  - LLM-based extraction from conversations
  - Batch message processing
  - Source tracking and confidence scoring
  - Manual extraction from arbitrary text
- Context builder (`api/aria/core/context.py`)
  - Memory injection into system prompts
  - Short-term + long-term memory integration
  - Relevance-based memory retrieval
- Memory API routes (`api/aria/api/routes/memories.py`)
  - List, create, get, update, delete memories
  - Hybrid search endpoint
  - Background extraction trigger
- CLI memory commands
  - `aria memories list` - List all memories
  - `aria memories search` - Search with hybrid search
  - `aria memories add` - Manually add memories
  - `aria memories extract` - Trigger extraction

### Changed
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Integrated context builder for memory-aware responses
  - Automatic memory extraction in background
  - Access tracking for retrieved memories
- Updated main app to include memory routes
- Updated CLI with memory management commands
- Updated `PROJECT_STATUS.md` to Phase 2

### Notes
- Requires MongoDB search indexes for vector and lexical search
- Embeddings require Ollama with embedding model (e.g., qwen3:8b)
- Memory extraction runs asynchronously to avoid blocking chat
- Hybrid search combines best of lexical and semantic search

## [2025-11-29] - Phase 1 - Core Implementation

### Added
- Docker infrastructure (`docker-compose.yml`, Dockerfile, `.env.example`)
- MongoDB initialization script with replica set and search indexes
- FastAPI application foundation
  - Main app with lifespan management (`api/aria/main.py`)
  - Configuration management (`api/aria/config.py`)
  - MongoDB connection layer (`api/aria/db/mongodb.py`)
  - Pydantic models for API (`api/aria/db/models.py`)
- LLM adapter layer
  - Base adapter interface (`api/aria/llm/base.py`)
  - Ollama adapter with streaming support (`api/aria/llm/ollama.py`)
  - LLM manager for backend selection (`api/aria/llm/manager.py`)
- Agent orchestrator (`api/aria/core/orchestrator.py`)
  - Message processing with streaming
  - Conversation context assembly
  - Response persistence
- API routes
  - Health check endpoint
  - Conversations CRUD with SSE streaming
  - Agents CRUD
- CLI client (`cli/aria_cli/main.py`)
  - Interactive chat mode
  - Conversation management commands
  - Agent listing

### Changed
- Updated `PROJECT_STATUS.md` to reflect Phase 1 progress
- All core Phase 1 checklist items marked complete

### Notes
- Phase 1 implementation complete, ready for testing
- Requires Ollama running locally for testing
- MongoDB 8.2 with mongot for vector search
- Testing infrastructure not yet implemented

## [Unreleased]

### Added
- Initial project specification (`SPECIFICATION.md`)
- Project status tracking (`PROJECT_STATUS.md`)
- This changelog

### Changed
- Updated MongoDB configuration to use Community Server 8.2 + mongot
- Switched from `mongodb-atlas-local` to separate `mongod` + `mongot` services
- Updated embedding dimension to 4096 (Qwen3-8b)
- Added hybrid search (BM25 + Vector) with RRF fusion for long-term memory

### Notes
- See `PROJECT_STATUS.md` for current checklist
- MongoDB 8.2 Vector Search is in Public Preview
- mongot image: `mongodb/mongodb-community-search:0.53.1`
- mongod image: `mongodb/mongodb-community-server:8.2.0-ubi9`

---

<!-- 
Template for new entries:

## [YYYY-MM-DD] - Phase X - [Summary]

### Added
- 

### Changed
- 

### Fixed
- 

### Notes
- 

-->
