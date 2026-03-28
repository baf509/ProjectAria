# ARIA — Architecture & Agent System

> How ARIA thinks, delegates, and gets things done.

## What Is ARIA?

ARIA (Autonomous Reasoning & Intelligence Architecture) is a personal AI agent that runs on your machine. She's not a chatbot — she has persistent memory, her own evolving identity (`SOUL.md`), and the ability to take action through tools, sub-agents, and background processes.

ARIA sits at the center of a hub-and-spoke architecture. Users interact with her through multiple interfaces (web UI, desktop widget, CLI, TUI, REST API), and she delegates specialized work to sub-agents and background processes — many of which are Claude Code instances running autonomously on your subscription.

```
                    You
                     │
        ┌────────────┼────────────┐
        │            │            │
     Web UI     TUI (Go)      CLI/API
        │            │            │
        └────────────┼────────────┘
                     │
              ┌──────┴──────┐
              │    ARIA     │
              │ Orchestrator│
              └──────┬──────┘
                     │
       ┌─────────────┼─────────────┐
       │             │             │
  Sub-Agents    Background     Tools &
  (Coding)      Processes       MCP
       │             │             │
  Pi Coding    Dream Cycle    Filesystem
  Claude Code  Research       Shell
  Codex        Heartbeat      Web Fetch
               Autopilot      Screenshot
               Awareness      Skills
               OODA Loop      MCP Servers
               Summarization
               Memory Extract
```

---

## The Orchestrator

Every user message flows through ARIA's orchestrator (`core/orchestrator.py`). This is the central loop:

