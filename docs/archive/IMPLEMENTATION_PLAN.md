# Aria Implementation Plan: Unified Local Agent Platform

**Created:** 2026-03-14
**Goal:** Evolve ProjectAria into a unified personal AI agent accessible from any device, with shared memory, multiple personalities/modes, model switching, and the orchestration capabilities currently in AgentBenchPlatform.

**Guiding Principles:**
- One agent, one memory, many interfaces
- Signal is the mobile interface — no dedicated mobile app
- HTTP API is the universal access layer
- Port ABP's best ideas as Aria features, then retire ABP
- Local-first, cloud-augmented
- Single-user, maximum simplicity

---

## Phase 7: Signal Integration (Port from ABP)

**Priority:** Highest — this is the missing mobile interface
**Estimated Complexity:** Medium
**Dependencies:** None (Aria API already exists)

### 7.1 Signal Client Service

Port ABP's `infra/signal/` architecture to Aria. ABP uses a four-layer stack: SignalDaemon (process lifecycle), SignalClient (JSON-RPC over HTTP to signal-cli), SignalHandler (message validation and routing), and SignalService (orchestration).

**Tasks:**

1. **Create `api/aria/signal/` package** with the following modules:

2. **`client.py` — Signal-CLI HTTP Client**
   - Connect to signal-cli daemon's HTTP API (JSON-RPC 2.0)
   - Receive messages via SSE/JSON-lines stream from signal-cli stdout
   - Send messages via HTTP POST to signal-cli REST API
   - Implement chunked message sending: split messages >2000 chars at newline boundaries, prefix with `[N/M]` for multi-part messages
   - Handle attachment sending (images, files) for research reports or code snippets
   - Implement auto-reconnection with 5-second backoff on connection errors

3. **`handler.py` — Message Router**
   - Parse incoming Signal envelopes (sender, timestamp, body, attachments)
   - Implement sender allowlist with `dm_policy` (allowlist or open)
   - Route text messages to Aria orchestrator as conversation messages
   - Support voice note transcription: read attachment files from signal-cli's attachment directory, send to STT service, use transcribed text as message content
   - Parse slash commands from message body (see 7.3)
   - Map each Signal sender to a persistent conversation (by phone number)

4. **`daemon.py` — signal-cli Lifecycle**
   - Start/stop signal-cli as subprocess (requires Java 21+)
   - Health checking via periodic ping
   - Graceful shutdown on Aria shutdown
   - Configuration: signal-cli path, account phone number, attachment directory

5. **`service.py` — Signal Service Coordinator**
   - Startup/shutdown lifecycle management
   - Message receive loop (background asyncio task)
   - Message send queue (debounce rapid responses)
   - Phone number registration and pairing flow
   - Status reporting (connected, last message, errors)

### 7.2 Signal API Routes

Add routes at `/api/aria/api/routes/signal.py`:

- `POST /signal/start` — Start Signal service
- `POST /signal/stop` — Stop Signal service
- `GET /signal/status` — Connection status, last message time, paired senders
- `POST /signal/pair` — Add allowed sender phone number
- `POST /signal/send` — Send message to a phone number (for proactive notifications)

### 7.3 Signal Slash Commands

Implement in-message command parsing for Signal users:

| Command | Action |
|---|---|
| `/mode <name>` | Switch agent personality (see Phase 9) |
| `/status` | List active background tasks, last conversation summary |
| `/research "<query>"` | Start deep research (see Phase 11) |
| `/remember <text>` | Store explicit memory |
| `/forget <text>` | Search and delete matching memory |
| `/model <name>` | Switch LLM backend for this conversation |
| `/modes` | List available modes/personalities |
| `/help` | List available commands |

### 7.4 Proactive Notifications via Signal

Port ABP's notification system. Aria should push messages to the user's phone when:

- A long-running task completes (research, code generation)
- An error occurs that needs attention
- A scheduled reminder fires (see Phase 14)
- A monitored coding agent stalls or completes (see Phase 12)

**Implementation:**
- Add `NotificationService` in `api/aria/notifications/service.py`
- Notification channels: Signal (primary), future: desktop notification, webhook
- Cooldown per (source, event_type): 60 seconds to prevent spam
- Format: `[source] EVENT: detail` (e.g., `[research] COMPLETE: "GPU benchmarks" — 12 learnings extracted`)
- Store notification preferences in agent config or global settings
- Background task checks notification queue every 5 seconds

### 7.5 Configuration

