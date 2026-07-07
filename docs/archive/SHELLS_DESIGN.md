# ARIA — Watched Shells Subsystem Design

**Status:** Proposed
**Author:** Ben (with Claude)
**Target:** `api/aria/shells/` — a new vertical feature inside ProjectAria
**Date:** 2026-04-14

---

## 1. Overview & Goals

Ben's primary "vibe coding" workflow is SSH-ing from his laptop into the Corsair AI 300 over Tailscale and running multiple `claude --dangerously-skip-permissions` sessions in parallel, one per project. He wants two things ARIA does not currently give him:

1. A frictionless "resume from anywhere" story — the laptop experience should be indistinguishable from vanilla Claude Code, and the phone should give him *enough* of that experience to steer or unblock a session on the go.
2. All the transcripts flowing into ARIA's existing MongoDB + memory pipeline so ARIA actually knows what he's been working on across every project.

This document specifies a new subsystem — **Watched Shells** — that delivers both, while leaving ARIA's existing managed-coding-session infrastructure completely untouched.

### Primary goals

- **Capture** every line of output from designated tmux sessions into MongoDB, durably and append-only.
- **Relay** the captured stream to a mobile-friendly dashboard view in real time (SSE).
- **Control** tmux sessions remotely via a `POST /input` endpoint that runs `tmux send-keys`.
- **Integrate** shell transcripts into ARIA's memory extraction pipeline so coding sessions become searchable and context-aware.
- **Surface** active shells into the chat orchestrator's context assembly so ARIA can answer questions about what Ben is working on.
- **Notify** Ben via Signal/Telegram when a shell goes idle at a prompt (the "Claude is waiting" case).
- **Preserve** the existing managed-session code path. No shared abstractions, no refactors, no behavioral changes to `agents/session.py`, `agents/watchdog.py`, or the safety subsystems.

### Non-goals

- Spawning or owning tmux sessions from inside ARIA. Ben creates sessions himself with a shell wrapper (`ac projectaria`); ARIA only observes and relays.
- Replacing the existing `coding_sessions` collection or its supporting code. Watched shells live in parallel collections.
- Building a full terminal emulator on mobile. The dashboard view is a "scrollback + input" affordance, not a TUI. Blink Shell + Tailscale remains the escape hatch for when a real terminal is required.
- Multi-user or multi-tenant concerns. ARIA is single-user; this stays single-user.

---

## 2. Mental Model: Watched vs Managed Sessions

ARIA today has **managed** coding sessions — she spawns them via `asyncio.create_subprocess_exec`, owns the process handle, registers them with a watchdog, runs auto-review on completion, and stores state in `coding_sessions`.

Watched shells are the inverse:

| Dimension | Managed Sessions (existing) | Watched Shells (new) |
|---|---|---|
| Who creates the process | ARIA | Ben (via tmux + shell wrapper) |
| Who owns the lifecycle | ARIA | tmux |
| Process handle | `asyncio.subprocess.Process` | None — ARIA never has one |
| Input path | `process.stdin.write` | `tmux send-keys` |
| Output path | `process.stdout.readline` | `tmux pipe-pane` → subprocess |
| Termination | ARIA calls `stop()` | tmux session ends (user detaches/exits) |
| Watchdog | Yes, supervised | No — observational only |
| Auto-review | Yes | No |
| Collections | `coding_sessions` | `shells`, `shell_events`, `shell_snapshots` |
| Module | `agents/` | `shells/` |

These two models should share nothing beyond ARIA's platform services (MongoDB, FastAPI app, memory pipeline, notifications, dashboard, SSE plumbing). Do **not** inherit from `CodingSession` or route through `SessionManager`. They look similar and they are not; the shapes will diverge quickly and a shared abstraction will make both uglier.

---

## 3. Architecture

```
Corsair AI 300 (host running ARIA API + MongoDB)
────────────────────────────────────────────────

 Ben's laptop               Corsair
 ────────────              ─────────────────────────────────────
  ssh corsair                tmux session "claude-projectaria"
    │                        │
    │  tmux attach           │ pipe-pane ──► aria-shell-capture ──┐
    │◄───────────────────────┤                                    │
    │                        │                                    ▼
    │   send-keys ◄──────────┤                               MongoDB
    │                        │                               ┌──────────┐
    │                        │                               │ shells   │
    │                        │                               │ shell_   │
    ├─ APScheduler: snapshot capture-pane every 30s ────────►│  events  │
    │                        │                               │ shell_   │
    │                        │                               │ snapshots│
    │                        │                               └────┬─────┘
    │                        │                                    │
    │                        │    FastAPI (systemd: aria-api)     │
    │                        │    ┌────────────────────────┐      │
    │                        └───►│ /api/v1/shells/*       │◄─────┘
    │                             │   SSE tail             │
 Mobile (Tailscale)               │   POST /input → tmux   │
 ───────────────                  │                        │
  ARIA web UI (Next.js) ─────────►│ Memory extraction →    │
    │                             │   memories (shared)    │
    │                             │ Orchestrator context   │
    │                             │ Idle notifier → Signal │
    │                             └────────────────────────┘
```

Key properties of this architecture:

- **Capture is out-of-band.** The capture script runs as a pipe-pane child of tmux, not inside the API process. If the API is down, capture continues; events queue in the capture script's local buffer and flush when Mongo is reachable again.
- **Writes are append-only.** `shell_events` is never updated, only inserted. Makes replication, change streams, and crash recovery trivial.
- **The API process and tmux live on the same host.** `send-keys` is a local `subprocess.run` call, not an SSH hop. This matters for latency and auth.
- **The dashboard is just another SSE consumer.** Same pattern as the existing chat streaming.

---

## 4. Module Layout

```
api/aria/shells/
├── __init__.py
├── service.py          # ShellService: list, get, tail, send_input, register
├── capture.py          # Entry point for pipe-pane subprocess
├── snapshot.py         # Periodic capture-pane snapshotter (APScheduler job)
├── tmux.py             # Thin wrapper around tmux CLI (list-sessions, send-keys, capture-pane, has-session)
├── ansi.py             # ANSI escape stripping, prompt pattern detection
├── models.py           # Pydantic models (Shell, ShellEvent, ShellSnapshot, ShellInput)
├── notifier.py         # IdleNotifier: watches shell_events, fires on idle-at-prompt
├── context.py          # build_shell_context() for orchestrator injection
└── extraction.py       # Adapter that feeds shell_events into the existing memory extractor

api/aria/api/routes/shells.py
cli/aria_cli/commands/shells.py
ui/src/app/dashboard/shells/page.tsx
ui/src/lib/api-client-shells.ts
scripts/aria-shell-register        # bash/python script invoked by tmux hook
scripts/aria-shell-capture          # bash/python shim that execs aria.shells.capture
scripts/aria-tmux-hook.conf         # tmux config snippet to source from ~/.tmux.conf
tests/shells/
├── test_ansi.py
├── test_capture.py
├── test_service.py
├── test_tmux.py
├── test_notifier.py
├── test_context.py
└── test_routes.py
```

