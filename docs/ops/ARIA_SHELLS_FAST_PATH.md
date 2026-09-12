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