Add to `config.py`:
```python
# Signal
SIGNAL_CLI_PATH: str = ""  # Path to signal-cli binary
SIGNAL_ACCOUNT: str = ""   # Registered phone number
SIGNAL_ENABLED: bool = False
SIGNAL_DM_POLICY: str = "allowlist"  # "allowlist" or "open"
SIGNAL_ALLOWED_SENDERS: list[str] = []
SIGNAL_ATTACHMENT_DIR: str = "~/.local/share/signal-cli/attachments/"
```

### 7.6 Docker Integration

Add signal-cli service to `docker-compose.yml`:
```yaml
signal-cli:
  image: bbernhard/signal-cli-rest-api  # or custom build
  volumes:
    - signal-data:/home/.local/share/signal-cli
  ports:
    - "8005:8080"
  network: shared-infra
```

---

## Phase 8: Hardening & Reliability

**Priority:** High — fix critical gaps before adding features
**Dependencies:** None

### 8.1 Token Counting

Replace the `1 token ≈ 4 chars` heuristic with actual token counting.

**Tasks:**
1. Add `tiktoken` dependency for OpenAI models
2. Add `anthropic` tokenizer for Claude models
3. Create `api/aria/core/tokenizer.py` with:
   - `count_tokens(text: str, model: str) -> int`
   - `truncate_to_budget(messages: list, budget: int, model: str) -> list`
   - Cache tokenizer instances per model family
4. Use in context builder to enforce actual token budgets
5. Use in short-term memory to trim conversation history accurately
6. Add `max_context_tokens` to `AgentLLMConfig` (default: model-specific)

### 8.2 Conversation Summarization

Aria has the `summary` field on conversations but never populates it. Port ABP's summarization pattern.

**Tasks:**
1. Create `api/aria/core/summarization.py`:
   - `summarize_conversation(messages: list[Message], llm: LLMAdapter) -> str`
   - Triggered when conversation exceeds `short_term_messages` threshold
   - Stores summary in conversation document
   - Summary injected into context as system message
2. Truncation strategy (port from ABP):
   - Keep last N user/assistant exchanges
   - Only cut before user messages (preserve tool-call sequences)
   - If dropping >10 messages, summarize the dropped portion
   - Track consecutive summarization failures; fall back to aggressive truncation after 3 failures
3. Periodic cleanup: remove stale conversation caches after 24h idle

### 8.3 Error Recovery & Retry Logic

**Tasks:**
1. Add retry with exponential backoff to:
   - Embedding generation (currently fails immediately)
   - LLM API calls (currently caught but not retried)
   - Memory extraction background tasks
   - MCP tool calls
2. Implement circuit breaker for external services:
   - Track failure count per service (LLM provider, embedding, Signal)
   - Open circuit after 5 consecutive failures
   - Half-open after 30 seconds (try one request)
   - Close on success
3. Add `api/aria/core/resilience.py` with `retry_async()` and `CircuitBreaker` utilities

### 8.4 Vector Index Setup Automation

**Tasks:**
1. Create `api/aria/db/migrations.py`:
   - Run on startup (idempotent)
   - Create `memory_vector_index` (1024-dim, cosine similarity)
   - Create `memory_text_index` (BM25 on content + categories)
   - Create standard indexes (conversation timestamps, memory access counts, etc.)
2. Add migration runner to FastAPI lifespan startup
3. Log migration results for debugging

### 8.5 SSE Format Standardization

The current SSE implementation uses raw JSON lines instead of proper SSE format.

**Tasks:**
1. Update conversation streaming to use standard SSE format:
   ```
   event: text
   data: {"content": "..."}

   event: tool_call
   data: {"name": "...", "arguments": {...}}

   event: done
   data: {"usage": {...}}
   ```
2. Add `event:` field typing for client parsing
3. Add `id:` field for reconnection support
4. Update Web UI and Widget API clients to parse new format
5. Add `Last-Event-ID` header support for reconnection

### 8.6 Usage & Cost Tracking

Port ABP's usage tracking system.

**Tasks:**
1. Create `api/aria/db/usage.py` with `UsageRepo`:
   - Store: model, input_tokens, output_tokens, source (conversation/research/extraction), agent_slug, conversation_id, timestamp
   - Aggregate: by time range, by agent, by model, totals
2. Track usage in orchestrator after each LLM call
3. Track usage in memory extraction
4. Add API routes:
   - `GET /usage/summary` — recent usage aggregation
   - `GET /usage/by-agent` — per-agent breakdown
   - `GET /usage/by-model` — per-model breakdown
5. Optional: cost estimation based on provider pricing (configurable rates per model)

---

## Phase 9: Mode & Personality System

**Priority:** High — core to the unified agent vision
**Dependencies:** Phase 8.2 (summarization)

### 9.1 Expand Agent Model to Full Mode System

The current agent model has the right fields but needs richer behavior.

