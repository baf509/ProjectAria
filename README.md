# ARIA - Local AI Agent Platform

> Personal AI agent with long-term memory, tool use, and multiple interfaces — runs on your hardware.

ARIA is a self-hosted AI assistant that remembers your conversations, uses tools, and works with any LLM backend. It runs entirely on your infrastructure with no cloud dependency required.

## Features

- **Long-term memory** — Hybrid search (vector + BM25) remembers facts and preferences across conversations
- **Any LLM backend** — llama.cpp (ROCm), Anthropic, OpenAI, OpenRouter — with automatic fallback
- **Multiple interfaces** — Web UI, desktop widget (Tauri), CLI, and REST API
- **Tool use & MCP** — Built-in filesystem/shell/web tools plus MCP server integration
- **Local-first** — MongoDB 8.2 + mongot for vector search, no Atlas subscription needed
- **Local embeddings** — voyage-4-nano via sentence-transformers, runs on CPU
- **Voice I/O** — Text-to-speech (Qwen3-TTS) and speech-to-text (Whisper) microservices, both on CPU
- **Single-user** — Personal agent, no auth complexity

## Architecture

```
                     ┌──────────────┐
                     │   Clients    │
                     │  Widget/UI/  │
                     │   CLI/API    │
                     └──────┬───────┘
                            │
                            ▼
┌───────────────────────────────────────────────┐
│  ARIA API (FastAPI)                           │
│  ┌─────────────┐  ┌──────────────────────┐   │
│  │ Orchestrator │→ │ LLM Manager          │   │
│  │              │  │  llama.cpp (ROCm)   │   │
│  │  Context     │  │  Claude / GPT        │   │
│  │  Builder     │  │  OpenRouter          │   │
│  └──────┬───────┘  └──────────────────────┘   │
│         │                                      │
│  ┌──────┴───────┐  ┌──────────────────────┐   │
│  │ Memory       │  │ Tool Router          │   │
│  │  Short-term  │  │  Built-in tools      │   │
│  │  Long-term   │  │  MCP servers         │   │
│  └──────┬───────┘  └──────────────────────┘   │
└─────────┼─────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────┐
│ MongoDB 8.2 + mongot  │  Embeddings │  Voice     │
│  mongod (data)        │  voyage-4-  │  TTS (CPU) │
│  mongot (search)      │  nano (CPU) │  STT (CPU) │
└────────────────────────────────────────────────────┘
```

## Quick Start

See **[GETTING_STARTED.md](GETTING_STARTED.md)** for the full setup guide.

```bash
# 1. Clone and configure
git clone https://github.com/baf509/ProjectAria.git
cd ProjectAria
cp .env.example .env        # Edit with your API keys and model paths

# 2. Start the stack
docker compose up -d

# 3. Access ARIA
open http://localhost:3000   # Web UI
aria chat "Hello, ARIA!"     # CLI (optional)
```

## Interfaces

| Interface | Description | Access |
|-----------|-------------|--------|
| **Web UI** | Next.js chat interface | http://localhost:3000 |
| **Desktop Widget** | Tauri app, system tray, `Ctrl+Space` hotkey | `cd widget && npm run tauri:dev` |
| **CLI** | Terminal chat client | `aria chat "message"` |
| **REST API** | Full API with streaming | http://localhost:8000/docs |

## LLM Backends

| Backend | Type | Config |
|---------|------|--------|
| **llama.cpp** | Local (ROCm) | Bundled — AMD APU/GPU acceleration |
| **Anthropic** | Cloud | `ANTHROPIC_API_KEY` in `.env` |
| **OpenAI** | Cloud | `OPENAI_API_KEY` in `.env` |
| **OpenRouter** | Cloud (multi) | `OPENROUTER_API_KEY` in `.env` |

