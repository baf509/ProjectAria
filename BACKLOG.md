# ARIA — Backlog

Single backlog: product vision + open research questions; shipped items live in CHANGELOG.

**Consolidated:** 2026-07-04

---

ARIA already has *capability* and, increasingly, *interiority*. The Dream Cycle
(`api/aria/dreams/`) gives it a mechanism for reflection and growth — a nightly
process that consolidates memories, finds connections, and journals what it
learned — and Ambient Awareness (`api/aria/awareness/`) gives it passive
situational context. What remains below are the systems that would deepen that
interiority (Part A) and the open questions that still need investigation before
they're ready to build (Part B).

The unifying theme of Part A: make ARIA **agentic in its own development**, not
just in task execution. The Dream Cycle planted the keystone; these extend it.

---

# Part A — Product Vision

Aspirational new paradigms. These are new systems, not enhancements — they give
ARIA a richer inner model of the user and of itself.

## Personal Knowledge Graph

Today memories are flat documents. Real understanding is **relational** — you
don't just know facts, you know how things connect.

**What it would track:**
- **Entities**: people, projects, technologies, companies, concepts — extracted
  from conversations.
- **Relationships**: "Ben works on ProjectAria", "ProjectAria uses MongoDB",
  "Ben prefers local-first architectures".
- **Temporal edges**: when a relationship was first observed, last confirmed, how
  it changed.
- **Strength/confidence**: relationships strengthen with more evidence.

**What it enables:**
- Traversal queries: "What do I know about Ben's relationship with cloud
  services?"
- Serendipity: non-obvious paths between entities.
- Better memory search: graph-based relevance, not just semantic similarity.
- Richer context building: pull in related entities, not just matching text.

**Implementation shape:** Either a new `knowledge_graph` collection
(`{entity_a, relationship, entity_b, evidence, timestamps}`) or a new
`content_type: "relationship"` in the existing memory system with entity
extraction during the memory pipeline. Add a `GraphService` to traverse and
query. (See also "Knowledge Base & Document Ingestion" in Part B — GraphRAG is
the overlap between the two.)

## Mood & Energy Model (Bilateral)

ARIA should sense *how the user is doing* — and how it is itself "doing" — and
adjust, not from sentiment-keyword analysis but from behavioral patterns.

**User model:**
- Message length trending shorter → hurried or frustrated.
- Lots of corrections/rephrasing → struggling to articulate, needs patience.
- Long gaps between messages → deep work or distracted.
- Late-night vs. morning → different modes.
- Rapid-fire questions → exploring/brainstorming.

**ARIA's own "energy":**
- After a complex multi-tool autonomous task → "heavier", more careful.
- After a run of simple questions → lighter, more casual.
- After errors → more cautious, double-checking.

**What it changes:** Response length and style adapt without being told. The soul
file's personality becomes dynamic rather than static. (Open questions on the
research side of this — sentiment reliability, avoiding a patronizing/manipulative
feel, boundary awareness — are tracked under "Emotional Intelligence" in Part B.)

**Implementation shape:** New `api/aria/core/mood.py` with a `MoodModel` class
maintaining a lightweight state vector updated per message (factors:
message_length_trend, response_time_gaps, error_rate, time_of_day,
session_duration). Output: a mood context dict injected into the system prompt
alongside SOUL.md. Start simple — let the user tell ARIA how they're feeling and
remember it — before inferring.

## Anticipatory Preparation

The best assistants don't wait to be asked — they have things ready.

**What it would do:**
- Always check deployment status after merging a PR → pre-fetch it when a merge
  is detected.
- Typically start Monday with "what was I working on?" → have a briefing ready.
- A scheduled task about to run → pre-warm the context.
- Been researching a topic across conversations → prepare a synthesis.

**How it learns patterns:**
- Track sequences: "after event X, the user usually asks Y within Z minutes".
- Build a simple Markov-like model of conversation starters and their triggers.
- Use the Dream Cycle to identify preparation opportunities.

**Implementation shape:** New `api/aria/anticipation/` module. A `PatternTracker`
correlating conversation openings with recent events (time of day, git activity,
prior topics), and an `Anticipator` that pre-computes likely-needed context into
a `prepared_contexts` collection with short TTLs. Note: the heartbeat, awareness
triggers, autopilot, and scheduler already cover *proactive* behavior broadly —
this item is specifically the **learned, pattern-anticipated preparation** layer
on top of them, not generic proactivity.

