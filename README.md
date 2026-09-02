# ARIA

ARIA is Ben's local-first agent control plane and project steward. The canonical
control plane runs on the MacBook Pro; Corsair is its primary model data plane.
ARIA is infrastructure other programs and operators drive. Hermes, not ARIA's
disabled default chat agent, is the conversational front door over Signal.

Last reconciled against the live deployment: **2026-09-02**.

## Deployment boundary

| Owner | Current responsibility |
|---|---|
| MacBook Pro (`bens-macbook-pro`) | ARIA API/UI, MongoDB/mongot, Hermes/Signal, embeddings, TTS, Mac-native Gemma, canonical general projects, watched shells, credentials, and operational state |
| Corsair (`corsair-ai`) | Qwen3.8 Radiance and Qwen3.8 Flash Next weights/runtimes, GPU tooling, benchmarks, restricted model actuation, and the thin `aria-node` compatibility runtime |
| `red`, `ridge` | Registered on-demand GPU nodes reached through Mac-managed proxies |
| NAS | CouchDB LiveSync hub and recovery repositories |

The desired-state authority is the vault-root `Architecture_Charter.md`. The
maintained observed-topology explanation is
`ProjectAria/Design/ARCHITECTURE.md`. Runtime state and functional probes win for
volatile facts.

The Mac source checkout is
`/Users/ben/Development/Infrastructure/ProjectAria`; production is deployed to
`/Users/ben/Services/apps/ProjectAria`. They are deliberately separate. Editing
source does not deploy it. `/home/ben/Development/ProjectAria` on Corsair is a
noncanonical compatibility/recovery checkout, not a second control plane.

## Live services

The Mac runs the production services as `ben` through system-domain launchd
jobs. The old `devboxsvc` and `devboxagent` paths and identities are historical.

| Component | Listener/surface | Manager |
|---|---|---|
| ARIA API and inference gateway | loopback `:8200`, tailnet-published `:8200` | `com.ben.devbox.aria-api` |
| ARIA UI | loopback `:3000`, tailnet-published `:3000` | `com.ben.devbox.aria-ui` |
| Hermes and Signal | private gateway and `:8090` | Mac launchd services |
| MongoDB/mongot, embeddings, TTS, Gemma | private/loopback | Mac launchd plus the Linux compatibility layer required by mongot |
| Corsair node agent | outbound to the Mac API | `aria-node.service` on Corsair |

Verified model data plane:

| Model | Corsair listener | Use |
|---|---|---|
| `Qwen3.8-27B-R9700-Radiance` | `127.0.0.1:8080` | general workhorse and Pi model |
| `Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K` | `127.0.0.1:8120` | long-context Pi model; live geometry is 1 × 256K despite the retained legacy slug |

DeepSeek V4 weights and experiments may remain on Corsair for rollback and model
engineering, but no DeepSeek listener is part of the default topology.

Raw model ports stay loopback-bound. Mac-managed SSH forwards feed the ARIA
gateway; clients must not target Corsair model listeners directly.

Interactive `claude`, `codex`, and `pi` commands create or reattach watched
ARIA tmux shells. Mac execution is the default; `--corsair`/`--remote` selects
an explicitly mapped Corsair worktree. The Mac API owns local shell creation;
Corsair's `aria-node` observes and captures its `claude-*` tmux namespace.
Disconnects reattach to the live shell, and recreation uses the resume-aware
tool launchers. A full machine reboot does not automatically start every
historical shell. During a cold boot, the wrappers wait up to five minutes for
ARIA readiness (Mongo, migrations, and workers) instead of launching an
unmanaged fallback or failing immediately on a temporarily closed API socket.

Install or refresh the maintained Mac wrappers from the canonical checkout:

```bash
/Users/ben/Development/Infrastructure/ProjectAria/scripts/aria-desk-install-mac
```

The installer requires an existing scoped key in `~/.config/aria/env`; it never
copies the production service environment or embeds a broad key.

## Operator entry points

- API: `http://bens-macbook-pro.tailb286a5.ts.net:8200`
- UI: `http://bens-macbook-pro.tailb286a5.ts.net:3000`
- API docs on the Mac: `http://127.0.0.1:8200/docs`
- Inference gateway: `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1`
- Identified-model gateway for Pi: `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified`

The current UI publication is private tailnet TCP on `:3000`. Publishing the
same Mac UI on tailnet HTTPS `:443`, and removing Corsair's stale `:443` rule,
remains an operational gap; do not describe the stale Corsair HTTPS endpoint as
working.

## Pi Coding policy

Every managed Pi installation has one provider, `aria`, and exactly two models:

- `Qwen3.8-27B-R9700-Radiance`
- `Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K`

Both use the Mac `/llm/v1-identified` gateway with an inference-only credential.
No Fireworks provider, cloud fallback, raw Corsair URL, or additional registered
Pi model is allowed. ARIA owns the shell, capture, watchdog, review, and model
awareness; Pi owns its coding transcript and tools.

## Verify before changing anything

```bash
# Mac
curl -fsS http://127.0.0.1:8200/api/v1/health/ready
curl -fsS http://127.0.0.1:3000/
sudo launchctl print system/com.ben.devbox.aria-api
sudo launchctl print system/com.ben.devbox.aria-ui

# Corsair — observation only
systemctl --user is-active aria-node.service
systemctl is-active qwen3.8-radiance.service qwen3.8-flash-next.service
ss -ltn | rg '127.0.0.1:(8080|8120)'
```

Authenticated infrastructure checks should compare ARIA's registry with the
backend identity/readiness probes. A healthy API does not by itself prove that a
shared-port model has the expected identity.

For a post-boot end-to-end check, run
`scripts/aria-boot-check --wait 300 --canary-shell`. It verifies process state,
application readiness, the deployed MCP contract, node heartbeats, and one
non-paid managed shell create/remove cycle without printing credentials.
The matching plist is installed as a per-login LaunchAgent, so it needs no
administrator password and exercises the same user/session context as the desk
wrappers.

Routine model lifecycle changes go through ARIA's restricted actuator. Direct
service work on Corsair is appropriate for an authorized model repair/test, but
ARIA must observe and reconcile the result.

## Development

```bash
# API tests from the canonical Mac source checkout
cd /Users/ben/Development/Infrastructure/ProjectAria/api
python3 -m pytest tests/ -v

# UI checks
cd /Users/ben/Development/Infrastructure/ProjectAria
make ui-check

# Run a development API without colliding with production :8200
cd api
uvicorn aria.main:app --reload --host 127.0.0.1 --port 18200
```

Do not use the legacy Corsair systemd/Docker instructions to repair the Mac
control plane. Deploy and restart Mac services deliberately through the current
launchd/service-tree procedure.

## Documentation map

- `CLAUDE.md` — repository mechanics and agent constraints
- `docs/ops/WEB_UI.md` — current UI deployment and publication runbook
- `docs/ops/LOCAL_INFERENCE_TOPOLOGY.md` — current model routing and hardware constraints
- `docs/ops/RETRIEVAL_CAPABILITIES.md` — retrieval switches and recovery
- `tui/README.md`, `cli/README.md`, `ui/README.md` — client-specific use
- Vault `ProjectAria/START_HERE.md` — plain-language orientation
- Vault `ProjectAria/Design/ARCHITECTURE.md` — maintained current topology

ARIA's default conversational agent remains intentionally disabled. `aria chat`
and direct conversation creation against agent `aria` refuse by design; use
Hermes for conversation and the UI/TUI/CLI for operations.
