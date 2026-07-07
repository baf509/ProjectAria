# Getting Started with ARIA

Operator setup + troubleshooting guide. For the project overview, capability
tour, and architecture, see **[README.md](README.md)**. For the TUI / remote
cockpit, see **[tui/README.md](tui/README.md)**.

ARIA is the single always-on service on this host. The **API runs as a native
systemd user service** (not a Docker container) so it has filesystem/process
access; only the UI, TTS, and STT run in Docker. It depends on **shared
infrastructure** (MongoDB, mongot, local LLMs, embeddings) that lives in a
separate project at `../infrastructure/` and is shared with AgentBenchPlatform —
**start it first.**

---

## Prerequisites

- **Docker** and **Docker Compose** — [Install Docker](https://docs.docker.com/get-docker/)
- **Git** — [Install Git](https://git-scm.com/downloads)
- **systemd (user services)** — the API runs as `aria-api` under `systemctl --user`
- **Python 3.12+** — for the CLI client (optional)
- **Node.js 18+** and **Rust** — for building the desktop widget (optional)

**For the local LLMs with ROCm (AMD GPU/APU):**
- AMD GPU or APU with ROCm support (gfx1151, gfx1150, gfx120X, gfx110X)
- `/dev/kfd` and `/dev/dri` device access
- User in the `video` and `render` groups

---

## Step 1: Start Shared Infrastructure

Provides MongoDB (`rs0` replica set), mongot, the three local LLMs, and the
embedding service. Set it up first.

```bash
cd /home/ben/Development/infrastructure

# Create the Docker network (one-time)
docker network create shared-infra

# Configure environment
cp .env.example .env
# Edit .env as needed (e.g. LLAMACPP_GPU_TARGET to match your hardware)

# Start core shared services (mongod, mongot, embeddings)
docker compose up -d

# Start the local LLMs (qwen-chat, qwen-agentic, context-1)
cd qwen-rocmfp4 && docker compose up -d && cd ..
```

Verify services are responding:

```bash
docker compose ps
curl http://localhost:8001/health   # embeddings
curl http://localhost:8092/health   # qwen-chat  (35B-A3B)
curl http://localhost:8093/health   # qwen-agentic (27B)
curl http://localhost:8081/health   # context-1 (Search Agent backend)
```

See `../infrastructure/README.md` for full configuration details.

---

## Step 2: Clone and Configure ARIA

```bash
git clone https://github.com/baf509/ProjectAria.git
cd ProjectAria

cp .env.example .env
```

Edit `.env`. Defaults for MongoDB, embeddings, and the local LLM URLs point at
the shared infra by container name and need no changes:

```bash
# === MongoDB (shared infra — defaults work) ===
MONGODB_URI=mongodb://mongod:27017/?directConnection=true&replicaSet=rs0
MONGODB_DATABASE=aria

# === Embeddings (shared infra — defaults work) ===
EMBEDDING_URL=http://embeddings:8001/v1
EMBEDDING_MODEL=voyageai/voyage-4-nano
EMBEDDING_DIMENSION=1024

# === Default agent backend (GLM 5.2 via Fireworks) ===
FIREWORKS_API_KEY=

# === Optional: other cloud LLM keys ===
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=
```

The default agents (ARIA orchestrator + Pi Coding Agent) run on **GLM 5.2 via
Fireworks**, so `FIREWORKS_API_KEY` is required for out-of-the-box chat. Backend
+ model are chosen **per agent** (config rows in `db.agents`) — see the LLM
Backends table in the README — so you can point an agent at a local qwen backend
instead if you prefer.

---

## Step 3: Start ARIA Services

```bash
# 1. Start the API (native systemd user service, binds :8200)
systemctl --user start aria-api
systemctl --user status aria-api
journalctl --user -u aria-api -f      # follow logs (Ctrl+C to stop)

# 2. Start the Docker services (ui, tts, stt)
cd /home/ben/Development/ProjectAria && docker compose up -d
docker compose ps
```

**First `docker compose up` will take a few minutes** — it builds the UI image
and (if enabled) downloads the TTS (Qwen3-TTS 0.6B) and STT
(whisper-large-v3-turbo) models.

---

## Step 4: Verify the Installation

```bash
# API health
curl http://localhost:8200/api/v1/health
# Concurrent probe of every backing service (mongod, mongot, the three
# local LLMs, embeddings, tts, stt, fireworks):
curl http://localhost:8200/api/v1/health/services

# Backing services directly
curl http://localhost:8001/health   # embeddings
curl http://localhost:8092/health   # qwen-chat
curl http://localhost:8002/health   # tts (if enabled)
curl http://localhost:8003/health   # stt (if enabled)
```

- Web UI: **http://localhost:3000**
- API docs (Swagger): **http://localhost:8200/docs**

---

## Step 5: Start Using ARIA

### Web UI
Open **http://localhost:3000** — create a conversation and start chatting.

### CLI (optional)
```bash
cd cli
pip install -e .

aria chat "Hello, ARIA!"
aria conversations list
aria chat -c CONVERSATION_ID "Tell me more"

aria memories search "query"
aria memories add "Important fact to remember"

aria tui                              # launch the TUI cockpit
```

### TUI (the cockpit)
```bash
cd tui
make install                          # → ~/.local/bin/aria-tui  (or: aria tui)
aria-tui
```

Run it on another machine (e.g. a MacBook) against this host over the tailnet
instead of SSHing in — build with `make build-darwin`, add a
`~/.config/aria/hosts` profile, then `aria-tui --host corsair`. Full recipe in
**[tui/README.md](tui/README.md)**.

### API directly
```bash
CONV_ID=$(curl -s -X POST http://localhost:8200/api/v1/conversations \
  -H "Content-Type: application/json" \
  -d '{"title":"My Chat"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

curl -N -X POST "http://localhost:8200/api/v1/conversations/$CONV_ID/messages" \
  -H "Content-Type: application/json" \
  -d '{"content":"Hello ARIA!","stream":true}'
```

---

## Desktop Widget (Optional)

Tauri v2 app that lives in the system tray and opens with `Ctrl+Space`. Once
running, open its settings panel and set the API URL to your ARIA server
(default `http://localhost:8200`).

### Linux
```bash
# Install Tauri system dependencies
sudo apt install libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf

cd widget
npm install
npm run tauri:dev                     # dev mode
npm run tauri:build                   # → widget/src-tauri/target/release/bundle/
```

### Windows
**Prerequisites:** Node.js 18+ ([nodejs.org](https://nodejs.org)), the Rust
toolchain ([rustup.rs](https://rustup.rs)), and **Visual Studio C++ Build
Tools** with the "Desktop development with C++" workload.

```powershell
cd widget
npm install
npm run tauri:dev                     # dev mode (hot-reload)
npm run tauri:build
# → widget\src-tauri\target\release\bundle\msi\   (MSI installer)
# → widget\src-tauri\target\release\bundle\nsis\  (NSIS installer)
```

---

## Services Reference

| Service | Port | How it runs | Description |
|---------|------|-------------|-------------|
| API | 8200 | systemd user service (`aria-api`) | FastAPI backend (native) |
| Web UI | 3000 | Docker (this repo) | Next.js chat interface |
| TTS | 8002 | Docker (this repo) | Qwen3-TTS 0.6B (CPU) |
| STT | 8003 | Docker (this repo) | whisper-large-v3-turbo (CPU) |
| mongod | 27017 | Docker (infrastructure) | MongoDB 8.2, replica set `rs0` |
| mongot | 27028 | Docker (infrastructure) | Vector + text search |
| embeddings | 8001 | Docker (infrastructure) | voyage-4-nano (1024-dim, CPU) |
| qwen-chat | 8092 | Docker (infrastructure/qwen-rocmfp4) | Qwen3.6 35B-A3B (ROCm) |
| qwen-agentic | 8093 | Docker (infrastructure/qwen-rocmfp4) | Qwen3.6 27B (ROCm) |
| context-1 | 8081 | Docker (infrastructure/qwen-rocmfp4) | context-1 20B, Search Agent backend |

> The default chat/coding model (GLM 5.2) is **cloud via Fireworks**, not on the
> GPU box. The old single `llama.cpp` on `:8080` is retired.

### Starting and Stopping

```bash
# Start (order matters — infra first)
cd /home/ben/Development/infrastructure && docker compose up -d
cd qwen-rocmfp4 && docker compose up -d && cd ..
systemctl --user start aria-api
cd /home/ben/Development/ProjectAria && docker compose up -d

# Stop / restart the API
systemctl --user stop aria-api
systemctl --user restart aria-api

# Stop ARIA Docker services (data lives in shared infra volumes)
docker compose down

# Stop shared infra (also affects AgentBenchPlatform!)
cd /home/ben/Development/infrastructure && docker compose down
# ...and delete all data:
docker compose down -v
```

---

## Troubleshooting

### Shared infrastructure not running
ARIA requires the shared infra first. If API health checks fail with connection
errors:
```bash
cd /home/ben/Development/infrastructure && docker compose ps
docker compose up -d              # start if not running
# wait for mongod to be healthy, then:
systemctl --user restart aria-api
```

### API won't start
```bash
systemctl --user status aria-api
journalctl --user -u aria-api -n 100 --no-pager
# check for Python import errors / missing dependencies / bad .env, then:
systemctl --user restart aria-api
```

### MongoDB replica set issues
```bash
cd /home/ben/Development/infrastructure
docker compose logs mongod

# Manually initialize the replica set
docker exec -it shared-mongod mongosh --eval \
  "rs.initiate({_id:'rs0',members:[{_id:0,host:'mongod:27017'}]})"

# Re-run index creation
docker compose run --rm mongo-init
```

### Local LLMs won't start
```bash
cd /home/ben/Development/infrastructure/qwen-rocmfp4
docker compose logs qwen-chat

# Check GPU access and group membership
ls -la /dev/kfd /dev/dri
groups                            # should include: video render
```
For gfx1150/gfx1151 APUs, if you see out-of-memory errors despite available
VRAM, add `ttm.pages_limit=12582912` to the kernel command line and reboot.

### Embedding service issues
```bash
curl http://localhost:8001/health
cd /home/ben/Development/infrastructure && docker compose logs embeddings

curl http://localhost:8001/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input":"test","model":"voyageai/voyage-4-nano"}'
```

### Memory search returns nothing
```bash
docker exec -it shared-mongod mongosh --eval "
  use aria;
  db.memories.getSearchIndexes();
"
# If empty, re-run initialization and wait ~30s for indexes to activate:
cd /home/ben/Development/infrastructure && docker compose run --rm mongo-init
```

### Widget build fails (Linux)
```bash
sudo apt install libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf
rustc --version                   # need 1.70+
cd widget
rm -rf node_modules src-tauri/target
npm install && npm run tauri:dev
```

### Widget build fails (Windows)
```powershell
rustc --version                   # need 1.70+
# Ensure "Desktop development with C++" is checked in the Visual Studio Installer
cd widget
Remove-Item -Recurse -Force node_modules, src-tauri\target
npm install; npm run tauri:dev
```
