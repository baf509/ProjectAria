# Aria Shells: direct attach and responsive screens

Existing Mac sessions attach straight to tmux before checking API readiness,
credentials, or agent launchers. A failed attach returns its error; it does not
start a second coding process. New sessions still use Aria registration.

From an SSH prompt outside tmux, in the same project directory:

```sh
codex                  # reattach the running Codex session
codex --aria-view      # read-only; this client's size does not shrink the pane
codex --aria-takeover  # detach other clients of this session, keeping its agent alive
```

The same flags work with `claude` and `pi`, and with the Corsair compatibility
wrapper. The default remains shared attachment with `window-size smallest`.
Takeover is explicit because attaching a narrow iPad client can resize the
shared application. In the browser, **Resize shared pane** asks for confirmation
before resizing for everyone. Font size and wrapping affect only the web view.

Aria retains the live process across SSH disconnects; native agent resume
retains the agent's conversation after that process has ended. Neither feature
requires replaying Aria's raw output history to launch an agent. Existing native
history and Aria's operational-history pruning policy remain in place.

## Ownership and deployment

On the Mac API configure:

```dotenv
NODE_SHELL_HOST_ALIASES={"mac-agents":"bens-macbook-pro"}
```

On both Mac node-agent launch environments configure:

```dotenv
ARIA_NODE_CAPTURE_ENABLED=false
```

This keeps the logical `mac-agents` command queue and its coding execution
capability. Node heartbeats omit shell inventory, and old capture spools are
not replayed while capture is disabled. The API adopter owns local shell
registration; one `pipe-pane` process per pane owns raw output, with the API
snapshot worker preserving historical snapshots. Existing local shell rows
whose host is `mac-agents` must be normalized to `bens-macbook-pro` at rollout.
Do not rewrite coding-session hosts: those identify command workers.

Deploy capture code to the API runtime, then replace each local pipe with
`tmux pipe-pane -t %PANE_ID 'CAPTURE_SHIM SHELL_NAME'` (without `-o`). Do not
restart tmux or the coding agents. Restart node workers only when their command
queue has no pending or claimed work. Keep their existing spools as recovery
artifacts. Install the local wrapper in `~/.config/aria/aria-local-shell`.

## Live web path

`GET /shells/{name}/screen/stream` emits current visible screens over SSE.
Visible viewers share one reader per shell per API process. The local hot path
uses a single bounded-time tmux capture, with no database access per refresh.
Pipe capture sends nonblocking Unix datagram hints (names only) to
`~/.local/state/aria/shell-screen.sock`; captures coalesce at 100 ms. A two-second
fallback repairs lost hints and refreshes remote snapshots. One-slot queues
replace obsolete screens for slow clients; payloads are capped at 100,000
characters. Hidden tabs close their subscription, and reconnect receives the
current screen. The existing Follow stream remains cursor-based history.

The Unix socket is mode 0600 and protected with an advisory lock. A second API
worker or unavailable socket falls back to shared polling. The current Mac API
uses one worker. `ARIA_SHELL_SCREEN_SOCKET` can override the path; set it alike
for API and capture and keep it within the platform's Unix socket path limit.

Capture drains terminal output independently of persistence. Its waiting
buffer is limited to 4 MiB and 10,000 records, plus one bounded in-flight batch;
oldest operational output is dropped on overflow. Mongo retries preserve event
IDs and allocated line numbers. EOF gets a bounded two-second drain period.

These changes remove Aria startup waits and duplicated capture. Termius's
interactive keystrokes still travel through SSH and tmux, so Wi-Fi latency,
terminal rendering, and large agent redraws can still affect iPad responsiveness.

## Rollout validation — 2026-09-08

Deployed to the Mac API, both Mac node-worker roles, the production web UI, and
installed local/Corsair attach wrappers. No coding agent or tmux pane was
restarted. All nine pre-existing pane IDs and process IDs survived. The mobile
clients changed height during the work; pane geometry follows attached clients
unless view/takeover controls are used.

| Measurement | Result |
|---|---:|
| Changed output → direct API stream, 25 updates | 142.9 ms median; 347.0 ms p95 |
| Changed output → web BFF stream, 25 updates | 144.1 ms median; 348.0 ms p95 |
| First web stream screen | 237.6 ms |
| Local reconnect startup to tmux handoff, 15 runs | 396.8 ms median; 581.1 ms maximum |
| Mac shell ownership, six samples over 12 seconds | Physical Mac owner throughout |
| Recent local output writers, three-minute sample | Only `pipe-pane` (219 events) |

The stream measurement used a disposable pane printing monotonic timestamps
at 400 ms intervals, with two simultaneous subscribers through the API and web
BFF. It excludes the initial pre-subscription timestamp. These are local Mac
measurements, not iPad network/rendering measurements. The reconnect measurement
includes fresh Python startup, exact pane liveness and batched options, with an
unreachable API configured, stopping at the interactive tmux handoff. It does
not measure the SSH connection or terminal first paint. The earlier 250 ms
reconnect target was not met in this sample; the API dependency is removed and
cold HTTP/TLS imports are avoided, but Mac process/command overhead remains.

Validation passed: all 104 backend checks after the final capture fix; the
production UI type/lint/build checks; tablet and desktop browser contract tests;
and a real production tablet-sized browser exercise covering current output,
hidden-tab disconnect, foreground resubscription, and no console errors.
A real tmux PTY test verified read-only viewing preserved shared geometry and
explicit takeover detached only the test session's other clients while keeping
its process alive. Capture no longer upserts shell registrations, preventing
its final EOF flush from recreating purged shell rows.

Raw results: [aria-shells-fast-validation-2026-09-08.json](aria-shells-fast-validation-2026-09-08.json).
The production UI build is `6aa61e0`; subsequent fixes affect backend/wrappers.
