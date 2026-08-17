# ARIA

ARIA is a local-first **agent substrate and project steward** that runs as an always-on
service on `corsair-ai`. It owns long-term memory, tool execution, the watched-shell fleet,
coding-session orchestration, the local model control plane, and — since 2026-08-15 — a
steward layer that keeps chartered projects moving while nobody is watching. It is a thing
other programs and a human operator *drive*, not a thing you chat with.

**The conversational front door is Hermes**, a separate agent with its own service, which
reaches ARIA only through ARIA's MCP server (`mcp/server.py`). ARIA's own default chat agent
is deliberately disabled — `POST /api/v1/conversations` returns
`{"detail":"Agent 'aria' is currently disabled"}`, and that is the intended behaviour, not a
broken install. It was a third redundant path that duplicated Hermes and made it easy to
accidentally rebuild a human-facing ARIA chat surface. The Web UI's `/chat` page and
`aria chat` still exist as code and hit the same refusal.

## Read these first

| Doc | What it gives you |
|---|---|
| `/home/ben/Obsidian/vault/ProjectAria/START_HERE.md` | What this system is and how it decides things, in plain English, in five minutes. Not in this repo — it lives in the Obsidian vault, which is synced to Ben's devices. |
| `CLAUDE.md` (this repo) | The authority on how things actually work today: model topology, registries, gotchas, every subsystem. Long, and worth it. |
| This README | What runs, how to start it, how to check it. |

## What actually runs

Verified live 2026-08-15T23:12-04:00:

| Service | Port | How it runs | State |
|---|---|---|---|
| `aria-api` | 8200 | systemd **user** unit `aria-api` — native, not Docker | running |
| `aria-tmux` | — | systemd user unit, oneshot; owns the tmux server for watched shells | `active (exited)` **by design** |
| `aria-ui` | 3000 | Docker, `ProjectAria/docker-compose.yml` | running |
| `shared-mongod` | 27017 | Docker, `infrastructure/docker-compose.yml`, bound `127.0.0.1` | running |
| `shared-mongot` | — | Docker, infrastructure; no host port, reached through mongod | running, but **switched off** in ARIA |
| `shared-embeddings` | 8001 | Docker, infrastructure | **stopped on purpose** |
| `shared-tts` | 8002 | Docker, `infrastructure/docker-compose.yml` | running — `hexgrad/Kokoro-82M`, 27 speakers |
| `aria-stt` | 8003 | Docker, `ProjectAria/docker-compose.yml` | stopped; classified `on_demand` |
| `hermes-gateway` | — | systemd user unit | running |
| `signal-cli` | 8090 | systemd user unit | running |

Model servers, two resident on two separate memory pools:

| Model server | Port | Device | Role |
|---|---|---|---|
| `DS4-0731-IQ3_XXS-Halo-Vulkan` | 8108 | Strix Halo iGPU (`gfx1151`), ~100 GiB | the pi coding agent's single 131K slot |
| `Qwen3.8-27B-R9700-Radiance` | 8080 | Radeon AI PRO R9700 (`gfx1201`), ~29 GiB | Hermes's default + ARIA's background LLM work. vllm-radiance / int4 AutoRound since 2026-08-16 (replaced the llama.cpp GGUF path); 196608 ctx × 1 slot |

**Don't trust the tables above — ask the machine.** `GET /api/v1/infrastructure/running` is a
union read over ARIA's two registries and cannot go stale the way a doc can. The API docs are
at `http://localhost:8200/docs`.

The tts row is an example of why: the non-LLM service registry still describes `:8002` as
"Qwen3-TTS 0.6B" from `ProjectAria/docker-compose.yml`, while the container actually serving
it is `infrastructure-tts` running Kokoro. ARIA's own `aria-tts` container is not running.

## Starting it

Order matters: shared infrastructure first, then the API, then ARIA's own containers.

```bash
# one-time, if the network does not exist yet
docker network create shared-infra

# 1. shared infrastructure — Mongo (replica set rs0), mongot, embeddings.
#    Also used by AgentBenchPlatform; stopping it breaks both projects.
cd /home/ben/Development/infrastructure && docker compose up -d

# 2. ARIA API — native systemd user service, binds :8200
systemctl --user start aria-api
systemctl --user status aria-api
journalctl --user -u aria-api -f

# 3. ARIA's own containers (ui, stt)
cd /home/ben/Development/ProjectAria && docker compose up -d
```

