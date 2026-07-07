# ARIA TUI — the cockpit

A Go (Bubble Tea) terminal dashboard for ARIA: a 4-quadrant dashboard (task tree,
session detail, tools, vitals) plus Chat, Fleet, Coding-session, Memory, Usage,
Tools, Observations, DB, Health, and Search screens.

It is a **thin, pure-HTTP client** — every interaction is `/api/v1` over HTTP with
an `X-API-Key` header, and nothing touches the local machine (no tmux, no
subprocess, no cgo). That's what lets it double as a **remote cockpit**: run it on
any machine and point it at the ARIA API on another (e.g. from a MacBook at
corsair over the tailnet, instead of SSHing in).

## Build & run

```bash
# native build for this machine
make build            # → ./aria-tui         (GO=... to override the toolchain)
make install          # → ~/.local/bin/aria-tui
./aria-tui            # or: aria tui   (launches via the Python CLI)

# cross-compile the cockpit binary
make build-darwin     # → ./aria-tui-darwin-arm64  (Apple Silicon, CGO off)
make build-linux      # → ./aria-tui-linux-amd64
```

> `go` may not be on PATH (SDK-managed toolchain). Override per-invocation:
> `make build GO=/home/ben/go-sdk/go/bin/go`.

## Pointing it at a host

Resolution precedence for the API base URL + key:

1. **`--host <name|host:port|url>`** flag (and `--api-key`)
2. **`ARIA_API_URL` / `ARIA_API_KEY`** env (or `.env`; `API_KEY` accepted too)
3. the **`default`** profile in the hosts file
4. built-in **`http://localhost:8200`**

**Host profiles** — `~/.config/aria/hosts` (override with `$ARIA_HOSTS`), a simple
`key = value` file:

```ini
default     = corsair
corsair.url = http://corsair-ai:8200
corsair.key = <the API_KEY from corsair's .env>
local.url   = http://localhost:8200
```

Then: `aria-tui --host corsair` (or `--host corsair-ai:8200`, or `--host http://…`).

**`.env` fallback chain** (real env vars still win): `$ARIA_ENV` →
`~/.config/aria/env` → `~/Development/ProjectAria/.env`.

## Keybindings (highlights)

- Dashboard: `c` chat · `f` fleet · `m` memory · `u` usage · `t` tools · `h` health · `s` search · `q` quit
- **Fleet** (`f`): `↑↓` select a session · `l` toggle the **Ralph loop** · `⏎` open · `r` refresh
- **Session** screen: `⏎` send input · `s` stop · `l` toggle the **Ralph loop** · `r` refresh

The **Ralph loop** keeps a coding session going — the watchdog nudges it forward
whenever it idles at its prompt until it emits `RALPH_DONE` or hits a nudge/deadline
cap. A `⟳` marks looping sessions; a HOST column shows which machine each shell
runs on (blank for coding sessions until the multi-machine node lands).

---

## Running the cockpit on a MacBook (remote, over Tailscale)

The Mac doesn't need the repo, Go, or SSH into corsair — just the binary + a host
profile. **Gotcha:** a darwin/arm64 binary cross-compiled on Linux is *unsigned*,
and Apple Silicon refuses to run unsigned binaries, so there's a **one-time
`codesign`** step on the Mac (you already have `codesign` via the Xcode tools).

**1. On corsair — build it and grab the key:**
```bash
cd ~/Development/ProjectAria/tui && make build-darwin GO=/home/ben/go-sdk/go/bin/go
grep '^API_KEY' ~/Development/ProjectAria/.env      # paste into the Mac hosts file
```

**2. On corsair — send it (Taildrop; no SSH needed):**
```bash
tailscale file cp aria-tui-darwin-arm64 bens-macbook-air:
# — or scp, if Remote Login / Tailscale SSH is enabled on the Mac:
# scp aria-tui-darwin-arm64 ben@bens-macbook-air:~/Downloads/
```

**3. On the Mac — receive, sign, configure, run:**
```bash
tailscale file get ~/Downloads/                     # if sent via Taildrop

mkdir -p ~/.local/bin
mv ~/Downloads/aria-tui-darwin-arm64 ~/.local/bin/aria-tui
chmod +x ~/.local/bin/aria-tui
xattr -dr com.apple.quarantine ~/.local/bin/aria-tui 2>/dev/null || true
codesign -s - --force ~/.local/bin/aria-tui         # ad-hoc sign (REQUIRED)

mkdir -p ~/.config/aria
cat > ~/.config/aria/hosts <<'EOF'
default     = corsair
corsair.url = http://corsair-ai:8200
corsair.key = PASTE_API_KEY_HERE
EOF

~/.local/bin/aria-tui --host corsair
```

**Notes**
- `corsair-ai` resolves via Tailscale MagicDNS; if not, use the IP
  (`corsair.url = http://100.123.245.84:8200`).
- Can't connect? The API binds `0.0.0.0:8200`, so the usual blocker is a firewall
  on corsair — allow `:8200` on the `tailscale0` interface. Transport is plain HTTP
  encrypted at the WireGuard layer (fine on a closed tailnet).
- Have Go on the Mac? `make install` builds a Mac-native (self-signed) binary and
  skips the transfer + `codesign` entirely.

## Related

- [`../MULTI_MACHINE_FLEET_DESIGN.md`](../MULTI_MACHINE_FLEET_DESIGN.md) — the plan
  to make the **fleet itself** span corsair + Mac (Layer A remote cockpit is done;
  Layer B2 the `aria-node` agent is planned).
