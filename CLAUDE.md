# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start for New Sessions

**Always start by reading these files in order:**
1. `PROJECT_STATUS.md` - Current phase and checklist
2. `CHANGELOG.md` (last 50 lines) - Recent changes
3. `SPECIFICATION.md` - Detailed architecture and requirements

**Quick phase check:**
```bash
grep -A5 "Current Phase" PROJECT_STATUS.md
```

## Architecture Overview

ARIA is a local-first AI agent platform with the following key principles:
- **No framework dependencies** - No LangChain, LlamaIndex, etc. Direct API integration only.
- **Single-user design** - Personal AI agent, no multi-tenancy or complex auth.
- **LLM agnostic** - Adapter pattern for Ollama (local), Anthropic, OpenAI, and OpenRouter.
- **Local-first** - Ollama primary, cloud APIs as fallback.
- **MongoDB 8.2 + mongot** - Self-hosted vector search without Atlas subscription.

### Core Flow

```
User Message
    → API (FastAPI)
    → Orchestrator
        ├─ Context Builder (assembles short-term + long-term memory)
        ├─ LLM Manager (selects backend with fallback chain)
        ├─ Tool Router (MCP + built-in tools)
        └─ Memory Extractor (background async)
    → Streaming Response
```

### Memory System (Two-Tier)

1. **Short-term**: Recent conversation context (embedded in `conversations` collection)
   - Fast MongoDB queries, no vector search
   - Current conversation + last 24h context

2. **Long-term**: Semantic memory with hybrid search (`memories` collection)
   - **Vector search** (`$vectorSearch`) - 1024-dim qwen3-embedding embeddings
   - **Lexical search** (`$search`) - BM25 full-text search
   - **RRF fusion** - Reciprocal Rank Fusion combines both
   - Background extraction from conversations via LLM

### Tool System

- **Built-in tools**: filesystem, shell, web (in `api/aria/tools/builtin/`)
- **MCP integration**: stdio transport, JSON-RPC 2.0 (in `api/aria/tools/mcp/`)
- **Tool router**: Central registration, execution with timeout
- Orchestrator handles tool calls during LLM streaming

### LLM Adapter Pattern

All LLM backends implement `LLMAdapter` base class:
- `stream()` - Async iterator yielding `StreamChunk` objects
- `complete()` - Non-streaming completion
- Message format conversion per provider
- Tool call support (function calling)

Adapters: `ollama.py`, `anthropic.py`, `openai.py`, `openrouter.py`

Manager handles selection and fallback chain logic.

## MongoDB 8.2 + mongot Setup

**Critical**: Uses MongoDB Community Server 8.2 with separate `mongot` service for search.

- **mongod** (port 27017): Main database, CRUD operations
- **mongot** (port 27028): Search engine (Apache Lucene-based)
- **Replica set required**: Even single-node must run as `rs0`
- **Connection string**: `mongodb://localhost:27017/?directConnection=true&replicaSet=rs0`

### Search Indexes

Vector and text search indexes created via `scripts/init-mongo.js`:
- `memory_vector_index` - Vector search (1024 dims, cosine similarity)
- `memory_text_index` - BM25 lexical search

Verify with: `db.memories.getSearchIndexes()`

## Development Commands

### Docker Compose Stack

```bash
# Start all services (mongod, mongot, api, ui)
docker compose up -d

# Check service health
docker compose ps

# View logs
docker compose logs -f api
docker compose logs -f mongod
docker compose logs -f mongot

# Stop all services
docker compose down

# Reset (delete volumes)
docker compose down -v
```

### API Development

```bash
cd api

# Install dependencies
pip install -r requirements.txt

# Run locally (requires MongoDB running)
uvicorn aria.main:app --reload --host 0.0.0.0 --port 8000

# API available at http://localhost:8000
# Docs at http://localhost:8000/docs
```

### UI Development

```bash
cd ui

# Install dependencies
npm install

# Run dev server
npm run dev

# UI available at http://localhost:3000

# Build for production
npm run build
npm run start
```

### CLI Client