⚠️ **Never hand-run `docker`/`systemctl`/`serve.sh` to start or stop a model server.** All of
that goes through ARIA's model-server registry
(`api/aria/infrastructure/model_servers.py`), which enforces RAM exclusivity, per-GPU-pool
fit and port conflicts that a raw `docker start` does not check. Rule since 2026-07-29. The
concrete cost: starting a Halo-class container by hand while DS4 holds ~100 GiB OOM-kills
something, and it has.

### Verifying

```bash
API_KEY=$(grep -E '^API_KEY=' .env | cut -d= -f2-)

curl -s http://localhost:8200/api/v1/health
curl -s -H "X-API-Key: $API_KEY" http://localhost:8200/api/v1/infrastructure/running
curl -s -H "X-API-Key: $API_KEY" http://localhost:8200/api/v1/capabilities/retrieval
```

`/api/v1/health` currently reports `"status":"degraded"` with `"embeddings":"unreachable"`.
That is expected: both retrieval capabilities were switched off by hand on 2026-08-15, so
memory search runs in `fallback` mode (mongod-native scan, no BM25, no vectors) and 1,849
memories are queued for re-embedding. **Check `/api/v1/capabilities/retrieval` before
concluding recall is broken** — degraded recall is the current deliberate state. Runbook:
`docs/ops/RETRIEVAL_CAPABILITIES.md`.

### Configuration

Everything is `.env` in this directory. Two traps in `.env.example`, which is a stale
skeleton rather than a working template:

- It sets `LLAMACPP_URL=http://localhost:8080/v1`. The live value must be
  `http://localhost:8200/llm/v1` — ARIA's own OpenAI-compatible passthrough, which forwards
  to whichever model server is currently resident. Pinning a model port here broke four
  times running, because the big servers are mutually RAM-exclusive and the named one goes
  down the moment another starts.
- It has no `ADMIN_KEY`. The irreversible routes (killswitch/e-stop deactivate, `PUT /agents`,
  `set_llm_route`, guard merge, policy accept) require it and **fail closed** if it is unset.
  `API_KEY` cannot serve this purpose: anything running as `ben` can read it out of `.env`.

## The steward layer

Shipped 2026-08-15 (`api/aria/steward/`, `api/aria/guard/`). Every project ARIA looks after
has a **charter** in the vault saying what it is for and how far ARIA may go; ARIA writes a
plan; nothing outward-facing happens without Ben's approval on that specific plan. Coding
sessions run in a bwrap sandbox with credentials scrubbed, in a per-session git worktree, and
**ARIA — not the agent — makes the checkpoint commits**, because an agent that can skip its
own checkpoint doesn't have one.

Running right now: the steward tick (30 min), the vault reader (60 s), the meta supervisor
(30 s, ladder cap L4), the outcome scorer (5 min), the paused-shell nudger (15 min) and the
relay watchdog (2 min, 20 min timeout). Every steward worker is **off by default** in a fresh
checkout — a phase is enabled only once its gate passes.

Full design and live status: `vault/ProjectAria/Planning/ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md`.
Plain-English version: `START_HERE.md`. Mechanics: `CLAUDE.md` → *Steward Layer*.

## Operator surfaces

**TUI** — the cockpit, a pure-HTTP Go client with no machine-local assumptions, so it doubles
as a remote cockpit over the tailnet (`aria tui --host corsair`). Screens: `f` fleet,
`j` projects, `h` health, `g` models (load one, and how it loads), `m` memories, `u` usage,
`s` search, `y` shell history, `l` toggles the Ralph loop. Build and transfer recipe:
`tui/README.md`.

**Watched shells** — any tmux session named `claude-*` is adopted automatically and mined for
memories, projects and idle alerts. Gated by `SHELLS_ENABLED`; disables cleanly if tmux is
absent.

```bash
tmux source-file scripts/aria-tmux-hook.conf   # enable the hook once
tmux new -s claude-myproject                   # any name prefixed claude-

aria shells list
aria shells info claude-myproject
aria shells tail claude-myproject --lines 50
aria shells send claude-myproject "yes"
aria shells send claude-myproject "C-c" --no-enter
aria shells search "compilation error"
aria shells tags claude-myproject primary urgent
```

**Web UI** (`http://localhost:3000`) — `/cockpit`, `/operate` (launch configuration + local
model route), `/inbox`, `/autonomy`, `/dashboard/shells` (sidebar list, live scrollback over
SSE, special-key palette, send-input form). `/chat` is a dead page against the disabled agent.