Wire-up:
- `api/aria/main.py` — `app.include_router(shells_router, prefix="/api/v1/shells")`; in the `lifespan` context manager, start `IdleNotifier` and the snapshot scheduler task.
- `api/aria/api/deps.py` — add `get_shell_service() -> ShellService` dependency.
- `api/aria/core/context.py` — call `shells.context.build_shell_context()` during context assembly.
- `api/aria/core/commands.py` — register `/shell` command family (list/tail/send).
- `cli/aria_cli/main.py` — register `shells` command group.
- `scripts/init-mongo.js` — add indexes for new collections.
- `.env.example` — add all `SHELLS_*` config keys.

---

## 5. Data Model

All timestamps are UTC. All collections share ARIA's existing MongoDB connection.

### 5.1 `shells` collection

One document per known tmux session. Created on first `session-created` hook fire; updated in place on status changes.

```python
class Shell(BaseModel):
    name: str                          # tmux session name, e.g. "claude-projectaria" — unique
    short_name: str                    # display name, "projectaria" (strip SHELLS_TMUX_SESSION_PREFIX)
    project_dir: str                   # cwd the tmux session was started in
    host: str                          # machine hostname (future-proofing for multi-host)
    status: Literal["active", "idle", "stopped", "unknown"]
    created_at: datetime
    last_activity_at: datetime         # ts of last shell_event
    last_output_at: datetime           # ts of last "output" event
    last_input_at: Optional[datetime]  # ts of last "input" event
    line_count: int                    # monotonic counter of events seen
    tags: list[str] = []               # user-assigned
    metadata: dict[str, Any] = {}      # tmux_pane_id, initial_command, etc.
```

Indexes:
```javascript
db.shells.createIndex({ name: 1 }, { unique: true });
db.shells.createIndex({ last_activity_at: -1 });
db.shells.createIndex({ status: 1, last_activity_at: -1 });
```

Status transitions:
- `active` → `idle`: no new events for `SHELLS_IDLE_THRESHOLD_SECONDS` (default 60s). Managed by the idle notifier tick.
- `idle` → `active`: any new event.
- `active`|`idle` → `stopped`: tmux `session-closed` hook fires, OR the capture script exits cleanly, OR periodic reconciliation (`tmux has-session`) fails.
- `stopped` → `active`: a new `session-created` hook fires with the same name (tmux allows this).

### 5.2 `shell_events` collection

Append-only log of everything that happened in a shell.

```python
class ShellEvent(BaseModel):
    shell_name: str                    # FK to shells.name
    ts: datetime                       # event time, set at insert
    line_number: int                   # monotonic per shell_name (assigned server-side)
    kind: Literal["output", "input", "system"]
    text_raw: str                      # may contain ANSI escape sequences
    text_clean: str                    # ANSI-stripped, normalized whitespace
    source: Literal["pipe-pane", "send-keys", "hook", "reconciler"]
    byte_offset: Optional[int] = None  # for future replay/seek
```

