# Design — Multi-Machine Cockpit & Fleet (corsair-ai + MacBook)

Status: IMPLEMENTED (Layer A + Layer B2) · Author: Claude (with Ben) · Date: 2026-07-04 (B2 landed 2026-07-06)

> **Update (2026-07-06):** Both layers are built. Layer A (remote cockpit) and
> Layer B2 (the API-mediated `aria-node` agent + host-aware `ShellService`
> dispatch + remote coding sessions) are implemented and unit-tested (see
> `api/aria/nodes/`, `api/aria/node/`, `test_nodes.py`). Live end-to-end
> verification with a real node is pending an `aria-api` restart. The B2.a→b→c
> staging below describes the build; it was delivered as one pass.

> **Goal in one line:** run the ARIA TUI as a single "cockpit" from either
> machine, and let the fleet **span** corsair-ai (Linux, the brain) and
> `bens-macbook-air` (macOS, where iOS builds/Xcode/signing must happen) — so
> ARIA has context across both and you stop SSH-hopping to drive work.

This doc covers two layers, to be built in order:

- **Layer A — Remote cockpit.** Run the TUI natively on the Mac against the
  central API over the tailnet. *Almost free — the TUI is already a thin client.*
- **Layer B2 — API-mediated pull node.** A small `aria-node` agent on the Mac
  that registers its tmux shells + coding sessions into the corsair brain and is
  driven back through the central API. *The real engineering.*

Layers C (Mac-native iOS routines, jump-to-shell, per-host health) are noted at
the end as follow-ons and are **out of scope** for this doc.

## Guiding principle — one brain, many hands

There is exactly **one** ARIA service, one Mongo, one memory/projects store, on
corsair-ai. The Mac never runs its own ARIA API or its own Mongo — that would
split-brain the memory. The Mac runs a *thin node agent* only. The TUI, on
either machine, is just a window onto the single corsair brain. Everything below
preserves this invariant.

## Where we are today (grounded in the code)

**The cockpit half is basically done.** The Go TUI (`tui/`) is a pure-HTTP client
with zero machine-local assumptions:
- URL/auth resolution in `tui/main.go`: `envOr("ARIA_API_URL", "http://localhost:8200", …)`, key from `ARIA_API_KEY` then `API_KEY`; `api.NewClient(baseURL, apiKey)`.
- `tui/internal/api/client.go` `do()` injects `X-API-Key` on every request; all ~40 methods target `{Base}/api/v1/…`. No `os/exec`, no `syscall`, no cgo, no local tmux — the fleet/tmux driving is entirely server-side.
- The API already binds the tailnet: `config.py` `api_host="0.0.0.0"`, `api_port=8200`, `api_auth_enabled=True`; `main.py` `api_key_middleware` enforces `X-API-Key` (public prefixes: `/docs`, `/openapi.json`, `/redoc`, `/api/v1/health`, `/`).
- The Python CLI (`cli/aria_cli/main.py`) already uses the identical remote pattern, and `aria tui` now launches the binary.

**The fleet half is hard-bound to the API's host.** Despite the branch name,
"multi-runtime" = multiple LLM/coding runtimes on *one box* (claude-code /
pi-code+GLM / pi-code+qwen), not multiple machines. Concretely:
- `Shell.host` exists (`api/aria/shells/models.py:24`) and is stamped with the local hostname in `service.py` (`register_shell`, `insert_events_batch`), `capture.py`, and `scripts/aria-shell-register` — but it is **written and never read**. No query, dispatch, or overview keys off it. `ShellEvent`/`ShellSnapshot`/`ShellOverviewItem` have no host at all.
- `coding_sessions` docs (`session.py`) have **no** host/node field.
- The **drive path is local-only**: `ShellService.send_input`/`current_screen`/`has_session`/`reconcile_adopt` all call `TmuxClient` (`shells/tmux.py`), which docstrings itself as *"tmux … on the same host as the API process"* and `create_subprocess_exec("tmux", …)` locally.
- Capture/register assume same-host: the tmux hook (`scripts/aria-tmux-hook.conf`) runs a local `aria-shell-register`; the capture shim (`scripts/aria-shell-capture`) hardcodes a local venv and writes **straight to Mongo**.
- The backend abstraction (`agents/backends/base.py` `AgentBackend` → `CommandSpec{argv,env,cwd}`; `registry.py` = `{codex, claude_code, pi-code}`) is a clean seam but every executor is local; `CommandSpec` has no host/target.
- The **read side is already host-agnostic**: Mongo-backed scrollback, extraction, and `fleet_overview()` don't care where a shell physically runs.

