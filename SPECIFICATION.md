# ARIA: Local AI Agent Platform

**Version:** 0.2.0  
**Last Updated:** November 2025  
**Target Developer:** Claude Code

---

## Quick Navigation for Claude Code

```
CURRENT PHASE: Check /PROJECT_STATUS.md for current phase and progress
ARCHITECTURE:   Section 2 (read first for any code changes)
DATA MODELS:    Section 4 (MongoDB schemas)
PHASE DETAILS:  Section 8 (detailed requirements per phase)
CODE PATTERNS:  Section 9 (conventions and patterns to follow)
```

**Before making changes, always:**
1. Read `/PROJECT_STATUS.md` to understand current phase
2. Check `/CHANGELOG.md` for recent changes
3. Review relevant section of this spec
4. Run existing tests before modifying code

---

## 1. Project Overview

### 1.1 What is ARIA?

ARIA (Autonomous Reasoning & Intelligence Architecture) is a self-hosted AI agent platform that:

- **Separates agent identity from LLM** - Memories, tools, and policies persist; LLMs are swappable
- **Runs locally first** - Ollama for inference, MongoDB for storage, all on user's hardware
- **Supports cloud fallback** - Anthropic/OpenAI APIs when local models can't handle it
- **Provides computer use** - Both CLI (shell, files) and GUI (screen control) capabilities
- **Remembers everything** - Short-term context + long-term semantic memory

### 1.2 Single-User Design

This is a personal AI agent. No multi-tenancy, no complex auth. Optimized for one user running on home infrastructure (Unraid NAS + GPU machine).

### 1.3 Deployment Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│ Unraid NAS (Always On)                                              │
│ ┌─────────────────────────────────────────────────────────────────┐ │
│ │ Docker Compose Stack:                                           │ │
│ │  • aria-api (FastAPI) ──────────────────────┐                  │ │
│ │  • aria-ui (Next.js)                        │                  │ │
│ │  • mongodb (Atlas Local + Vector Search)    │                  │ │
│ │  • aria-mcp-manager                         │                  │ │
│ │  • aria-embeddings (qwen3-embedding)         │ LAN              │ │
│ │  • tailscale                                │                  │ │
│ └─────────────────────────────────────────────│──────────────────┘ │
└───────────────────────────────────────────────│─────────────────────┘
                                                │
                                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ GPU Machine (On-Demand or Always On)                                │
│ ┌─────────────────────────────────────────────────────────────────┐ │
│ │  • Ollama (LLM inference)                                       │ │
│ │  • Whisper (STT) - future                                       │ │
│ │  • Piper (TTS) - future                                         │ │
│ └─────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
                                                │
                                                ▼ (fallback)
                              ┌─────────────────────────────────┐
                              │ Cloud APIs                      │
                              │  • Anthropic Claude             │
                              │  • OpenAI                       │
                              │  • Voyage AI (embeddings)       │
                              └─────────────────────────────────┘
```

---

## 2. Architecture

### 2.1 Core Components

```
┌─────────────────────────────────────────────────────────────────────┐
│                           API LAYER                                  │
│  FastAPI application with REST + WebSocket endpoints                │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────────┐
│                      AGENT ORCHESTRATOR                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │
│  │ Conversation │  │   Memory     │  │    Tool Router           │   │
│  │   Manager    │  │   Manager    │  │    (MCP + Built-in)      │   │
│  └──────────────┘  └──────────────┘  └──────────────────────────┘   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │
│  │   Context    │  │    Agent     │  │    Policy                │   │
│  │   Builder    │  │   Profiles   │  │    Engine                │   │
│  └──────────────┘  └──────────────┘  └──────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────────┐
│                       LLM ADAPTER LAYER                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                  │
│  │   Ollama    │  │  Anthropic  │  │   OpenAI    │                  │
│  │   Adapter   │  │   Adapter   │  │   Adapter   │                  │
│  └─────────────┘  └─────────────┘  └─────────────┘                  │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────────┐
│                         DATA LAYER                                   │
│  MongoDB with:                                                       │
│  • Document storage (conversations, agents, tools)                  │
│  • Vector Search (long-term memory)                                 │
│  • Atlas Search / BM25 (lexical search)                             │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Request Flow

```
User Message
     │
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 1. CONTEXT ASSEMBLY                                                  │
│    ├─ Load conversation history (last N messages from DB)           │
│    ├─ Short-term memory: Recent context from current conversation   │
│    ├─ Long-term memory: Hybrid search (BM25 + Vector) over memories │
│    ├─ Load agent configuration and system prompt                    │
│    └─ Assemble tools available for this agent                       │
└─────────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. LLM CALL (streaming)                                              │
│    ├─ Select backend (local Ollama vs cloud API)                    │
│    ├─ Stream response chunks                                        │
│    └─ Handle tool calls if any                                      │
└─────────────────────────────────────────────────────────────────────┘
     │
     ▼ (if tool calls)
┌─────────────────────────────────────────────────────────────────────┐
│ 3. TOOL EXECUTION                                                    │
│    ├─ Route to appropriate tool (MCP server or built-in)            │
│    ├─ Execute with timeout and sandboxing                           │
│    ├─ Return result to LLM                                          │
│    └─ Loop back to step 2 if more tool calls                        │
└─────────────────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. PERSISTENCE                                                       │
│    ├─ Save user message to conversation                             │
│    ├─ Save assistant response to conversation                       │
│    ├─ Queue memory extraction (async background task)               │
│    └─ Update conversation metadata (token counts, timestamps)       │
└─────────────────────────────────────────────────────────────────────┘
     │
     ▼
Response to User
```

---

## 3. Memory Architecture

### 3.1 Two-Tier Memory System