**Tasks:**
1. Add fields to Agent model:
   - `mode_category: str` — "chat", "coding", "research", "creative", "gaming", "admin"
   - `greeting: str` — What Aria says when switching to this mode
   - `icon: str` — Emoji or icon identifier for UI
   - `keyboard_shortcut: str` — Optional hotkey for widget (e.g., "ctrl+1")
   - `memory_weight_tags: list[str]` — Memory categories this mode prioritizes in search
   - `context_instructions: str` — Additional instructions injected when mode is active
   - `voice_config: dict` — TTS voice/speed preferences per mode (future)

2. Create default mode configurations (seed data):

   **Chat Mode** (`chat`):
   - System prompt: Conversational, warm, personal assistant personality
   - Tools: web (for quick lookups)
   - LLM: Local llama.cpp (fast, private for casual chat)
   - Memory: Full extraction enabled
   - Temperature: 0.8

   **Programming Mode** (`coding`):
   - System prompt: Technical, precise, code-focused
   - Tools: filesystem, shell, web, MCP servers
   - LLM: Claude Sonnet (best for code)
   - Fallback: OpenRouter → local
   - Memory: Extract decisions, architecture choices
   - Temperature: 0.3

   **Research Mode** (`research`):
   - System prompt: Analytical, thorough, citation-focused
   - Tools: web, filesystem (for saving reports)
   - LLM: Claude Opus or Sonnet (needs deep reasoning)
   - Memory: Extract learnings, facts, sources
   - Temperature: 0.5

   **Creative Mode** (`creative`):
   - System prompt: Imaginative, exploratory, no-judgment brainstorming
   - Tools: web (for reference), filesystem (for saving drafts)
   - LLM: Local or Claude (temperature-dependent)
   - Memory: Extract ideas, themes, preferences
   - Temperature: 0.9

   **Gaming Mode** (`gaming`):
   - System prompt: Game-savvy, strategic, wiki-knowledgeable
   - Tools: web (for wiki lookups)
   - LLM: Local llama.cpp (fast responses during gameplay)
   - Memory: Extract game preferences, strategies, character builds
   - Temperature: 0.6

   **Admin Mode** (`admin`):
   - System prompt: System administration, automation, DevOps
   - Tools: filesystem, shell
   - LLM: Claude Sonnet
   - Memory: Extract system configs, server details, procedures
   - Temperature: 0.2

3. Create `api/aria/db/seed.py` — Idempotent seed script that creates default modes if they don't exist

### 9.2 Mid-Conversation Mode Switching

Currently agents are per-conversation. Enable switching within a conversation.

**Tasks:**
1. Add `active_agent_id` field to Conversation model (nullable, overrides conversation-level agent)
2. In orchestrator `process_message()`:
   - Detect mode switch commands (natural language: "switch to code mode" or explicit: `/mode coding`)
   - Load new agent config
   - Swap system prompt, tool set, and LLM adapter
   - Inject mode greeting message
   - Continue conversation with full history intact
3. Add `POST /conversations/{id}/switch-mode` route:
   - Body: `{agent_slug: str}`
   - Updates conversation's active_agent_id
   - Returns confirmation with new mode details
4. Update context builder:
   - When mode has `memory_weight_tags`, boost those categories in long-term memory search
   - Inject `context_instructions` from active mode

### 9.3 Auto-Mode Detection (Optional Enhancement)

Let Aria suggest mode switches based on conversation content.

**Tasks:**
1. After each user message, if no explicit mode set:
   - Run lightweight classification (local LLM, single prompt)
   - If confidence > 0.8 and different from current mode, suggest switch
   - Never auto-switch without user confirmation
2. Classification prompt analyzes: topic keywords, tool needs, formality level
3. Suggestion format: "This looks like a coding question — want me to switch to Programming mode?"

---

## Phase 10: Memory Enhancements

**Priority:** High — shared memory is the backbone
**Dependencies:** Phase 8.1 (token counting), Phase 9 (modes)

### 10.1 Memory Categories & Cross-Mode Relevance

**Tasks:**
1. Expand memory content_type enum:
   - Current: fact, preference, event, skill, document
   - Add: decision, architecture, idea, strategy, system_config, person, project, game_info
2. Add `tags: list[str]` field to Memory model (freeform, in addition to categories)
3. Update hybrid search to accept weight modifiers:
   - `category_weights: dict[str, float]` — boost/penalize categories
   - Applied as score multipliers after RRF fusion
4. In context builder, use active mode's `memory_weight_tags` to set category weights
5. All memories remain searchable from all modes — weights adjust relevance, never filter

### 10.2 Memory Lifecycle Management