So the gap is precisely the **write/drive + provenance** side. Layer B2 closes it
without touching the read side or the watchdog/checkpoint/review overlay.

---

# Layer A — Remote cockpit

## A.1 What ships

1. **macOS binary.** `GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 go build` in `tui/`
   produces a working Apple-Silicon binary with **no code changes** (verified: no
   cgo/syscall/build-tags/local-exec in the tree). Add a `tui/Makefile` (or
   `build.sh`) with `build`, `build-darwin`, and `install` targets.
2. **Host profiles for `aria tui`.** Extend the `aria tui` command
   (`cli/aria_cli/main.py`) and the TUI to accept `--host <name|url>`, resolving a
   small profiles file `~/.config/aria/hosts.toml`:
   ```toml
   default = "corsair"
   [hosts.corsair]
   url = "http://corsair-ai:8200"
   # key pulled from $ARIA_API_KEY or hosts.toml key field
   [hosts.local]
   url = "http://localhost:8200"
   ```
   Precedence unchanged: explicit `--host`/env > profile > built-in default.
3. **`.env` fallback fix.** `tui/main.go` hardcodes
   `~/Development/ProjectAria/.env`, which won't exist on the Mac. Fall back to
   `$ARIA_ENV` then `~/.config/aria/env`. (Real env vars already win, so this is
   ergonomics, not correctness.)
4. **HOST column in the fleet.** Surface the vestigial `host` field now: add it to
   `ShellOverviewItem` + `/shells/overview`, and render a HOST column + filter in
   the TUI fleet view (`tui/internal/ui/components/fleet_view.go`). This is the UI
   Layer B2 needs the moment nodes exist, and it's useful immediately for
   provenance.

## A.2 Security posture

Tailnet-only, plain HTTP over WireGuard (no TLS), gated by `X-API-Key` — matches
the existing "closed Tailscale tailnet, not internet-exposed" deployment. The Mac
cockpit needs `ARIA_API_URL=http://corsair-ai:8200` + the corsair `API_KEY`.
Nothing new is exposed.

## A.3 Acceptance

From the Mac, with no SSH: `aria tui --host corsair` shows the live fleet, drives
corsair coding sessions (including the Ralph-loop toggle), streams chat, browses
memory/DB/usage/health. All existing corsair behavior is unchanged.

---

# Layer B2 — API-mediated pull node (`aria-node`)

## B2.1 Topology

```
        bens-macbook-air (macOS)                 corsair-ai (Linux)
   ┌───────────────────────────────┐      ┌──────────────────────────────┐
   │  aria-node (launchd agent)     │      │  ARIA API :8200 (systemd)     │
   │   • local tmux adopt+capture   │      │   • /api/v1/nodes/*  (new)    │
   │   • push events/snapshots  ────┼─ ►──┼─►  • shell_commands queue      │
   │   • pull commands (long-poll)◄─┼─◄───┼──   • host-aware ShellService  │
   │   • heartbeat                  │      │   • watchdog / memory / …     │
   │   • drives LOCAL tmux only     │      │  Mongo (rs0) — single store   │
   └───────────────────────────────┘      └──────────────────────────────┘
        outbound-only, over Tailscale (WireGuard), X-API-Key / node token
```

The Mac agent talks **only to the corsair API** (outbound). No inbound ports on
the Mac, and **Mongo is never exposed** — the node ingests through the API front
door, preserving the single trust boundary. corsair's own shells keep the
existing direct-local fast path (no queue, zero regression).

## B2.2 The node agent

A small long-running process on each non-API machine. Implementation language:
**Python**, to maximize reuse — it can drive local tmux with the existing
`aria.shells.tmux.TmuxClient` verbatim (already just `create_subprocess_exec("tmux", …)`,
which works on macOS), and reuse the ANSI-strip/line logic from
`aria.shells.capture`, swapping the sink from *direct Mongo* to *API ingest*.
Packaged as an `aria.node` module + a launchd plist; distributed via the repo
(the Mac has the checkout for iOS work anyway) or a `pip install`.