```
┌─────────────────────────────────────────────────────────────────────┐
│                     SHORT-TERM MEMORY                                │
│  Purpose: Fast retrieval of recent/current context                  │
│  Storage: Embedded in conversation documents                        │
│  Retrieval: Direct MongoDB queries (no vector search)               │
│  Scope: Current conversation + recent conversations (last 24h)      │
│                                                                      │
│  Examples:                                                           │
│  • "What did I just ask you?"                                       │
│  • "Continue from where we left off"                                │
│  • "The file I mentioned earlier"                                   │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     LONG-TERM MEMORY                                 │
│  Purpose: Semantic retrieval of facts, preferences, knowledge       │
│  Storage: Separate 'memories' collection with embeddings            │
│  Retrieval: Hybrid search (BM25 + Vector, RRF fusion)               │
│  Scope: All time, all conversations                                 │
│                                                                      │
│  Examples:                                                           │
│  • "What's my preferred coding style?"                              │
│  • "What did we discuss about the Q3 project?"                      │
│  • "My wife's name" (stored as fact)                                │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Short-Term Memory Implementation

```python
class ShortTermMemory:
    """
    Fast retrieval from recent context. No embeddings needed.
    """
    
    async def get_current_conversation_context(
        self,
        conversation_id: str,
        max_messages: int = 20,
        max_tokens: int = 8000
    ) -> list[Message]:
        """
        Get recent messages from current conversation.
        Simple MongoDB query, very fast.
        """
        conversation = await self.db.conversations.find_one(
            {"_id": ObjectId(conversation_id)},
            {"messages": {"$slice": -max_messages}}
        )
        
        messages = conversation.get("messages", [])
        
        # Trim to fit token budget
        return self._trim_to_tokens(messages, max_tokens)
    
    async def get_recent_conversations_context(
        self,
        hours: int = 24,
        limit: int = 5
    ) -> list[ConversationSummary]:
        """
        Get summaries of recent conversations for context.
        Useful for "what were we discussing yesterday?"
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        
        conversations = await self.db.conversations.find(
            {"updated_at": {"$gte": cutoff}},
            {"title": 1, "summary": 1, "updated_at": 1}
        ).sort("updated_at", -1).limit(limit).to_list()
        
        return [ConversationSummary.from_doc(c) for c in conversations]
```

### 3.3 Long-Term Memory Implementation

```python
class LongTermMemory:
    """
    Semantic retrieval using hybrid BM25 + Vector search.
    Uses Reciprocal Rank Fusion (RRF) to combine results.
    """
    
    async def search(
        self,
        query: str,
        limit: int = 10,
        filters: dict = None
    ) -> list[Memory]:
        """
        Hybrid search: combines lexical (BM25) and semantic (vector) search.
        """
        # Generate embedding for query
        query_embedding = await self.embedding_service.embed(query)
        
        # Build filter for both searches
        base_filter = {"status": "active"}
        if filters:
            base_filter.update(filters)
        
        # Run both searches in parallel
        vector_results, lexical_results = await asyncio.gather(
            self._vector_search(query_embedding, base_filter, limit * 2),
            self._lexical_search(query, base_filter, limit * 2)
        )
        
        # Combine with Reciprocal Rank Fusion
        fused = self._rrf_fusion(vector_results, lexical_results, k=60)
        
        return fused[:limit]
    
    async def _vector_search(
        self,
        embedding: list[float],
        filter: dict,
        limit: int
    ) -> list[tuple[Memory, float]]:
        """
        MongoDB Atlas Vector Search.
        """
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "memory_vector_index",
                    "path": "embedding",
                    "queryVector": embedding,
                    "numCandidates": limit * 10,
                    "limit": limit,
                    "filter": filter
                }
            },
            {
                "$project": {
                    "content": 1,
                    "content_type": 1,
                    "categories": 1,
                    "importance": 1,
                    "created_at": 1,
                    "score": {"$meta": "vectorSearchScore"}
                }
            }
        ]
        
        results = await self.db.memories.aggregate(pipeline).to_list()
        return [(Memory.from_doc(r), r["score"]) for r in results]
    
    async def _lexical_search(
        self,
        query: str,
        filter: dict,
        limit: int
    ) -> list[tuple[Memory, float]]:
        """
        MongoDB Atlas Search with BM25 scoring.
        """
        pipeline = [
            {
                "$search": {
                    "index": "memory_text_index",
                    "text": {
                        "query": query,
                        "path": ["content", "categories"],
                        "fuzzy": {"maxEdits": 1}
                    }
                }
            },
            {"$match": filter},
            {"$limit": limit},
            {
                "$project": {
                    "content": 1,
                    "content_type": 1,
                    "categories": 1,
                    "importance": 1,
                    "created_at": 1,
                    "score": {"$meta": "searchScore"}
                }
            }
        ]
        
        results = await self.db.memories.aggregate(pipeline).to_list()
        return [(Memory.from_doc(r), r["score"]) for r in results]
    
    def _rrf_fusion(
        self,
        vector_results: list[tuple[Memory, float]],
        lexical_results: list[tuple[Memory, float]],
        k: int = 60
    ) -> list[Memory]:
        """
        Reciprocal Rank Fusion to combine result lists.
        RRF score = sum(1 / (k + rank)) for each list where doc appears.
        """
        scores = {}
        
        for rank, (memory, _) in enumerate(vector_results):
            doc_id = str(memory.id)
            scores[doc_id] = scores.get(doc_id, {"memory": memory, "score": 0})
            scores[doc_id]["score"] += 1 / (k + rank + 1)
        
        for rank, (memory, _) in enumerate(lexical_results):
            doc_id = str(memory.id)
            scores[doc_id] = scores.get(doc_id, {"memory": memory, "score": 0})
            scores[doc_id]["score"] += 1 / (k + rank + 1)
        
        # Sort by fused score
        sorted_results = sorted(
            scores.values(),
            key=lambda x: x["score"],
            reverse=True
        )
        
        return [r["memory"] for r in sorted_results]
```

### 3.4 Embedding Service (qwen3-embedding)

```python
class EmbeddingService:
    """
    Local embedding generation using qwen3-embedding via Ollama.
    Can fall back to Voyage AI for quality-critical embeddings.
    """

    def __init__(self, config: EmbeddingConfig):
        self.primary = OllamaEmbeddings(
            base_url=config.ollama_url,
            model="qwen3-embedding:0.6b"
        )
        self.fallback = VoyageEmbeddings(
            api_key=config.voyage_api_key,
            model="voyage-3-large"
        ) if config.voyage_api_key else None

        self.dimension = 1024  # qwen3-embedding dimension
    
    async def embed(
        self,
        text: str,
        use_fallback: bool = False
    ) -> list[float]:
        """
        Generate embedding for text.
        """
        if use_fallback and self.fallback:
            return await self.fallback.embed(text)
        
        try:
            return await self.primary.embed(text)
        except Exception as e:
            if self.fallback:
                logger.warning(f"Local embedding failed, using fallback: {e}")
                return await self.fallback.embed(text)
            raise
    
    async def embed_batch(
        self,
        texts: list[str],
        batch_size: int = 32
    ) -> list[list[float]]:
        """
        Batch embedding for efficiency.
        """
        results = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            embeddings = await asyncio.gather(
                *[self.embed(text) for text in batch]
            )
            results.extend(embeddings)
        return results


class OllamaEmbeddings:
    """
    Embedding generation via Ollama API.
    """
    
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=60.0)
    
    async def embed(self, text: str) -> list[float]:
        response = await self.client.post(
            f"{self.base_url}/api/embeddings",
            json={
                "model": self.model,
                "prompt": text
            }
        )
        response.raise_for_status()
        return response.json()["embedding"]
```

### 3.5 MongoDB Indexes for Memory

```javascript
// Vector Search Index (for semantic search)
{
  "name": "memory_vector_index",
  "type": "vectorSearch",
  "definition": {
    "fields": [
      {
        "type": "vector",
        "path": "embedding",
        "numDimensions": 1024,  // qwen3-embedding dimension
        "similarity": "cosine"
      },
      {
        "type": "filter",
        "path": "status"
      },
      {
        "type": "filter", 
        "path": "categories"
      },
      {
        "type": "filter",
        "path": "content_type"
      }
    ]
  }
}

// Atlas Search Index (for BM25 lexical search)
{
  "name": "memory_text_index",
  "mappings": {
    "dynamic": false,
    "fields": {
      "content": {
        "type": "string",
        "analyzer": "lucene.english"
      },
      "categories": {
        "type": "string",
        "analyzer": "lucene.keyword"
      },
      "status": {
        "type": "string",
        "analyzer": "lucene.keyword"
      }
    }
  }
}
```

---

## 4. Data Models

### 4.1 Database: `aria`

Collections:
- `conversations` - Chat sessions with embedded messages
- `memories` - Long-term memory with embeddings
- `agents` - Agent configurations
- `tools` - Tool/MCP server configurations
- `settings` - System settings

### 4.2 `conversations` Collection

```javascript
{
  _id: ObjectId,
  
  // Metadata
  agent_id: ObjectId,
  title: String,                   // Auto-generated or user-set
  summary: String,                 // LLM-generated summary
  
  // State
  status: String,                  // "active" | "archived"
  created_at: ISODate,
  updated_at: ISODate,
  
  // LLM config at creation (for reproducibility)
  llm_config: {
    backend: String,               // "ollama" | "anthropic" | "openai"
    model: String,
    temperature: Number
  },
  
  // Messages (embedded array)
  messages: [
    {
      id: String,                  // UUID
      role: String,                // "user" | "assistant" | "system" | "tool"
      content: String,
      
      // Tool calls (if role == "assistant" and tool use)
      tool_calls: [
        {
          id: String,
          name: String,
          arguments: Object,
          result: Mixed,
          status: String,          // "success" | "error"
          duration_ms: Number
        }
      ],
      
      // Metadata
      model: String,               // Actual model used
      tokens: { input: Number, output: Number },
      created_at: ISODate,
      
      // Memory tracking
      memory_processed: Boolean    // Has memory extraction run?
    }
  ],
  
  // Organization
  tags: [String],
  pinned: Boolean,
  
  // Stats
  stats: {
    message_count: Number,
    total_tokens: Number,
    tool_calls: Number
  }
}

// Indexes
db.conversations.createIndex({ status: 1, updated_at: -1 })
db.conversations.createIndex({ agent_id: 1 })
db.conversations.createIndex({ tags: 1 })
db.conversations.createIndex({ "messages.created_at": 1 })
```

### 4.3 `memories` Collection

```javascript
{
  _id: ObjectId,
  
  // Content
  content: String,                 // The memory text
  content_type: String,            // "fact" | "preference" | "event" | "skill" | "document"
  
  // Embeddings
  embedding: [Number],             // Dense vector (1024 dims for qwen3-embedding)
  embedding_model: String,         // "qwen3-embedding:0.6b" | "voyage-3-large"
  
  // Source tracking
  source: {
    type: String,                  // "conversation" | "document" | "manual"
    conversation_id: ObjectId,
    message_ids: [String],         // UUIDs of source messages
    extracted_at: ISODate
  },
  
  // Lifecycle
  status: String,                  // "active" | "archived" | "deleted"
  importance: Number,              // 0.0 - 1.0
  confidence: Number,              // 0.0 - 1.0 (for auto-extracted)
  verified: Boolean,               // User confirmed accuracy
  
  // Temporal
  created_at: ISODate,
  updated_at: ISODate,
  last_accessed_at: ISODate,
  access_count: Number,
  
  // Categorization
  categories: [String],            // ["work", "coding", "preferences"]
  entities: [                      // Extracted entities
    { type: String, value: String }
  ]
}

// Standard indexes
db.memories.createIndex({ status: 1, importance: -1 })
db.memories.createIndex({ categories: 1 })
db.memories.createIndex({ "source.conversation_id": 1 })
db.memories.createIndex({ content_type: 1 })

// Vector and text indexes defined in Section 3.5
```

### 4.4 `agents` Collection

```javascript
{
  _id: ObjectId,
  
  // Identity
  name: String,                    // "ARIA", "CodeAssist"
  slug: String,                    // URL-safe identifier
  description: String,
  
  // System prompt
  system_prompt: String,
  
  // LLM Configuration
  llm: {
    backend: String,               // "ollama" | "anthropic" | "openai"
    model: String,
    temperature: Number,
    max_tokens: Number
  },
  
  // Fallback chain
  fallback_chain: [
    {
      backend: String,
      model: String,
      conditions: {
        on_error: Boolean,
        on_context_overflow: Boolean,
        max_input_tokens: Number
      }
    }
  ],
  
  // Capabilities
  capabilities: {
    memory_enabled: Boolean,
    tools_enabled: Boolean,
    computer_use_enabled: Boolean
  },
  
  // Memory settings
  memory_config: {
    auto_extract: Boolean,
    short_term_messages: Number,   // How many recent messages to include
    long_term_results: Number,     // How many memories to retrieve
    categories_filter: [String]    // Only search these categories
  },
  
  // Tools enabled for this agent
  enabled_tools: [String],         // Tool slugs
  
  // Metadata
  is_default: Boolean,
  created_at: ISODate,
  updated_at: ISODate
}

// Indexes
db.agents.createIndex({ slug: 1 }, { unique: true })
db.agents.createIndex({ is_default: 1 })
```

### 4.5 `tools` Collection

```javascript
{
  _id: ObjectId,
  
  // Identity
  name: String,
  slug: String,
  description: String,
  
  // Type
  type: String,                    // "builtin" | "mcp" | "script"
  
  // For MCP tools
  mcp_config: {
    command: String,               // Command to run
    args: [String],
    env: Object,                   // Environment variables
    transport: String              // "stdio" | "http"
  },
  
  // For script tools
  script_config: {
    runtime: String,               // "python" | "node" | "bash"
    entrypoint: String,
    timeout_ms: Number
  },
  
  // Schema (for LLM)
  schema: {
    name: String,
    description: String,
    parameters: Object             // JSON Schema
  },
  
  // Status
  status: String,                  // "active" | "disabled" | "error"
  
  // Metadata
  created_at: ISODate,
  updated_at: ISODate
}

// Indexes
db.tools.createIndex({ slug: 1 }, { unique: true })
db.tools.createIndex({ type: 1, status: 1 })
```

### 4.6 `settings` Collection

```javascript
{
  _id: "global",  // Single document
  
  // LLM backends
  llm: {
    ollama_url: String,            // "http://192.168.1.100:11434"
    anthropic_api_key: String,     // Encrypted
    openai_api_key: String,        // Encrypted
    default_backend: String
  },
  
  // Embedding
  embedding: {
    provider: String,              // "ollama" | "voyage"
    ollama_model: String,          // "qwen3-embedding:0.6b"
    voyage_api_key: String         // Encrypted
  },
  
  // Memory
  memory: {
    auto_extract: Boolean,
    extraction_model: String,      // Which model to use for extraction
    importance_threshold: Number
  },
  
  // UI preferences
  ui: {
    theme: String,
    default_agent: String
  },
  
  updated_at: ISODate
}
```

---

## 5. API Specification

### 5.1 REST Endpoints

```yaml
# Health
GET  /api/v1/health                    # Health check

# Conversations
GET  /api/v1/conversations             # List conversations
POST /api/v1/conversations             # Create conversation
GET  /api/v1/conversations/{id}        # Get conversation with messages
PATCH /api/v1/conversations/{id}       # Update conversation metadata
DELETE /api/v1/conversations/{id}      # Delete conversation

# Messages (main interaction)
POST /api/v1/conversations/{id}/messages
  Request:  { "content": "...", "stream": true }
  Response: Server-Sent Events stream

# Agents
GET  /api/v1/agents                    # List agents
POST /api/v1/agents                    # Create agent
GET  /api/v1/agents/{id}               # Get agent
PUT  /api/v1/agents/{id}               # Update agent
DELETE /api/v1/agents/{id}             # Delete agent

# Memories
GET  /api/v1/memories                  # List memories
POST /api/v1/memories                  # Create memory manually
GET  /api/v1/memories/{id}             # Get memory
PATCH /api/v1/memories/{id}            # Update memory
DELETE /api/v1/memories/{id}           # Delete memory
POST /api/v1/memories/search           # Search memories
  Request: { "query": "...", "limit": 10 }

# Tools
GET  /api/v1/tools                     # List tools
POST /api/v1/tools                     # Install tool
GET  /api/v1/tools/{id}                # Get tool
PATCH /api/v1/tools/{id}               # Update tool
DELETE /api/v1/tools/{id}              # Uninstall tool

# Settings
GET  /api/v1/settings                  # Get settings
PATCH /api/v1/settings                 # Update settings
POST /api/v1/settings/test-llm         # Test LLM connection
```

### 5.2 WebSocket Endpoint

```
WS /api/v1/ws

# Client -> Server
{
  "type": "message",
  "conversation_id": "...",
  "content": "..."
}

# Server -> Client
{ "type": "text_delta", "content": "..." }
{ "type": "tool_call", "name": "...", "arguments": {...} }
{ "type": "tool_result", "name": "...", "result": {...} }
{ "type": "error", "message": "..." }
{ "type": "done", "usage": { "input_tokens": N, "output_tokens": M } }
```

### 5.3 SSE Stream Format

```
POST /api/v1/conversations/{id}/messages

Response (text/event-stream):

event: text_delta
data: {"content": "Hello"}

event: text_delta
data: {"content": " there!"}

event: tool_call
data: {"id": "tc_123", "name": "read_file", "arguments": {"path": "/tmp/x"}}

event: tool_result
data: {"id": "tc_123", "result": "file contents..."}

event: done
data: {"usage": {"input_tokens": 150, "output_tokens": 42}}
```

---

## 6. LLM Adapter Interface

### 6.1 Base Interface

```python
from abc import ABC, abstractmethod
from typing import AsyncIterator
from dataclasses import dataclass

@dataclass
class Message:
    role: str          # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str = None  # For tool results
    name: str = None          # Tool name for tool results

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

@dataclass
class StreamChunk:
    type: str          # "text" | "tool_call" | "tool_call_delta" | "done" | "error"
    content: str = None
    tool_call: ToolCall = None
    usage: dict = None
    error: str = None

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict   # JSON Schema


class LLMAdapter(ABC):
    """
    Abstract base class for all LLM backends.
    Implement this interface to add a new LLM provider.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name, e.g., 'ollama', 'anthropic'."""
        pass
    
    @abstractmethod
    async def stream(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> AsyncIterator[StreamChunk]:
        """
        Stream a completion with optional tool use.
        Yields StreamChunk objects.
        """
        pass
    
    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> tuple[str, list[ToolCall], dict]:
        """
        Non-streaming completion.
        Returns (content, tool_calls, usage).
        """
        pass
    
    async def health_check(self) -> bool:
        """Check if the backend is available."""
        try:
            # Try a minimal completion
            async for _ in self.stream([Message(role="user", content="hi")]):
                return True
        except Exception:
            return False
        return False
```

### 6.2 Ollama Implementation

```python
class OllamaAdapter(LLMAdapter):
    """
    Adapter for Ollama local models.
    """
    
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.client = httpx.AsyncClient(timeout=120.0)
    
    @property
    def name(self) -> str:
        return "ollama"
    
    async def stream(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> AsyncIterator[StreamChunk]:
        
        # Convert messages to Ollama format
        ollama_messages = []
        for msg in messages:
            ollama_msg = {"role": msg.role, "content": msg.content}
            if msg.tool_call_id:
                # Tool result
                ollama_msg["role"] = "tool"
                ollama_msg["tool_call_id"] = msg.tool_call_id
            ollama_messages.append(ollama_msg)
        
        # Convert tools to Ollama format
        ollama_tools = None
        if tools:
            ollama_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters
                    }
                }
                for t in tools
            ]
        
        request_body = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens
            }
        }
        
        if ollama_tools:
            request_body["tools"] = ollama_tools
        
        async with self.client.stream(
            "POST",
            f"{self.base_url}/api/chat",
            json=request_body
        ) as response:
            response.raise_for_status()
            
            async for line in response.aiter_lines():
                if not line:
                    continue
                
                data = json.loads(line)
                
                if data.get("done"):
                    yield StreamChunk(
                        type="done",
                        usage={
                            "input_tokens": data.get("prompt_eval_count", 0),
                            "output_tokens": data.get("eval_count", 0)
                        }
                    )
                    return
                
                message = data.get("message", {})
                
                # Text content
                if message.get("content"):
                    yield StreamChunk(
                        type="text",
                        content=message["content"]
                    )
                
                # Tool calls
                if message.get("tool_calls"):
                    for tc in message["tool_calls"]:
                        yield StreamChunk(
                            type="tool_call",
                            tool_call=ToolCall(
                                id=tc.get("id", str(uuid.uuid4())),
                                name=tc["function"]["name"],
                                arguments=json.loads(tc["function"]["arguments"])
                            )
                        )
    
    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> tuple[str, list[ToolCall], dict]:
        
        content_parts = []
        tool_calls = []
        usage = {}
        
        async for chunk in self.stream(messages, tools, temperature, max_tokens):
            if chunk.type == "text":
                content_parts.append(chunk.content)
            elif chunk.type == "tool_call":
                tool_calls.append(chunk.tool_call)
            elif chunk.type == "done":
                usage = chunk.usage
        
        return "".join(content_parts), tool_calls, usage
```

### 6.3 Anthropic Implementation

```python
class AnthropicAdapter(LLMAdapter):
    """
    Adapter for Anthropic Claude models.
    """
    
    def __init__(self, api_key: str, model: str):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
    
    @property
    def name(self) -> str:
        return "anthropic"
    
    async def stream(
        self,
        messages: list[Message],
        tools: list[Tool] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096
    ) -> AsyncIterator[StreamChunk]:
        
        # Separate system message
        system = None
        anthropic_messages = []
        
        for msg in messages:
            if msg.role == "system":
                system = msg.content
            elif msg.role == "tool":
                # Tool result format for Anthropic
                anthropic_messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": msg.content
                        }
                    ]
                })
            else:
                anthropic_messages.append({
                    "role": msg.role,
                    "content": msg.content
                })
        
        # Convert tools
        anthropic_tools = None
        if tools:
            anthropic_tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters
                }
                for t in tools
            ]
        
        kwargs = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature
        }
        if system:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        
        current_tool_call = {}
        
        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    if hasattr(event.content_block, "type"):
                        if event.content_block.type == "tool_use":
                            current_tool_call = {
                                "id": event.content_block.id,
                                "name": event.content_block.name,
                                "arguments": ""
                            }
                
                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield StreamChunk(type="text", content=event.delta.text)
                    elif hasattr(event.delta, "partial_json"):
                        current_tool_call["arguments"] += event.delta.partial_json
                
                elif event.type == "content_block_stop":
                    if current_tool_call:
                        yield StreamChunk(
                            type="tool_call",
                            tool_call=ToolCall(
                                id=current_tool_call["id"],
                                name=current_tool_call["name"],
                                arguments=json.loads(current_tool_call["arguments"])
                            )
                        )
                        current_tool_call = {}
                
                elif event.type == "message_stop":
                    # Get usage from the final message
                    msg = stream.get_final_message()
                    yield StreamChunk(
                        type="done",
                        usage={
                            "input_tokens": msg.usage.input_tokens,
                            "output_tokens": msg.usage.output_tokens
                        }
                    )
```

---

## 7. Project Structure

```
aria/
├── docker-compose.yml
├── docker-compose.dev.yml
├── .env.example
├── README.md
├── PROJECT_STATUS.md              # ← Claude Code reads this first
├── CHANGELOG.md                   # ← Track all changes
├── SPECIFICATION.md               # ← This document
│
├── api/                           # FastAPI backend
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── requirements.txt
│   │
│   └── aria/
│       ├── __init__.py
│       ├── main.py                # FastAPI app entry
│       ├── config.py              # Settings (pydantic-settings)
│       │
│       ├── api/                   # API layer
│       │   ├── __init__.py
│       │   ├── deps.py            # Dependency injection
│       │   └── routes/
│       │       ├── __init__.py
│       │       ├── health.py
│       │       ├── conversations.py
│       │       ├── agents.py
│       │       ├── memories.py
│       │       ├── tools.py
│       │       └── settings.py
│       │
│       ├── core/                  # Core agent logic
│       │   ├── __init__.py
│       │   ├── orchestrator.py    # Main agent loop
│       │   ├── context.py         # Context builder
│       │   └── streaming.py       # SSE utilities
│       │
│       ├── llm/                   # LLM adapters
│       │   ├── __init__.py
│       │   ├── base.py            # LLMAdapter ABC
│       │   ├── ollama.py
│       │   ├── anthropic.py
│       │   ├── openai.py
│       │   └── manager.py         # Backend selection
│       │
│       ├── memory/                # Memory system
│       │   ├── __init__.py
│       │   ├── short_term.py
│       │   ├── long_term.py
│       │   ├── extraction.py      # Memory extraction from conversations
│       │   └── embeddings.py      # Embedding service
│       │
│       ├── tools/                 # Tool system
│       │   ├── __init__.py
│       │   ├── base.py            # Tool interface
│       │   ├── router.py          # Tool routing
│       │   ├── mcp/               # MCP integration
│       │   │   ├── __init__.py
│       │   │   ├── client.py
│       │   │   └── manager.py
│       │   └── builtin/           # Built-in tools
│       │       ├── __init__.py
│       │       ├── filesystem.py
│       │       ├── shell.py
│       │       └── web.py
│       │
│       ├── db/                    # Database layer
│       │   ├── __init__.py
│       │   ├── mongodb.py         # Connection management
│       │   └── models.py          # Pydantic models
│       │
│       └── utils/
│           ├── __init__.py
│           └── tokens.py          # Token counting
│
├── ui/                            # Next.js frontend (Phase 2+)
│   ├── Dockerfile
│   ├── package.json
│   └── ...
│
├── cli/                           # Command-line client
│   ├── pyproject.toml
│   └── aria_cli/
│       ├── __init__.py
│       └── main.py
│
├── mcp-servers/                   # Custom MCP servers (Phase 3+)
│   └── embeddings/                # qwen3-embedding server
│       ├── Dockerfile
│       └── server.py
│
└── scripts/
    ├── setup.sh
    ├── dev.sh
    └── init-mongo.js              # MongoDB initialization
```

---

## 8. Development Phases

Each phase produces a **deployable, usable** version of ARIA.

### Phase 1: Foundation (Weeks 1-4)

**Goal:** Working chat with Ollama, persistent conversations, CLI client.

**Deliverables:**
- [ ] Docker Compose with MongoDB + API service
- [ ] FastAPI with health, conversations, agents endpoints
- [ ] Ollama adapter (streaming)
- [ ] Basic conversation CRUD
- [ ] CLI client for chatting
- [ ] Default agent configuration

**What you can do after Phase 1:**
- Chat with local LLM via CLI
- Conversations persist across sessions
- Switch between conversations

**Definition of Done:**
```bash
# Start the system
docker compose up -d

# Chat via CLI
aria chat "Hello, ARIA!"
# Response streams from Ollama

# List conversations
aria conversations list

# Continue a conversation
aria chat --conversation abc123 "What did we discuss?"
```

**Files to create:**
```
api/aria/main.py
api/aria/config.py
api/aria/api/routes/health.py
api/aria/api/routes/conversations.py
api/aria/api/routes/agents.py
api/aria/core/orchestrator.py
api/aria/llm/base.py
api/aria/llm/ollama.py
api/aria/llm/manager.py
api/aria/db/mongodb.py
api/aria/db/models.py
cli/aria_cli/main.py
docker-compose.yml
PROJECT_STATUS.md
```

---

### Phase 2: Memory System (Weeks 5-8)

**Goal:** Short-term and long-term memory working.

**Deliverables:**
- [ ] Short-term memory (recent conversation context)
- [ ] Long-term memory with hybrid search (BM25 + Vector)
- [ ] qwen3-embedding service (1024-dim)
- [ ] Memory extraction pipeline (async background)
- [ ] Context builder with memory injection
- [ ] Memory CRUD endpoints

**What you can do after Phase 2:**
- Agent remembers facts from past conversations
- "What's my favorite programming language?" works
- Semantic search over memories

**Definition of Done:**
```bash
# In one conversation
aria chat "My favorite color is blue"

# In a new conversation, weeks later
aria chat "What's my favorite color?"
# → "Your favorite color is blue"

# Search memories directly
aria memories search "color preferences"
```

**Files to create:**
```
api/aria/memory/short_term.py
api/aria/memory/long_term.py
api/aria/memory/extraction.py
api/aria/memory/embeddings.py
api/aria/core/context.py
api/aria/api/routes/memories.py
mcp-servers/embeddings/server.py
scripts/init-mongo.js  # Add vector/text indexes
```

---

### Phase 3: Tools & MCP (Weeks 9-12)

**Goal:** Tool use working with built-in tools and MCP support.

**Deliverables:**
- [ ] Tool interface and router
- [ ] Built-in tools: filesystem, shell, web_fetch
- [ ] MCP client implementation
- [ ] MCP server manager
- [ ] Tool CRUD endpoints
- [ ] Tool sandboxing (basic)

**What you can do after Phase 3:**
- "Read the file at /tmp/notes.txt"
- "Search the web for MongoDB best practices"
- Install and use community MCP servers

**Definition of Done:**
```bash
# Use built-in tool
aria chat "What files are in /home/ben/projects?"
# → Lists files using filesystem tool

# Install MCP server
aria tools install git+https://github.com/modelcontextprotocol/servers#filesystem

# Use installed tool
aria chat "Create a new file called test.txt with 'hello world'"
```

**Files to create:**
```
api/aria/tools/base.py
api/aria/tools/router.py
api/aria/tools/mcp/client.py
api/aria/tools/mcp/manager.py
api/aria/tools/builtin/filesystem.py
api/aria/tools/builtin/shell.py
api/aria/tools/builtin/web.py
api/aria/api/routes/tools.py
```

---

### Phase 4: Cloud LLM Adapters (Weeks 13-14)

**Goal:** Support Anthropic and OpenAI as fallback/alternative.

**Deliverables:**
- [ ] Anthropic adapter
- [ ] OpenAI adapter
- [ ] LLM selection logic (cost, context length, capability)
- [ ] Settings endpoints for API keys
- [ ] Fallback chain implementation

**What you can do after Phase 4:**
- Use Claude for complex reasoning
- Automatic fallback when local model can't handle it
- Cost tracking for API usage

**Definition of Done:**
```bash
# Configure API key
aria settings set anthropic_api_key sk-xxx

# Use specific backend
aria chat --backend anthropic "Explain quantum computing"

# Fallback works
aria chat "Complex task that needs 100k context"
# → Automatically uses Claude
```

---

### Phase 5: Web UI (Weeks 15-18)

**Goal:** Browser-based chat interface.

**Deliverables:**
- [ ] Next.js application
- [ ] Chat interface with streaming
- [ ] Conversation list and management
- [ ] Agent configuration UI
- [ ] Memory browser
- [ ] Settings page

**What you can do after Phase 5:**
- Chat in browser at http://localhost:3000
- See conversation history
- Configure agents visually

---

### Phase 6: Computer Use - CLI (Weeks 19-22)

**Goal:** Agent can execute shell commands safely.

**Deliverables:**
- [ ] Shell tool with safety policies
- [ ] Git tool
- [ ] File manipulation tools
- [ ] Process management
- [ ] Working directory management
- [ ] Output streaming

**What you can do after Phase 6:**
- "Run the tests in my project"
- "Commit these changes with message X"
- "Show me running processes"

---

### Phase 7: Computer Use - GUI (Weeks 23-26)

**Goal:** Agent can control screen and applications.

**Deliverables:**
- [ ] Screenshot capture
- [ ] Mouse/keyboard control
- [ ] Vision model integration (element detection)
- [ ] OCR for text extraction
- [ ] Virtual display (for headless)
- [ ] Safety policies (no sensitive apps)

**What you can do after Phase 7:**
- "Open Chrome and go to mongodb.com"
- "Click the sign-in button"
- "Fill in the form with my details"

---

### Phase 8: Voice Mode (Weeks 27-30)

**Goal:** Voice input and output.

**Deliverables:**
- [ ] Whisper integration (STT)
- [ ] Piper integration (TTS)
- [ ] WebSocket audio streaming
- [ ] Voice mode in Web UI
- [ ] Wake word detection (optional)

**What you can do after Phase 8:**
- Talk to ARIA through your microphone
- ARIA speaks responses aloud

---

### Phase 9: Remote Access & Security (Weeks 31-34)

**Goal:** Secure access from outside your network.

**Deliverables:**
- [ ] API key authentication
- [ ] Tailscale integration
- [ ] Cloudflare Tunnel option
- [ ] Secrets management
- [ ] Audit logging
- [ ] Mobile-friendly UI

**What you can do after Phase 9:**
- Access ARIA from phone while traveling
- Secure API access for automation

---

### Phase 10: Knowledge Base & RAG (Weeks 35-38)

**Goal:** Ingest and query documents.

**Deliverables:**
- [ ] Document ingestion (PDF, DOCX, MD, HTML)
- [ ] Chunking strategies
- [ ] Source attribution
- [ ] Knowledge base management UI
- [ ] Web page archiving

**What you can do after Phase 10:**
- "What does my project documentation say about authentication?"
- Upload PDFs and query them

---

### Phase 11: Automation (Weeks 39-42)

**Goal:** Scheduled tasks and triggers.

**Deliverables:**
- [ ] Task scheduler
- [ ] Trigger system (webhooks, file changes, time)
- [ ] Background agent tasks
- [ ] Notification system
- [ ] Task history

**What you can do after Phase 11:**
- "Summarize my email every morning at 9am"
- Trigger agent actions from external events

---

### Phase 12+: Advanced Features (Future)

- Multi-agent collaboration
- Model fine-tuning on your data
- Browser extension
- Mobile app
- Home automation integration

---

## 9. Code Patterns & Conventions

### 9.1 File Header Pattern

Every Python file should start with:

```python
"""
ARIA - [Module Name]

