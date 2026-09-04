# ProjectAria agent guide

This file is the repository-local operating guide for coding agents. It does not
override the vault-root `Architecture_Charter.md`.

Last reconciled: **2026-08-29**.

## Read first

1. `/Users/ben/Obsidian/Architecture_Charter.md` — desired-state authority.
2. `/Users/ben/Obsidian/ProjectAria/START_HERE.md` — system orientation.
3. `/Users/ben/Obsidian/ProjectAria/Design/ARCHITECTURE.md` — maintained observed topology.
4. This repository's `CHANGELOG.md`, `BACKLOG.md`, and relevant `docs/ops/*` runbook.

On Corsair, the synchronized vault projection is `/home/ben/Obsidian/vault`.
Runtime state and functional probes win over prose for volatile health/model
facts. Reviewed commits win over dirty or duplicated checkouts for source truth.

## Current machine boundary

- The MacBook Pro is the permanent ARIA control plane and a direct development
  host. It owns ARIA API/UI, MongoDB/mongot, Hermes/Signal, credentials,
  embeddings/TTS/Gemma, canonical general project trees, watched shells, and
  operational state.
- Corsair is the primary model data plane. It owns Qwen3.8 weights/runtimes,
  hardware tooling, benchmarks, restricted model actuation, and a thin
  `aria-node` compatibility runtime.
- Do not start or repair a second ARIA API/UI, MongoDB, mongot, Hermes, Signal,
  or general autonomous-agent service on Corsair.
- The Mac source checkout is
  `/Users/ben/Development/Infrastructure/ProjectAria`; the separately deployed
  service tree is `/Users/ben/Services/apps/ProjectAria`.
- `/home/ben/Development/ProjectAria` is a noncanonical Corsair
  compatibility/recovery checkout. Its presence is not authorization to restore
  the former Corsair control plane.
- All current Mac services execute as `ben`. `devboxsvc` and `devboxagent` names
  are historical and must not appear in new instructions or installers.

## Product boundary

ARIA is an agent substrate, operations cockpit, and project steward. Hermes is
the human conversational front door over Signal and reaches ARIA through its MCP
bridge. ARIA's default `aria` chat agent is deliberately disabled; the UI/CLI
conversation code may return that refusal and should not be "repaired" into a
third conversational front door.

ARIA owns:

- machine, service, model, task, project, session, and shell registries;
- watched tmux shells and coding-session supervision;
- model routing and desired/observed reconciliation;
- Mongo-backed memories, metrics, alerts, and operational history;
- charter-driven stewardship, guardrails, checkpoints, and approval handling;
- the API, web cockpit, TUI/CLI, and Hermes MCP surface.

The vault owns prose, decisions, runbooks, and Ben's approval fields. Operational
state must not exist only in Markdown; prose knowledge should not be trapped only
in Mongo.

## Network and inference rules

- ARIA API: `http://bens-macbook-pro.tailb286a5.ts.net:8200`
- UI: `http://bens-macbook-pro.tailb286a5.ts.net:3000`
- Gateway: `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1`
- Identified gateway: `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified`

Services bind to loopback or an authenticated/private tailnet interface by
default. Raw model ports are loopback-only and consumed through Mac-managed SSH
forwards and the gateway. Do not publish raw model ports, put broad ARIA keys in
interactive Corsair shells, or change host power/network/Tailscale settings.

Every local-model consumer should go through the gateway. Known direct Hermes
Radiance/Gemma routes are migration gaps to close, not exemptions. Cloud clients
remain subject to the charter's unresolved cloud-exception rule.

## Current model plane

| Deployment | Host/device | Listener | Role |
|---|---|---|---|
| `Qwen3.8-27B-R9700-Radiance` | Corsair R9700 | `127.0.0.1:8080` | resident general model and Pi option |
| `Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K` | Corsair Strix Halo | `127.0.0.1:8120` | resident long-context Pi option; live geometry is 1 × 256K |
| Gemma 4 E4B Q4 | Mac | `127.0.0.1:8104` | auxiliary workers |

DeepSeek V4 weights/runtimes may remain on Corsair for rollback, testing, and
model engineering. They are retained-but-inactive and are not part of the
default listener topology.

ARIA's registry owns desired state; backend identity/readiness plus the host's
process manager own observed state. A port alone is not identity. Routine model
lifecycle uses the restricted actuator. Direct `systemctl`/runtime work is
allowed only for an authorized model repair/test; ARIA must observe and reconcile
the result.