Responsibilities:
1. **Register + heartbeat.** On start, `POST /api/v1/nodes/register` with
   `{node_id, hostname, os, arch, capabilities, agent_version}`; then
   `POST /api/v1/nodes/{node}/heartbeat` every ~10s. Central marks the node
   online/offline and surfaces it in `/health/services`.
2. **Adopt + capture (local).** Run a node-local adopt loop (reuse
   `reconcile_adopt` logic against **local** tmux) to pick up `claude-*` sessions,
   and a local `tmux pipe-pane` capture per shell. Stream ANSI-stripped lines to
   `POST /api/v1/nodes/{node}/events` (batched); push pane rehydration to
   `POST /api/v1/nodes/{node}/snapshot`. Server stamps `host = node_id`.
3. **Pull + execute commands.** Long-poll `GET /api/v1/nodes/{node}/commands`
   (server holds the request ~25s, returns immediately when a command is queued).
   Execute against **local** tmux, then `POST …/commands/{id}/result`. Command
   kinds mirror `ShellService`: `send_input(shell, text, wait_ms)` →
   `(line, screen)`; `current_screen(shell, lines)` → screen; `start_session(...)`
   → creates a local `claude-coding-*` shell and returns its name; `stop(shell)`.

The node owns **only** its host's tmux. It never reaches another machine.

## B2.3 Central changes

New collections:
- **`nodes`** — `{_id: node_id, hostname, os, arch, capabilities[], status, agent_version, registered_at, last_heartbeat_at}`.
- **`shell_commands`** — `{_id, node_id, kind, args, status: pending|claimed|done|error, result, created_at, claimed_at, done_at, expires_at}`. TTL index on `expires_at`; index on `(node_id, status)`.

New routes (`api/aria/api/routes/nodes.py`):
- `POST /nodes/register`, `POST /nodes/{node}/heartbeat`
- `POST /nodes/{node}/events`, `POST /nodes/{node}/snapshot` (ingest → existing `shell_events`/`shells` with `host=node`)
- `GET  /nodes/{node}/commands` (long-poll, claims pending → `claimed`)
- `POST /nodes/{node}/commands/{id}/result` (→ `done`/`error`, wakes any waiter)
- `GET  /nodes` (list, for TUI/health)

**Host-aware `ShellService` (the core seam).** Introduce a dispatcher:
```
async def _dispatch(shell) -> ShellDriver:
    if shell.host in (None, "", settings.local_node_id):
        return LocalTmuxDriver(self.tmux)          # today's path, unchanged
    return RemoteNodeDriver(self.db, shell.host)   # enqueue + await result
```
`send_input`, `current_screen`, `has_session` route through `_dispatch`.
`RemoteNodeDriver.send_input` inserts a `shell_commands` doc and awaits its
`result` (asyncio event or short poll) up to a timeout; if the node is
offline/slow, it returns a clear error rather than hanging. The act-and-observe
contract (`send_input(..., wait_ms=)` → `(line, screen)`) is preserved: the node
captures the screen after `wait_ms` and returns the tuple. Reads that are already
Mongo-backed (`fleet_overview`, scrollback, `list_events`) need **no** dispatch —
just add `host` to `ShellOverviewItem` and an optional `host=` filter.

**Coding sessions gain a host.** Add `host` (default = local node) + `node_id` to
the `coding_sessions` doc and to `start_session(host=…)`. When `host != local`,
creation routes to that node's command queue (`start_session` command → node
creates a `claude-coding-*` shell locally) instead of `ShellService.create_shell`.
Everything downstream — `get_output`, `send_input`, `stop` — already goes through
the manager, which now dispatches by host. **The watchdog + Ralph loop work on Mac
sessions for free**, because they drive through `session_manager.send_input`,
which is now host-aware.

**MCP + TUI.** `mcp/server.py` gains `list_nodes` and a `host` arg on
`create_coding_session`. The TUI fleet view shows the HOST column (Layer A) and
lets `start`/loop/stop act on Mac rows identically.

## B2.4 Identity, auth, resilience

- **Local node id.** `settings.local_node_id = socket.gethostname()` (corsair).
  corsair shells keep `host == local_node_id` → local fast path, no queue, no
  latency, no behavioral change to the existing fleet.
- **Node auth.** Per-node token (`nodes.token`, revocable) sent as
  `X-Node-Token`, *or* the shared `X-API-Key` to start. Node → API is
  outbound-only; no inbound port on the Mac; Mongo stays private.