The llama.cpp service uses pre-built ROCm binaries from [lemonade-sdk/llamacpp-rocm](https://github.com/lemonade-sdk/llamacpp-rocm) and supports AMD APUs (gfx1151/gfx1150) and RDNA3/4 GPUs.

## Embedding Service

Embeddings are generated locally by a lightweight sentence-transformers service running `voyageai/voyage-4-nano` on CPU. It exposes an OpenAI-compatible `/v1/embeddings` endpoint on port 8001. The model is downloaded at Docker build time so startup is instant.

- **Model**: `voyageai/voyage-4-nano` (MRL truncated to 1024 dims)
- **Service**: `http://localhost:8001`
- **Fallback**: Voyage AI cloud API (if `VOYAGE_API_KEY` is set)

## Voice Services

### Text-to-Speech (TTS)

Speech synthesis powered by Qwen3-TTS 0.6B CustomVoice running on CPU. The widget and web UI show a play button on assistant messages to read responses aloud.

- **Model**: `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`
- **Service**: `http://localhost:8002`
- **9 speakers**: Chelsie, Ethan, Ryan, Layla, Luke, Natasha, Oliver, Sophia, Tyler

### Speech-to-Text (STT)

Transcription powered by `openai/whisper-large-v3-turbo` via faster-whisper, running on CPU with int8 quantization. The widget mic button records audio and inserts the transcribed text into the input field.

- **Model**: `openai/whisper-large-v3-turbo` (int8)
- **Service**: `http://localhost:8003`
- **Auto language detection** with optional language hint

## Directory Structure

```
ProjectAria/
├── api/                    # FastAPI backend
│   └── aria/
│       ├── core/           # Orchestrator, context builder
│       ├── llm/            # LLM adapters (llamacpp, anthropic, openai, openrouter)
│       ├── memory/         # Short-term + long-term memory, embeddings
│       ├── tools/          # Built-in tools + MCP integration
│       └── db/             # MongoDB models and connection
├── embeddings/             # Embedding microservice (sentence-transformers)
├── tts/                    # TTS microservice (Qwen3-TTS)
├── stt/                    # STT microservice (whisper-large-v3-turbo)
├── ui/                     # Next.js web UI
├── widget/                 # Tauri desktop widget
├── cli/                    # Python CLI client
├── llamacpp/               # llama.cpp ROCm Dockerfile
├── models/                 # GGUF model files (gitignored)
├── scripts/                # MongoDB init, setup scripts
├── docker-compose.yml      # Full service stack
├── GETTING_STARTED.md      # Setup guide
├── SPECIFICATION.md        # Detailed architecture
└── PROJECT_STATUS.md       # Current progress
```

## Development

```bash
# API (with hot-reload)
cd api && uvicorn aria.main:app --reload --host 0.0.0.0 --port 8000

# Web UI (with hot-reload)
cd ui && npm run dev

# Desktop Widget
cd widget && npm install && npm run tauri:dev

# CLI
cd cli && pip install -e .
```

## Documentation

- **[GETTING_STARTED.md](GETTING_STARTED.md)** — Full setup and usage guide
- **[SPECIFICATION.md](SPECIFICATION.md)** — Detailed architecture and requirements
- **[PROJECT_STATUS.md](PROJECT_STATUS.md)** — Current phase and progress
- **[CHANGELOG.md](CHANGELOG.md)** — Change history

## Key Design Decisions

1. **No framework dependencies** — No LangChain, LlamaIndex, etc. Direct API integration only.
2. **LLM agnostic** — Adapter pattern makes backends swappable with automatic fallback.
3. **MongoDB 8.2 + mongot** — Community Server with vector search, no Atlas needed.
4. **Local-first** — Local LLMs primary, cloud APIs as fallback.
5. **Hybrid search** — BM25 + vector with RRF fusion for memory retrieval.

## Current Status

See `PROJECT_STATUS.md` for detailed progress. Phases 1-5 are complete (Foundation, Memory, Tools, Cloud LLMs, Web UI). The desktop widget and llama.cpp ROCm support are the latest additions.