Hardware constraints:

- R9700 discrete VRAM and Halo GTT are separate pools, though checkpoint loading
  can pressure host-wide memory.
- On Corsair, DRM `card0` is the R9700 and `card1` is the Halo.
- Runtime-specific Vulkan/ROCm device numbering must be verified, never copied.
- llama.cpp `-c` is per sequence; total KV scales with `-c × -np`.

The Flash registry compatibility slug still says `2x256K`; correct its static
geometry/runtime metadata before treating the label as literal.

## Pi Coding invariant

Pi is an external coding harness, not an ARIA persona. Every managed Pi
installation has exactly:

- provider `aria`;
- model `Qwen3.8-27B-R9700-Radiance`;
- model `Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K`;
- base URL `/llm/v1-identified` on the Mac;
- an inference-only scoped credential.

No Fireworks provider, raw Corsair endpoint, cloud fallback, or third Pi model is
permitted. The legacy `pi-coding-ridge` profile name is retained only as a
compatibility selector for Flash Next. ARIA owns the shell, worktree selection,
capture, concurrency, watchdog, review, and Ralph loop; Pi owns its transcript
and coding tools.

## Repository map

```text
api/aria/             FastAPI application
api/aria/agents/      coding sessions, watchdog, budget/e-stop/review
api/aria/shells/      watched-shell capture and fleet operations
api/aria/memory/      retrieval, extraction, backfill
api/aria/infrastructure/ model and service registries, routing, probes
api/aria/steward/     charter-driven project stewardship
api/aria/guard/       sandbox/worktree/merge guard
api/aria/nodes/       central node registry/queue
api/aria/node/        thin remote node client
mcp/server.py         Hermes MCP bridge
cli/                  Python operations client
tui/                  Go cockpit
ui/                   Next.js operations UI
scripts/              launchers, hooks, migrations, recovery tooling
docs/ops/             current operational runbooks
systemd/              historical pre-Mac control-plane units only
```

## Safety and data rules

- `ADMIN_KEY` gates irreversible routes and fails closed. Do not weaken it or
  give it to Hermes/Pi/general interactive agents.
- Do not expose MongoDB on the tailnet or let node agents connect directly.
- Preserve the 1024-dimensional embedding contract unless performing a planned
  index/vector migration.
- Disabled capabilities degrade explicitly and should not generate incidents.
- Do not infer a sandbox/account boundary from logical node IDs: the unified Mac
  deployment currently runs as `ben`.
- A coding/model agent may start a registered model for an authorized repair or
  test, but may not silently create an unregistered deployment.
- Use worktrees/checkpoints/merge gates for ARIA-managed autonomous work. Direct
  interactive operator sessions are distinct and must still be registered and
  watched through ARIA shells.
- Never rewrite human-owned charter/approval content. Agent vault writes go
  through ARIA's guarded Obsidian writer.

## Common development commands

```bash
# API
cd api
python3 -m pytest tests/ -q
uvicorn aria.main:app --reload --host 127.0.0.1 --port 18200

# UI
cd ui
npm install
npm run typecheck
npm run build
npm run gate

# CLI
cd cli
pip install -e .

# TUI
cd tui
make build
```

Production is already using `:8200` and `:3000` on the Mac. Development servers
must use spare loopback ports. Source changes do not modify the deployed service
tree; deploy deliberately and verify the live build and health afterward.

## Verification

```bash
# Mac
curl -fsS http://127.0.0.1:8200/api/v1/health
curl -fsS http://127.0.0.1:3000/ >/dev/null
sudo launchctl print system/com.ben.devbox.aria-api
sudo launchctl print system/com.ben.devbox.aria-ui

# Corsair observed data plane
systemctl --user is-active aria-node.service
systemctl --user is-active qwen3.8-radiance.service qwen3.8-flash-next.service
ss -ltn | rg '127.0.0.1:(8080|8120)'
```

Authenticated ARIA registry/service/health responses should be compared with
host-native process state and backend identity. The current retrieval state is
documented in `docs/ops/RETRIEVAL_CAPABILITIES.md`; model operations are in
`docs/ops/LOCAL_INFERENCE_TOPOLOGY.md`; UI deployment is in
`docs/ops/WEB_UI.md`.

## Documentation policy

Design/spec/analysis/research/planning notes live in the Obsidian vault. Current
agent-operational docs live in the repository. Dated migration and benchmark
records may retain historical paths and models only when labeled historical.
Subordinate docs may add detail but cannot contradict the Architecture Charter.
