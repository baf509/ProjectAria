# ARIA Setup Guide

## Quick Start

### Prerequisites

1. **Docker and Docker Compose** installed
2. **Ollama** running locally (for LLM inference)
   ```bash
   # Install Ollama: https://ollama.ai/download
   # Pull a model (e.g., Llama 3.2)
   ollama pull llama3.2
   ```

### 1. Clone and Configure

```bash
git clone https://github.com/baf509/ProjectAria.git
cd ProjectAria

# Copy environment template
cp .env.example .env

# Edit .env if needed (default should work for local setup)
```

### 2. Start the Stack

```bash
# Start all services
docker compose up -d

# Check status
docker compose ps

# View logs
docker compose logs -f
```

Services that will start:
- **mongod** - MongoDB database (port 27017)
- **mongot** - MongoDB search server (port 27028)
- **mongo-init** - One-time initialization (exits after setup)
- **api** - ARIA FastAPI server (port 8000)

### 3. Verify Health

```bash
# Check API health
curl http://localhost:8000/api/v1/health

# Should return:
# {
#   "status": "healthy",
#   "version": "0.2.0",
#   "database": "connected",
#   "timestamp": "..."
# }
```

### 4. Install CLI (Optional)

```bash
cd cli
pip install -r requirements.txt
pip install -e .

# Test CLI
aria health
```

### 5. Chat with ARIA

#### Using CLI

```bash
# Interactive mode
aria chat

# One-shot message
aria chat "Hello, ARIA!"

# List conversations
aria conversations list

# Continue specific conversation
aria chat -c <conversation-id> "What did we discuss?"
```

#### Using API directly

```bash
# Create conversation
CONV_ID=$(curl -s -X POST http://localhost:8000/api/v1/conversations \
  -H "Content-Type: application/json" \
  -d '{"title":"Test Chat"}' | jq -r '.id')

# Send message (streaming)
curl -X POST http://localhost:8000/api/v1/conversations/$CONV_ID/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"Hello!","stream":true}'
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ CLI Client (Python)                                     │
│  - Interactive chat                                     │
│  - Conversation management                              │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│ FastAPI Server (port 8000)                              │
│  - REST + SSE endpoints                                 │
│  - Agent orchestrator                                   │
│  - LLM adapter layer                                    │
└────────┬───────────────────────┬────────────────────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌────────────────────────────┐
│ Ollama (local)   │    │ MongoDB 8.2                │
│  - LLM inference │    │  - mongod (data)           │
│  - Port 11434    │    │  - mongot (search)         │
└──────────────────┘    │  - Vector + BM25 search    │
                        └────────────────────────────┘
```

## Configuration

### Environment Variables

Edit `.env` to customize:

```bash
# Ollama URL (if running outside Docker)
OLLAMA_URL=http://host.docker.internal:11434

# Cloud LLM APIs (optional, for Phase 4)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# MongoDB (default should work)
MONGODB_URI=mongodb://mongod:27017/?directConnection=true&replicaSet=rs0
```

## Troubleshooting

### MongoDB not starting

```bash
# Check logs
docker compose logs mongod

# Restart with clean state
docker compose down -v
docker compose up -d
```

### Ollama connection error

```bash
# Verify Ollama is running
curl http://localhost:11434/api/version

# Check if model is available
ollama list

# Pull model if needed
ollama pull llama3.2
```

### API not responding

```bash
# Check API logs
docker compose logs api

# Restart API
docker compose restart api
```

## Development

### Hot Reload (Development Mode)

```bash
# Use development compose file
docker compose -f docker-compose.yml -f docker-compose.dev.yml up

# API will reload on code changes
```

### Run Tests

```bash
cd api
pytest
```

### Database Inspection

```bash
# Connect to MongoDB
docker exec -it aria-mongod mongosh

# Use aria database
use aria

# List collections
show collections

# Check default agent
db.agents.findOne({is_default: true})

# List conversations
db.conversations.find().pretty()
```

## Next Steps

1. **Test basic chat** - Verify end-to-end conversation flow
2. **Explore API docs** - Visit http://localhost:8000/docs
3. **Phase 2** - Implement memory system (short-term + long-term)

## Resources

- [SPECIFICATION.md](SPECIFICATION.md) - Complete technical spec
- [PROJECT_STATUS.md](PROJECT_STATUS.md) - Current phase and progress
- [CHANGELOG.md](CHANGELOG.md) - Change history
- [MongoDB 8.2 Docs](https://www.mongodb.com/docs/manual/)
- [Ollama API](https://github.com/ollama/ollama/blob/main/docs/api.md)
