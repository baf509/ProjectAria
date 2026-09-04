# ARIA TUI — the cockpit

A Go/Bubble Tea operations client for ARIA: tasks, sessions, fleet, coding,
models, memories, usage, tools, observations, database health, and search.

It is a thin HTTP client. It does not access local tmux, MongoDB, or model ports;
every interaction goes to the single Mac ARIA API with an API key.

## Build

```bash
make build
make install
./aria-tui

# Optional cross-builds
make build-darwin
make build-linux
```

## Host configuration

Resolution precedence is `--host`/`--api-key`, environment, the default entry in
`~/.config/aria/hosts`, then `http://localhost:8200`.

```ini
default = mac
mac.url = http://bens-macbook-pro.tailb286a5.ts.net:8200
mac.key = <scoped API key>
```

Then run `aria-tui --host mac` or `aria tui --host mac`.

Do not create a `corsair.url = http://corsair-ai:8200` profile. Corsair is a
model data plane and runs only the thin node agent; it must not host a second
ARIA API. Do not copy a broad ARIA key into Corsair.

Important controls include Fleet, Health, Models, Memories, Usage, Search,
Shells, and the opt-in Ralph loop. The exact key map is shown in the running
client's help screen and should be treated as more current than copied prose.

An Apple Silicon binary cross-compiled on Linux may need a one-time ad-hoc
signature on the Mac:

```bash
xattr -dr com.apple.quarantine ~/.local/bin/aria-tui 2>/dev/null || true
codesign -s - --force ~/.local/bin/aria-tui
```
