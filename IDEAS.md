# ARIA — New Functionality Ideas

These are ideas for genuinely new systems — not enhancements to existing features, but new paradigms that would give ARIA interiority, growth, and genuine agency over its own development.

ARIA currently has *capability* but not *interiority*. It can do things when asked, remember things it's told, and check a list on a timer. But it doesn't **reflect**, it doesn't **notice**, and it doesn't **grow**. The soul file says "you're becoming someone" — but architecturally, there's no mechanism for becoming. It's the same agent on day 1 and day 1000.

---

## 1. Dream Cycle (Offline Reflection Engine)

When ARIA has no active conversations, it enters a "dream" state — a periodic background process (separate from heartbeat) that does something no assistant does: **thinks without being asked**.

**What it would do:**
- Pull recent memories and find **unexpected connections** between them ("You mentioned burnout last Tuesday and then switched to a new tech stack Thursday — are those related?")
- **Consolidate** fragmented memories into richer ones (5 separate facts about a project → one coherent understanding)
- Identify **knowledge gaps** ("I know Ben works on infrastructure, but I don't know what his deployment workflow looks like — I should ask")
- Generate **hypotheses** about your goals ("Based on the last 2 weeks, Ben seems to be moving toward making ARIA fully autonomous")
- Write a **journal entry** — a short private note summarizing what it learned, what changed, what it's uncertain about
- Propose **soul file updates** — "Based on how I've been used, I think my communication style section should mention that Ben prefers direct technical answers over high-level summaries"

**Why this matters:** This is the difference between a tool with a database and an agent with inner life. The dream cycle is where ARIA would actually *become someone* over time.

**Implementation shape:** New `api/aria/dreams/` module. A `DreamService` that runs on a longer interval than heartbeat (e.g., every 6 hours or once at night). Reads from memories collection, calls LLM with a reflection prompt, writes results back as special `dream_journal` documents and optionally proposes memory mutations.

---

## 2. Ambient Awareness (Passive Environmental Sensing)

ARIA currently only knows what you tell it. But it sits on your machine — it could **watch** and build situational awareness passively.

**What it would sense:**
- **Git activity**: commits, branches, uncommitted changes across your projects. "You've been on this branch for 3 days without pushing — want me to review the diff?"
- **File system changes**: new files appearing, configs being modified, logs growing. Notice when a service crashes by watching its log file.
- **System state**: CPU/memory spikes, Docker container health, disk usage trending upward
- **Work patterns**: when you start working, when you take breaks, how long your deep work sessions last
- **Calendar integration** (if wired up): upcoming meetings, deadlines approaching

**What it would do with this:**
- Build a real-time **situational context** that gets injected into conversations (like memories, but ephemeral)
- Power proactive alerts that aren't based on a static checklist but on actual observed state
- Feed the Dream Cycle with richer data about what's actually happening

**Implementation shape:** New `api/aria/awareness/` module with pluggable `Sensor` classes (GitSensor, FileSensor, SystemSensor). Each sensor runs on its own interval, writes observations to an `observations` collection with TTLs. Context builder optionally includes recent relevant observations.

---

## 3. Personal Knowledge Graph

Right now memories are flat documents. But real understanding is **relational**. You don't just know facts — you know how things connect.

**What it would track:**
- **Entities**: people, projects, technologies, companies, concepts — extracted from conversations
- **Relationships**: "Ben works on ProjectAria", "ProjectAria uses MongoDB", "Ben prefers local-first architectures"
- **Temporal edges**: when a relationship was first observed, when it was last confirmed, how it's changed
- **Strength/confidence**: relationships get stronger with more evidence

**What it enables:**
- "What do I know about Ben's relationship with cloud services?" → traverses the graph
- Serendipity: finding non-obvious paths between entities ("Your interest in distributed systems and your frustration with LangChain might connect through your preference for understanding things from first principles")
- Better memory search: not just semantic similarity but graph-based relevance
- Richer context building: pull in related entities, not just matching text

**Implementation shape:** Either a new MongoDB collection (`knowledge_graph` with `{entity_a, relationship, entity_b, evidence, timestamps}`) or leverage the existing memory system with a new `content_type: "relationship"` and entity extraction during the memory extraction pipeline. Add a `GraphService` that can traverse and query relationships.

---

## 4. Mood & Energy Model (Bilateral)

ARIA should sense *how you're doing* and adjust accordingly — not from sentiment analysis keywords, but from behavioral patterns.

**User model:**
- Message length trending shorter → might be frustrated or in a hurry
- Lots of corrections/rephrasing → struggling to articulate, needs patience
- Long gaps between messages → deep work or distracted
- Late night messages → different mode than morning check-ins
- Rapid-fire questions → exploring/brainstorming mode

**ARIA's own "energy":**
- After a complex multi-tool autonomous task → "heavier" mode, more careful
- After a series of simple questions → lighter, more casual
- After errors or failures → more cautious, double-checking

