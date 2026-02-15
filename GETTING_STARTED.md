# Getting Started with ARIA

Complete guide to setting up and running ARIA on your machine.

---

## What You'll Set Up

| Service | Purpose | Port |
|---------|---------|------|
| **ARIA API** | FastAPI backend — chat, memory, tools | 8000 |
| **MongoDB** (mongod) | Data storage — conversations, memories, agents | 27017 |
| **MongoDB Search** (mongot) | Vector + text search engine | 27028 |
| **Embeddings** | voyage-4-nano embeddings (CPU, sentence-transformers) | 8001 |
| **llama.cpp** (optional) | Local LLM with ROCm GPU acceleration | 8080 |
| **Web UI** | Next.js chat interface | 3000 |

---

## Prerequisites

- **Docker** and **Docker Compose** — [Install Docker](https://docs.docker.com/get-docker/)
- **Git** — [Install Git](https://git-scm.com/downloads)
- **Node.js 18+** — For the desktop widget (optional)
- **Rust** — For building the desktop widget (optional)
- **Python 3.12+** — For the CLI client (optional)

**For llama.cpp with ROCm (AMD GPU/APU users):**
- AMD GPU or APU with ROCm support (gfx1151, gfx1150, gfx120X, gfx110X)
- `/dev/kfd` and `/dev/dri` device access
- User in `video` and `render` groups

---

## Step 1: Clone and Configure

```bash
git clone https://github.com/baf509/ProjectAria.git
cd ProjectAria

# Create your environment file
cp .env.example .env
```

Edit `.env` with your settings:

```bash
# === Required ===

# MongoDB (defaults work — no changes needed)
MONGODB_URI=mongodb://mongod:27017/?directConnection=true&replicaSet=rs0
MONGODB_DATABASE=aria

# Embeddings (defaults work — local sentence-transformers service)
EMBEDDING_URL=http://embeddings:8001/v1
EMBEDDING_MODEL=voyageai/voyage-4-nano
EMBEDDING_DIMENSION=1024

# === Optional: llama.cpp with ROCm ===

# Set your GPU target (gfx1151 for Ryzen AI MAX+ Pro 395)
LLAMACPP_GPU_TARGET=gfx1151

# Path to your GGUF model file
LLAMACPP_MODEL=/models/model.gguf

# Directory containing your GGUF files (mounted into the container)
LLAMACPP_MODELS_DIR=./models

# GPU layers to offload (99 = all layers)
LLAMACPP_GPU_LAYERS=99

# Context window size
LLAMACPP_CTX_SIZE=8192

# === Optional: Cloud LLM API Keys ===

ANTHROPIC_API_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=
```

---

## Step 2: Download a Model (for llama.cpp users)

If you plan to use llama.cpp with ROCm, place a GGUF model in the `models/` directory:

```bash
# Example: download a model (replace with your preferred model)
mkdir -p models
cd models

# Option A: Use huggingface-cli
pip install huggingface-hub
huggingface-cli download TheBloke/Llama-2-7B-Chat-GGUF llama-2-7b-chat.Q4_K_M.gguf --local-dir .

# Option B: Download directly with curl/wget
# (find GGUF files on https://huggingface.co)

cd ..
```

Then update `.env`:
```bash
LLAMACPP_MODEL=/models/llama-2-7b-chat.Q4_K_M.gguf
```

If you're only using cloud LLMs, skip this step.

---

## Step 3: Start the Stack

```bash
# Start all services
docker compose up -d

# Watch the logs to confirm everything starts
docker compose logs -f
# (Ctrl+C to stop following logs)
```

**First run will take a few minutes** — Docker needs to:
- Build the API image
- Build the embedding service image (downloads the voyage-4-nano model)
- Build the llama.cpp ROCm image (downloads ~450MB of pre-built binaries)
- Pull MongoDB images
- Initialize the replica set and search indexes

Check that services are healthy:

```bash
docker compose ps
```

Expected output:
```
NAME              STATUS
aria-api          running
aria-mongod       running (healthy)
aria-mongot       running
aria-embeddings   running
aria-llamacpp     running        # only if using llama.cpp
aria-ui           running
aria-mongo-init   exited (0)     # one-time setup, expected to exit
```

---

## Step 4: Verify the Installation

```bash
# Check API health
curl http://localhost:8000/api/v1/health

# Expected:
# {"status":"healthy","version":"0.2.0","database":"connected",...}

# Check LLM backends
curl http://localhost:8000/api/v1/health/llm

# Check embedding service
curl http://localhost:8001/health

# Check llama.cpp is serving (if using)
curl http://localhost:8080/health
```

Open the Web UI: **http://localhost:3000**

Open the API docs: **http://localhost:8000/docs**

---

## Step 5: Configure Your Agent

ARIA creates a default agent on first run. To switch it to use llama.cpp:

```bash
# List agents to get the agent ID
curl -s http://localhost:8000/api/v1/agents | python3 -m json.tool

# Update agent to use llama.cpp
curl -X PUT http://localhost:8000/api/v1/agents/YOUR_AGENT_ID \
  -H "Content-Type: application/json" \
  -d '{
    "llm": {
      "backend": "llamacpp",
      "model": "default",
      "temperature": 0.7,
      "max_tokens": 4096
    },
    "fallback_chain": [{
      "backend": "openrouter",
      "model": "anthropic/claude-3.5-sonnet",
      "conditions": {"on_error": true}
    }]
  }'
```

---

## Step 6: Start Chatting

### Web UI

Open **http://localhost:3000** — create a conversation and start chatting.

### CLI (optional)

```bash
cd cli
pip install -r requirements.txt
pip install -e .

# Chat
aria chat "Hello, ARIA!"

# Continue a conversation
aria conversations list
aria chat -c CONVERSATION_ID "Tell me more"

# Memory commands
aria memories list
aria memories search "query"
aria memories add "Important fact to remember"
```

### API directly

```bash
# Create a conversation
CONV_ID=$(curl -s -X POST http://localhost:8000/api/v1/conversations \
  -H "Content-Type: application/json" \
  -d '{"title":"My Chat"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Send a message (streaming)
curl -N -X POST "http://localhost:8000/api/v1/conversations/$CONV_ID/messages" \
  -H "Content-Type: application/json" \
  -d '{"content":"Hello ARIA!","stream":true}'
```

---

## Step 7: Desktop Widget (Optional)

The desktop widget is a Tauri app that lives in your system tray and opens with `Ctrl+Space`.

### Linux

```bash
# Install Tauri system dependencies
sudo apt install libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf

# Install dependencies and run
cd widget
npm install

# Run in development mode
npm run tauri:dev

# Build for production
npm run tauri:build
# Output: widget/src-tauri/target/release/bundle/
```

### Windows

**Prerequisites:**
1. **Node.js 18+** — [nodejs.org](https://nodejs.org)
2. **Rust toolchain** — Install via [rustup.rs](https://rustup.rs)
3. **Visual Studio C++ Build Tools** — Install from [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-studio-build-tools/), select **"Desktop development with C++"** workload

```powershell
cd widget

# Install dependencies
npm install

# Run in development mode (with hot-reload)
npm run tauri:dev

# Build for production (.exe / .msi installer)
npm run tauri:build
# Output: widget\src-tauri\target\release\bundle\msi\  (MSI installer)
# Output: widget\src-tauri\target\release\bundle\nsis\ (NSIS installer)
```

### Configuration

Once running, open the settings panel in the widget and set the API URL to your ARIA server (e.g., `http://your-server:8000`). The default is `http://localhost:8000`.

**Widget features:**
- `Ctrl+Space` — Toggle the chat window
- `Escape` — Hide the window
- System tray icon with menu (Show, New Chat, Quit)
- Streaming responses from the ARIA API
- Settings panel for API URL configuration

---

## Services Reference

### Starting and Stopping

```bash
# Start everything
docker compose up -d

# Stop everything (data is preserved in volumes)
docker compose down

# Stop and delete all data (fresh start)
docker compose down -v

# Restart a single service
docker compose restart api

# View logs
docker compose logs -f api          # API logs
docker compose logs -f llamacpp     # llama.cpp logs
docker compose logs -f embeddings   # Embedding service logs
docker compose logs -f mongod       # MongoDB logs
```

### Service URLs

| Service | URL | Description |
|---------|-----|-------------|
| Web UI | http://localhost:3000 | Chat interface |
| API | http://localhost:8000 | REST API |
| API Docs | http://localhost:8000/docs | Swagger/OpenAPI |
| Embeddings | http://localhost:8001 | OpenAI-compatible embedding API |
| llama.cpp | http://localhost:8080 | OpenAI-compatible API |

### Useful API Endpoints

```bash
# Health
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/health/llm

# Conversations
curl http://localhost:8000/api/v1/conversations
curl http://localhost:8000/api/v1/conversations/ID

# Memories
curl http://localhost:8000/api/v1/memories
curl -X POST http://localhost:8000/api/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query":"search term","limit":10}'

# Agents
curl http://localhost:8000/api/v1/agents

# Tools
curl http://localhost:8000/api/v1/tools
```

---

## llama.cpp ROCm Configuration

The llama.cpp service uses pre-built ROCm binaries from [lemonade-sdk/llamacpp-rocm](https://github.com/lemonade-sdk/llamacpp-rocm).

### Supported GPU Targets

| Target | Hardware |
|--------|----------|
| `gfx1151` | Ryzen AI MAX+ Pro 395 (STX Halo APU) |
| `gfx1150` | Ryzen AI 300 (STX Point APU) |
| `gfx120X` | Radeon RX 9070/9060 (RDNA4) |
| `gfx110X` | Radeon RX 7900/7800/7700/7600 (RDNA3) |

Change the target in `.env`:
```bash
LLAMACPP_GPU_TARGET=gfx1151   # Change to match your hardware
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLAMACPP_GPU_TARGET` | `gfx1151` | ROCm GPU architecture target |
| `LLAMACPP_MODEL` | `/models/model.gguf` | Path to GGUF model inside container |
| `LLAMACPP_MODELS_DIR` | `./models` | Host directory mounted to `/models` |
| `LLAMACPP_GPU_LAYERS` | `99` | Number of layers to offload to GPU |
| `LLAMACPP_CTX_SIZE` | `8192` | Context window size |

### Linux APU Note

For gfx1150/gfx1151 APUs, if you see out-of-memory errors despite available VRAM, add this to your kernel command line parameters and reboot:
```
ttm.pages_limit=12582912
```

---

## Troubleshooting

### API won't start

```bash
docker compose logs api
# Check for Python import errors or missing dependencies
docker compose restart api
```

### MongoDB replica set issues

```bash
# Check mongod health
docker compose logs mongod

# Manually initialize replica set
docker exec -it aria-mongod mongosh --eval "rs.initiate({_id:'rs0',members:[{_id:0,host:'mongod:27017'}]})"

# Re-run index creation
docker compose up mongo-init
```

### llama.cpp won't start

```bash
# Check logs
docker compose logs llamacpp

# Verify model file exists
ls -la models/*.gguf

# Check GPU access
ls -la /dev/kfd /dev/dri

# Verify user is in video/render groups
groups
# Should include: video render
```

### Embedding service issues

```bash
# Check embedding service health
curl http://localhost:8001/health

# Check logs
docker compose logs embeddings

# Test embedding generation
curl http://localhost:8001/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input":"test","model":"voyageai/voyage-4-nano"}'
```

### Memory search returns nothing

```bash
# Check search indexes exist
docker exec -it aria-mongod mongosh --eval "
  use aria;
  db.memories.getSearchIndexes();
"

# If empty, re-run initialization
docker compose up mongo-init
# Wait 30 seconds for indexes to activate
```

### Widget build fails (Linux)

```bash
# Install system dependencies (Ubuntu/Debian)
sudo apt install libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf

# Verify Rust is installed
rustc --version  # Need 1.70+

# Clean and rebuild
cd widget
rm -rf node_modules src-tauri/target
npm install
npm run tauri:dev
```

### Widget build fails (Windows)

```powershell
# Verify Rust is installed
rustc --version  # Need 1.70+

# Verify Visual Studio Build Tools are installed
# Open "Visual Studio Installer" and ensure "Desktop development with C++" is checked

# Clean and rebuild
cd widget
Remove-Item -Recurse -Force node_modules, src-tauri\target
npm install
npm run tauri:dev
```

---

## What's Next

After setup, try:

1. **Chat** — Send messages via Web UI or CLI
2. **Test memory** — Tell ARIA a fact ("My favorite language is Rust"), then ask about it in a new conversation
3. **Add tools** — Enable tools in agent config for filesystem/shell access
4. **Add MCP servers** — Connect external tool servers

Future plans:
- Voice input/output (STT/TTS)
- React Native mobile app
- Computer use (screen control)
