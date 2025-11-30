# Getting Started with ARIA

**Complete guide to understanding, installing, and using ARIA**

---

## What is ARIA?

ARIA is a **self-hosted AI agent platform** with:
- 🧠 **Long-term memory** - Remembers facts and preferences across conversations
- 🔧 **Local-first** - Runs on your hardware (Ollama + MongoDB)
- 💬 **CLI interface** - Chat via terminal (Web UI coming in Phase 5)
- 🔌 **Extensible** - Add tools and MCP servers (Phase 3+)

Think of it as your personal AI assistant that runs entirely on your infrastructure and remembers everything you tell it.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│ You interact with:                                          │
│                                                             │
│  • CLI (aria chat)                                         │
│  • API (http://localhost:8000)                             │
│  • Swagger Docs (http://localhost:8000/docs)              │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ Docker Compose Stack (runs on your machine):               │
│                                                             │
│  [1] ARIA API (FastAPI)                                    │
│      - Port 8000                                           │
│      - Handles chat, memory, agents                        │
│      - Orchestrates LLM calls                              │
│                                                             │
│  [2] MongoDB (mongod)                                      │
│      - Port 27017                                          │
│      - Stores conversations, memories, agents              │
│      - Replica set for search features                     │
│                                                             │
│  [3] MongoDB Search (mongot)                               │
│      - Port 27028                                          │
│      - Vector search (embeddings)                          │
│      - Text search (BM25)                                  │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ External (must be running separately):                     │
│                                                             │
│  [4] Ollama                                                │
│      - Port 11434                                          │
│      - LLM inference (e.g., Llama 3.2)                    │
│      - Embedding generation (e.g., Qwen3)                 │
│      - Install: https://ollama.ai                          │
└─────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

### Required

1. **Docker Desktop** (or Docker + Docker Compose)
   - [Install Docker](https://docs.docker.com/get-docker/)
   - Verify: `docker --version` and `docker compose version`

2. **Ollama** (for local LLM inference)
   - [Install Ollama](https://ollama.ai/download)
   - Verify: `ollama --version`

3. **Git** (to clone the repository)
   - [Install Git](https://git-scm.com/downloads)

### Optional

4. **Python 3.12+** (for CLI client)
   - Only needed if you want the `aria` CLI command
   - Can use API directly without Python

---

## Installation Steps

### Step 1: Install Ollama and Pull Models

```bash
# Install Ollama from https://ollama.ai/download

# Pull an LLM model (for chat)
ollama pull llama3.2

# Pull an embedding model (for memory)
ollama pull qwen3:8b

# Verify Ollama is running
curl http://localhost:11434/api/version
```

**Why these models?**
- `llama3.2` - Fast, capable chat model
- `qwen3:8b` - Generates embeddings for memory search

### Step 2: Clone ARIA Repository

```bash
git clone https://github.com/baf509/ProjectAria.git
cd ProjectAria
```

### Step 3: Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit if needed (defaults work for local setup)
nano .env  # or use your favorite editor
```

**Default configuration** (in `.env`):
```bash
# MongoDB (managed by Docker)
MONGODB_URI=mongodb://mongod:27017/?directConnection=true&replicaSet=rs0
MONGODB_DATABASE=aria

# Ollama (running on your host machine)
OLLAMA_URL=http://host.docker.internal:11434

# Embeddings
EMBEDDING_PROVIDER=ollama
EMBEDDING_OLLAMA_MODEL=qwen3:8b
EMBEDDING_DIMENSION=4096

# Optional: Cloud LLM fallback
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
```

### Step 4: Start Docker Services

```bash
# Start all services in background
docker compose up -d

# Check status (all should be "running" or "exited" for mongo-init)
docker compose ps

# View logs (Ctrl+C to exit)
docker compose logs -f

# View specific service logs
docker compose logs api
docker compose logs mongod
```

**Expected services:**
- `aria-api` - Running (port 8000)
- `aria-mongod` - Running (port 27017)
- `aria-mongot` - Running (port 27028)
- `aria-mongo-init` - Exited (0) - one-time setup

### Step 5: Verify Installation

```bash
# Test API health
curl http://localhost:8000/api/v1/health

# Expected response:
# {
#   "status": "healthy",
#   "version": "0.2.0",
#   "database": "connected",
#   "timestamp": "2025-11-29T..."
# }

# Open API documentation in browser
open http://localhost:8000/docs  # Mac
# or visit: http://localhost:8000/docs
```

### Step 6: Install CLI (Optional but Recommended)

```bash
cd cli
pip install -r requirements.txt
pip install -e .

# Test CLI
aria health

# Should show:
# ✓ Status: healthy
# ✓ Version: 0.2.0
# ✓ Database: connected
```

---

## How to Use ARIA

### Option 1: CLI (Recommended)

```bash
# Interactive chat mode
aria chat
# You: Hello!
# ARIA: Hi! How can I help you today?

# One-shot message
aria chat "What's the weather like?"

# Start new conversation
aria chat --new "Let's discuss Python"

# Continue specific conversation
aria conversations list
aria chat -c <conversation-id> "Continue from before"

# List all conversations
aria conversations list

# List agents
aria agents list

# Memory commands
aria memories list
aria memories search "Python preferences"
aria memories add "User prefers tabs over spaces" --type preference
```

### Option 2: Direct API Calls

```bash
# Create conversation
curl -X POST http://localhost:8000/api/v1/conversations \
  -H "Content-Type: application/json" \
  -d '{"title":"My Chat"}'

# Send message (streaming response)
curl -X POST http://localhost:8000/api/v1/conversations/{id}/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"Hello!","stream":true}'

# Search memories
curl -X POST http://localhost:8000/api/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{"query":"Python","limit":10}'
```

### Option 3: Swagger UI (Interactive)

Visit **http://localhost:8000/docs** in your browser for interactive API documentation.

---

## Key Concepts

### 1. Conversations
- A conversation is a chat session with ARIA
- Messages are stored with full history
- Conversations persist across restarts
- Each conversation uses an agent configuration

### 2. Agents
- Define the AI's personality and capabilities
- Default agent "ARIA" is created automatically
- Configure LLM backend, temperature, tools, memory settings
- You can create multiple agents with different roles

### 3. Memory System (Phase 2)

**Short-term Memory:**
- Last 20 messages from current conversation
- Recent conversations (last 24 hours)
- Fast retrieval, no embeddings needed

**Long-term Memory:**
- Facts, preferences, events stored permanently
- Hybrid search (Vector + BM25)
- Automatically extracted from conversations
- Manually add important facts

**How it works:**
1. You chat: "I prefer Python for backend work"
2. ARIA responds normally
3. Background task extracts memory: "User prefers Python for backend"
4. Memory stored with embedding vector
5. Future chats: ARIA searches memories and finds relevant context
6. Response uses your preferences!

### 4. Memory Commands

```bash
# Add memory manually
aria memories add "User is building an AI platform called ARIA" \
  --type fact \
  --importance 0.9 \
  --categories "projects,work"

# Search memories
aria memories search "what am I working on"
# Returns: User is building an AI platform called ARIA

# List all memories
aria memories list --type preference

# Extract from conversation
aria memories extract <conversation-id>
```

---

## What Works Now (Phase 1 & 2)

✅ **Chat with local LLM** via Ollama
✅ **Streaming responses** in real-time
✅ **Conversation management** (create, list, continue)
✅ **Short-term memory** (recent context)
✅ **Long-term memory** (facts/preferences with hybrid search)
✅ **Automatic memory extraction** from conversations
✅ **Memory search** (semantic + keyword)
✅ **Agent management** (configure LLM settings)
✅ **CLI client** for easy interaction
✅ **REST API** with full OpenAPI docs

---

## What's Coming Next

### Phase 3: Tools & MCP (In Progress)
- Execute shell commands
- Read/write files
- Web searches
- MCP server integration

### Phase 4: Cloud LLMs
- Anthropic Claude fallback
- OpenAI integration
- Automatic model selection

### Phase 5: Web UI
- Browser-based chat interface
- Visual memory browser
- Agent configuration UI

---

## Troubleshooting

### MongoDB won't start
```bash
# Check logs
docker compose logs mongod

# Clean restart
docker compose down -v
docker compose up -d
```

### Ollama connection error
```bash
# Verify Ollama is running
curl http://localhost:11434/api/version

# Check models
ollama list

# Pull missing models
ollama pull llama3.2
ollama pull qwen3:8b
```

### CLI not working
```bash
# Reinstall
cd cli
pip uninstall aria-cli
pip install -e .

# Check Python version (need 3.12+)
python --version
```

### API errors
```bash
# Check API logs
docker compose logs api

# Restart API
docker compose restart api

# Check health
curl http://localhost:8000/api/v1/health
```

### Memory search not working
```bash
# Connect to MongoDB
docker exec -it aria-mongod mongosh

# Check indexes
use aria
db.memories.getIndexes()
db.memories.getSearchIndexes()

# If missing, restart mongo-init
docker compose up mongo-init
```

---

## Daily Usage

### Starting ARIA
```bash
# Start Docker services (if not running)
docker compose up -d

# Start Ollama (if not running)
# It usually starts automatically, but check:
ollama serve  # Run in separate terminal if needed

# Start chatting
aria chat
```

### Stopping ARIA
```bash
# Stop Docker services
docker compose down

# Ollama can stay running (lightweight)
```

### Checking Status
```bash
# Check all services
docker compose ps

# Check API health
aria health

# View recent conversations
aria conversations list

# View memories
aria memories list --limit 10
```

---

## File Locations

**Configuration:**
- `.env` - Your environment settings
- `docker-compose.yml` - Docker services

**Data (persists across restarts):**
- Docker volumes: `mongod-data`, `mongot-data`, `aria-data`

**Code:**
- `api/aria/` - FastAPI application
- `cli/aria_cli/` - CLI client
- `scripts/` - MongoDB initialization

**Logs:**
```bash
docker compose logs api     # API logs
docker compose logs mongod  # Database logs
```

---

## Next Steps

1. **Try the basics:**
   - Chat with ARIA
   - Create a few conversations
   - Add some manual memories

2. **Test memory:**
   - Tell ARIA something about yourself
   - In a new conversation, ask about it
   - Watch it remember!

3. **Explore API:**
   - Visit http://localhost:8000/docs
   - Try different endpoints
   - Understand the data models

4. **Read the spec:**
   - `SPECIFICATION.md` - Full technical details
   - `PROJECT_STATUS.md` - Current progress
   - `CHANGELOG.md` - Recent changes

---

## Getting Help

- **Documentation:** See `SPECIFICATION.md` for technical details
- **Issues:** https://github.com/baf509/ProjectAria/issues
- **Status:** Check `PROJECT_STATUS.md` for current phase

---

## Summary

**What you installed:**
1. Ollama (local LLM server)
2. Docker containers (API + MongoDB)
3. CLI client (optional)

**What you can do:**
1. Chat via `aria chat` or API
2. ARIA remembers conversations
3. Add/search long-term memories
4. Configure different agents

**What to remember:**
- Ollama must be running for chat to work
- Docker services handle the rest automatically
- Data persists in Docker volumes
- CLI is easiest for daily use

Enjoy your personal AI agent! 🤖