**Tasks:**
1. Add `status` field values: active, archived, deleted (soft delete already exists)
2. Auto-archive: memories not accessed in 90 days get archived status
3. Archived memories excluded from default search but included with `include_archived=true`
4. Add `confidence` decay: auto-extracted memories lose 0.01 confidence per week until verified
5. Manual verification: `PATCH /memories/{id}` with `verified: true` locks confidence
6. Memory deduplication: before creating a new memory, search for similar (cosine > 0.95) and merge if found

### 10.3 Explicit Memory Commands

Make memory a first-class conversational feature.

**Tasks:**
1. In orchestrator, detect memory commands in user messages:
   - "Remember that..." → extract and store immediately (not background)
   - "Forget about..." → search and soft-delete matching memories
   - "What do you know about..." → search and display relevant memories
   - "What do you remember?" → list recent memories
2. These work identically across all interfaces (web, widget, Signal, CLI)
3. Confirmation messages: "Got it, I'll remember that [summary]" or "I found 3 matching memories and removed them"

### 10.4 Memory Import/Export

**Tasks:**
1. `GET /memories/export` — Export all memories as JSON
2. `POST /memories/import` — Import memories from JSON (with dedup)
3. Support markdown format for human-readable export
4. This enables backup and migration between Aria instances

---

## Phase 11: Research Service (Port from ABP)

**Priority:** Medium-High
**Dependencies:** Phase 8.6 (usage tracking)

### 11.1 Recursive Depth-First Research

Port ABP's research service pattern to Aria.

**Tasks:**
1. Create `api/aria/research/` package:

2. **`service.py` — Research Orchestrator**
   - `start_research(query: str, depth: int = 3, breadth: int = 4, model: str = None) -> str` — returns research_id
   - Runs as background asyncio task
   - Implements recursive pattern from ABP:
     1. LLM generates N sub-queries (temperature 0.7, 1024 tokens)
     2. Each sub-query → web search (Brave or other provider) → 5 results
     3. LLM extracts atomic "learnings" — facts with confidence scores (temperature 0.3, 2048 tokens)
     4. If depth > 0: generate follow-up queries, recurse with depth-1, breadth/2
     5. Final synthesis (temperature 0.5, 4096 tokens) → research report
   - Store each learning as a memory (category: "research", tags from query)
   - Store final report as a memory
   - Track token usage per LLM call

3. **`models.py` — Research Data Models**
   - `ResearchConfig`: query, depth, breadth, search_provider, llm_model
   - `Learning`: content, source_url, confidence, depth_found, query_context
   - `ResearchProgress`: current_depth, max_depth, queries_completed, queries_total, learnings_count
   - `ResearchReport`: query, report_text, learnings, total_tokens, duration

