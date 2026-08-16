# ARIA CLI

Command-line client for ARIA - Local AI Agent Platform

## Installation

```bash
pip install -r requirements.txt
pip install -e .
```

## Usage

```bash
# Health check
aria health

# Interactive chat
aria chat

# One-shot message
aria chat "Hello, ARIA!"

# List conversations
aria conversations list

# Continue conversation
aria chat -c <conversation-id> "Continue"

# List agents
aria agents list

# Memory commands
aria memories list
aria memories search "query"
aria memories add "fact" --type fact

# Launch the Go TUI (the cockpit)
aria tui
aria tui --host corsair          # remote: point at another host (~/.config/aria/hosts)
```

`aria tui` resolves the `aria-tui` binary (from `$ARIA_TUI_BIN`, the repo's `tui/`
dir, or PATH) and execs it; extra args pass through. See `tui/README.md` for host
profiles and running the cockpit remotely (e.g. from a MacBook over the tailnet).

## Configuration

- `ARIA_API_URL` (default `http://localhost:8200`) — the API base URL; set it to
  point the CLI/TUI at a remote host.
- `ARIA_API_KEY` — the `X-API-Key` sent on every request.

## Requirements

- Python 3.12+
- ARIA API running (http://localhost:8200)

## Documentation

See the main repository README for full documentation.