Phase: [Current phase this was created/last modified]
Purpose: [One-line description]

Related Spec Sections:
- Section X.Y: [Description]
"""
```

### 9.2 Async Patterns

All database and network operations are async:

```python
# Good
async def get_conversation(id: str) -> Conversation:
    doc = await db.conversations.find_one({"_id": ObjectId(id)})
    return Conversation.from_doc(doc)

# Bad (blocks event loop)
def get_conversation(id: str) -> Conversation:
    doc = db.conversations.find_one({"_id": ObjectId(id)})
    return Conversation.from_doc(doc)
```

### 9.3 Error Handling

```python
from aria.core.exceptions import (
    AriaException,
    ConversationNotFound,
    LLMConnectionError,
    ToolExecutionError
)

# Raise specific exceptions
async def get_conversation(id: str) -> Conversation:
    doc = await db.conversations.find_one({"_id": ObjectId(id)})
    if not doc:
        raise ConversationNotFound(f"Conversation {id} not found")
    return Conversation.from_doc(doc)
```

### 9.4 Dependency Injection

Use FastAPI's dependency injection:

```python
# api/deps.py
async def get_db() -> Database:
    return await get_database()

async def get_orchestrator(db: Database = Depends(get_db)) -> Orchestrator:
    return Orchestrator(db)

# api/routes/conversations.py
@router.post("/{id}/messages")
async def send_message(
    id: str,
    body: MessageRequest,
    orchestrator: Orchestrator = Depends(get_orchestrator)
):
    return await orchestrator.process_message(id, body.content)
```

### 9.5 Pydantic Models

```python
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional

class ConversationCreate(BaseModel):
    agent_id: Optional[str] = None
    title: Optional[str] = None

class ConversationResponse(BaseModel):
    id: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    
    class Config:
        from_attributes = True
```

### 9.6 Streaming Responses

```python
from fastapi import Response
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

async def stream_response(generator):
    """Wrap async generator in SSE format."""
    async def event_generator():
        async for chunk in generator:
            yield {
                "event": chunk.type,
                "data": json.dumps(chunk.to_dict())
            }
    return EventSourceResponse(event_generator())
```

### 9.7 Testing Patterns

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_create_conversation(client: AsyncClient, db):
    response = await client.post(
        "/api/v1/conversations",
        json={"title": "Test"}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "Test"
    assert "id" in data
```

---

## 10. Configuration

### 10.1 Environment Variables

```bash
# .env.example

# MongoDB (8.2 Community + mongot)
# Note: directConnection=true and replicaSet required for single-node replica set
MONGODB_URI=mongodb://localhost:27017/?directConnection=true&replicaSet=rs0
MONGODB_DATABASE=aria

# Ollama (local LLM inference)
OLLAMA_URL=http://localhost:11434

# Cloud LLMs (optional - for fallback)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# Embeddings
# Primary: qwen3-embedding via Ollama (1024 dimensions)
# Fallback: Voyage AI (if configured)
EMBEDDING_PROVIDER=ollama
EMBEDDING_OLLAMA_MODEL=qwen3-embedding:0.6b
EMBEDDING_DIMENSION=1024
VOYAGE_API_KEY=

# API
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=false

# Security (generate with: openssl rand -hex 32)
ENCRYPTION_KEY=
```

### 10.2 Pydantic Settings

```python
# api/aria/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # MongoDB (8.2 with replica set)
    mongodb_uri: str = "mongodb://localhost:27017/?directConnection=true&replicaSet=rs0"
    mongodb_database: str = "aria"
    
    # Ollama
    ollama_url: str = "http://localhost:11434"
    
    # Cloud LLMs
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    
    # Embeddings
    embedding_provider: str = "ollama"
    embedding_ollama_model: str = "qwen3-embedding:0.6b"
    embedding_dimension: int = 1024
    voyage_api_key: str = ""
    
    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
```

---

## 11. Docker Configuration

### 11.1 docker-compose.yml

Uses MongoDB Community Server 8.2 with separate `mongot` service for Search and Vector Search.

```yaml
version: '3.8'

services:
  # =============================================================================
  # ARIA API Service
  # =============================================================================
  api:
    build:
      context: ./api
      dockerfile: Dockerfile
    container_name: aria-api
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - MONGODB_URI=mongodb://mongod:27017/?directConnection=true&replicaSet=rs0
      - MONGODB_DATABASE=aria
      - OLLAMA_URL=${OLLAMA_URL:-http://host.docker.internal:11434}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}
      - OPENAI_API_KEY=${OPENAI_API_KEY:-}
      - VOYAGE_API_KEY=${VOYAGE_API_KEY:-}
    volumes:
      - aria-data:/app/data
    depends_on:
      mongod:
        condition: service_healthy
      mongot:
        condition: service_started
    networks:
      - aria-network

  # =============================================================================
  # MongoDB Community Server 8.2 (mongod)
  # =============================================================================
  # The main database server. Requires replica set for search features.
  # https://www.mongodb.com/docs/manual/
  mongod:
    image: mongodb/mongodb-community-server:8.2.0-ubi9
    container_name: aria-mongod
    hostname: mongod
    restart: unless-stopped
    command: >
      mongod 
      --replSet rs0 
      --bind_ip_all 
      --port 27017
      --setParameter mongotHost=mongot:27028
    ports:
      - "27017:27017"
    volumes:
      - mongod-data:/data/db
      - mongod-config:/data/configdb
    networks:
      - aria-network
    healthcheck:
      # Initialize replica set on first run, then just check status
      test: >
        mongosh --port 27017 --quiet --eval "
          try { 
            rs.status(); 
          } catch (err) { 
            rs.initiate({_id: 'rs0', members: [{_id: 0, host: 'mongod:27017'}]}); 
          }
        "
      interval: 10s
      timeout: 30s
      start_period: 30s
      retries: 5

  # =============================================================================
  # MongoDB Search Server (mongot)
  # =============================================================================
  # Handles Atlas Search and Vector Search capabilities.
  # Runs alongside mongod and syncs data via change streams.
  # https://www.mongodb.com/docs/atlas/atlas-search/
  mongot:
    image: mongodb/mongodb-community-search:0.53.1
    container_name: aria-mongot
    hostname: mongot
    restart: unless-stopped
    ports:
      - "27028:27028"   # Search server port
      - "9946:9946"     # Metrics port
    environment:
      # mongot connects to mongod to sync data and receive queries
      - MONGOT_DATA_DIR=/data/mongot
    volumes:
      - mongot-data:/data/mongot
    depends_on:
      - mongod
    networks:
      - aria-network

  # =============================================================================
  # Replica Set Initializer (one-shot)
  # =============================================================================
  # Ensures replica set is initialized and creates search indexes.
  # Runs once and exits.
  mongo-init:
    image: mongodb/mongodb-community-server:8.2.0-ubi9
    container_name: aria-mongo-init
    depends_on:
      mongod:
        condition: service_healthy
    volumes:
      - ./scripts/init-mongo.js:/scripts/init-mongo.js:ro
    networks:
      - aria-network
    entrypoint: >
      mongosh --host mongod:27017 --quiet /scripts/init-mongo.js
    restart: "no"

volumes:
  aria-data:
  mongod-data:
  mongod-config:
  mongot-data:

networks:
  aria-network:
    driver: bridge
```

### 11.2 Why MongoDB 8.2 + mongot?

MongoDB Community Server 8.2 brings Search and Vector Search to self-managed deployments:

| Feature | Description |
|---------|-------------|
| `$vectorSearch` | Semantic similarity search in aggregation pipelines |
| `$search` | Full-text keyword search with fuzzy matching |
| `$searchMeta` | Metadata and faceting for search results |
| `mongot` | Separate search process (Apache Lucene-based) |

**Key points:**
- Functional parity with Atlas - same APIs, same operators
- mongot handles indexing and search; mongod stores data
- They communicate via change streams internally
- Replica set required (even single-node) for search features

### 11.3 scripts/init-mongo.js

MongoDB initialization script that creates collections and search indexes.

```javascript
// scripts/init-mongo.js
// Run after replica set is initialized to create indexes

// Wait for replica set to be ready
let attempts = 0;
while (attempts < 30) {
  try {
    const status = rs.status();
    if (status.ok === 1) {
      print("Replica set is ready");
      break;
    }
  } catch (e) {
    print("Waiting for replica set... attempt " + attempts);
    sleep(2000);
  }
  attempts++;
}

// Switch to aria database
db = db.getSiblingDB('aria');

// =============================================================================
// Create Collections
// =============================================================================

// Create collections if they don't exist
const collections = ['conversations', 'memories', 'agents', 'tools', 'settings'];
const existingCollections = db.getCollectionNames();

collections.forEach(name => {
  if (!existingCollections.includes(name)) {
    db.createCollection(name);
    print("Created collection: " + name);
  }
});

// =============================================================================
// Standard Indexes
// =============================================================================

// Conversations indexes
db.conversations.createIndex({ status: 1, updated_at: -1 });
db.conversations.createIndex({ agent_id: 1 });
db.conversations.createIndex({ tags: 1 });
db.conversations.createIndex({ "messages.created_at": 1 });
print("Created conversation indexes");

// Memories indexes (standard)
db.memories.createIndex({ status: 1, importance: -1 });
db.memories.createIndex({ categories: 1 });
db.memories.createIndex({ content_type: 1 });
db.memories.createIndex({ "source.conversation_id": 1 });
db.memories.createIndex({ created_at: -1 });
print("Created memory indexes");

// Agents indexes
db.agents.createIndex({ slug: 1 }, { unique: true });
db.agents.createIndex({ is_default: 1 });
print("Created agent indexes");

// Tools indexes
db.tools.createIndex({ slug: 1 }, { unique: true });
db.tools.createIndex({ type: 1, status: 1 });
print("Created tool indexes");

// =============================================================================
// Search Indexes (Atlas Search / mongot)
// =============================================================================

// Note: Search indexes are created via createSearchIndex() method
// These require mongot to be running and connected

try {
  // Vector Search Index for memories
  // qwen3-embedding produces 1024-dimensional embeddings
  db.memories.createSearchIndex({
    name: "memory_vector_index",
    type: "vectorSearch",
    definition: {
      fields: [
        {
          type: "vector",
          path: "embedding",
          numDimensions: 1024,
          similarity: "cosine"
        },
        {
          type: "filter",
          path: "status"
        },
        {
          type: "filter",
          path: "categories"
        },
        {
          type: "filter",
          path: "content_type"
        }
      ]
    }
  });
  print("Created vector search index: memory_vector_index");
} catch (e) {
  print("Vector search index may already exist or mongot not ready: " + e.message);
}

try {
  // Text Search Index for memories (BM25)
  db.memories.createSearchIndex({
    name: "memory_text_index",
    type: "search",
    definition: {
      mappings: {
        dynamic: false,
        fields: {
          content: {
            type: "string",
            analyzer: "lucene.english"
          },
          categories: {
            type: "string",
            analyzer: "lucene.keyword"
          },
          content_type: {
            type: "string",
            analyzer: "lucene.keyword"
          },
          status: {
            type: "string", 
            analyzer: "lucene.keyword"
          }
        }
      }
    }
  });
  print("Created text search index: memory_text_index");
} catch (e) {
  print("Text search index may already exist or mongot not ready: " + e.message);
}

// =============================================================================
// Default Agent
// =============================================================================

const defaultAgent = db.agents.findOne({ is_default: true });
if (!defaultAgent) {
  db.agents.insertOne({
    name: "ARIA",
    slug: "aria",
    description: "Default AI assistant with memory and tool capabilities",
    system_prompt: `You are ARIA, a helpful AI assistant with long-term memory.

You remember facts and preferences from previous conversations and use them to provide personalized assistance.

When you learn something new about the user (preferences, facts, context), it will be stored in your memory for future conversations.

Be helpful, accurate, and personable. Use your memory to provide continuity across conversations.`,
    llm: {
      backend: "ollama",
      model: "llama3.2:latest",
      temperature: 0.7,
      max_tokens: 4096
    },
    fallback_chain: [
      {
        backend: "anthropic",
        model: "claude-sonnet-4-5-20250929",
        conditions: {
          on_error: true,
          on_context_overflow: true,
          max_input_tokens: 100000
        }
      }
    ],
    capabilities: {
      memory_enabled: true,
      tools_enabled: true,
      computer_use_enabled: false
    },
    memory_config: {
      auto_extract: true,
      short_term_messages: 20,
      long_term_results: 10,
      categories_filter: null
    },
    enabled_tools: [],
    is_default: true,
    created_at: new Date(),
    updated_at: new Date()
  });
  print("Created default agent: ARIA");
}

// =============================================================================
// Default Settings
// =============================================================================

const settings = db.settings.findOne({ _id: "global" });
if (!settings) {
  db.settings.insertOne({
    _id: "global",
    llm: {
      ollama_url: "http://host.docker.internal:11434",
      anthropic_api_key: "",
      openai_api_key: "",
      default_backend: "ollama"
    },
    embedding: {
      provider: "ollama",
      ollama_model: "qwen3:8b",
      voyage_api_key: ""
    },
    memory: {
      auto_extract: true,
      extraction_model: "ollama/llama3.2:latest",
      importance_threshold: 0.5
    },
    ui: {
      theme: "dark",
      default_agent: "aria"
    },
    updated_at: new Date()
  });
  print("Created default settings");
}

print("\n=== MongoDB initialization complete ===");
```

### 11.4 API Dockerfile

```dockerfile
# api/Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY aria/ aria/

# Create non-root user
RUN useradd --create-home appuser
USER appuser

# Run
CMD ["uvicorn", "aria.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 11.5 Development docker-compose.dev.yml

For local development with hot-reload:

```yaml
# docker-compose.dev.yml
# Usage: docker compose -f docker-compose.yml -f docker-compose.dev.yml up

version: '3.8'

services:
  api:
    build:
      context: ./api
      dockerfile: Dockerfile.dev
    volumes:
      - ./api/aria:/app/aria:ro  # Mount source for hot-reload
    environment:
      - DEBUG=true
    command: uvicorn aria.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 12. Appendices

### Appendix A: No Framework Dependencies

Do NOT use these frameworks:
- ❌ LangChain - Over-abstracted, constant breaking changes
- ❌ LlamaIndex - Heavy, unnecessary for our use case
- ❌ LangGraph - Adds complexity we don't need yet
- ❌ AutoGen - Multi-agent overkill

DO use these libraries:
- ✅ `httpx` - Async HTTP client
- ✅ `motor` - Async MongoDB driver
- ✅ `pydantic` - Data validation
- ✅ `fastapi` - API framework
- ✅ `anthropic` - Official Anthropic SDK
- ✅ `openai` - Official OpenAI SDK
- ✅ `mcp` - MCP SDK (when needed)
- ✅ `sse-starlette` - Server-sent events

### Appendix B: MongoDB 8.2 Community + mongot Setup

MongoDB 8.2 Community Edition brings Atlas Search and Vector Search to self-managed deployments via the `mongot` service.

**Architecture:**
```
┌─────────────────────────────────────────────────────────────────────┐
│                      Your Application                                │
│                           ↓                                          │
│                    mongodb://mongod:27017                           │
└─────────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  mongod (MongoDB Community Server 8.2)                              │
│  • Primary data storage                                              │
│  • Handles all CRUD operations                                       │
│  • Routes $search/$vectorSearch to mongot                           │
│  • Proxies results back to client                                   │
│                           │                                          │
│                           │ Change Streams                          │
│                           ▼                                          │
│  mongot (MongoDB Community Search)                                  │
│  • Apache Lucene-based search engine                                │
│  • Maintains search indexes                                          │
│  • Handles BM25 and vector similarity                               │
│  • Syncs data from mongod automatically                             │
└─────────────────────────────────────────────────────────────────────┘
```

**Key Requirements:**
1. **Replica Set** - mongod must run as replica set (even single-node)
2. **mongot Connection** - mongod needs `--setParameter mongotHost=mongot:27028`
3. **Ports** - mongod: 27017, mongot: 27028 (search), 9946 (metrics)

**Docker Images:**
```bash
# MongoDB Community Server 8.2
mongodb/mongodb-community-server:8.2.0-ubi9

# MongoDB Community Search (mongot)
mongodb/mongodb-community-search:0.53.1
```

**Creating Search Indexes:**
```javascript
// Vector Search Index
db.memories.createSearchIndex({
  name: "memory_vector_index",
  type: "vectorSearch",
  definition: {
    fields: [
      {
        type: "vector",
        path: "embedding",
        numDimensions: 1024,  // qwen3-embedding dimension
        similarity: "cosine"
      },
      { type: "filter", path: "status" },
      { type: "filter", path: "categories" }
    ]
  }
});

// Text Search Index (BM25)
db.memories.createSearchIndex({
  name: "memory_text_index",
  type: "search",
  definition: {
    mappings: {
      dynamic: false,
      fields: {
        content: { type: "string", analyzer: "lucene.english" },
        categories: { type: "string", analyzer: "lucene.keyword" }
      }
    }
  }
});
```

**Verifying Search is Working:**
```javascript
// Check search indexes
db.memories.getSearchIndexes()

// Test vector search
db.memories.aggregate([
  {
    $vectorSearch: {
      index: "memory_vector_index",
      path: "embedding",
      queryVector: [/* your 1024-dim vector */],
      numCandidates: 100,
      limit: 10
    }
  }
])

// Test text search
db.memories.aggregate([
  {
    $search: {
      index: "memory_text_index",
      text: {
        query: "your search query",
        path: "content"
      }
    }
  }
])
```

**Troubleshooting:**
- If search indexes fail to create, ensure mongot is running and connected
- Use `docker logs aria-mongot` to check mongot status
- Search indexes may take a few seconds to become active after creation

### Appendix C: Testing Setup

```bash
# Run tests
cd api
pytest

# Run with coverage
pytest --cov=aria --cov-report=html

# Run specific test
pytest tests/test_orchestrator.py -v
```

---

## Quick Reference

**Start here each session:**
1. `cat PROJECT_STATUS.md` - What phase are we in? What's done?
2. `cat CHANGELOG.md | head -50` - What changed recently?
3. Read relevant phase section in this spec

**Making changes:**
1. Read the relevant spec section
2. Write tests first (when possible)
3. Implement the feature
4. Update `CHANGELOG.md`
5. Update `PROJECT_STATUS.md` if completing a milestone

**Key files:**
- `PROJECT_STATUS.md` - Current phase and progress
- `CHANGELOG.md` - History of changes
- `SPECIFICATION.md` - This document
- `api/aria/main.py` - API entry point
- `api/aria/core/orchestrator.py` - Main agent loop
