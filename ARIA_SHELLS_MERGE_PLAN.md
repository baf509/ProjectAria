# Merge Plan — Fold `aria-shells` advances back into ProjectAria

Status: IMPLEMENTED + CUT OVER · Author: Claude (with Ben) · Date: 2026-06-24

> **Done (2026-06-24):** All five phases implemented on branch
> `feat/absorb-aria-shells` (full test suite green except 3 pre-existing docgen
> failures). Cutover executed: ProjectAria now serves :8200 (systemd drop-in),
> `aria-shells-api` stopped + disabled, capture/register/MCP symlinks repointed
> to ProjectAria, Hermes restarted (MCP reaches :8200, keys matched), UI rebuilt
> for :8200. Live-verified: 18 existing shells visible, a brand-new `claude-*`
> session auto-adopted + captured in 2s, alert enqueue→list→ack round-trip.
> Note: local llama.cpp (:8080) is down — pre-existing infra, surfaced by the
> new selfcheck worker (not caused by the merge).

## Goal

Replicate the entirety of `aria-shells`' functionality inside ProjectAria so
ProjectAria becomes the single always-on service, while `aria-shells` stays in
place untouched as the reference. Plus two forward-looking requirements Ben
added:

1. **Auto-adopt shells** — start Claude Code in any tmux shell and have the
   always-on ProjectAria discover + watch it automatically (no `aria shells new`).
