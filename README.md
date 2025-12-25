# ARIA - Local AI Agent Platform

> Personal AI agent with long-term memory, tool use, and computer control.

## For Claude Code

**Start every session by reading:**
1. `PROJECT_STATUS.md` - Current phase and what's done
2. `CHANGELOG.md` - Recent changes (last 50 lines)
3. `SPECIFICATION.md` - Detailed requirements

**Quick phase check:**
```bash
grep -A5 "Current Phase" PROJECT_STATUS.md
```

## Architecture Summary

```
User → FastAPI → Orchestrator → LLM (Ollama/Claude/OpenAI)
                     ↓
              Memory Manager
              (Short-term: recent messages via MongoDB query)
              (Long-term: BM25 + Vector search via mongot)
                     ↓
               Tool Router → MCP Servers / Built-in Tools
                     ↓
              MongoDB 8.2 + mongot
              (mongod: data storage)
              (mongot: search indexes)
```

## Key Design Decisions

1. **No framework dependencies** - No LangChain, LlamaIndex, etc.
2. **Single-user** - No auth complexity, personal use only
3. **LLM agnostic** - Adapter pattern for Ollama, Anthropic, OpenAI, and OpenRouter
4. **MongoDB 8.2 + mongot** - Community Server with Vector Search (no Atlas needed)
5. **Local-first** - Ollama primary, cloud APIs as fallback
6. **qwen3-embedding for embeddings** - 1024-dimensional, local via Ollama
7. **Hybrid search** - BM25 + Vector with RRF fusion

## Quick Start

### Automated Setup (Recommended)

```bash
# Run the setup script
./scripts/setup.sh
```

This installs Docker, Ollama, pulls models, and configures everything.

### Manual Start

```bash
# Configure
cp .env.example .env
# Edit .env with your API keys

# Start all services
docker compose up -d

# Access the UI
open http://localhost:3000

# Or use the CLI
aria chat "Hello, ARIA!"
```

📖 **For detailed instructions, see [GETTING_STARTED.md](GETTING_STARTED.md)**

## Directory Structure

```
aria/
├── SPECIFICATION.md      # Full spec (read this!)
├── PROJECT_STATUS.md     # Current progress
├── CHANGELOG.md          # Change history
├── docker-compose.yml    # Container setup (mongod + mongot + api)
├── scripts/
│   └── init-mongo.js     # MongoDB initialization
├── api/                  # FastAPI backend
│   └── aria/
│       ├── main.py       # Entry point
│       ├── core/         # Agent logic
│       ├── llm/          # LLM adapters
│       ├── memory/       # Memory system (short-term + long-term)
│       ├── tools/        # Tool system
│       └── db/           # Database
├── cli/                  # CLI client
├── ui/                   # Web UI (Phase 5+)
└── mcp-servers/          # Custom MCP servers
```

## Current Phase

See `PROJECT_STATUS.md` for current phase and checklist.

## MongoDB 8.2 + mongot

This project uses MongoDB Community Server 8.2 with the separate `mongot` service:

- **mongod** - Main database server (port 27017)
- **mongot** - Search server with Atlas Search/Vector Search (port 27028)

This setup provides:
- `$vectorSearch` - Semantic similarity search
- `$search` - Full-text BM25 search
- Hybrid search with RRF fusion

No MongoDB Atlas subscription required - runs entirely locally.

## References

- [MongoDB 8.2 Vector Search](https://www.mongodb.com/docs/manual/core/indexes/index-types/index-vector/)
- [MCP Protocol](https://modelcontextprotocol.io/)
- [FastAPI](https://fastapi.tiangolo.com/)
- [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md)
