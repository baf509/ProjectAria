# ARIA

ARIA is a local-first **agent substrate, control plane, and project steward**. Its API, UI,
MongoDB/mongot, Hermes/Signal gateway, and supporting services run continuously on Ben's
MacBook Pro. Corsair is the Linux compute and default coding-agent host: it owns the large-model
runtimes, model data and benchmarks, mapped project worktrees, and Emulator tooling. ARIA
controls and observes that remote data plane through its Corsair node/actuator. It is a thing
other programs and a human operator *drive*, not a thing you chat with.

**The conversational front door is Hermes**, a separate agent with its own service, which
reaches ARIA only through ARIA's MCP server (`mcp/server.py`). ARIA's own default chat agent
is deliberately disabled — `POST /api/v1/conversations` returns
`{"detail":"Agent 'aria' is currently disabled"}`, and that is the intended behaviour, not a
broken install. It was a third redundant path that duplicated Hermes and made it easy to
accidentally rebuild a human-facing ARIA chat surface. The Web UI's `/chat` page and
`aria chat` still exist as code and hit the same refusal.

> **Verified deployment boundary (2026-08-27):** The Mac owns the always-on control plane,
> personal Obsidian vault, Hermes/Signal, and native support services. Interactive
> `claude`, `codex`, and `pi` sessions launched from mapped Mac projects route to Corsair by
> default; `--local` is the explicit trusted-operator escape hatch. Corsair owns the default
> agent execution environment, project worktrees, Emulator access, and large-model data plane.
> The canonical current-state document is
> `/Users/ben/Obsidian/ProjectAria/Design/ARCHITECTURE.md`.

## Read these first

| Doc | What it gives you |
|---|---|
| The Mac Obsidian vault's `ProjectAria/Design/ARCHITECTURE.md` | Canonical human-readable current deployment and source-of-truth hierarchy. |
| The Mac Obsidian vault's `ProjectAria/START_HERE.md` | Plain-English orientation. |
| `CLAUDE.md` (this repo) | Repository internals, model registries, operational constraints, and gotchas. |
| This README | Deployment summary and verification entry points. |

## What actually runs

Verified live 2026-08-27T16:45-04:00. The Mac runs these native LaunchDaemons:

| Component | Port | LaunchDaemon(s) | Runtime identity |
|---|---|---|---|
| ARIA control plane | 8200, 3000 | `aria-api`, `aria-ui` | `devboxsvc` |
| MongoDB + Linux-only mongot | local/tunnelled | `mongo-tunnel`, `lima-mongot` | `devboxsvc` plus Linux VM/container |
| Hermes + Signal | gateway + 8090 | `hermes-gateway`, `signal-cli` | `devboxsvc` |
| Local inference support | 8001, 8002, Gemma port | `embeddings`, `tts`, `gemma` | `devboxsvc` |
| Remote connectivity | forwarded ports | `corsair-forwards`, `ridge-proxy`, `red-proxy`, `wake-relay` | `devboxsvc` |
| Vault replication | none public | `obsidian-bridge` | `devboxsvc` |
| Managed autonomous node | ARIA node API | `aria-agent-node` | `devboxagent` |

Corsair's directly verified active model services are:

| Model server | Corsair listener | Runtime | Status |
|---|---|---|---|
| `Qwen3.8-27B-R9700-Radiance` | `127.0.0.1:8080` | `qwen3.8-radiance.service` | active |
| `Qwen3.8-Flash-Next-IQ4_XS-Halo` | `127.0.0.1:8120` | `qwen3.8-flash-next.service` | active |

DeepSeek V4/DS4 is not an active production model. Its principal weights were retired on
2026-08-26; disabled runtimes, archives, and benchmarking utilities remain on Corsair.

**Ask both the control plane and the machine.** ARIA's deployed API currently sees Radiance
but omits the active Flash Next service even though the source registry contains it. The Mac
forward has a healthy route for `:8080`, no route for `:8120`, and a legacy `:8112` route that
resets. Until the registry is redeployed and forwarding is repaired, direct Corsair
`systemctl`/listener inspection is the observed-state authority for models. API docs are at
`http://127.0.0.1:8200/docs`.

## Starting it

The production services are managed by launchd on the Mac and systemd/Docker on Corsair. Do
not use the old Docker Compose sequence from pre-migration documentation.

```bash
# Mac: inspect production services
sudo launchctl print system/com.ben.devbox.aria-api
sudo launchctl print system/com.ben.devbox.aria-ui
curl -fsS http://127.0.0.1:8200/api/v1/health

# Corsair: inspect the remote node and model data plane
systemctl --user is-active aria-node.service
systemctl is-active qwen3.8-radiance.service qwen3.8-flash-next.service
```

⚠️ **Use ARIA's restricted Corsair actuator for routine model lifecycle operations.** Direct
`systemctl` remains the break-glass and model-development path on Corsair. When a model is
promoted there, update and deploy ARIA's registry and the Mac forward in the same change;
otherwise the control panel will not become current automatically.

### Verifying

```bash
curl -s http://localhost:8200/api/v1/health
# Authenticated where configured:
curl -s -H "X-API-Key: $API_KEY" http://localhost:8200/api/v1/nodes
curl -s -H "X-API-Key: $API_KEY" http://localhost:8200/api/v1/infrastructure/running
```

For model truth, compare the ARIA response with Corsair's active units and listeners. A
successful ARIA health response proves the control plane is up; it does not prove every model
unit has been discovered. Retrieval runbook: `docs/ops/RETRIEVAL_CAPABILITIES.md`.

### Configuration