4. **`search.py` — Search Provider Interface**
   - Abstract `SearchProvider` with `search(query, max_results) -> list[SearchResult]`
   - `BraveSearchProvider` implementation (port from ABP's `infra/search/`)
   - `SearchResult`: title, url, snippet

### 11.2 Research API Routes

Add routes at `/api/aria/api/routes/research.py`:

- `POST /research` — Start research task (body: `{query, depth?, breadth?, model?}`)
- `GET /research/{id}` — Get progress and status
- `GET /research/{id}/report` — Get final report
- `GET /research/{id}/learnings` — Get extracted learnings
- `GET /research` — List all research tasks

### 11.3 Research via Conversation

Make research available as a conversational action:

1. User says "Research X deeply" or `/research "X"` in any interface
2. Aria confirms: "Starting deep research on X. I'll notify you when it's done."
3. Background task runs research
4. On completion: notification via Signal (if enabled), and result available in conversation
5. Extracted learnings automatically added to memory for future reference

---

## Phase 12: Coding Agent Orchestration (Port from ABP)

**Priority:** Medium — this is what retires ABP
**Dependencies:** Phase 7 (Signal, for notifications), Phase 8 (reliability)

### 12.1 Subprocess Agent Tool

Add the ability for Aria to spawn and monitor external coding agents as subprocesses.

**Tasks:**
1. Create `api/aria/agents/` package (distinct from the personality "agents"):

2. **`backends/base.py` — Agent Backend Protocol**
   Port ABP's protocol:
   ```python
   class AgentBackend(Protocol):
       def start_command(params: StartParams) -> CommandSpec: ...
       def resume_command(session_id: str, params: StartParams) -> CommandSpec: ...
       def matches_process(cmdline: str) -> bool: ...
   ```

3. **`backends/claude_code.py`** — Claude Code CLI backend
   - Command: `claude --session-id {id} --model {model} --allowedTools {tools} -p "{prompt}"`
   - Resume: `claude --session-id {id} --resume -p "{prompt}"`
   - Environment: ANTHROPIC_API_KEY, CLAUDE_CODE_MAX_TURNS

4. **`backends/codex.py`** — OpenAI Codex CLI backend
   - Command: `codex --sandbox workspace-write --ask-for-approval never -p "{prompt}"`

5. **`backends/registry.py`** — Backend registry
   - Map backend names to implementations
   - Lazy-load backends

6. **`subprocess_mgr.py` — Process Manager**
   Port ABP's subprocess management, but use direct PTY instead of tmux (simpler, cross-platform):
   - Spawn agent subprocess with isolated environment
   - Capture stdout/stderr in ring buffer (last 500 lines)
   - Send input to stdin
   - Track PID for liveness checking
   - Signal management (pause/resume/stop)
   - Optional: tmux integration for users who want it (configurable)

7. **`session.py` — Coding Session Manager**
   - `start_session(workspace: str, backend: str, prompt: str, branch: str = None) -> CodingSession`
   - `stop_session(session_id: str)`
   - `get_output(session_id: str, lines: int = 50) -> str`
   - `send_input(session_id: str, text: str)`
   - `list_sessions() -> list[CodingSession]`
   - Git worktree creation (optional, configurable)

### 12.2 Coding Session Tools

Register as Aria tools so the orchestrator can use them:

- `start_coding_session` — Spawn a coding agent on a workspace
- `stop_coding_session` — Stop a running session
- `get_coding_output` — Get recent output from a session
- `send_to_coding_session` — Send text/commands to a session
- `list_coding_sessions` — List all active sessions
- `get_coding_diff` — Get git diff from session's worktree

### 12.3 Watchdog (Port from ABP)

Background monitoring of coding sessions.

**Tasks:**
1. Create `api/aria/agents/watchdog.py`:
   - Poll session output every 5 seconds (when sessions active), 60 seconds (when idle)
   - MD5 hash output for change detection
   - Stall detection: 60 seconds unchanged → emit STALLED notification
   - Interactive prompt detection with two-tier pattern matching:
     - SAFE patterns (3s): file reads, directory listings, permission prompts
     - NORMAL patterns (10s): yes/no confirmations, proceed/continue
   - Auto-respond to detected prompts (configurable, off by default)
   - Deadline management: `set_deadline(session_id, minutes)` auto-stops session

2. **Notifications on events:**
   - STALLED → Signal notification with last 3 output lines
   - COMPLETED → Signal notification with summary
   - ERROR → Signal notification with error context
   - Cooldown: 60s per (session_id, event_type)

### 12.4 Session Review (Port from ABP)

Automated review of coding session output.

**Tasks:**
1. Create `api/aria/agents/review.py`:
   - `review_session(session_id: str) -> SessionReport`
   - Gather diff stats (git diff --numstat)
   - Run tests (detect pytest/npm test/etc.)
   - Run linting (detect ruff/eslint/etc.)
   - Parse test results
   - LLM summary of changes (200 tokens)
   - Determine status: success / partial / failed
2. Store reports in MongoDB `session_reports` collection
3. Add API routes for report retrieval

### 12.5 Coding Mode Integration

When Aria is in Programming mode and the user describes a coding task:

1. Aria can decide to spawn a coding agent (tool call)
2. Monitor progress via watchdog
3. Report back to user when done (via current conversation or Signal)
4. User can ask "how's the coding going?" and Aria checks session output
5. On completion, Aria reviews the session and summarizes changes

---

## Phase 13: Background Task System

**Priority:** Medium
**Dependencies:** Phase 7 (Signal notifications)

### 13.1 Task Runner

A general-purpose background task system for long-running operations.

**Tasks:**
1. Create `api/aria/tasks/` package:

2. **`runner.py` — Task Runner**
   - `submit_task(name: str, coroutine: Coroutine, notify: bool = True) -> str` — returns task_id
   - Run coroutine as background asyncio task
   - Track: status (pending/running/completed/failed), progress (0-100), result, error
   - On completion/failure: trigger notification if `notify=True`
   - Timeout enforcement (configurable per task, default 30 minutes)
   - Cancellation support

3. **`models.py` — Task Models**
   - `BackgroundTask`: id, name, status, progress, result, error, created_at, completed_at
   - Stored in MongoDB `background_tasks` collection

4. **API Routes:**
   - `GET /tasks` — List background tasks (filterable by status)
   - `GET /tasks/{id}` — Get task status and result
   - `POST /tasks/{id}/cancel` — Cancel running task

### 13.2 Integration Points

Background tasks used by:
- Research service (Phase 11) — long-running research
- Coding session monitoring (Phase 12) — watchdog loop
- Memory extraction — already background, formalize with task runner
- Future: scheduled tasks, automation workflows

---

## Phase 14: Scheduled Tasks & Reminders

**Priority:** Medium
**Dependencies:** Phase 13 (task runner), Phase 7 (Signal)

### 14.1 Scheduler

**Tasks:**
1. Create `api/aria/scheduler/` package:

2. **`service.py` — Scheduler Service**
   - Cron-like scheduling using `asyncio` and a tick loop
   - `schedule(name, cron_expr, action, notify)` — register recurring task
   - `remind(message, when: datetime, channel: str)` — one-shot reminder
   - Actions: run a prompt through orchestrator, execute a tool, send a notification
   - Persistence: store schedules in MongoDB, reload on startup

3. **Conversational interface:**
   - "Remind me to X at Y" → parse time, create reminder
   - "Every morning, check X" → create recurring schedule
   - "Cancel my reminder about X" → search and remove
   - Works via all interfaces (chat, widget, Signal)

4. **API Routes:**
   - `GET /schedules` — List all schedules
   - `POST /schedules` — Create schedule or reminder
   - `DELETE /schedules/{id}` — Remove schedule

---

## Phase 15: Desktop Widget Enhancements

**Priority:** Medium
**Dependencies:** Phase 9 (modes)

### 15.1 Mode Switcher

**Tasks:**
1. Add mode selector dropdown/tabs to widget UI
2. Show current mode indicator (icon + name) in titlebar
3. Keyboard shortcuts for mode switching (Ctrl+1 through Ctrl+6)
4. Mode switch sends `/mode` command to API

### 15.2 Voice Input

**Tasks:**
1. Wire STT service into widget
2. Hold-to-speak button (or push-to-talk hotkey)
3. Audio captured via Tauri's audio API
4. Sent to `POST /stt/transcribe`
5. Transcribed text inserted as user message
6. Optional: TTS playback of responses (toggle)

### 15.3 Quick Actions

**Tasks:**
1. Add action buttons for common operations:
   - Start/stop coding session
   - Check coding session status
   - Run research query
   - View recent memories
2. Accessible via right-click context menu or toolbar

### 15.4 Cross-Platform Builds

**Tasks:**
1. Set up CI/CD for Tauri builds:
   - Linux (AppImage, deb) — current
   - Windows (MSI, exe) — add GitHub Actions runner
   - macOS (DMG, app) — add GitHub Actions runner
2. Auto-update mechanism via Tauri's updater plugin
3. Platform-specific hotkey configuration (Ctrl+Space on Windows/Linux, Cmd+Space on macOS — but check for Spotlight conflict)

---

## Phase 16: Web UI Completion

**Priority:** Medium
**Dependencies:** Phase 9 (modes), Phase 10 (memory), Phase 11 (research)

### 16.1 Agent/Mode Management UI

**Tasks:**
1. Mode selector in chat sidebar
2. Mode configuration page (edit system prompts, tools, LLM settings)
3. Mode creation wizard
4. Visual indicator of active mode in chat

### 16.2 Memory Browser

**Tasks:**
1. Searchable memory list with filters (category, tags, date range, confidence)
2. Memory detail view with edit/delete
3. Memory creation form
4. Visualization of memory access patterns (most used, recently created)

### 16.3 Research Dashboard

**Tasks:**
1. Research task list with status indicators
2. Progress visualization (depth/breadth tree)
3. Learnings browser
4. Report viewer with source links

### 16.4 Usage & Cost Dashboard

**Tasks:**
1. Token usage charts (by model, by agent, over time)
2. Cost estimates per conversation
3. Model usage breakdown
4. Budget alerts (configurable thresholds)

### 16.5 Settings Panel

**Tasks:**
1. LLM provider configuration (API keys, default models)
2. Signal settings
3. Memory settings (extraction frequency, auto-archive threshold)
4. Tool management (enable/disable, MCP server configuration)
5. Notification preferences

### 16.6 Conversation Management

**Tasks:**
1. Conversation search (full-text across all messages)
2. Conversation export (markdown, JSON)
3. Conversation archiving
4. Pin/favorite conversations
5. Conversation tags

---

## Phase 17: CLI Enhancements

**Priority:** Low-Medium
**Dependencies:** Phase 9, Phase 11, Phase 12

### 17.1 Full CLI Coverage

**Tasks:**
1. Memory commands: `aria memory search`, `aria memory list`, `aria memory store`, `aria memory delete`
2. Mode commands: `aria mode list`, `aria mode switch <slug>`, `aria mode create`
3. Research commands: `aria research start "<query>"`, `aria research status`, `aria research report <id>`
4. Coding commands: `aria code start <workspace>`, `aria code status`, `aria code stop`
5. Schedule commands: `aria remind "<msg>" --at "<time>"`, `aria schedule list`
6. Usage commands: `aria usage summary`, `aria usage by-model`

### 17.2 Interactive Chat Mode

**Tasks:**
1. `aria chat` enters interactive REPL with Rich formatting
2. Mode switching via `/mode` command within chat
3. Streaming responses displayed in real-time
4. History navigation (up/down arrows)
5. Tab completion for commands

---

## Phase 18: Playbook/Workflow System (Port from ABP)

**Priority:** Low-Medium
**Dependencies:** Phase 12 (coding sessions), Phase 13 (task runner)

### 18.1 Workflow Engine

Port ABP's playbook concept but generalize beyond coding.

**Tasks:**
1. Create `api/aria/workflows/` package:

2. **`models.py` — Workflow Models**
   ```python
   WorkflowStep:
     action: str  # "prompt", "tool", "research", "code_session", "notify", "wait", "condition"
     params: dict
     depends_on: list[int]  # step indices

   Workflow:
     name: str
     description: str
     steps: list[WorkflowStep]
     tags: list[str]
   ```

3. **`engine.py` — Workflow Engine**
   - `run_workflow(workflow_id, dry_run=False)`
   - Execute steps respecting dependencies
   - Collect outputs from each step for downstream steps
   - Support conditions (if previous step output matches pattern)
   - Timeout per step and per workflow

4. **Action Types:**
   - `prompt` — Send a message to Aria and get a response
   - `tool` — Execute a specific tool with given arguments
   - `research` — Start a research task and wait for completion
   - `code_session` — Start a coding session with given prompt
   - `notify` — Send a notification via Signal/desktop
   - `wait` — Wait for specified duration or condition
   - `condition` — Branch based on previous step output

5. **API Routes:**
   - `GET /workflows` — List workflows
   - `POST /workflows` — Create workflow
   - `POST /workflows/{id}/run` — Execute workflow
   - `GET /workflows/{id}/status` — Execution status

### 18.2 Conversational Workflow Creation

Let users create workflows through conversation:
- "Every time I start a new project, I want you to: create a git repo, set up the standard structure, and create a README"
- Aria extracts steps, creates a workflow, and confirms
- Stored as a named workflow for future use

---

## Phase 19: Security Hardening

**Priority:** Low-Medium (but important before remote access)
**Dependencies:** None

### 19.1 Tool Sandboxing

**Tasks:**
1. Shell tool: add resource limits (CPU time, memory, disk I/O) via `resource` module or cgroups
2. Shell tool: network restrictions via seccomp or namespace isolation
3. Filesystem tool: expand denied paths to include `/etc`, `/sys`, `/proc`, sensitive directories
4. Filesystem tool: add file size limits for reads and writes
5. MCP tools: timeout enforcement, output size limits
6. All tools: audit logging (who called what, when, with what arguments)

### 19.2 API Authentication

**Tasks:**
1. Add optional API key authentication (`X-API-Key` header)
2. Configurable: disabled for local-only use, required for remote access
3. Rate limiting per API key
4. Add to config: `API_KEY`, `API_AUTH_ENABLED`

### 19.3 Remote Access Preparation

**Tasks:**
1. TLS termination (reverse proxy or built-in)
2. IP allowlisting option
3. Audit log of all API calls
4. Configurable CORS origins (currently hardcoded)

---

## Phase 20: Retire AgentBenchPlatform

**Priority:** Happens naturally after Phase 12
**Dependencies:** Phase 12 (coding orchestration at parity)

### 20.1 Feature Parity Checklist

Before retiring ABP, verify Aria can:
- [ ] Start/stop/monitor coding sessions (Claude Code, Codex)
- [ ] Detect stalls and auto-respond to interactive prompts
- [ ] Send notifications via Signal when sessions complete/fail
- [ ] Review sessions (run tests, lint, generate report)
- [ ] Run deep research and store learnings
- [ ] Track token usage and costs
- [ ] Manage git worktrees for session isolation

### 20.2 Data Migration

**Tasks:**
1. Export ABP memories to Aria format
2. Export ABP usage data
3. Map ABP tasks to Aria workflow templates (if applicable)
4. Verify shared MongoDB can be used or data needs copying

### 20.3 Cutover

1. Run both systems in parallel for 1 week
2. Verify all ABP use cases work through Aria
3. Disable ABP systemd service
4. Archive ABP repository

---

## Implementation Order Summary

| Order | Phase | Description | Blocks |
|-------|-------|-------------|--------|
| 1 | **Phase 8** | Hardening & Reliability | Everything |
| 2 | **Phase 7** | Signal Integration | Mobile access |
| 3 | **Phase 9** | Mode & Personality System | Core experience |
| 4 | **Phase 10** | Memory Enhancements | Cross-mode intelligence |
| 5 | **Phase 11** | Research Service | Deep research capability |
| 6 | **Phase 12** | Coding Agent Orchestration | ABP retirement |
| 7 | **Phase 13** | Background Task System | Long-running operations |
| 8 | **Phase 14** | Scheduled Tasks | Proactive agent behavior |
| 9 | **Phase 15** | Widget Enhancements | Desktop experience |
| 10 | **Phase 16** | Web UI Completion | Full management interface |
| 11 | **Phase 17** | CLI Enhancements | Terminal power users |
| 12 | **Phase 18** | Workflow System | Automation |
| 13 | **Phase 19** | Security Hardening | Remote access prep |
| 14 | **Phase 20** | Retire ABP | Consolidation |

---

## Architecture After Completion

```
┌─────────────────────────────────────────────────────────────────┐
│                        ARIA CLIENTS                              │
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │  Signal   │  │  Tauri   │  │  Web UI  │  │   CLI    │       │
│  │ (Phone)   │  │ (Desktop)│  │ (Browser)│  │(Terminal)│       │
│  └─────┬────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘       │
│        │            │              │              │              │
│        └────────────┴──────┬───────┴──────────────┘              │
│                            │                                     │
│                     HTTP / SSE API                               │
├────────────────────────────┼─────────────────────────────────────┤
│                            │                                     │
│                    ┌───────┴───────┐                             │
│                    │  FastAPI App  │                             │
│                    └───────┬───────┘                             │
│                            │                                     │
│  ┌─────────────────────────┼──────────────────────────┐         │
│  │              ORCHESTRATOR LAYER                     │         │
│  │                                                     │         │
│  │  ┌────────────┐  ┌──────────┐  ┌────────────────┐ │         │
│  │  │  Context   │  │   Mode   │  │   Fallback     │ │         │
│  │  │  Builder   │  │ Switcher │  │   Chain        │ │         │
│  │  └────────────┘  └──────────┘  └────────────────┘ │         │
│  └─────────────────────────┼──────────────────────────┘         │
│                            │                                     │
│  ┌─────────┬───────────────┼───────────────┬─────────┐         │
│  │         │               │               │         │         │
│  │  ┌──────┴──────┐ ┌─────┴──────┐ ┌──────┴──────┐ │         │
│  │  │   Memory    │ │   Tools    │ │  Research   │ │         │
│  │  │  (2-tier)   │ │ (MCP+built)│ │  (recursive)│ │         │
│  │  └─────────────┘ └────────────┘ └─────────────┘ │         │
│  │                                                   │         │
│  │  ┌─────────────┐ ┌────────────┐ ┌─────────────┐ │         │
│  │  │  Coding     │ │ Scheduler  │ │ Workflows   │ │         │
│  │  │  Agents     │ │ & Remind   │ │ & Playbooks │ │         │
│  │  └──────┬──────┘ └────────────┘ └─────────────┘ │         │
│  │         │                                         │         │
│  │  ┌──────┴──────┐                                 │         │
│  │  │  Watchdog   │                                 │         │
│  │  │  & Monitor  │                                 │         │
│  │  └─────────────┘                                 │         │
│  │                                                   │         │
│  │  ┌─────────────┐ ┌────────────┐ ┌─────────────┐ │         │
│  │  │Notifications│ │  Usage &   │ │  Background │ │         │
│  │  │  (Signal+)  │ │  Costs     │ │  Tasks      │ │         │
│  │  └─────────────┘ └────────────┘ └─────────────┘ │         │
│  └───────────────────────────────────────────────────┘         │
│                            │                                     │
│  ┌─────────────────────────┼──────────────────────────┐         │
│  │              INFRASTRUCTURE                         │         │
│  │                                                     │         │
│  │  ┌──────┐ ┌─────────┐ ┌────────┐ ┌──────┐ ┌────┐ │         │
│  │  │Mongo │ │llama.cpp│ │Anthropic│ │OpenAI│ │ OR │ │         │
│  │  │ DB   │ │ (ROCm)  │ │  API   │ │ API  │ │    │ │         │
│  │  └──────┘ └─────────┘ └────────┘ └──────┘ └────┘ │         │
│  │                                                     │         │
│  │  ┌──────┐ ┌─────────┐ ┌────────┐ ┌──────────────┐ │         │
│  │  │Voyage│ │signal-cli│ │ Brave  │ │  Coding CLI  │ │         │
│  │  │Embed │ │ daemon  │ │ Search │ │  Subprocesses│ │         │
│  │  └──────┘ └─────────┘ └────────┘ └──────────────┘ │         │
│  └───────────────────────────────────────────────────┘         │
│                                                                  │
│                     ARIA SERVER                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

*Last updated: 2026-03-14*