1. **Pre-message hooks** fire (extensibility)
2. **Command routing** — intercepts `/mode`, `/research`, `/memory`, `/code` commands
3. **Agent resolution** — loads the active agent config (ARIA by default, or a mode-specific agent)
4. **Context assembly** — builds the LLM prompt from:
   - SOUL.md (ARIA's identity)
   - Conversation summary (if exists)
   - Long-term memories (hybrid vector + BM25 search)
   - Short-term messages (recent conversation)
   - Skill catalog (names only — progressive disclosure)
   - Awareness context (environmental observations)
5. **LLM streaming** with fallback chain (primary backend fails → try next)
6. **Tool execution loop** — may run multiple rounds of tool calls
7. **Steering message check** — between tool calls, check for user interrupts
8. **Background tasks** — fire-and-forget memory extraction, summary updates
9. **Post-message hooks** fire

---

## Sub-Agents

Sub-agents are specialized agents that ARIA can delegate to. They appear in the TUI sidebar and can be invoked by ARIA herself or by the user directly.

### Pi Coding Agent

A local LLM coding assistant running on llama.cpp. Free, private, always available.

| Property | Value |
|----------|-------|
| **Slug** | `pi-coding` |
| **Backend** | llama.cpp (local) |
| **Temperature** | 0.4 (focused) |
| **Tools** | filesystem, shell, web |
| **Memory** | Enabled, auto-extraction |

**How it works:** ARIA creates a persistent conversation with the Pi Coding Agent and processes it through her own orchestrator using the local LLM. The conversation persists — you can jump into it from the TUI to see what happened or continue the work.

**Invoked by:** ARIA's `pi_coding_agent` tool, or directly from the TUI sidebar.

### Claude Code Sessions

Interactive coding sessions using the Claude Code CLI. These run as autonomous subprocesses with `--dangerously-skip-permissions` so ARIA can delegate without human intervention.

| Property | Value |
|----------|-------|
| **Binary** | `claude` CLI |
| **Permissions** | `--dangerously-skip-permissions` |
| **Output** | Captured in 500-line circular buffer |
| **Monitoring** | Watchdog checks every 5s for stalls |
| **Review** | Auto-review on completion (git diff, tests, linting) |

**How it works:** ARIA spawns a Claude Code process via `asyncio.create_subprocess_exec()`, captures stdout/stderr, and monitors it through the watchdog service. When the session completes, it auto-reviews the work (runs tests, checks lint, captures the git diff) and stores a report.

**Invoked by:** ARIA's `start_coding_session` tool (backend: `claude_code`), or via the API.

### Codex Sessions

Same session infrastructure as Claude Code but using the Codex CLI with `--sandbox workspace-write --ask-for-approval never`.

**Invoked by:** ARIA's `start_coding_session` tool (backend: `codex`).

---

## Background Processes (Claude Code Subprocess)

These are autonomous background tasks where ARIA delegates work to a Claude Code CLI instance via the `ClaudeRunner` infrastructure. They use your Claude subscription tokens (not API tokens) and run without interactive permissions.

All background processes share:
- **Runner:** `ClaudeRunner` (`core/claude_runner.py`) — spawns `claude -p "prompt"`
- **Fallback:** If CLI unavailable, falls back to LLM API adapter
- **Token source:** Subscription (CLI) when available, API tokens as fallback
- **Config toggle:** `use_claude_runner = True` (global switch)

### Dream Cycle

ARIA's offline reflection system. During quiet hours (1am-5am by default), she reviews her memories, finds patterns, and evolves.

| Property | Value |
|----------|-------|
| **Schedule** | Every 6 hours, during quiet hours only |
| **Timeout** | 300 seconds (extended) |
| **Prompt** | `prompts/dream_reflection.md` |
| **Config** | `dream_enabled`, `dream_interval_hours` |

**What ARIA receives:**
- Her SOUL.md identity
- Last 50 memories
- Recent conversation summaries
- Previous journal entries

**What ARIA produces:**
- A private journal entry (2-4 paragraphs of genuine reflection)
- Non-obvious connections between memories
- Knowledge gaps (specific things she wishes she knew)
- Memory consolidation proposals (merge redundant memories)
- Soul evolution proposals (changes to SOUL.md, require user review)

**Post-processing:** Journal entries are stored in `dream_journal`. Memory consolidations are applied (originals soft-deleted). Soul proposals are stored for user review — ARIA can't change her own identity without approval.

### Research Service

Recursive web research with branching queries, learning extraction, and report synthesis.

| Property | Value |
|----------|-------|
| **Trigger** | Manual via `/research query` or API |
| **Timeout** | 120 seconds per stage |
| **Prompts** | `research_query.md`, `research_learnings.md`, `research_synthesis.md` |

**Three-stage pipeline:**
1. **Query generation** — Takes the original query, generates follow-up queries for recursive branching
2. **Learning extraction** — Fetches web sources, extracts structured learnings with confidence scores
3. **Report synthesis** — Combines all learnings into a narrative report

High-confidence findings are stored as memories. The final report is persisted to the database.

### Heartbeat Service

Periodic check-in that reviews a checklist and alerts the user if anything needs attention.

| Property | Value |
|----------|-------|
| **Schedule** | Every 30 minutes, active hours only (9am-10pm) |
| **Timeout** | 120 seconds |
| **Prompt** | `prompts/heartbeat.md` |
| **Config** | `heartbeat_enabled`, `heartbeat_interval_minutes` |

**What ARIA receives:** The `HEARTBEAT.md` checklist, current time, relevant memories.

**What ARIA returns:** Either exactly `HEARTBEAT_OK` (nothing to report) or a concise alert delivered via Signal/Telegram notification.

### OODA Loop (Self-Correction)

Evaluates ARIA's own response quality and optionally retries if below threshold.

| Property | Value |
|----------|-------|
| **Trigger** | During response generation (if enabled) |
| **Timeout** | 120 seconds |
| **Prompt** | `prompts/ooda_evaluation.md` |
| **Threshold** | 0.7 (configurable) |
| **Max retries** | 2 |

Claude Code scores ARIA's candidate response (0.0-1.0) and provides feedback. If below threshold, ARIA retries with the feedback incorporated.

### Autopilot

Goal decomposition and step-by-step autonomous execution.

| Property | Value |
|----------|-------|
| **Trigger** | Manual via API |
| **Timeout** | 120s planning, 300s per step |
| **Prompts** | `prompts/autopilot_planning.md` |

**Two phases:**
1. **Planner** — Decomposes a goal into structured steps (each step is either an LLM query or a tool call)
2. **Executor** — Runs steps sequentially, with optional user approval before each step

### Ambient Awareness

Passive environmental monitoring that watches git activity, system health, and filesystem changes.

| Property | Value |
|----------|-------|
| **Schedule** | Every 30 minutes (if enabled) |
| **Timeout** | 120 seconds |
| **Prompt** | `prompts/awareness_analysis.md` |
| **Config** | `awareness_enabled` |

Sensors feed observations into the system. Claude Code analyzes them and produces a situational summary injected into ARIA's context on the next conversation.

### Conversation Summarization

Rolling conversation compaction when the context window gets too large.

| Property | Value |
|----------|-------|
| **Trigger** | Automatic when messages exceed threshold |
| **Timeout** | 120 seconds |
| **Prompts** | `prompts/summarization.md`, `prompts/structured_compaction.md` |

Structured compaction preserves: goal/task, constraints, progress, decisions, files touched, open questions, and key context.

### Memory Extraction

Background extraction of structured memories from conversations.

| Property | Value |
|----------|-------|
| **Trigger** | After conversations (if agent has `auto_extract` enabled) |
| **Timeout** | 120 seconds |
| **Prompt** | `prompts/extraction.md` |

Extracts facts, relationships, events, and beliefs from conversation messages. Each memory gets categories, importance scores, and embeddings for later retrieval.

### Session Digest

Analyzes completed Claude Code sessions and extracts key takeaways.

| Property | Value |
|----------|-------|
| **Trigger** | Awareness sensor flags sessions with 3+ messages |
| **Prompt** | `prompts/session_digest.md` |

Extracts: key takeaways, decisions made, open questions, and topics discussed.

---

## Built-in Tools

Tools ARIA can call during conversations:

| Tool | Description |
|------|-------------|
| `shell` | Execute shell commands |
| `filesystem` | Read, write, list, delete files |
| `web_fetch` | HTTP requests |
| `screenshot` | Capture and analyze screen |
| `start_coding_session` | Spawn Claude Code or Codex |
| `stop_coding_session` | Stop a running session |
| `get_coding_output` | Read session output |
| `get_coding_diff` | Get git diff from session workspace |
| `send_to_coding_session` | Send input to running session |
| `list_coding_sessions` | List active sessions |
| `list_llamacpp_models` | List local LLM models |
| `switch_llamacpp_model` | Switch active local model |
| `claude_agent` | Delegate a one-shot task to Claude Code |
| `pi_coding_agent` | Delegate to Pi Coding Agent (local LLM) |
| `update_soul` | Read or modify SOUL.md |
| `document_generation` | Generate documentation |

Additionally, MCP servers provide dynamic tools via JSON-RPC 2.0, and the skill system allows installing packaged tool bundles.

---

## Memory System

Two-tier architecture:

**Short-term:** Recent conversation messages. Fast MongoDB queries, token-aware truncation. Configurable depth per agent (default: 20 messages).

**Long-term:** Hybrid search combining:
- `$vectorSearch` — 1024-dim voyage-4-nano embeddings, cosine similarity
- `$search` — BM25 lexical search
- RRF fusion (k=60) merges both result sets

Memories have: content, content_type (fact/preference/experience/relationship), categories, confidence scores, importance, and access counts. Confidence decays over time; frequently accessed memories rank higher.

---

## Interfaces

| Interface | Technology | Description |
|-----------|-----------|-------------|
| **Web UI** | Next.js | Chat interface at localhost:3000 |
| **TUI** | Go (Bubble Tea) | Terminal dashboard with sidebar, chat, sessions, memory browser, usage monitor, tools browser, observations |
| **Desktop Widget** | Tauri v2 | System tray app, `Ctrl+Space` hotkey |
| **CLI** | Python | `aria chat "message"`, `aria conversations list`, etc. |
| **REST API** | FastAPI | Full API with SSE streaming at localhost:8000 |

---

## Configuration Reference

Key settings in `.env`:

```bash
# Background process master switch
USE_CLAUDE_RUNNER=true              # Route background tasks through Claude CLI
CLAUDE_RUNNER_TIMEOUT_SECONDS=120   # Default timeout

# Dream cycle
DREAM_ENABLED=false
DREAM_INTERVAL_HOURS=6
DREAM_TIMEOUT_SECONDS=300

# Heartbeat
HEARTBEAT_ENABLED=false
HEARTBEAT_INTERVAL_MINUTES=30

# Awareness
AWARENESS_ENABLED=false

# Coding sessions
CODING_DEFAULT_BACKEND=codex        # or claude_code
CODING_OUTPUT_LINES=500
CODING_WATCHDOG_INTERVAL_SECONDS=5
CODING_STALL_SECONDS=60
```
