# ARIA Changelog

All notable changes to ARIA will be documented in this file.

Format: 
```
## [Date] - Phase X - [Summary]

### Added
- New features

### Changed  
- Changes to existing features

### Fixed
- Bug fixes

### Removed
- Removed features

### Notes
- Important notes for future work
```

---

## [2025-12-06] - Phase 4 - Cloud LLM Adapters

### Added
- Anthropic/Claude adapter (`api/aria/llm/anthropic.py`)
  - Streaming support with proper message formatting
  - Tool use (function calling) support
  - Support for all Claude 3 models (Opus, Sonnet, Haiku)
  - System prompt handling
  - Error handling with proper error messages
- OpenAI adapter (`api/aria/llm/openai.py`)
  - Streaming support with chunk accumulation
  - Function calling support
  - Support for GPT-4, GPT-4 Turbo, GPT-3.5
  - Proper message role handling (including tool role)
  - Error handling
- LLM backend availability check (`api/aria/llm/manager.py`)
  - Check if API keys are configured
  - Verify SDK packages are installed
  - Helpful error messages for missing configuration
- LLM health endpoint (`api/aria/api/routes/health.py`)
  - `GET /api/v1/health/llm` - Check all LLM backend status
  - Returns availability and reason for each backend

### Changed
- Updated LLM manager (`api/aria/llm/manager.py`)
  - Register Anthropic adapter when API key present
  - Register OpenAI adapter when API key present
  - Lazy import of cloud adapters to avoid import errors
  - Validate API keys before creating adapters
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Added `_get_llm_with_fallback()` method
  - Automatic fallback to cloud LLMs on error
  - User notification when fallback is used
  - Support for configurable fallback conditions
- Updated configuration
  - API keys already present in config (ANTHROPIC_API_KEY, OPENAI_API_KEY)
  - Already in `.env.example` file

### Notes
- Cloud LLM packages (anthropic, openai) already in requirements.txt
- API keys must be set in environment variables
- Fallback chain configured per agent in agent.fallback_chain
- Fallback conditions: on_error, on_context_overflow (future)
- Cloud LLMs automatically used when primary LLM fails

---

## [2025-12-06] - Phase 3 - Tools & MCP

### Added
- Tool infrastructure (`api/aria/tools/`)
  - BaseTool abstract class with parameter validation
  - ToolRouter for tool registration and execution
  - ToolDefinition and ToolParameter models
  - ToolResult with status tracking and metadata
- Built-in tools (`api/aria/tools/builtin/`)
  - Filesystem tool: read/write files, list directories, manage files
  - Shell tool: execute commands with timeout and sandboxing
  - Web tool: HTTP GET requests with size limits
- MCP (Model Context Protocol) integration (`api/aria/tools/mcp/`)
  - MCP client with JSON-RPC 2.0 over stdio
  - MCP manager for multi-server lifecycle management
  - MCPToolWrapper for BaseTool compatibility
  - Server health tracking and tool registration
- Tool management API routes (`api/aria/api/routes/tools.py`)
  - List tools (with type filtering)
  - Get tool details
  - Execute tools directly
  - MCP server CRUD (add, remove, list)
  - List tools per MCP server
  - Tool statistics endpoint
- CLI tool commands
  - `aria tools list` - List available tools
  - `aria tools info <name>` - Show tool details
  - `aria tools execute <name> <args>` - Execute a tool
  - `aria mcp list` - List MCP servers
  - `aria mcp add <id> <command>` - Add MCP server
  - `aria mcp remove <id>` - Remove MCP server
  - `aria mcp tools <id>` - List server's tools

### Changed
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Added tool_router parameter
  - Tool definitions passed to LLM when tools enabled
  - Handle tool calls from LLM responses
  - Execute tools and save results to conversation
  - Tool results included in streaming response
- Updated main app (`api/aria/main.py`)
  - Initialize built-in tools on startup
  - Register tools with tool router
  - Shutdown MCP servers on app shutdown
  - Added tools routes
- Updated API dependencies (`api/aria/api/deps.py`)
  - Added get_tool_router() dependency
  - Added get_mcp_manager() dependency
  - Pass tool_router to orchestrator
- Updated CLI with tool and MCP management commands
- Updated `PROJECT_STATUS.md` to Phase 3

### Notes
- Tools must be explicitly enabled per agent (capabilities.tools_enabled)
- Agents can specify which tools they can use (enabled_tools list)
- Filesystem tool is sandboxed to allowed paths (default: user home)
- Shell commands can be filtered with allow/deny lists
- MCP servers communicate via stdio transport
- Tool execution has configurable timeout (default: 5 minutes)