The source checkout uses `.env` for development. Production configuration is service-managed
under `/Users/devboxsvc/Services`; changing this repo's `.env` does not change the running
Mac service. Two historical traps in `.env.example`, which remains a skeleton rather than a
working production template:

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
plan; nothing outward-facing happens without Ben's approval on that specific plan. The guard
and checkpoint design still applies to jobs ARIA launches through its managed agent path.
It does **not** describe ordinary interactive Claude/Codex/Pi sessions: mapped-project shells
route to Corsair by default, run as Corsair's `ben`, and are trusted operator sessions rather
than bwrap-isolated sandboxes. Native Mac Claude sessions are also intentionally unsandboxed
after removal of the system-wide managed policy on 2026-08-27. Codex retains its own
workspace-write/on-request defaults.

Running right now: the steward tick (30 min), the vault reader (60 s), the meta supervisor
(30 s, ladder cap L4), the outcome scorer (5 min), the paused-shell nudger (15 min) and the
relay watchdog (2 min, 20 min timeout). Every steward worker is **off by default** in a fresh
checkout — a phase is enabled only once its gate passes.

Historical design: the vault's
`ProjectAria/Planning/ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md`. Current deployment:
`ProjectAria/Design/ARCHITECTURE.md`. Mechanics: `CLAUDE.md` → *Steward Layer*.

## Operator surfaces

**TUI** — the cockpit, a pure-HTTP Go client with no machine-local assumptions, so it doubles
as a remote cockpit over the tailnet when pointed at the Mac ARIA API. Screens: `f` fleet,
`j` projects, `h` health, `g` models (load one, and how it loads), `m` memories, `u` usage,
`s` search, `y` shell history, `l` toggles the Ralph loop. Build and transfer recipe:
`tui/README.md`.

**Aria Shells** — the Mac shell wrappers maintain named tmux sessions and route mapped
projects to Corsair by default. Legacy session names may still begin with `claude-`; the tmux
status branding is `Aria Shells`, and the active agent name is shown separately. ARIA can
observe adopted shells when shell tracking is enabled.

```bash
tmux source-file scripts/aria-tmux-hook.conf   # source the repo hook when managing tmux manually
tmux new -s claude-myproject                   # any name prefixed claude-
claude / codex / pi                            # Mac zsh wrappers: mapped projects route to Corsair

aria shells list
aria shells info claude-myproject
aria shells tail claude-myproject --lines 50
aria shells send claude-myproject "yes"
aria shells send claude-myproject "C-c" --no-enter
aria shells search "compilation error"
aria shells tags claude-myproject primary urgent
```

**Web UI** (`http://100.125.251.55:3000` over the tailnet;
`http://127.0.0.1:3000` on the Mac) — `/supervise` (was `/cockpit`), `/operate` (launch configuration + local
model route), `/inbox`, `/autonomy`, `/dashboard/shells` (sidebar list, live scrollback over
SSE, special-key palette, send-input form). `/chat` is a dead page against the disabled agent.

**MCP bridge** — `mcp/server.py`, deployed into the Mac service tree and launched by the
Hermes gateway, wraps `/api/v1`. Source changes do not alter the running service until they
are deliberately deployed; restart the Mac `com.ben.devbox.hermes-gateway` LaunchDaemon only
as part of that deployment. Old instructions referring to `hermes-gateway.service` and a
Corsair home-directory symlink describe the pre-migration installation.

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

These are subsystem design/default cadences from the source tree, not a substitute for
checking the deployed Mac process configuration:

| Job | Cadence | Notes |
|---|---|---|
| Dream cycle | every 6h, active hours 01:00–05:00 | `DREAM_ENABLED=true` |
| Awareness | 120 s poll, 30 min analysis | git activity, system health, filesystem |
| Embedding backfill | every 300 s, 100/batch | also wakes immediately when embeddings are switched back on |
| Backups | historical systemd schedule | Mac data protection is Time Machine; Corsair service/model recovery uses the established Restic/NAS path |
| Hourly safety snapshot | historical systemd schedule | confirm the deployed launchd equivalent before relying on it |

Heartbeat is **off** (`HEARTBEAT_ENABLED=false`) and its config defaults still name a dead
OpenRouter account — do not turn it on without repointing it at a local backend first.

## Things that will cost you an hour

- **`localhost:8092` is connection-refused even though `ss` shows a listener.** It is
  `ridge-llama-proxy`, bound on the tailnet IP only. This has been misdiagnosed repeatedly.
- **DRM enumeration is inverted:** `card0` is the discrete R9700, `card1` is the Strix Halo
  iGPU. Code that reads `card0` to check "GPU memory" reports the wrong pool.
- **The old `aria-tmux.service` cgroup warning is historical.** It still explains why tmux
  ownership matters on Corsair, but the Mac ARIA API itself is now a LaunchDaemon and default
  interactive sessions are routed to Corsair by the shell client.
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
scripts/           service templates, tmux hook, backup, node agent, guard red-team drill
docs/ops/          runbooks that stay in the repo
docs/{design,specs}/  migration stubs pointing into the Obsidian vault
```

Design, spec, analysis and planning docs live in the vault at
`/Users/ben/Obsidian/ProjectAria/` on the Mac. Corsair's synchronized service projection is
`/home/ben/Obsidian/vault/ProjectAria/`. Agent-operational docs (`CLAUDE.md`,
`CHANGELOG.md`, `BACKLOG.md`, `docs/ops/`) stay in the repo.

## Development

```bash
# tests
cd api && python3 -m pytest tests/ -v
cd api && python3 -m pytest tests/ -k "tool"

# API with auto-reload — do not collide with the production Mac LaunchDaemon on :8200;
# use a different port, or deliberately unload the service through the migration tooling.
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

Last verified: 2026-08-27T16:45:00-04:00