- **Offline handling.** Missed heartbeats → node marked `offline`; its shells
  shown greyed/stale in the fleet; drive commands to an offline node fail fast
  with a useful message. Commands have `expires_at` (TTL) so a dead node doesn't
  accumulate a backlog.
- **Latency.** Long-poll keeps interactive `send_input` responsive (sub-second
  when the node is connected). Capture streaming is local on the node (fast,
  reliable pipe-pane); only ANSI-stripped lines cross the tailnet.

## B2.5 Phasing (build order within B2)

- **B2.a — Observe-only.** Node registers, captures, pushes events/snapshots +
  heartbeat; central shows Mac shells in the fleet (read-only) and in
  `/health/services`. *High value alone: you SEE Mac work in the cockpit.*
- **B2.b — Drive.** `shell_commands` queue + host-aware `ShellService`;
  `send_input`/`current_screen`/`stop` route to the node. Full straddle for
  watched shells.
- **B2.c — Remote coding sessions.** `coding_sessions.host`,
  `start_coding_session(host="bens-macbook-air", …)`, watchdog + Ralph loop over
  the wire. iOS coding sessions become first-class fleet citizens.

## B2.6 Testing

- Unit: `_dispatch` local-vs-remote routing; `RemoteNodeDriver` enqueue/await/
  timeout; command TTL expiry; node online/offline transitions. Mock the queue
  like the existing `make_mock_db` tests.
- Integration: a loopback node process on corsair with a *second* fake host id,
  driving a throwaway tmux session end-to-end (register → capture → send_input →
  result) without needing the Mac in the loop.
- Regression: existing coding-session/watchdog/shells suites must stay green
  (corsair's local fast path is unchanged).

## Resolved design decisions

| # | Decision | Resolution |
|---|----------|------------|
| D1 | Split-brain vs single brain | **Single brain on corsair.** Mac runs a thin node only; one Mongo, one memory. |
| D2 | Node ↔ central transport | **API-mediated**, not direct Mongo. Preserves one trust boundary; Mongo never exposed on the tailnet. |
| D3 | Command channel | **Pull / long-poll** from the node. No inbound ports on the Mac; survives flaky links; interactive-latency good enough. (Reconsider SSE/WebSocket only if long-poll proves laggy.) |
| D4 | Node language | **Python**, to reuse `TmuxClient` + capture logic verbatim (sink swapped Mongo→API). |
| D5 | corsair's own shells | **Local fast path**, keyed on `host == local_node_id` — no queue, zero regression. |
| D6 | Drive-path seam | **Host-aware `ShellService._dispatch`**; the watchdog/checkpoint/review overlay and the Ralph loop are untouched and inherit remote support for free. |
| D7 | Node auth | **Per-node revocable token** (fallback: shared `X-API-Key`). Outbound-only. |
| D8 | Cross-machine `send_input` over SSH (the B1 alternative) | **Rejected** for the hub: per-keystroke SSH latency + awkward `pipe-pane`-over-SSH. Kept only as a fallback idea. |

## Out of scope (Layer C follow-ons)

- iOS routines on the Mac node (`xcodebuild`/test/TestFlight → alerts + memory).
- "Jump to shell": TUI key that `tmux attach`es over Tailscale SSH to a row's host.
- Per-host vitals beyond the node heartbeat.
- More than two machines (the design generalizes to N nodes, but only corsair +
  Mac are targeted here).

## File-touch summary

**Layer A:** `tui/main.go` (.env fallback), `tui/Makefile` (new),
`cli/aria_cli/main.py` (`--host`), `tui/internal/ui/components/fleet_view.go` +
`api/aria/shells/models.py` + `api/aria/api/routes/shells.py` (HOST column).

**Layer B2:** `api/aria/node/` (new: agent + client), `api/aria/api/routes/nodes.py`
(new), `api/aria/shells/service.py` (host-aware `_dispatch` + `RemoteNodeDriver`),
`api/aria/shells/models.py` (host on overview items), `api/aria/agents/session.py`
(`host` on `coding_sessions` + start routing), `api/aria/db/migrations.py`
(`nodes`, `shell_commands` indexes), `mcp/server.py` (`list_nodes`, `host` arg),
`api/aria/config.py` (`local_node_id`, node settings), launchd plist +
`scripts/aria-node` (new).