---

## [2025-11-29] - Phase 2 - Memory System

### Added
- Embedding service (`api/aria/memory/embeddings.py`)
  - Ollama embeddings using Qwen3-8b (4096 dimensions)
  - Voyage AI fallback provider
  - Batch embedding support
- Short-term memory (`api/aria/memory/short_term.py`)
  - Current conversation context retrieval
  - Recent conversations context
  - Token budget management
- Long-term memory (`api/aria/memory/long_term.py`)
  - Vector search using MongoDB Atlas Vector Search
  - Lexical search using MongoDB Atlas Search (BM25)
  - Hybrid search with Reciprocal Rank Fusion (RRF)
  - Memory CRUD operations with automatic embedding generation
  - Access tracking and statistics
- Memory extraction pipeline (`api/aria/memory/extraction.py`)
  - LLM-based extraction from conversations
  - Batch message processing
  - Source tracking and confidence scoring
  - Manual extraction from arbitrary text
- Context builder (`api/aria/core/context.py`)
  - Memory injection into system prompts
  - Short-term + long-term memory integration
  - Relevance-based memory retrieval
- Memory API routes (`api/aria/api/routes/memories.py`)
  - List, create, get, update, delete memories
  - Hybrid search endpoint
  - Background extraction trigger
- CLI memory commands
  - `aria memories list` - List all memories
  - `aria memories search` - Search with hybrid search
  - `aria memories add` - Manually add memories
  - `aria memories extract` - Trigger extraction

### Changed
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Integrated context builder for memory-aware responses
  - Automatic memory extraction in background
  - Access tracking for retrieved memories
- Updated main app to include memory routes
- Updated CLI with memory management commands
- Updated `PROJECT_STATUS.md` to Phase 2

### Notes
- Requires MongoDB search indexes for vector and lexical search
- Embeddings require Ollama with embedding model (e.g., qwen3:8b)
- Memory extraction runs asynchronously to avoid blocking chat
- Hybrid search combines best of lexical and semantic search

## [2025-11-29] - Phase 1 - Core Implementation

### Added
- Docker infrastructure (`docker-compose.yml`, Dockerfile, `.env.example`)
- MongoDB initialization script with replica set and search indexes
- FastAPI application foundation
  - Main app with lifespan management (`api/aria/main.py`)
  - Configuration management (`api/aria/config.py`)
  - MongoDB connection layer (`api/aria/db/mongodb.py`)
  - Pydantic models for API (`api/aria/db/models.py`)
- LLM adapter layer
  - Base adapter interface (`api/aria/llm/base.py`)
  - Ollama adapter with streaming support (`api/aria/llm/ollama.py`)
  - LLM manager for backend selection (`api/aria/llm/manager.py`)
- Agent orchestrator (`api/aria/core/orchestrator.py`)
  - Message processing with streaming
  - Conversation context assembly
  - Response persistence
- API routes
  - Health check endpoint
  - Conversations CRUD with SSE streaming
  - Agents CRUD
- CLI client (`cli/aria_cli/main.py`)
  - Interactive chat mode
  - Conversation management commands
  - Agent listing

### Changed
- Updated `PROJECT_STATUS.md` to reflect Phase 1 progress
- All core Phase 1 checklist items marked complete

### Notes
- Phase 1 implementation complete, ready for testing
- Requires Ollama running locally for testing
- MongoDB 8.2 with mongot for vector search
- Testing infrastructure not yet implemented

## [Unreleased]

### Added
- Initial project specification (`SPECIFICATION.md`)
- Project status tracking (`PROJECT_STATUS.md`)
- This changelog

### Changed
- Updated MongoDB configuration to use Community Server 8.2 + mongot
- Switched from `mongodb-atlas-local` to separate `mongod` + `mongot` services
- Updated embedding dimension to 4096 (Qwen3-8b)
- Added hybrid search (BM25 + Vector) with RRF fusion for long-term memory

### Notes
- See `PROJECT_STATUS.md` for current checklist
- MongoDB 8.2 Vector Search is in Public Preview
- mongot image: `mongodb/mongodb-community-search:0.53.1`
- mongod image: `mongodb/mongodb-community-server:8.2.0-ubi9`

---

<!-- 
Template for new entries:

## [YYYY-MM-DD] - Phase X - [Summary]

### Added
- 

### Changed
- 

### Fixed
- 

### Notes
- 

-->
