# ARIA CLI

Python client for the single ARIA API on the MacBook Pro.

## Install

```bash
pip install -r requirements.txt
pip install -e .
```

## Configure

```bash
export ARIA_API_URL=http://bens-macbook-pro.tailb286a5.ts.net:8200
export ARIA_API_KEY='<scoped key>'
```

Use the least-privileged key appropriate to the client. Never place a broad
control-plane key in an interactive Corsair shell.

## Common operations

```bash
aria health
aria agents list
aria memories list
aria memories search 'query'
aria shells list
aria tui
aria tui --host mac
```

`aria chat` and conversation creation against the default `aria` agent refuse
by design. Hermes/Signal is the conversational front door; the CLI is primarily
an operational client.

`aria tui` resolves the `aria-tui` binary and passes through remaining flags.
The host profile should point at the Mac ARIA API, not a Corsair `:8200` service.
See `tui/README.md`.