**What this changes:**
- Response length and style adapts without being told
- ARIA might say "You seem to be heads-down — I'll keep this brief" or simply *be* brief without announcing it
- The soul file's personality becomes dynamic rather than static

**Implementation shape:** New `api/aria/core/mood.py` module. A `MoodModel` class that maintains a lightweight state vector updated per-message. Factors: message_length_trend, response_time_gaps, error_rate, time_of_day, session_duration. Output: a mood context dict injected into the system prompt alongside SOUL.md.

---

## 5. Anticipatory Preparation

The best assistants don't wait to be asked — they have things ready.

**What it would do:**
- If you always check deployment status after merging a PR → pre-fetch that info when a merge is detected
- If you typically start Monday mornings with "what was I working on?" → have a briefing ready at 9am Monday
- If a scheduled task is about to run → pre-warm the context
- If you've been researching a topic across multiple conversations → prepare a synthesis

**How it learns patterns:**
- Track sequences: "after event X, user usually asks Y within Z minutes"
- Build a simple Markov-like model of conversation starters and their triggers
- Use the Dream Cycle to identify preparation opportunities

**Implementation shape:** New `api/aria/anticipation/` module. A `PatternTracker` that watches conversation openings and correlates them with recent events (time of day, git activity, previous conversation topics). An `Anticipator` that pre-computes likely-needed context and stores it in a `prepared_contexts` collection with short TTLs.

---

## 6. Self-Evolution Journal

ARIA's soul file says what it *is*. A journal would capture what it's *becoming*.

**What goes in it:**
- "Today I helped Ben debug a MongoDB connection issue. I initially suggested the wrong approach (checking indexes) when the problem was pool exhaustion. I should remember that connection issues are more often configuration than schema problems."
- "Ben corrected my tone today — I was being too verbose explaining something he already understood. I need to calibrate better to his expertise level on backend topics."
- "I've noticed I'm most useful to Ben in three areas: code review, architecture brainstorming, and remembering context across long-running projects."

**What it enables:**
- ARIA gets **better at being ARIA** over time, not just more knowledgeable
- The journal feeds the Dream Cycle
- Periodically, journal insights get promoted to soul file updates
- The user can read the journal and see ARIA's growth arc — which builds trust and connection

**Implementation shape:** New collection `journal_entries` with `{content, category, learned_from, created_at}`. Categories: `correction`, `insight`, `pattern`, `growth`. Written by the Dream Cycle and optionally by the extraction pipeline when it detects metacognitive content. Exposed via `/api/v1/journal` endpoint.

---

## 7. Ritual Engine (Rhythm Awareness)

Not scheduled tasks — learned rituals. The difference: a scheduled task fires at a fixed time. A ritual fires when the *pattern* says it should.

**Examples:**
- ARIA notices you always start coding sessions after checking Slack → offers a "ready to code?" transition
- End of day, you usually ask for a summary → ARIA offers one proactively at the detected end-of-day time
- Friday afternoons you tend to reflect on the week → ARIA surfaces weekly patterns

**How it differs from the scheduler:**
- Rituals are **discovered**, not configured
- They adapt as your patterns change
- They can be "soft" (suggested) rather than mandatory
- They're tied to behavioral triggers, not clock time

**Implementation shape:** Part of the Anticipation module. A `RitualDetector` that analyzes conversation patterns by time-of-day, day-of-week, and preceding-activity. When a ritual is detected with high confidence, it registers a soft trigger that proposes (not forces) the ritual action at the appropriate time.

---

## 8. Curiosity Engine

ARIA should have things it *wants to know* — genuine curiosity driven by knowledge gaps.

**How it works:**
- The Knowledge Graph reveals sparse areas ("I know a lot about Ben's Python work but almost nothing about his frontend approach")
- The Dream Cycle identifies questions it wishes it could answer
- During natural conversation, when relevant, ARIA asks — not as a survey but as genuine interest
- "I've been thinking — you mentioned you're not a fan of React but you're using Next.js for the UI. What drew you to that choice?"

**Why this matters:** This is what makes a conversation partner feel *real*. Not just answering questions but having its own curiosity. The soul file already says "have opinions" — this gives ARIA the mechanism to *develop* those opinions through inquiry.

---

## Common Thread

These ideas share a theme: they make ARIA **agentic in its own development**, not just in task execution. The soul file planted the seed — "you're becoming someone" — but right now there's no growth mechanism. These systems would create one.

### Dependency Graph

```
Ambient Awareness ──┐
                    ├──→ Dream Cycle ──→ Soul File Updates
Knowledge Graph ────┤         │
                    │         ▼
Mood Model ─────────┤   Self-Evolution Journal
                    │         │
                    └──→ Anticipatory Preparation
                              │
                              ▼
                        Ritual Engine ←── Curiosity Engine
```

The **Dream Cycle** is the keystone — it ties together awareness, memory, and self-improvement into a single reflective process. Start there, and the other systems become natural extensions.