**MCP bridge** — `mcp/server.py`, launched by Hermes from `~/.hermes/config.yaml`, wrapping
`/api/v1`. After editing it, restart `hermes-gateway.service`; there is no per-tool whitelist
on the Hermes side, so that restart alone publishes a new tool.
⚠️ `~/.local/share/aria-mcp/server.py` must stay a **symlink** to this repo's file. It was a
hand-made copy until 2026-08-15 and had drifted 19 tools behind, with the only symptom being
a tool that "didn't exist" for no visible reason.

**Computer use** — the Playwright MCP server is registered against the running API and
persisted in Mongo, so it is restored on startup. To install or re-register it:

```bash
npm install -g @playwright/mcp@latest
node "$(npm root -g)/@playwright/mcp/cli.js" install-browser chrome-for-testing
curl -X POST -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"server_id":"playwright","command":["node","'"$(npm root -g)"'/@playwright/mcp/cli.js","--headless","--isolated","--browser","chromium"]}' \
  http://localhost:8200/api/v1/mcp/servers
```

## Background cadences

| Job | Cadence | Notes |
|---|---|---|
| Dream cycle | every 6h, active hours 01:00–05:00 | `DREAM_ENABLED=true` |
| Awareness | 120 s poll, 30 min analysis | git activity, system health, filesystem |
| Embedding backfill | every 300 s, 100/batch | also wakes immediately when embeddings are switched back on |
| Backups | daily 03:30, `aria-backup.timer` | `scripts/aria-backup.sh` — mongodump of `aria` plus SOUL/journals/skills, with rotation |
| Hourly safety snapshot | hourly, `hourly-safety.timer` | work-in-progress push to a local mirror that refuses rewritten history |

Heartbeat is **off** (`HEARTBEAT_ENABLED=false`) and its config defaults still name a dead
OpenRouter account — do not turn it on without repointing it at a local backend first.

## Things that will cost you an hour

- **`localhost:8092` is connection-refused even though `ss` shows a listener.** It is
  `ridge-llama-proxy`, bound on the tailnet IP only. This has been misdiagnosed repeatedly.
- **DRM enumeration is inverted:** `card0` is the discrete R9700, `card1` is the Strix Halo
  iGPU. Code that reads `card0` to check "GPU memory" reports the wrong pool.
- **The tmux server must be owned by `aria-tmux.service`.** If `aria-api` spawns it first, the
  server lands in aria-api's cgroup and the next `systemctl --user restart aria-api` kills
  every watched session with it. This ate whole days of live sessions before it was found.
- **`-c` is per sequence, not a total to divide.** Total KV = `-c` × `-np`.

`CLAUDE.md` → *Critical Gotchas* has the rest, with the failures that produced them.

## Layout

```
api/aria/          FastAPI service. Subsystems: agents, guard, steward, shells,
                   memory, ontology, planning, workflows, notifications,
                   infrastructure (the two registries), llm, tools, nodes, node.
cli/               Python CLI (`aria`) — pip install -e .
tui/               Go TUI cockpit (`aria tui`, or the raw `aria-tui` binary)
ui/                Next.js web UI
widget/            Tauri v2 tray widget
mcp/               MCP server — Hermes's only path into ARIA
guard/             guard runtime state
scripts/           systemd units, tmux hook, backup, node agent, guard red-team drill
docs/ops/          runbooks that stay in the repo
docs/{design,specs}/  migration stubs pointing into the Obsidian vault
```

Design, spec, analysis and planning docs live in the vault at
`/home/ben/Obsidian/vault/ProjectAria/`, not here. Agent-operational docs (`CLAUDE.md`,
`CHANGELOG.md`, `BACKLOG.md`, `docs/ops/`) stay in the repo.

## Development

```bash
# tests
cd api && python3 -m pytest tests/ -v
cd api && python3 -m pytest tests/ -k "tool"

# API with auto-reload — stop the systemd service first, it already binds :8200
systemctl --user stop aria-api
cd api && uvicorn aria.main:app --reload --host 0.0.0.0 --port 8200

# web UI
cd ui && npm install && npm run dev

# CLI
cd cli && pip install -e .
```

**Desktop widget (Tauri v2).** Tray app, opens with `Ctrl+Space`; set the API URL in its
settings panel. On Linux you need the system dependencies first:

```bash
sudo apt install libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf
cd widget && npm install
npm run tauri:dev
npm run tauri:build      # → widget/src-tauri/target/release/bundle/
```

On Windows: Node.js 18+, the Rust toolchain, and **Visual Studio C++ Build Tools with the
"Desktop development with C++" workload**. `npm run tauri:build` produces installers under
`src-tauri/target/release/bundle/msi/` and `.../nsis/`.

Last updated: 2026-08-15T23:12:39-04:00