```bash
cd cli

# Install in development mode
pip install -e .

# Chat commands
aria chat "Hello ARIA!"
aria chat --conversation <id> "Continue the conversation"
aria conversations list

# Memory commands
aria memories list
aria memories search "query text"
aria memories add "My favorite color is blue"

# Tool commands
aria tools list
aria tools info <tool_name>
aria tools execute <tool_name> <args_json>

# MCP commands
aria mcp list
aria mcp add <id> <command>
aria mcp remove <id>
```

### Database Access

```bash
# Connect to MongoDB
mongosh mongodb://localhost:27017/?directConnection=true&replicaSet=rs0

# Use ARIA database
use aria

# Check collections
show collections

# Check search indexes
db.memories.getSearchIndexes()

# Test vector search
db.memories.aggregate([{
  $vectorSearch: {
    index: "memory_vector_index",
    path: "embedding",
    queryVector: [...],  // 1024-dim array
    numCandidates: 100,
    limit: 10
  }
}])
```

## Code Patterns

### File Headers

Every Python file starts with:
```python
"""
ARIA - [Module Name]

Phase: [Phase number(s)]
Purpose: [One-line description]

Related Spec Sections:
- Section X.Y: [Description]
"""
```

### Async Everywhere

All database and network operations are async:
```python
# Good
async def get_conversation(id: str) -> Conversation:
    doc = await db.conversations.find_one({"_id": ObjectId(id)})
    return Conversation.from_doc(doc)

# Bad - blocks event loop
def get_conversation(id: str) -> Conversation:
    doc = db.conversations.find_one({"_id": ObjectId(id)})
```

### Error Handling

Use specific exception classes (not implemented yet, use generic for now):
```python
async def get_conversation(id: str) -> Conversation:
    doc = await db.conversations.find_one({"_id": ObjectId(id)})
    if not doc:
        raise ValueError(f"Conversation {id} not found")
    return Conversation.from_doc(doc)
```

### Dependency Injection (FastAPI)

```python
# api/deps.py
async def get_db() -> Database:
    return await get_database()

async def get_orchestrator(db: Database = Depends(get_db)) -> Orchestrator:
    return Orchestrator(db)

# Routes
@router.post("/{id}/messages")
async def send_message(
    id: str,
    body: MessageRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator)
):
    return await orchestrator.process_message(id, body.content)
```

### Streaming Responses

```python
from sse_starlette.sse import EventSourceResponse

async def stream_response(generator):
    async def event_generator():
        async for chunk in generator:
            yield {
                "event": chunk.type,
                "data": json.dumps(chunk.to_dict())
            }
    return EventSourceResponse(event_generator())
```

## Key Files

### Core Application

- `api/aria/main.py` - FastAPI app entry point, route registration
- `api/aria/config.py` - Pydantic settings from environment
- `api/aria/core/orchestrator.py` - Main agent loop, message processing
- `api/aria/core/context.py` - Context assembly (memory + system prompt)

### LLM Layer

- `api/aria/llm/base.py` - `LLMAdapter` abstract base class
- `api/aria/llm/manager.py` - Backend selection, fallback chain
- `api/aria/llm/ollama.py` - Ollama adapter
- `api/aria/llm/anthropic.py` - Anthropic/Claude adapter
- `api/aria/llm/openai.py` - OpenAI/GPT adapter
- `api/aria/llm/openrouter.py` - OpenRouter unified API adapter

### Memory System

- `api/aria/memory/short_term.py` - Recent conversation context
- `api/aria/memory/long_term.py` - Hybrid search (BM25 + Vector)
- `api/aria/memory/embeddings.py` - qwen3-embedding via Ollama
- `api/aria/memory/extraction.py` - LLM-based memory extraction

### Tool System

- `api/aria/tools/base.py` - `BaseTool` abstract class
- `api/aria/tools/router.py` - Tool registration and execution
- `api/aria/tools/builtin/` - Filesystem, shell, web tools
- `api/aria/tools/mcp/client.py` - MCP JSON-RPC client
- `api/aria/tools/mcp/manager.py` - Multi-server management

### Database

- `api/aria/db/mongodb.py` - Connection management
- `api/aria/db/models.py` - Pydantic models for API
- `scripts/init-mongo.js` - Database initialization, indexes

