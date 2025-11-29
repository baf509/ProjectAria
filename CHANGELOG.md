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