`kind` semantics:
- `output`: captured from `pipe-pane`. `source` will be `"pipe-pane"`.
- `input`: captured when ARIA herself sends keys via the API. `source` will be `"send-keys"`. (User input typed locally is *not* in this collection — tmux doesn't expose it via pipe-pane. This is a known limitation; see §18.)
- `system`: lifecycle markers — session started, session ended, capture reconnected, etc. `source` will be `"hook"` or `"reconciler"`.

`line_number` is assigned by the capture writer using `$inc` on a counter in the parent `shells` doc. This gives a gap-free ordering per shell without relying on clock precision.

Indexes:
```javascript
db.shell_events.createIndex({ shell_name: 1, ts: 1 });
db.shell_events.createIndex({ shell_name: 1, line_number: 1 });
db.shell_events.createIndex({ shell_name: 1, kind: 1, ts: -1 });
// Optional TTL for long-term retention control:
// db.shell_events.createIndex({ ts: 1 }, { expireAfterSeconds: SHELLS_RETENTION_DAYS * 86400 });
```

Retention: **do not** enable the TTL index by default. Ben explicitly wants long-term memory. Make it a config flag (`SHELLS_RETENTION_DAYS`, default `None` = keep forever).

### 5.3 `shell_snapshots` collection

Periodic full-pane snapshots for redraw-resilient replay (see §6.4).

```python
class ShellSnapshot(BaseModel):
    shell_name: str
    ts: datetime
    content: str                       # full `tmux capture-pane -pS -N` output, ANSI-stripped
    content_hash: str                  # sha256 of content, for dedup
    line_count_at_snapshot: int        # shell_events.line_number at time of snapshot
```

Indexes:
```javascript
db.shell_snapshots.createIndex({ shell_name: 1, ts: -1 });
db.shell_snapshots.createIndex({ shell_name: 1, content_hash: 1 });
```

Snapshot writer skips insertion if `content_hash` matches the previous snapshot for that shell. This keeps the collection bounded for idle sessions.

---

## 6. tmux Integration

### 6.1 Session naming convention

All shells that ARIA should watch are named with the prefix `claude-` (configurable via `SHELLS_TMUX_SESSION_PREFIX`). Any other tmux session on the host is ignored. The `short_name` is the name with the prefix stripped.

### 6.2 Shell wrapper (laptop side)

Ben adds this to his `~/.zshrc` (on the laptop, not the Corsair):

```bash
# ac = "attach claude" — resume or create a named claude session on the Corsair box
ac() {
  local proj="${1:?usage: ac <project> [dir]}"
  local dir="${2:-$HOME/dev/$proj}"
  ssh -t corsair "tmux new-session -A -s claude-$proj -c $dir 'claude --dangerously-skip-permissions'"
}

acs() {
  ssh corsair tmux ls 2>/dev/null | grep '^claude-' || echo "no claude sessions running"
}
```

The `-A` flag on `new-session` makes it idempotent: attaches if the session exists, creates otherwise. This is the primitive that makes resume work.

### 6.3 tmux hook configuration

`scripts/aria-tmux-hook.conf` (sourced from `~/.tmux.conf` on the Corsair):

```tmux
# ARIA shell capture — only active for sessions named claude-*
set-hook -g session-created '\
  if -F "#{m:claude-*,#{session_name}}" "\
    run-shell \"/home/ben/.local/bin/aria-shell-register #{session_name} #{session_path} #{pane_id}\" ; \
    pipe-pane -o -t #{session_name} \"/home/ben/.local/bin/aria-shell-capture #{session_name}\"\
  "'

set-hook -g session-closed '\
  if -F "#{m:claude-*,#{hook_session_name}}" "\
    run-shell \"/home/ben/.local/bin/aria-shell-register --closed #{hook_session_name}\"\
  "'

# Reapply pipe-pane after server restart: force a session-created fire on attach if no capture running.
set-hook -g client-attached '\
  if -F "#{m:claude-*,#{session_name}}" "\
    run-shell \"/home/ben/.local/bin/aria-shell-register --ensure-capture #{session_name} #{session_path} #{pane_id}\"\
  "'
```

`aria-shell-register` is a thin bash or Python script that:
1. Upserts the `shells` doc (create on new, update `last_activity_at` on ensure).
2. On `--closed`, sets status to `stopped`.
3. On `--ensure-capture`, checks whether a capture process is already running for that session (by PID file in `/tmp/aria-shell-capture-$name.pid`); if not, it starts one via `pipe-pane`.

`aria-shell-capture` is a shim that execs into the capture module:

```bash
#!/usr/bin/env bash
exec python3 -m aria.shells.capture "$@"
```

Both scripts need `MONGO_URL` (and any other env ARIA needs) in their environment. Source from `/etc/aria/shells.env` or user-level `~/.aria-env`.

### 6.4 Capture script behavior

`api/aria/shells/capture.py` — entry point: `python -m aria.shells.capture <shell_name>`

Core loop:

```python
async def main(shell_name: str) -> None:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("MONGO_DB", "aria")]
    shells = db.shells
    events = db.shell_events

    # Buffered line reader on stdin
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_event_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    pending: list[dict] = []
    last_flush = time.monotonic()
    FLUSH_MS = int(os.environ.get("SHELLS_CAPTURE_FLUSH_MS", "500")) / 1000
    BATCH = int(os.environ.get("SHELLS_CAPTURE_BATCH_SIZE", "50"))

    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=FLUSH_MS)
        except asyncio.TimeoutError:
            line = b""

        if line:
            raw = line.decode("utf-8", errors="replace").rstrip("\n")
            clean = strip_ansi(raw)
            pending.append({
                "shell_name": shell_name,
                "kind": "output",
                "text_raw": raw,
                "text_clean": clean,
                "source": "pipe-pane",
            })

        now = time.monotonic()
        if pending and (len(pending) >= BATCH or now - last_flush >= FLUSH_MS):
            await flush(shells, events, shell_name, pending)
            pending.clear()
            last_flush = now
```

`flush()` does one `$inc` on the `shells` doc to get `line_count` for the batch, assigns `line_number` to each event, inserts them in one `insert_many` call, and updates `last_activity_at` / `last_output_at`.

On Mongo connection errors: retry with exponential backoff (capped at 30s), buffer up to `SHELLS_CAPTURE_MAX_BUFFER` events (default 10000) in-memory, drop oldest on overflow and log a `system` event marker on reconnect. Never crash — crashing the capture process causes tmux to close the pipe.

ANSI stripping: use a well-tested regex (`re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')`), not a homegrown one. Normalize carriage returns (`\r\n` → `\n`, bare `\r` → discard — terminal overwrite). Keep `text_raw` untouched.

### 6.5 Snapshot worker

Separate APScheduler job (or a simple `asyncio.create_task` loop in the API lifespan) that runs every `SHELLS_SNAPSHOT_INTERVAL_SECONDS` (default 30s):

```python
async def snapshot_all_active_shells(service: ShellService) -> None:
    active = await service.list_shells(status__in=["active", "idle"])
    for shell in active:
        try:
            content = await service.tmux.capture_pane(
                shell.name, lines=int(os.environ.get("SHELLS_SNAPSHOT_LINES", "10000"))
            )
        except TmuxSessionNotFoundError:
            await service.mark_stopped(shell.name)
            continue
        clean = strip_ansi(content)
        h = hashlib.sha256(clean.encode()).hexdigest()
        last = await service.get_last_snapshot(shell.name)
        if last and last.content_hash == h:
            continue
        await service.insert_snapshot(shell.name, clean, h)
```

Snapshots back up pipe-pane. Pipe-pane only captures *new* output; if Claude Code redraws a region in place (progress bars, REPL prompts), pipe-pane may miss the final state. The snapshot is the source of truth for "what does the pane look like right now."

---

## 7. Service Layer — `shells/service.py`

```python
class ShellService:
    def __init__(self, db: AsyncIOMotorDatabase, tmux: TmuxClient, settings: Settings):
        self.db = db
        self.tmux = tmux
        self.settings = settings

    # Read
    async def list_shells(self, status: Optional[str] = None) -> list[Shell]: ...
    async def get_shell(self, name: str) -> Optional[Shell]: ...
    async def list_events(
        self, name: str, since: Optional[datetime] = None,
        before: Optional[datetime] = None, limit: int = 500,
        kinds: Optional[list[str]] = None,
    ) -> list[ShellEvent]: ...
    async def get_last_snapshot(self, name: str) -> Optional[ShellSnapshot]: ...
    async def tail(self, name: str, lines: int = 100) -> list[ShellEvent]: ...

    # Write / lifecycle
    async def register_shell(self, name: str, project_dir: str, pane_id: str) -> Shell: ...
    async def mark_stopped(self, name: str) -> None: ...
    async def insert_events_batch(self, name: str, events: list[dict]) -> int: ...
    async def insert_snapshot(self, name: str, content: str, content_hash: str) -> None: ...

    # Streaming (used by SSE route)
    async def stream_events(
        self, name: str, since_line: Optional[int] = None
    ) -> AsyncIterator[ShellEvent]:
        """Yields new events as they arrive. Uses MongoDB change streams if
        replica set supports it, otherwise polls."""
        ...

    # Control
    async def send_input(
        self, name: str, text: str, append_enter: bool = True,
        literal: bool = False
    ) -> None:
        """Runs `tmux send-keys` on the local host. Logs an 'input' event."""
        if not await self.tmux.has_session(name):
            raise ShellNotFoundError(name)
        await self.tmux.send_keys(name, text, append_enter=append_enter, literal=literal)
        await self.insert_events_batch(name, [{
            "kind": "input",
            "text_raw": text,
            "text_clean": text,
            "source": "send-keys",
        }])

    # Status reconciliation
    async def reconcile_statuses(self) -> None:
        """Checks every non-stopped shell against `tmux has-session`. Marks
        missing sessions as stopped. Runs on a timer and on API startup."""
        ...
```

`TmuxClient` in `shells/tmux.py` is a thin wrapper:

```python
class TmuxClient:
    async def list_sessions(self) -> list[str]: ...
    async def has_session(self, name: str) -> bool: ...
    async def send_keys(
        self, name: str, text: str, *, append_enter: bool, literal: bool
    ) -> None:
        args = ["tmux", "send-keys", "-t", name]
        if literal:
            args.append("-l")
        args.append(text)
        if append_enter:
            args.append("Enter")
        proc = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise TmuxCommandError(stderr.decode())

    async def capture_pane(self, name: str, *, lines: int = 10000) -> str: ...
    async def kill_session(self, name: str) -> None: ...   # not exposed via API in v1
```

---

## 8. API Routes

All routes live under `/api/v1/shells` and require the existing API key middleware (Phase 19 auth).

### 8.1 List shells

```
GET /api/v1/shells?status=active,idle
```

Response:
```json
{
  "shells": [
    {
      "name": "claude-projectaria",
      "short_name": "projectaria",
      "project_dir": "/home/ben/dev/ProjectAria",
      "status": "idle",
      "created_at": "2026-04-14T08:12:00Z",
      "last_activity_at": "2026-04-14T10:47:22Z",
      "last_input_at": "2026-04-14T10:45:11Z",
      "line_count": 4821,
      "tags": ["primary"]
    }
  ]
}
```

### 8.2 Get a shell

```
GET /api/v1/shells/{name}
```

Returns the full `Shell` document plus the most recent snapshot's `content_hash` and timestamp (for cache validation on the client).

### 8.3 List historical events

```
GET /api/v1/shells/{name}/events
  ?since=<iso8601>         # events with ts > since
  &since_line=<int>        # events with line_number > since_line (preferred)
  &before=<iso8601>
  &limit=500               # max 2000
  &kinds=output,input
```

Response:
```json
{
  "events": [
    {
      "line_number": 4820,
      "ts": "2026-04-14T10:47:22Z",
      "kind": "output",
      "text_clean": "Running tests...",
      "source": "pipe-pane"
    }
  ],
  "has_more": false
}
```

Default sort: `line_number` ascending. This is the endpoint the dashboard calls for the initial scrollback fetch.

### 8.4 SSE tail

```
GET /api/v1/shells/{name}/stream?since_line=<int>
```

Server-Sent Events. Three event types:

```
event: shell_event
data: {"line_number": 4821, "ts": "...", "kind": "output", "text_clean": "..."}

event: shell_status
data: {"status": "idle", "last_activity_at": "..."}

event: heartbeat
data: {}
```

Implementation: start with a `list_events(since_line=...)` catchup fetch, then switch to `stream_events()` for live updates. Heartbeat every 15s to keep the connection alive through any reverse proxies or load balancers. Close on client disconnect.

Use `sse-starlette.EventSourceResponse` — same as the conversations route. Look at `api/aria/api/routes/conversations.py` for the pattern.

### 8.5 Send input

```
POST /api/v1/shells/{name}/input
Content-Type: application/json

{
  "text": "yes, proceed with the refactor",
  "append_enter": true,
  "literal": false
}
```

Response:
```json
{
  "ok": true,
  "line_number": 4822
}
```

Errors:
- `404 ShellNotFound` if tmux has-session fails.
- `409 ShellStopped` if the shell is in `stopped` state.
- `429 RateLimited` — enforce a per-shell limit (default 30 inputs/minute) via a simple in-memory token bucket. Ben is a single user but this protects against mobile-UI bugs that spam send.
- `500 TmuxError` on subprocess failure.

`literal=true` maps to `tmux send-keys -l`, which is required for sending text containing tmux key names (`C-c`, `Escape`, `Enter`) as literal characters. Default `false` so key names like `Enter` work as expected.

Every input is logged as a `shell_events` doc with `kind=input`, `source=send-keys`. **Always.** This is the audit trail.

### 8.6 Latest snapshot

```
GET /api/v1/shells/{name}/snapshot
```

Returns the most recent snapshot. Used by the dashboard to render "what does the pane look like right now" without replaying the full event log.

### 8.7 Update tags

```
POST /api/v1/shells/{name}/tags
{ "tags": ["primary", "urgent"] }
```

Simple. Used for organization in the dashboard.

### 8.8 Full-text event search

```
GET /api/v1/shells/search?q=<query>&limit=50
```

Searches `shell_events.text_clean` across all shells. Uses a MongoDB text index or mongot. Returns events with their shell context. Powers the CLI `aria shells search` command.

```javascript
db.shell_events.createIndex({ text_clean: "text" });
// OR define a mongot index mirroring the memories text index
```

---

## 9. CLI Commands

`cli/aria_cli/commands/shells.py`:

```
aria shells list [--status active,idle]
aria shells info <name>
aria shells tail <name> [--lines 100] [--follow]
aria shells send <name> <text...> [--no-enter] [--literal]
aria shells search <query> [--limit 50]
aria shells tags <name> --add <tag> --remove <tag>
```

`tail --follow` connects to the SSE endpoint and prints events as they arrive. Ctrl-C disconnects cleanly.

---

## 10. Dashboard Tab

New route: `/dashboard/shells` in the existing Next.js app. Add a "Shells" entry to the dashboard sidebar next to Modes/Memories/Research/etc.

### 10.1 Layout

Two-pane layout on desktop, single-pane stack on mobile:

- **Left (or top on mobile)**: list of shells. Each row shows `short_name`, status pill (active/idle/stopped), relative `last_activity_at`, line count, and tags. Sorted by `last_activity_at` desc. Tap to select.
- **Right (or below on mobile)**: selected shell detail view.

### 10.2 Shell detail view

- **Header**: `short_name`, status, project dir, last activity, action buttons (refresh, copy session name, view snapshot).
- **Scrollback**: a monospace, scrollable div rendering `text_clean` from recent events. Auto-scroll to bottom when a new event arrives AND the user was already at the bottom; otherwise show a "↓ 3 new" chip.
- **Input box**: single-line textarea at the bottom. Enter submits, Shift+Enter adds a newline. A "Send" button for mobile users who hate virtual-keyboard Enter handling.
- **Special keys palette** (mobile-friendly): buttons for `Esc`, `Ctrl-C`, `Ctrl-D`, `↑` (up arrow — useful for recalling last command in a REPL). These send via `literal=true` with the appropriate tmux key name.

### 10.3 Data flow

1. On mount: `GET /api/v1/shells` → populate the list.
2. On shell select: `GET /api/v1/shells/{name}/events?limit=500` → initial scrollback.
3. Open SSE connection to `/api/v1/shells/{name}/stream?since_line=<last>`.
4. On input submit: `POST /api/v1/shells/{name}/input` → echo the input optimistically in the scrollback with a pending marker, replace with real event when it arrives through the SSE stream.
5. On tab close or shell switch: close the SSE connection.

Reuse `ui/src/lib/api-client.ts` patterns. Add `ui/src/lib/api-client-shells.ts` with typed methods.

### 10.4 Styling

Terminal-ish. Dark theme default. Monospace font for scrollback. Green/amber status dots. Don't try to do full ANSI color rendering in v1 — `text_clean` is already stripped, which is fine. If/when needed, use `ansi_up` or similar to render `text_raw` into colored HTML.

---

## 11. Memory Pipeline Integration

This is where watched shells stop being a separate feature and start being part of ARIA.

### 11.1 Source type

Add `"shell"` as a valid `source_type` on the existing `memories` collection. Each extracted memory carries:

```python
source_type: "shell"
source_id: shell_name            # e.g. "claude-projectaria"
source_metadata: {
    "shell_short_name": "projectaria",
    "event_range": [start_line, end_line],
    "extracted_at": datetime,
}
```

No schema migration needed — `source_metadata` is already a free-form dict.

### 11.2 Extraction trigger

In `api/aria/shells/extraction.py`, add a background task that runs on a timer (via APScheduler or a loop in lifespan):

```python
async def extract_recent_shell_activity(
    service: ShellService, extractor: MemoryExtractor, settings: Settings
) -> None:
    """For each shell with activity since last extraction, extract memories
    from the new events using the existing memory extractor."""
    shells = await service.list_shells()
    for shell in shells:
        state = await get_extraction_state(shell.name)  # tracks last line_number extracted
        new_events = await service.list_events(
            shell.name, since_line=state.last_line, limit=5000, kinds=["output", "input"]
        )
        if len(new_events) < settings.SHELLS_EXTRACTION_MIN_EVENTS:
            continue
        transcript = format_as_transcript(new_events)
        memories = await extractor.extract_from_text(
            text=transcript,
            source_type="shell",
            source_id=shell.name,
            source_metadata={
                "shell_short_name": shell.short_name,
                "event_range": [new_events[0].line_number, new_events[-1].line_number],
            },
        )
        await save_extraction_state(shell.name, new_events[-1].line_number)
```

Cadence: every 10 minutes, and on a `session-closed` hook event. Track extraction state in a tiny `shell_extraction_state` collection or as a subdocument on `shells`.

`MemoryExtractor.extract_from_text` should already exist in `api/aria/memory/extraction.py` — if it takes a conversation, add a thin wrapper that accepts arbitrary text + source metadata and reuses the same prompt (`prompts/extraction.md`). If the prompt needs a shell-specific variant, add `prompts/extraction_shell.md`.

### 11.3 Retrieval

The existing hybrid search (`$vectorSearch` + `$search` + RRF) over `memories` automatically picks up shell-sourced memories. No changes to the retrieval path — this is the whole reason the source_type pattern is used.

When a memory is retrieved, the orchestrator already injects it into the system prompt. Shell-sourced memories get rendered with their source: "Remembered from coding session in projectaria, 2 days ago: [content]". See §12 for the orchestrator wiring.

---

## 12. Orchestrator Context Integration

This is the answer to "yes, ARIA's chat orchestrator should know about shells."

### 12.1 New context block

In `api/aria/core/context.py`, the `ContextBuilder.build()` method already assembles sections: SOUL.md, summary, long-term memories, short-term messages, skill catalog, awareness. Add a new section: **Active Shells**.

```python
async def build_shell_context(
    shell_service: ShellService,
    settings: Settings,
    token_budget: int,
) -> str:
    """Returns a formatted markdown block listing recently active shells
    with their last few lines of output. Empty string if no active shells
    or feature disabled."""
    if not settings.SHELLS_INCLUDE_IN_CHAT_CONTEXT:
        return ""

    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.SHELLS_CONTEXT_LOOKBACK_HOURS  # default 24
    )
    shells = await shell_service.list_shells()
    shells = [s for s in shells if s.last_activity_at >= cutoff and s.status != "stopped"]
    shells.sort(key=lambda s: s.last_activity_at, reverse=True)

    if not shells:
        return ""

    blocks = []
    tokens_used = 0
    per_shell_lines = settings.SHELLS_CONTEXT_LINES_PER_SHELL  # default 20

    for shell in shells:
        recent = await shell_service.list_events(
            shell.name, limit=per_shell_lines, kinds=["output", "input"]
        )
        recent.sort(key=lambda e: e.line_number)
        transcript = "\n".join(
            f"{'>' if e.kind == 'input' else ' '} {e.text_clean}" for e in recent
        )
        block = (
            f"### {shell.short_name} "
            f"(last activity: {humanize_delta(shell.last_activity_at)}, "
            f"status: {shell.status})\n"
            f"```\n{transcript}\n```"
        )
        block_tokens = estimate_tokens(block)
        if tokens_used + block_tokens > token_budget:
            break
        blocks.append(block)
        tokens_used += block_tokens

    if not blocks:
        return ""

    return "## Active Shells\n\n" + "\n\n".join(blocks)
```

Call it from `ContextBuilder.build()`, insert the result between long-term memories and short-term conversation history in the assembled system prompt. Budget: `SHELLS_CONTEXT_MAX_TOKENS` (default 2000).

### 12.2 Soft behavioral expectations for ARIA

Add a line to SOUL.md (or the core agent system prompt) along the lines of:

> You have awareness of Ben's active coding shells. When he asks about a project or says something like "what was I working on", refer to the active shells section of your context. You can also request ARIA send input to a shell on his behalf — but always confirm before sending.

That last clause matters. See §13.3.

### 12.3 Gating

The whole thing is gated by `SHELLS_INCLUDE_IN_CHAT_CONTEXT` (default `true`). Disabling it cleanly removes the section from the context. Useful when debugging prompt length issues.

---

## 13. Chat Commands & Tool Integration

### 13.1 `/shell` command family

Register in `api/aria/core/commands.py`:

```
/shell list                       — list active shells
/shell tail <name> [--lines N]    — inject recent output into the next turn
/shell send <name> <text>         — ARIA sends input on Ben's behalf (with confirmation)
/shell pin <name>                 — mark a shell as pinned (higher context priority)
```

`/shell tail projectaria` injects the last N lines as a user message prefix, so the next LLM turn sees them explicitly. Useful for "look at what Claude just said and help me debug it" flows.

### 13.2 `send_shell_input` tool

Add a new tool to the tool router:

```python
class SendShellInputTool(BaseTool):
    name = "send_shell_input"
    description = (
        "Send text input to one of Ben's active tmux coding shells. Use when "
        "Ben asks you to tell a shell something, answer a prompt a shell is "
        "waiting on, or forward a decision to a running Claude Code session. "
        "Always confirm the shell name and the exact text before sending."
    )
    parameters = [
        ToolParameter(name="shell_name", type="string", required=True,
                      description="Full tmux session name, e.g. 'claude-projectaria'"),
        ToolParameter(name="text", type="string", required=True),
        ToolParameter(name="append_enter", type="boolean", required=False, default=True),
    ]

    async def execute(self, shell_name: str, text: str, append_enter: bool = True) -> ToolResult:
        await self.shell_service.send_input(shell_name, text, append_enter=append_enter)
        return ToolResult(ok=True, content=f"Sent to {shell_name}: {text!r}")
```

Register this only for agents that have `shells_enabled: true` in their config (default ARIA agent: yes).

### 13.3 Safety: confirmation before sending

Don't let ARIA send input autonomously without a confirmation step. Two options:

- **Soft**: the system prompt tells her to always repeat back what she's about to send and ask for confirmation. Works, but trusts the model.
- **Hard**: the tool executor intercepts `send_shell_input` calls and requires a separate `confirm_shell_input` tool call with a matching nonce. This is more defensive.

For v1, go soft. For v2, add the hard check if it becomes a problem.

---

## 14. Idle Notifier

`api/aria/shells/notifier.py`:

```python
class IdleNotifier:
    def __init__(
        self, service: ShellService,
        notifications: NotificationService,
        settings: Settings,
    ):
        self.service = service
        self.notifications = notifications
        self.settings = settings
        self._last_notified: dict[str, datetime] = {}
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        interval = self.settings.SHELLS_IDLE_NOTIFIER_INTERVAL_SECONDS  # default 30
        while True:
            try:
                await self._tick()
            except Exception:
                logger.exception("idle notifier tick failed")
            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        shells = await self.service.list_shells()
        now = datetime.now(timezone.utc)
        threshold = timedelta(seconds=self.settings.SHELLS_IDLE_THRESHOLD_SECONDS)
        for shell in shells:
            if shell.status == "stopped":
                continue
            idle_for = now - shell.last_activity_at
            if idle_for < threshold:
                continue

            # Status transition active → idle
            if shell.status == "active":
                await self.service.set_status(shell.name, "idle")

            # Dedup: don't re-notify for the same idle period
            last_notified = self._last_notified.get(shell.name)
            if last_notified and last_notified >= shell.last_activity_at:
                continue

            # Fetch last few lines to determine if Claude is at a prompt
            tail = await self.service.tail(shell.name, lines=5)
            if not tail:
                continue
            last_line = tail[-1].text_clean.rstrip()
            if not matches_prompt(last_line, self.settings.SHELLS_IDLE_PROMPT_PATTERNS):
                continue

            # Fire notification
            preview = "\n".join(e.text_clean for e in tail[-3:])
            await self.notifications.send(
                severity="medium",
                title=f"{shell.short_name}: waiting for input",
                body=(
                    f"Idle for {int(idle_for.total_seconds())}s.\n\n"
                    f"Last output:\n{preview}"
                ),
                channels=["signal", "telegram"],
                metadata={"shell_name": shell.name, "kind": "shell_idle"},
            )
            self._last_notified[shell.name] = shell.last_activity_at
```

`matches_prompt` checks the last line against a list of regex patterns from config:

```python
SHELLS_IDLE_PROMPT_PATTERNS = [
    r"\?\s*$",              # question mark at end of line
    r">\s*$",               # REPL prompt
    r"Human:\s*$",          # Claude Code's waiting prompt
    r"\[y/n\]\s*$",         # yes/no prompt
    r"(?i)press.*to continue",
]
```

Reuse `NotificationService` from `api/aria/notifications/` — same channels, same routing, same escalation protocol.

Start/stop in `main.py` lifespan:

```python
async with lifespan():
    idle_notifier = IdleNotifier(shell_service, notifications, settings)
    if settings.SHELLS_IDLE_NOTIFIER_ENABLED:
        await idle_notifier.start()
    try:
        yield
    finally:
        await idle_notifier.stop()
```

---

## 15. Configuration

Add to `api/aria/config.py` and `.env.example`:

```bash
# === Watched Shells ===
SHELLS_ENABLED=true
SHELLS_TMUX_SESSION_PREFIX=claude-

# Capture
SHELLS_CAPTURE_BATCH_SIZE=50
SHELLS_CAPTURE_FLUSH_MS=500
SHELLS_CAPTURE_MAX_BUFFER=10000

# Snapshots
SHELLS_SNAPSHOT_INTERVAL_SECONDS=30
SHELLS_SNAPSHOT_LINES=10000

# Status / idle
SHELLS_IDLE_THRESHOLD_SECONDS=60
SHELLS_RECONCILE_INTERVAL_SECONDS=120

# Idle notifier
SHELLS_IDLE_NOTIFIER_ENABLED=true
SHELLS_IDLE_NOTIFIER_INTERVAL_SECONDS=30
SHELLS_IDLE_PROMPT_PATTERNS='\?\s*$,>\s*$,Human:\s*$,\[y/n\]\s*$'

# Context integration
SHELLS_INCLUDE_IN_CHAT_CONTEXT=true
SHELLS_CONTEXT_MAX_TOKENS=2000
SHELLS_CONTEXT_LOOKBACK_HOURS=24
SHELLS_CONTEXT_LINES_PER_SHELL=20

# Memory extraction
SHELLS_EXTRACTION_ENABLED=true
SHELLS_EXTRACTION_INTERVAL_MINUTES=10
SHELLS_EXTRACTION_MIN_EVENTS=20

# Rate limiting
SHELLS_INPUT_RATE_LIMIT_PER_MINUTE=30

# Retention
SHELLS_RETENTION_DAYS=                   # blank = keep forever
```

---

## 16. Security & Safety

- **API auth**: all `/api/v1/shells/*` routes require the existing API key middleware. No new auth code.
- **Send-keys is powerful.** Any caller with API access can execute arbitrary input in any watched shell. Since this is a single-user system behind Tailscale, the blast radius is bounded, but still:
  - Log every `/input` call to `shell_events` with `source=send-keys`. This is the audit trail. Do not rely on HTTP access logs.
  - Rate limit inputs per shell (in-memory token bucket, default 30/min).
  - Consider a "confirm-dangerous" flag: if the text contains patterns from a denylist (e.g. `rm -rf`, `curl .* | sh`, `DROP TABLE`), require an explicit `confirm=true` body field. Log the denial if `confirm` is absent. Optional for v1; recommended for v2.
- **Tmux host isolation**: the API process must run as the same user that owns the tmux sessions, otherwise `send-keys` fails silently. Document this in the deployment notes. Ben is running the API under his user account on the Corsair box, so this is already the case.
- **Capture script credentials**: the capture processes need `MONGO_URL`. Keep it in `/etc/aria/shells.env` with `chmod 600` ownership matching the tmux user.
- **No shell-to-shell crosstalk**: the API never lets one shell reference another's contents in a way that crosses trust boundaries. Since there's only one user, this is mostly a code hygiene concern.

---

## 17. Testing Plan

### 17.1 Unit

- `tests/shells/test_ansi.py` — ANSI stripping on sample Claude Code output, carriage return handling, mixed-encoding edge cases.
- `tests/shells/test_capture.py` — line buffering, batch flushing, error recovery on mock Mongo failures, `line_number` monotonicity.
- `tests/shells/test_service.py` — CRUD roundtrips against a motor mock or a real test Mongo, status transitions, reconcile logic.
- `tests/shells/test_tmux.py` — `TmuxClient` wraps subprocess calls correctly; mock `create_subprocess_exec`.
- `tests/shells/test_notifier.py` — prompt pattern matching, dedup logic, notification firing.
- `tests/shells/test_context.py` — `build_shell_context` output formatting, token budgeting, lookback filtering.

### 17.2 Integration

- `tests/shells/test_routes.py` — FastAPI TestClient against a real Mongo (same pattern as existing route tests). Covers all endpoints including SSE (read a few events then disconnect).
- End-to-end tmux test: spin up a real tmux session in a test fixture, run the capture script, verify events land in Mongo, send-keys round trip, shutdown cleanly. Mark as `@pytest.mark.integration` and skip in CI if `TMUX_AVAILABLE` env var not set.

### 17.3 Manual acceptance

Checklist Ben runs before merging each PR:

- [ ] `ac projectaria` from laptop attaches cleanly, Claude Code works normally.
- [ ] `aria shells list` on the Corsair shows the session.
- [ ] Typing in the tmux session produces events in `db.shell_events.find({shell_name: "claude-projectaria"}).sort({line_number: -1}).limit(5)`.
- [ ] Dashboard "Shells" tab shows the session, scrollback renders, SSE stream updates live.
- [ ] Sending input from the dashboard reaches the tmux session and Claude sees it.
- [ ] Detaching + reattaching from laptop preserves state and capture continues.
- [ ] Killing the tmux session marks status=stopped within `SHELLS_RECONCILE_INTERVAL_SECONDS`.
- [ ] Leaving Claude at a prompt for 90s fires a Signal notification.
- [ ] Asking ARIA "what am I working on in projectaria" in the chat UI produces an answer sourced from the shell context.

---

## 18. Known Limitations

1. **User input typed directly in the tmux session is not captured as `kind=input`.** Pipe-pane only sees output (which includes the echoed input line as rendered by the shell, but not as a distinct event). The captured transcript still contains the typed text via the echo, but you can't cleanly distinguish "Ben typed this" from "Claude printed this" in the stored data. Good enough for v1.

2. **ANSI-heavy TUIs are approximated, not rendered.** `text_clean` loses color and cursor-positioning information. The snapshot collection mitigates this for "current state" queries but replay of a colorful session won't look like the original. Acceptable.

3. **Redraws between snapshots can be missed.** If Claude draws a progress bar that updates 10 times in 30 seconds, pipe-pane captures each update as a separate line (cluttering the event log) while the snapshot captures only the final state. The dashboard should probably collapse consecutive near-identical lines in its scrollback view.

4. **Single-host assumption.** The design hardcodes "API and tmux are on the same machine." Multi-host (e.g., running watched shells across multiple remote boxes) is a future extension; it would require an agent on each host that relays over HTTPS/gRPC.

5. **No input capture for keystrokes sent via `ac` directly.** When Ben types in the laptop-attached tmux, those keystrokes arrive at the pane but ARIA only sees the resulting output (and the echo). Fine for transcript purposes, slightly misleading for "who sent this" analysis.

6. **The capture script is a separate Python process per shell.** Three active shells = three Python processes holding a Mongo connection each. Not a problem at the scale of "Ben's personal use" but worth noting.

---

## 19. Incremental PR Breakdown

Each PR is independently useful and mergeable. Stop at any point and what exists still works.

### PR 1 — Capture only

**Goal:** transcripts flowing into Mongo. No API, no UI.

Files:
- `api/aria/shells/__init__.py`
- `api/aria/shells/models.py`
- `api/aria/shells/ansi.py`
- `api/aria/shells/capture.py`
- `api/aria/shells/tmux.py` (minimal: `has_session`, `capture_pane`)
- `api/aria/shells/service.py` (register/insert/get only)
- `scripts/aria-shell-register`
- `scripts/aria-shell-capture`
- `scripts/aria-tmux-hook.conf`
- `scripts/init-mongo.js` (add shells/shell_events/shell_snapshots indexes)
- `.env.example` additions
- `api/aria/config.py` additions (settings)
- `tests/shells/test_ansi.py`
- `tests/shells/test_capture.py`

Acceptance: `ac projectaria` creates a session, capture runs, `db.shell_events.countDocuments({shell_name: "claude-projectaria"})` grows as Ben types.

### PR 2 — Read API + Dashboard tab

**Goal:** watch a session from the phone.

Files:
- `api/aria/api/routes/shells.py` (GET list, GET shell, GET events, GET stream, GET snapshot)
- `api/aria/api/deps.py` (get_shell_service)
- `api/aria/main.py` (register router)
- `api/aria/shells/service.py` (expand with list/tail/stream)
- `api/aria/shells/snapshot.py` (snapshot worker started in lifespan)
- `ui/src/app/dashboard/shells/page.tsx`
- `ui/src/lib/api-client-shells.ts`
- `ui/src/components/dashboard/ShellSidebar.tsx` (add nav entry)
- `tests/shells/test_routes.py` (read endpoints)
- `tests/shells/test_service.py`

Acceptance: open ARIA dashboard on phone over Tailscale, see list of active shells, tap one, see scrollback updating live.

### PR 3 — Write path

**Goal:** steer shells from mobile.

Files:
- `api/aria/shells/service.py` (add `send_input`)
- `api/aria/shells/tmux.py` (add `send_keys`)
- `api/aria/api/routes/shells.py` (POST /input, rate limiter)
- Dashboard: add input box + special keys palette + optimistic echo
- CLI: `aria shells send`
- `tests/shells/test_routes.py` (input endpoint, rate limit)

Acceptance: dashboard input box lands keystrokes in the actual tmux session and Claude sees them.

### PR 4 — Memory + Orchestrator context

**Goal:** ARIA knows what Ben's been working on.

Files:
- `api/aria/shells/extraction.py`
- `api/aria/shells/context.py`
- `api/aria/core/context.py` (integrate `build_shell_context`)
- `api/aria/memory/extraction.py` (add `extract_from_text(source_type, source_id, source_metadata)` if not already present)
- `api/aria/prompts/extraction_shell.md` (optional, if prompt needs tuning)
- `api/aria/core/commands.py` (`/shell tail`, `/shell list`)
- `api/aria/tools/builtin/send_shell_input.py`
- `api/aria/tools/router.py` (register tool, gate on agent config)
- Update ARIA's SOUL.md or system prompt to mention shells
- `tests/shells/test_context.py`
- `tests/shells/test_extraction.py`

Acceptance: ask ARIA "what have I been working on in projectaria today?" and get a grounded answer. `aria memories search "shell capture design"` returns hits sourced from the shell transcript.

### PR 5 — Idle notifier

**Goal:** phone buzzes when Claude is waiting.

Files:
- `api/aria/shells/notifier.py`
- `api/aria/main.py` (start/stop in lifespan)
- `api/aria/config.py` (prompt patterns, intervals)
- `tests/shells/test_notifier.py`

Acceptance: leave Claude hanging at a question for 90s, Signal notification arrives with the last 3 lines as preview.

---

## 20. File-by-File Checklist (Consolidated)

```
NEW
  api/aria/shells/__init__.py                     [PR1]
  api/aria/shells/models.py                       [PR1]
  api/aria/shells/ansi.py                         [PR1]
  api/aria/shells/tmux.py                         [PR1→PR3]
  api/aria/shells/capture.py                      [PR1]
  api/aria/shells/service.py                      [PR1→PR3]
  api/aria/shells/snapshot.py                     [PR2]
  api/aria/shells/context.py                      [PR4]
  api/aria/shells/extraction.py                   [PR4]
  api/aria/shells/notifier.py                     [PR5]
  api/aria/api/routes/shells.py                   [PR2→PR3]
  api/aria/tools/builtin/send_shell_input.py      [PR4]
  api/aria/prompts/extraction_shell.md            [PR4 — optional]
  scripts/aria-shell-register                     [PR1]
  scripts/aria-shell-capture                      [PR1]
  scripts/aria-tmux-hook.conf                     [PR1]
  ui/src/app/dashboard/shells/page.tsx            [PR2→PR3]
  ui/src/lib/api-client-shells.ts                 [PR2→PR3]
  ui/src/components/dashboard/ShellListItem.tsx   [PR2]
  ui/src/components/dashboard/ShellScrollback.tsx [PR2]
  ui/src/components/dashboard/ShellInputBar.tsx   [PR3]
  cli/aria_cli/commands/shells.py                 [PR2→PR3]
  tests/shells/test_ansi.py                       [PR1]
  tests/shells/test_capture.py                    [PR1]
  tests/shells/test_service.py                    [PR1→PR3]
  tests/shells/test_tmux.py                       [PR1]
  tests/shells/test_routes.py                     [PR2→PR3]
  tests/shells/test_context.py                    [PR4]
  tests/shells/test_extraction.py                 [PR4]
  tests/shells/test_notifier.py                   [PR5]

MODIFIED
  api/aria/main.py                                [PR2, PR5] (register router, start workers)
  api/aria/api/deps.py                            [PR2] (get_shell_service)
  api/aria/config.py                              [PR1, PR4, PR5] (settings)
  api/aria/core/context.py                        [PR4] (call build_shell_context)
  api/aria/core/commands.py                       [PR4] (/shell commands)
  api/aria/memory/extraction.py                   [PR4] (extract_from_text if missing)
  api/aria/tools/router.py                        [PR4] (register tool)
  api/aria/prompts/<soul or system>.md            [PR4] (mention shells)
  scripts/init-mongo.js                           [PR1] (new indexes)
  .env.example                                    [PR1, PR4, PR5] (settings)
  cli/aria_cli/main.py                            [PR2] (register shells command group)
  ui/src/components/dashboard/Sidebar.tsx         [PR2] (add nav entry)
  README.md / ARCHITECTURE.md                     [PR1 or final] (document new subsystem)
  CHANGELOG.md                                    [every PR]
  PROJECT_STATUS.md                               [PR1, PR5]
```

---

## 21. Open Questions

1. **Should `ac` run on the Corsair or the laptop?** Currently specced as a laptop function that SSHs in. Alternative: deploy `ac` on the Corsair and invoke it via `ssh corsair ac projectaria`. Pro: one canonical source of truth for the command; con: an extra layer of shell escaping for the project name. Recommend keeping it as a laptop function.

2. **Should watched shells support non-Claude-Code tmux sessions?** The `claude-*` prefix filter is configurable but the extraction prompt and notifier assume Claude Code's prompt conventions. For v1, scope strictly to Claude Code sessions. Widen later if useful.

3. **How does the dashboard render ANSI color?** v1 strips it. v2 could use `ansi_up` or `anser` in the client to render `text_raw` into styled HTML. Decide based on whether Ben misses the colors in the mobile view.

4. **Does the chat orchestrator need a new "active_shells" awareness sensor instead of direct context injection?** The awareness module already has the sensor-pattern scaffolding. Routing shells through `awareness/` would be more consistent with the existing architecture. Counterpoint: awareness runs on a 30-minute schedule and updates a summary file, which is too slow for "what's in my shell right now". Direct context injection is the right call. Keeping this open in case someone prefers the awareness route.

5. **What's the right behavior when a shell's `last_activity_at` is very stale (days old)?** Currently status stays `idle` indefinitely, and it still shows up in the dashboard list. Options: auto-archive after N days of inactivity, or add an explicit `archived` status. Recommend adding an `archived` status transition via a config `SHELLS_AUTO_ARCHIVE_DAYS` (default: 7).

6. **Should the idle notifier escalate if Ben doesn't respond?** The escalation protocol in `notifications/escalation.py` supports re-escalation. A shell idle at a prompt for 5 minutes might warrant a louder ping. Probably over-engineering for v1. Revisit after usage data.

---

*End of design. Hand this and the chat transcript to Claude Code and start with PR 1.*