## Ritual Engine (Rhythm Awareness)

Not scheduled tasks — *learned* rituals. A scheduled task fires at a fixed time;
a ritual fires when the *pattern* says it should.

**Examples:**
- Always start coding after checking Slack → offer a "ready to code?" transition.
- End of day you usually ask for a summary → offer one at the detected
  end-of-day time.
- Friday afternoons you reflect on the week → surface weekly patterns.

**How it differs from the scheduler:**
- Rituals are **discovered**, not configured.
- They adapt as patterns change.
- They can be "soft" (suggested) rather than mandatory.
- They're tied to behavioral triggers, not clock time.

**Implementation shape:** Part of the Anticipation module. A `RitualDetector`
analyzing conversation patterns by time-of-day, day-of-week, and
preceding-activity; when a ritual is detected with high confidence it registers a
soft trigger that *proposes* (not forces) the action.

## Curiosity Engine

ARIA should have things it *wants to know* — genuine curiosity driven by
knowledge gaps, which is also what makes an identity feel coherent and real.

**How it works:**
- The Knowledge Graph reveals sparse areas ("I know a lot about Ben's Python work
  but almost nothing about his frontend approach").
- The Dream Cycle identifies questions it wishes it could answer.
- During natural conversation, when relevant, ARIA asks — as genuine interest,
  not a survey. "You mentioned you're not a fan of React but you're using Next.js
  for the UI — what drew you to that?"

**Why it matters:** The soul file says "have opinions"; this gives ARIA the
mechanism to *develop* opinions through inquiry, and reinforces a single coherent
identity across modes and interfaces (a shared identity preamble, consistent
self-reference, cross-mode continuity). It depends on the Knowledge Graph (to
find gaps) and the Dream Cycle (to form questions).

### Dependency Graph (remaining vision items)

```
Ambient Awareness (shipped) ─┐
                             ├──→ Dream Cycle (shipped) ──→ Soul File Updates
Knowledge Graph ─────────────┤
                             │
Mood Model ──────────────────┤
                             │
                             └──→ Anticipatory Preparation
                                        │
                                        ▼
                                  Ritual Engine ←── Curiosity Engine
```

The Dream Cycle and Ambient Awareness (both shipped) are the substrate the rest
build on.

---

# Part B — Research Questions

Open investigations. These need exploration before committing to implementation.

## Local Model Strategy

**Question:** As local models improve rapidly, what's the right strategy for
mapping modes/agents to local vs. cloud models?

**Areas to explore:**
- Benchmark local models (Qwen 3.6, Llama 4, Mistral, DeepSeek) specifically on
  ARIA's use cases: casual chat, code generation, research synthesis, creative
  writing.
- Latency vs. quality tradeoffs on the ROCm GPU box.
- Can a small local model reliably handle mode classification / memory extraction
  to save cloud spend on background tasks?
- Speculative decoding for faster local inference.
- Multi-model inference: small model drafting + large model review simultaneously.
- Context-length limits: is 8K–32K local enough per mode, or do some modes need
  128K+ cloud?

**Why it matters:** If local models handle 80% of interactions, ARIA becomes
nearly free to run and fully private; cloud becomes the exception.

## Voice Interface Design (VAD / Wake-word)

**Question:** Beyond basic STT→LLM→TTS, what makes voice interaction actually
useful?

**Areas to explore:**
- Voice activity detection (VAD): start/stop listening without push-to-talk.
- Interruption handling: user cuts ARIA off mid-response — how does conversation
  state react?
- Ambient/wake-word mode: passive listening, respond only when addressed ("Hey
  ARIA").
- Voice personality: different TTS voices per mode? Speed/tone adjustments?
- Multimodal input: voice + screen context (what's on screen when the user
  speaks).
- Latency budget: likely <2s end-to-end acceptable.
- Whisper vs. faster alternatives (Moonshine, Canary) for real-time transcription.
- Streaming TTS: start speaking before the full response is generated.
- Qwen3-TTS quality vs. Kokoro / Piper / Coqui.
- Phone-call interface via SIP/VoIP — useful or gimmicky?

**Why it matters:** Voice is the most natural interface for many contexts
(driving, cooking, gaming). But bad voice UX is worse than none.

## Computer Use & Screen Context

**Question:** Beyond CLI tools and the Playwright MCP browser family, how deeply
should ARIA see and interact with the user's screen? This is the research
counterpart to the (shipped) Ambient Awareness sensors — the sensors watch git /
filesystem / system / sessions; open here is *visual* screen understanding.

**Areas to explore:**
- Screenshot analysis: periodic screenshots → vision model → context on what the
  user is doing.
- Active-window detection: know which app is focused, adjust mode automatically.
- GUI automation beyond Playwright: platform accessibility APIs (AT-SPI on Linux,
  UIAutomation, macOS Accessibility).
- Privacy: what should ARIA see vs. not (banking, passwords, private messages)?
  Opt-in regions the user defines.
- Gaming integration: see the game screen and give real-time advice? Overlay HUD?
- Cross-platform: X11 vs. Wayland vs. Windows vs. macOS screenshot/automation
  APIs differ.
- Performance: continuous capture + vision inference cost.

**Why it matters:** Screen context is the richest signal about what the user is
doing — but invasive, expensive, and technically hard across platforms.

## Emotional Intelligence & Relationship Building

**Question:** This is the research side of the Mood & Energy Model (Part A):
before ARIA infers and adapts to emotional state, what actually works and what
backfires?

**Areas to explore:**
- Sentiment analysis reliability: detecting frustration, excitement, fatigue
  without false positives.
- Adaptive tone: encouraging when frustrated, concise when busy — how much before
  it feels patronizing?
- Long-term relationship patterns: does the user prefer directness or warmth?
  Does it vary by time of day?
- Boundary awareness: when to offer support vs. stay professional.
- Risk: getting this wrong feels manipulative or patronizing — worse than none.
- Lightweight implementation: an "emotional context" memory category with
  sentiment tags; start by simply letting the user say how they feel and
  remembering it.

## Knowledge Base & Document Ingestion

**Question:** Is the current conversational-memory system enough, or does ARIA
need a proper RAG pipeline for documents?

**Areas to explore:**
- Use cases: personal notes, project docs, research papers, bookmarks, saved
  articles.
- Ingestion formats: PDF, markdown, web pages, emails, code repositories.
- Chunking strategies: fixed-size vs. semantic vs. hierarchical.
- Is MongoDB vector search sufficient at scale (10K+ docs, 100K+ chunks)?
- Alternative: memories for facts + tool calls for on-demand document access.
- Hybrid: index metadata + summaries in memory, full content via tools.
- Does LLM-based extraction from documents scale (cost, accuracy, hallucination)?
- GraphRAG / knowledge graphs for entity relationships — **this is the same
  substrate as the Personal Knowledge Graph in Part A**; design them together.
- Integration with an Obsidian vault, Notion export, or browser bookmarks.

**Why it matters:** The user has knowledge in files ARIA can't reach unless
asked. Open question: is proactive indexing worth the cost vs. on-demand reads?

## Privacy & Data Sovereignty

**Question:** How to balance usefulness (ARIA knows everything) with privacy (that
data is sensitive)? Note ARIA currently runs on a closed Tailscale tailnet, not
internet-exposed — calibrate accordingly, but the accumulation risk still stands.

**Areas to explore:**
- Memory sensitivity levels: some memories (health, finances, relationships)
  should never leave the device.
- Per-mode privacy rules: chat memories stay local, coding may use cloud.
- Encryption at rest for the MongoDB data.
- Data minimization: the minimum ARIA needs to remember to stay useful.
- Cloud-provider trust: which memories to exclude when sending context to
  Claude/OpenAI/Fireworks.
- Memory redaction: auto-detect and redact secrets (API keys, passwords, SSNs)
  before cloud calls.
- User audit + right to forget: scoped deletion ("forget everything about X").
  (A `/forget` command already exists — this extends it toward automatic
  sensitivity classification.)

**Why it matters:** ARIA accumulates deeply personal information; a breach or
misconfiguration shouldn't be catastrophic.

## Plugin / Extension System

**Question:** MCP provides tool extensibility — is a broader plugin architecture
worth it?

**Areas to explore:**
- Plugin types beyond tools: modes/personalities, memory extractors, notification
  channels, LLM providers, UI components.
- Distribution: package registry vs. git repos.
- Sandboxing: plugins run in ARIA's process — how to contain malicious ones?
- Configuration: per-plugin settings, enable/disable, priority ordering.
- Alternative: just MCP for tools + manual agent config for everything else.
  Don't over-engineer.

**Why it matters:** Extensibility is good, but plugin systems are hard to get
right and maintain. MCP might already be enough.

## Multi-Device Synchronization

**Question:** If the user is chatting on Signal and opens the desktop widget, what
happens?

**Areas to explore:**
- Conversation continuity: same conversation from all devices?
- Active-device tracking and notification routing to the device in use.
- Conflict resolution: messages sent from two devices at once.
- Context awareness: "on my phone" vs. "at my desk" changing behavior.
- State sync: conversation list, mode, settings.
- Offline support for the widget (probably not worth the complexity).

**Why it matters:** The unified-agent vision wants a seamless cross-device
experience, but true sync is an engineering quagmire — find the right level of
simplicity.

## Collaborative / Multi-Agent Patterns

**Question:** Beyond spawning coding agents, should ARIA support general
multi-agent workflows?

**Areas to explore:**
- Agent-to-agent communication: delegate sub-tasks to specialists and synthesize.
- Parallel agents on different aspects of one problem.
- Specialized cloud models as agents (Claude for code, others for analysis).
- Consensus: multiple agents review the same work and vote.
- ABP's coordinator pattern (one meta-agent monitoring workers) — does it
  generalize beyond coding?
- Cost: multi-agent workflows multiply API spend.

**Why it matters:** Multi-agent patterns solve problems single agents can't, but
they're expensive and complex — build only where ROI is clear.

## Integration Ecosystem

**Question:** Beyond Signal and coding tools, which external integrations are
worth the maintenance burden?

**Candidates to investigate:**
- **Calendar / Email / Drive**: Google Calendar, Gmail, and Google Drive are
  already reachable via MCP — the open question is whether native, deeper
  integration (proactive triage, scheduling, bidirectional sync) beats the
  on-demand MCP path.
- **GitHub/GitLab**: PR reviews, issue triage, notification filtering.
- **Note-taking** (Obsidian, Notion): bidirectional sync with memory.
- **Music** (Spotify, local): mood-based recommendations, playback control.
- **Smart home** (Home Assistant): light/temperature control, automation triggers.
- **Task management** (Todoist, Linear): task tracking, project management.
- **Weather/Location**: context-aware suggestions.

**Decision framework:** For each — (1) how often used? (2) doable via existing
web/shell/MCP tools without dedicated integration? (3) build/maintain cost? (4)
credential exposure? Pick the 2–3 highest-value.

## Performance & Scaling

**Question:** After a year of use — thousands of memories, hundreds of
conversations — will ARIA still be fast?

**Areas to explore:**
- MongoDB vector-search performance at 10K / 50K / 100K memories.
- Retrieval latency as the corpus grows — is hybrid search still fast?
- Conversation loading with long histories (1000+ messages).
- Embedding throughput for batch operations.
- Background-task concurrency limits.
- FastAPI process memory over time (leaks?).
- Index optimization for common query patterns; archival to cold storage.
- Establish baseline benchmarks.

**Why it matters:** A personal agent that slows over time gets abandoned.

## Testing Strategy for an AI Agent

**Question:** How do you test a system whose core behavior is non-deterministic?

**Areas to explore:**
- LLM-as-judge evaluation of ARIA's responses.
- Golden conversation sets with expected behaviors.
- Memory-extraction accuracy: given a conversation, are the right memories
  produced?
- Mode-switching correctness: right system prompt / tool set activated.
- Tool-call validation: right tools, right arguments.
- Regression testing: detect when a model upgrade degrades behavior.
- Full-stack integration tests (HTTP → LLM → response).
- Cost of testing: each run burns tokens — mock LLM responses for deterministic
  orchestrator tests.

**Why it matters:** Without testing, every change is a gamble; testing AI systems
differs fundamentally from testing traditional software.

## ARIA Identity & Continuity

**Question:** What makes ARIA feel like "one agent" across modes and interfaces?
(Closely tied to the Curiosity Engine in Part A, which is one mechanism for a
coherent, opinionated identity.)

**Areas to explore:**
- Core personality traits that persist across modes.
- Consistent name/self-reference and pronouns.
- Memory continuity across mode switches, with acknowledgement of the switch.
- Cross-mode references ("the architecture decision from our research session").
- Identity vs. role: one agent playing roles, not multiple chatbots.
- A shared "identity preamble" prepended to all mode-specific system prompts.

**Why it matters:** The unified-agent vision requires ARIA to feel like one
entity, not a collection of disconnected chatbots.

---

## Type the API boundary (Literal/Enum on enum-like fields)

**Why this is here:** on 2026-07-27 a one-word typo — `backend="pi"` instead of
`"pi-code"` — cost a debugging round-trip and silently gave the user a Claude
agent when they asked for a pi agent. An audit for that *shape* then found the
same defect in seven more places. Every one is a consequence of a single
structural gap.

**The gap.** `api/aria/db/models.py` has **zero** `Literal`/`Enum` types for any
`backend`, `status`, `type`, or `kind` field — all bare `str`. Same for
`MemoryCreate.content_type` and the shells `status`/`kinds` query params. With
no boundary type, **the docstring becomes the only schema**, and nothing forces
it to match the dispatch site. `api/aria/planning/models.py` is the one module
that uses Literals — and the one module the audit found no defects in.

**The work:**
- Add `Literal` types to the enum-like fields in `db/models.py`, mirroring the
  dispatch sites (`agents/backends/registry.py`, `llm/manager.py`,
  `shells/models.py`, session status writes in `agents/session.py`).
- Validate the shells `status` / `kinds` query params — today an unknown value
  returns `[]` with **200**, which reads as "no results" rather than "bad input".
- Give `MemoryCreate.content_type` a real type: it is a *comment*, not a
  Literal, so `content_type="note"` is stored with 201 and no documented filter
  ever retrieves it — silently, forever.
- Then make the MCP docstrings generate from (or be tested against) those types,
  so the two cannot drift again. A test asserting
  "every value named in an MCP docstring is accepted by its dispatch" would have
  caught all eight findings.

**Already fixed piecemeal** (2026-07-27, commits `0484bbb`, `60056f5`,
`3a8fc1f`): backend aliases + a 400 naming valid values; `RuntimeError` → 409;
`error`/`result_summary` exposed on coding sessions; `WorkflowStepRequest.action`
typed as a Literal of all ten actions; memory-search dependency failures → 503;
two wrong MCP docstring values. Those are the symptoms. This item is the cause.

**Known remaining instances** (from the audit, not yet fixed): two different
`backend` vocabularies documented in one `create_workflow` docstring with
neither stated; `coding.py` advertising an `llm` value with no key configured
and never validating `llm` at spawn (so the API returns 201 and the session dies
asynchronously); `/shells` status/kinds accepting anything; several silent
fallbacks in `agents/session.py` and `core/orchestrator.py` that substitute a
different backend/model without telling the caller.

---

## Recently shipped (moved out of the backlog)

These were on the vision/investigation lists and are now implemented — see
`CHANGELOG.md` for details:

- **Dream Cycle** (offline reflection + journal) → `api/aria/dreams/`.
- **Ambient Awareness** (git / filesystem / system / session sensors + triggers)
  → `api/aria/awareness/`. Subsumes the old "Self-Evolution Journal" idea (the
  Dream Cycle now writes the journal).
- **Proactive Agent Behavior** → heartbeat (`api/aria/heartbeat/`), awareness
  triggers, autopilot (`api/aria/autopilot/`), and the scheduler
  (`api/aria/scheduler/`). Only the *learned/anticipatory* slice remains open,
  tracked as "Anticipatory Preparation" and "Ritual Engine" in Part A.
- **Signal integration** → `api/aria/signal/`. (Mobile-interface investigations
  are settled around Signal + the PWA; other bots — including Telegram, now
  removed as dead code — are out of scope.)

---

*Add new items as they arise. Move items to the implementation plan / CHANGELOG
when they ship.*