2. **Signal via Hermes, not ProjectAria** — ProjectAria stops pushing Signal
   directly (avoids colliding with Hermes's signal-cli daemon). Alerts become an
   MCP-exposed queue that the Hermes agent pulls and relays.

## Why this is a port, not a rewrite

The two repos share a near-identical common ancestor:

- `db/`, `notifications/`, `security/` — byte-identical.
- `core/`, `llm/`, `memory/`, `signal/` — differ by exactly **one file each**.
- ProjectAria is the **superset of subsystems** (28 routes, UI/widget/cli/tui,
  plus autopilot/dreams/telegram/agents/planning). `aria-shells` only advanced
  the **shells slice** + added an MCP/Hermes bridge ProjectAria lacks.

The divergence is concentrated and **overwhelmingly additive** (verified by
file-level diff). Only one shared file is a true conflicting merge
(`signal/service.py`), and the design decisions below dissolve even that.

## Resolved design decisions

| # | Decision | Resolution |
|---|---|---|
| D1 | Signal transport (native :8090 vs REST :8088 daemon) | **Moot — drop direct Signal from ProjectAria.** Do NOT port `aria-shells`' `signal/service.py` rewrite. Route alerts to an MCP-exposed queue; Hermes relays via its own Signal daemon. |
| D2 | MCP route shape mismatch | **Update the MCP server to ProjectAria's native paths** (`/todos`, `/projects/{id}` + slug lookup). Do not add legacy alias routes. |
| D3 | Projects/tasks vs harvesting | **One schema, two extractors.** ProjectAria `planning/` owns `db.projects` + `db.tasks`; `harvest.py` becomes a deterministic extractor feeding the same collection alongside the existing LLM `TaskExtractor`. |
| D4 | Listen port / Hermes wiring | **ProjectAria takes over `:8200`** (inherits Hermes's existing MCP/API wiring; no Hermes-side base-URL change). aria-shells and ProjectAria therefore cannot both bind `:8200` — cutover is atomic (stop `aria-shells-api.service`, start ProjectAria on `:8200`); during dev, run ProjectAria on a temp port. Update ProjectAria UI/widget/CLI base URLs + systemd unit from `:8000` → `:8200`. |
| D5 | Alert delivery to Hermes | **Hermes pulls via MCP.** ProjectAria writes alerts to a queue; Hermes polls `list_alerts()` on its heartbeat and relays + acks. No inbound webhook on either side. |
| D6 | APNs / iOS push | **Drop it.** Omit `apns.py`, the APNs block in `notifier.py`, and all `apns_*` config. Consistent with CLAUDE.md ("no native mobile/iOS client"). |
| D7 | Telegram channel | **Drop it.** Signal-via-Hermes (MCP pull) is the sole notification path. Remove Telegram from `NotificationService.notify()`. |

---

## Phase A — Shared core (mechanical, low risk)

Straight copies (pure-additive, confirmed):

- `llm/llamacpp.py` — add `llamacpp_timeout_seconds` wall-clock cap on the
  AsyncOpenAI client (prevents indefinite hangs).
- `memory/extraction.py` — add tolerant `_parse_memory_array()` (strips
  `<think>` blocks / markdown fences, falls back to first `[...]` span; returns
  `None` instead of raising). Repoint the two call sites.
- `core/orchestrator.py` — trivial comment change (CLI→iOS renderer note). Skip
  or apply; no behavioral impact.

**Do NOT port `signal/service.py`** (see D1). ProjectAria keeps its current
`SignalService` for now, but it will be bypassed by the new alert routing
(Phase E).

Config keys added in this phase:
- `llamacpp_timeout_seconds: int = 120`
- `shells_extraction_timeout_seconds: int = 240`

## Phase B — Shells subsystem

### New files (drop in to `api/aria/shells/`)
| File | Lines | Purpose |
|---|---|---|
| `prune.py` | 156 | `ShellEventsPruneWorker` — per-shell token-budget scrollback retention; never touches derived data (memories/projects/tasks). |
| `selfcheck.py` | 156 | `SelfCheckWorker` — periodic DB/LLM/embeddings/extraction health check → alert (with cooldown). |
| `report.py` | 119 | `HeartbeatReportWorker` — weekly "all good" summary (so silence is never ambiguous). |
| `claude_trust.py` | 118 | Pre-seed Claude Code folder-trust so a new shell doesn't block on the trust dialog. |
| `harvest.py` | 312 | `ProjectHarvestWorker` — see Phase D (merged into planning). |

**Dropped (D6):** `apns.py` is NOT ported.

### Additive merges into existing files
- `shells/service.py` (+109 lines): new `fleet_overview()`, `current_screen()`;
  `send_input()` gains `wait_ms` and returns `(line, screen)`; `launch_session()`
  calls `ensure_trusted(workdir)` when `shells_claude_autotrust`.
- `shells/extraction.py`: stale-cursor self-heal + `asyncio.wait_for` timeout per
  extraction tick.
- `shells/ansi.py`: 3 more escape-sequence patterns (DCS/SOS/PM/APC, charset
  designation, remaining two-byte escapes) — fixes TUI redraw leakage.
- `shells/models.py`: `ShellInput.wait_ms`, `ShellInputResponse.screen`, new
  `ShellOverviewItem` / `ShellOverviewResponse`.
- `shells/notifier.py`: port **without** the APNs block (D6). Alerts flow through
  `NotificationService` → alert queue (Phase E).
- `shells/capture.py`: comment only.
- `api/routes/shells.py`: add `GET /shells/overview` (→ `fleet_overview`) and
  `GET /shells/{name}/screen` (→ `current_screen`); enrich `/shells/search`.

### Worker wiring (`main.py` lifespan)
ProjectAria already starts `SnapshotWorker`, `IdleNotifier` (gated),
`ShellExtractionWorker`. Add, each gated by config and stored on `app.state`
with graceful `.stop()` on shutdown:
- `ShellEventsPruneWorker` (`shells_prune_enabled`)
- `ProjectHarvestWorker` (`projects_harvest_enabled`) — Phase D
- `SelfCheckWorker` (`selfcheck_enabled`)
- `HeartbeatReportWorker` (`report_enabled`)

Config keys added:
```
shells_prune_enabled=True, shells_event_token_budget=150000, shells_prune_interval_hours=6
projects_harvest_enabled=True, projects_harvest_interval_minutes=30
selfcheck_enabled=True, selfcheck_interval_minutes=10, selfcheck_alert_cooldown_minutes=60
report_enabled=True, report_weekday=6, report_hour=9
shells_claude_autotrust=True, shells_claude_config_path=""
# apns_* NOT added (D6)
```

## Phase C — Planning / harvest unification (best of both worlds)

Principle: **one projects registry + one to-do list, fed by two complementary
extractors** (LLM-ambient `TaskExtractor` + deterministic `harvest`).

1. **Extend `planning/models.py` `Project`** with optional derived fields:
   `path`, `last_activity_at`, `activity_status` (`active`/`idle`),
   `sources`, `git`, `harvested_at`. (`relevant_paths`, `next_steps`, `slug`,
   `summary`, `tags` already exist.)
2. **Separate the two status axes** (the key fix): `status` stays the
   human/dashboard lifecycle (`active`/`paused`/`archived`); harvest writes
   activity only to `activity_status`. Harvest already uses `$setOnInsert` for
   human-editable fields and `$set` for derived — preserve that split so the
   dashboard stays authoritative.
3. **Move/port `harvest.py`** (under `planning/` or kept in `shells/`) and make
   `ProjectHarvestWorker` upsert into `db.projects` via/compatible-with
   `PlanningService` (slug-keyed; Mongo assigns `_id`). No second collection.
4. **Tasks**: nothing to port — ProjectAria's `db.tasks` lifecycle + content-hash
   dedup + `TaskSource(type="shell")` is a strict superset of aria-shells' simple
   list. `tasks/runner.py` is an unrelated background-job runner; untouched.
5. **Slug collision note**: harvest keys on basename slug; PlanningService
   auto-suffixes manual slugs (`-2`). Document that a harvested repo merges into
   an existing same-slug project (desirable). Edge case, low frequency.

## Phase D — MCP server + auto-adopt

### MCP server (`mcp/server.py`)
ProjectAria has **no MCP server today** — this is the single biggest net-new
capability. Bring `aria-shells/mcp/server.py` in (e.g. `ProjectAria/mcp/`).
Per D2, **repoint the 6 projects/tasks tools** to ProjectAria's native routes:

| MCP tool | aria-shells path | ProjectAria path |
|---|---|---|
| `list_projects(status)` | `GET /projects` | `GET /projects` (map status vocab) |
| `get_project(slug)` | `GET /projects/{slug}` | add `get_project_by_slug` → `GET /projects/{id}` |
| `list_tasks(project,status)` | `GET /tasks` | `GET /todos` |
| `create_task(...)` | `POST /tasks` | `POST /todos` |
| `update_task(...)` | `PATCH /tasks/{id}` | `PATCH /todos/{id}` (+ `/todos/{id}/done`) |

The 13 shell tools already map onto ProjectAria's `shells.py` routes once Phase B
adds `/overview` + `/{name}/screen`. Update `~/.hermes/config.yaml`
`mcp_servers.aria` to point at the ProjectAria-hosted server; keep tool
docstrings (the agent-facing contract) intact. Add to it the new alert tools
(Phase E).

### Auto-adopt (new capability)
Today: `launch_session()` creates the tmux session **and** wires `pipe-pane`.
Add an **adopt** path so externally-started Claude sessions get captured:

- `adopt_session(name)` in `shells/service.py` — register an existing tmux
  session in `db.shells` and start capture, without creating it.
- **Hook-based (primary, real-time):** global tmux `session-created` hook →
  calls the API to adopt any session matching a configurable pattern
  (`claude-*`, or any pane whose command is `claude`). Reuse the existing
  tmux-hook plumbing (history shows async hooks + empty-`-t` guards already).
- **Poll reconciler (backstop):** `ShellAdoptWorker` lists tmux sessions every
  N seconds, diffs vs `db.shells`, adopts untracked matches. Catches sessions
  started while the API was down or that predate the hook.
- Harvested projects pick these up for free (harvest already reads `db.shells`).

Config: `shells_adopt_enabled=True`, `shells_adopt_pattern="claude-*"`,
`shells_adopt_interval_seconds=15`.

## Phase E — Decouple Signal → MCP-relay (per D1)

ProjectAria stops being a Signal sender; Hermes (which owns the signal-cli
daemon) becomes the relay.

1. Add an **alert queue**: persist alerts to a collection (e.g. `alerts`) with
   `acked` flag, source, severity, message, created_at.
2. **Re-route `NotificationService.notify()`**: the alert queue becomes the
   **sole** channel. Remove the direct Signal send and the Telegram secondary
   (D7) from the notify path — ProjectAria no longer sends notifications itself.
3. **Expose via MCP**: `list_alerts(unacked_only=True)` + `ack_alert(id)` tools.
4. Hermes, on its heartbeat, pulls unacked alerts and relays them over Signal,
   then acks. selfcheck / weekly report / idle-notifier all flow through this
   path unchanged (they already call `notify()`).

Result: one Signal daemon (Hermes's), no double-sending, ProjectAria's alerting
fully observable + drivable by the agent.

---

## Suggested execution order & checkpoints

1. Branch `feat/absorb-aria-shells` in ProjectAria.
2. Phase A (core copies) → run `pytest`. Checkpoint.
3. Phase B new files + additive merges + worker wiring → `pytest` + boot the API,
   hit `/shells/overview`, `/shells/{name}/screen`. Checkpoint.
4. Phase C planning/harvest merge → run a manual `harvest()`, confirm
   `db.projects` populated and dashboard human fields preserved. Checkpoint.
5. Phase D MCP server (repointed) + auto-adopt → start a raw `claude-foo` tmux
   session, confirm it gets adopted + watched; drive a tool from the MCP. Checkpoint.
6. Phase E alert-queue + Hermes relay → trigger a selfcheck failure, confirm it
   lands in the queue and Hermes relays. Checkpoint.
7. **Cutover (D4):** ProjectAria takes `:8200`. Run dev/test on a temp port; for
   go-live, stop `aria-shells-api.service`, repoint ProjectAria's systemd unit +
   UI/widget/CLI base URLs `:8000`→`:8200`, start ProjectAria on `:8200`. The two
   services cannot bind `:8200` simultaneously, so this step is atomic.
8. Update `CHANGELOG.md` / `PROJECT_STATUS.md`. Keep the `aria-shells` repo as
   reference; its service is decommissioned at cutover (step 7).

## Decisions — all resolved

D1 drop direct Signal · D2 MCP→native paths · D3 one schema/two extractors ·
D4 take over `:8200` (atomic cutover) · D5 Hermes pulls alerts via MCP ·
D6 drop APNs · D7 drop Telegram. No open questions remain.