### API Routes

- `api/aria/api/routes/health.py` - Health checks, LLM status
- `api/aria/api/routes/conversations.py` - Conversation CRUD, messaging
- `api/aria/api/routes/agents.py` - Agent configuration
- `api/aria/api/routes/memories.py` - Memory CRUD, search
- `api/aria/api/routes/tools.py` - Tool listing, execution

## Configuration

### Environment Variables (.env)

```bash
# MongoDB
MONGODB_URI=mongodb://localhost:27017/?directConnection=true&replicaSet=rs0
MONGODB_DATABASE=aria

# Ollama (local LLM)
OLLAMA_URL=http://localhost:11434

# Cloud LLMs (optional)
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
OPENROUTER_API_KEY=sk-or-...

# Embeddings
EMBEDDING_PROVIDER=ollama
EMBEDDING_OLLAMA_MODEL=qwen3-embedding:0.6b
EMBEDDING_DIMENSION=1024
VOYAGE_API_KEY=  # Optional fallback

# API
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=false
```

**Note**: Using `qwen3-embedding:0.6b` model with 1024-dimensional embeddings for optimal balance of quality and performance.

## Important Notes

### DO NOT Use These Frameworks
- ❌ LangChain - Over-abstracted, breaking changes
- ❌ LlamaIndex - Too heavy
- ❌ LangGraph - Unnecessary complexity
- ❌ AutoGen - Multi-agent overkill

### DO Use These Libraries
- ✅ `httpx` - Async HTTP
- ✅ `motor` - Async MongoDB
- ✅ `pydantic` - Data validation
- ✅ `fastapi` - API framework
- ✅ `anthropic` - Official SDK
- ✅ `openai` - Official SDK (also used for OpenRouter)
- ✅ `sse-starlette` - Server-sent events

### API Key Security

**IMPORTANT**: API keys are stored in `.env` file which is git-ignored.

1. Copy `.env.example` to `.env`: `cp .env.example .env`
2. Add your API keys to `.env`
3. Never commit `.env` to git (already in `.gitignore`)
4. The `.env` file is loaded by docker-compose and pydantic-settings

Supported API keys:
- `ANTHROPIC_API_KEY` - For Claude models
- `OPENAI_API_KEY` - For GPT models
- `OPENROUTER_API_KEY` - For unified access to multiple providers
- `VOYAGE_API_KEY` - For Voyage AI embeddings (fallback)

### When Making Changes

1. Check current phase in `PROJECT_STATUS.md`
2. Read relevant section in `SPECIFICATION.md`
3. Make changes following established patterns
4. Update `CHANGELOG.md` with changes
5. Update `PROJECT_STATUS.md` if completing checklist items
6. Test locally before committing

### MongoDB 8.2 Gotchas

- **Replica set required**: Search features only work with replica set
- **mongot connectivity**: mongod must connect to mongot via `--setParameter mongotHost=mongot:27028`
- **Index creation**: Search indexes may take a few seconds to become active
- **Connection string**: Must include `directConnection=true&replicaSet=rs0`

### Memory System Gotchas

- **Embedding dimensions**: Using 1024-dim qwen3-embedding:0.6b model
- **Vector search**: Requires mongot to be running and healthy
- **RRF fusion**: Combines vector + lexical results with k=60
- **Background extraction**: Memory extraction is async, doesn't block responses

### Tool System Gotchas

- **Tool execution timeout**: Default 30 seconds, configurable per tool
- **MCP transport**: Only stdio transport implemented (not HTTP)
- **Sandboxing**: Filesystem tool has path restrictions
- **Tool calls**: Handled during LLM streaming, may trigger multiple rounds

## Current Phase

See `PROJECT_STATUS.md` for current phase progress. As of last update:
- **Phase 1-5**: Complete (Foundation, Memory, Tools, Cloud LLMs, Web UI)
- **Phase 6+**: Not started (Computer Use CLI/GUI, Voice, Security, RAG, Automation)

## Testing

No formal test suite yet. Manual testing via:
- CLI commands
- API endpoints via curl or `/docs`
- Docker Compose integration testing

Future phases will add proper test infrastructure.
