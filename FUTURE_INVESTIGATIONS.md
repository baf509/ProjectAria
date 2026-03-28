# Future Investigations

Research topics, experimental ideas, and longer-term explorations that don't belong in the implementation plan yet. These need investigation before committing to implementation.

**Created:** 2026-03-14

---

## 1. Local Model Strategy

### Investigation: Which local models for which modes?

**Question:** As local models improve rapidly, what's the right strategy for mapping modes to local vs. cloud models?

**Areas to explore:**
- Benchmark local models (Qwen 3, Llama 4, Mistral, DeepSeek) specifically on Aria's use cases: casual chat, code generation, research synthesis, creative writing
- Measure latency vs. quality tradeoffs on your hardware (ROCm GPU)
- Can a small local model handle mode classification / memory extraction reliably? (saves cloud API costs on background tasks)
- Investigate speculative decoding for faster local inference
- Multi-model inference: can you run a small model for drafting + large model for review simultaneously?
- Context length limits: local models often have 8K-32K context — is this enough for each mode, or do some modes need 128K+ cloud models?

**Why it matters:** If local models can handle 80% of interactions, Aria becomes nearly free to run and fully private. Cloud becomes the exception, not the rule.

---

## 2. Voice Interface Design

### Investigation: What does a good voice-first AI interaction feel like?

**Question:** Beyond basic STT→text→LLM→text→TTS, what makes voice interaction with an AI agent actually useful?

**Areas to explore:**
- Voice activity detection (VAD): when to start/stop listening without a push-to-talk button
- Interruption handling: can the user cut off Aria mid-response? How does this affect the conversation state?
- Ambient mode: Aria listens passively and only responds when addressed (wake word detection — "Hey Aria")
- Voice personality: different TTS voices per mode? Speed/tone adjustments?
- Multimodal input: voice + screen context (what's on screen when the user speaks)
- Latency budget: what's the maximum acceptable end-to-end latency for voice? (likely <2 seconds)
- Whisper vs. faster alternatives (Moonshine, Canary) for real-time transcription
- Streaming TTS: start speaking before the full response is generated
- Investigate Qwen3-TTS quality vs. alternatives (Kokoro, Piper, Coqui)
- Phone call interface: could Aria answer actual phone calls via SIP/VoIP? Is this useful or gimmicky?

**Why it matters:** Voice is the most natural interface for many contexts (driving, cooking, walking, gaming). But bad voice UX is worse than no voice UX.

---

## 3. Computer Use & Screen Context

### Investigation: How should Aria understand and interact with the user's screen?

**Question:** Beyond CLI tools, should Aria be able to see your screen, click things, and understand visual context?

**Areas to explore:**
- Screenshot analysis: periodic screenshots → vision model → context about what the user is doing
- Active window detection: know which app is in focus, adjust mode automatically
- GUI automation: Anthropic's computer use, or platform-native accessibility APIs (AT-SPI on Linux, UIAutomation on Windows, Accessibility API on macOS)
- Privacy implications: what should Aria see vs. not see? Banking, passwords, private messages?
- Opt-in regions: user defines screen regions Aria can observe
- Gaming integration: can Aria see your game screen and provide real-time advice? Overlay HUD?
- Cross-platform challenges: X11 vs Wayland vs Windows vs macOS all have different screenshot/automation APIs
- Performance impact: continuous screenshot capture + vision model inference cost

**Why it matters:** Screen context is the richest signal about what the user is doing. But it's invasive, expensive, and technically challenging across platforms.

---

## 4. Multi-Device Synchronization

### Investigation: How should Aria handle being accessed from multiple devices simultaneously?

**Question:** If the user is chatting on their phone (Signal) and opens the desktop widget, what happens?

**Areas to explore:**
- Conversation continuity: should the same conversation be accessible from all devices?
- Active device tracking: does Aria know which device the user is currently on?
- Notification routing: send notifications to the device the user is currently using, not all devices
- Conflict resolution: what if the user sends messages from two devices at once?
- Context awareness: "I'm on my phone" vs "I'm at my desk" should change Aria's behavior
- State synchronization: conversation list, mode selection, settings — what syncs across devices?
- Offline support: should the widget work offline and sync later? (probably not worth the complexity)

**Why it matters:** The unified agent vision means seamless cross-device experience. But true multi-device sync is an engineering quagmire — need to find the right level of simplicity.

---

## 5. Knowledge Base & Document Ingestion

### Investigation: Should Aria have a persistent knowledge base beyond conversational memory?

**Question:** Is the current memory system (extracted facts from conversations) enough, or does Aria need a proper RAG pipeline for documents?

**Areas to explore:**
- Use cases: personal notes, project documentation, research papers, bookmarks, saved articles
- Ingestion formats: PDF, markdown, web pages, emails, code repositories
- Chunking strategies: fixed-size vs semantic vs hierarchical
- Is MongoDB's vector search sufficient at scale (10K+ documents, 100K+ chunks)?
- Alternative: use memories for facts and tool calls for document access (read file on demand)
- Hybrid approach: index document metadata + summaries in memory, full content accessed via tools
- Investigate: does LLM-based memory extraction from documents scale? (cost, accuracy, hallucination risk)
- Consider: GraphRAG or knowledge graphs for relationship modeling between entities
- Integration with existing tools: Obsidian vault, Notion export, browser bookmarks

**Why it matters:** The current memory system captures conversational learnings. But the user has knowledge in files, notes, and documents that Aria can't access unless asked. The question is whether proactive indexing is worth the engineering cost vs. on-demand file reading.

---

## 6. Privacy & Data Sovereignty

### Investigation: What's the right privacy model for a personal AI agent?

**Question:** How do you balance usefulness (Aria knows everything about you) with privacy (that data is sensitive)?

**Areas to explore:**
- Memory sensitivity levels: some memories should never leave the device (health, finances, relationships)
- Per-mode privacy rules: chat mode memories stay local, coding mode can use cloud
- Encryption at rest: should the MongoDB data be encrypted? (probably yes for remote access)
- Data minimization: what's the minimum Aria needs to remember to be useful?
- Cloud provider trust: when Aria sends context to Claude/OpenAI, what memories should be excluded?
- Memory redaction: automatically detect and redact sensitive info (API keys, passwords, SSNs) before sending to cloud
- User audit: easy way for the user to see everything Aria knows and delete anything
- Right to forget: one-click memory wipe, or scoped deletion ("forget everything about X")
- GDPR-like controls even for single-user: it's good practice and builds trust

**Why it matters:** Aria will accumulate deeply personal information over time. A breach or misconfiguration shouldn't be catastrophic.

---

## 7. Plugin / Extension System

### Investigation: Should Aria support third-party plugins beyond MCP?

**Question:** MCP provides tool extensibility, but should Aria have a broader plugin architecture?

**Areas to explore:**
- Plugin types: tools (MCP covers this), modes/personalities, memory extractors, notification channels, LLM providers, UI components
- Distribution: how would plugins be shared? Package registry? Git repos?
- Sandboxing: plugins run in Aria's process — how to prevent malicious plugins?
- Configuration: per-plugin settings, enable/disable, priority ordering
- Community: is there a community for this, or is it just for you? (probably just you — keep it simple)
- Alternative: just use MCP for tools and manual agent configuration for everything else. Don't over-engineer.

**Why it matters:** Extensibility is good, but plugin systems are notoriously hard to get right and maintain. MCP might be enough.

---

## 8. Proactive Agent Behavior

### Investigation: When should Aria act without being asked?

**Question:** Beyond notifications and reminders, should Aria proactively do things?

**Areas to explore:**
- Morning briefing: summarize overnight notifications, calendar, weather, news relevant to current projects
- Context-triggered actions: detect you're in a git repo with failing tests → offer to investigate
- Memory-triggered suggestions: "Last time you worked on X, you noted Y — want to pick that up?"
- Learning from patterns: detect recurring tasks and offer to automate them
- Ambient awareness: if Aria can see your screen (Investigation 3), suggest actions based on context
- Risk: proactive behavior can be annoying if poorly calibrated. Need strong opt-in/opt-out per behavior type.
- Investigate ABP's patrol system autonomy levels (observe/nudge/full) as a model for configurable proactivity

**Why it matters:** A truly useful personal agent anticipates needs. But an annoying agent gets turned off. The line is thin.

---

## 9. Collaborative / Multi-Agent Patterns

### Investigation: Should Aria be able to coordinate multiple AI agents?

**Question:** Beyond spawning coding agents, should Aria support general multi-agent workflows?

**Areas to explore:**
- Agent-to-agent communication: can Aria delegate sub-tasks to specialized agents and synthesize results?
- Parallel agents: multiple agents working on different aspects of the same problem
- Agent marketplace: use different cloud models as specialized agents (Claude for code, GPT for analysis, Gemini for multimodal)
- Consensus: multiple agents review the same work and vote on quality
- ABP's coordinator pattern: one meta-agent monitoring multiple worker agents — does this generalize beyond coding?
- Cost implications: multi-agent workflows multiply API costs
- Complexity: multi-agent coordination is a research problem, not an engineering problem. Don't build what doesn't have clear ROI.

**Why it matters:** Multi-agent patterns can solve problems single agents can't. But they're expensive, complex, and often unnecessary.

---

## 10. Emotional Intelligence & Relationship Building

### Investigation: Should Aria model the user's emotional state and adapt?

**Question:** Beyond factual memory, should Aria track mood, energy, stress levels?

**Areas to explore:**
- Sentiment analysis on user messages: detect frustration, excitement, fatigue
- Adaptive tone: more encouraging when the user is frustrated, more concise when busy
- Long-term relationship patterns: does the user prefer directness or warmth? Does this vary by time of day?
- Boundary awareness: when to offer support vs. stay professional
- Risk: getting this wrong feels manipulative or patronizing
- Implementation: could be a memory category ("emotional context") with lightweight sentiment tags
- Start simple: just let the user tell Aria how they're feeling and remember it

**Why it matters:** The best personal assistants adapt to your mood. But artificial emotional intelligence done poorly is worse than none at all.

---

## 11. Integration Ecosystem

### Investigation: What external services should Aria integrate with?

**Question:** Beyond Signal and coding tools, what integrations would be most valuable?

**Candidates to investigate:**
- **Calendar** (Google Calendar, CalDAV): scheduling, reminders, availability
- **Email** (IMAP/SMTP): summarize, draft, triage
- **GitHub/GitLab**: PR reviews, issue triage, notification filtering
- **Note-taking** (Obsidian, Notion): bidirectional sync with memory
- **Music** (Spotify, local): mood-based recommendations, playback control
- **Smart home** (Home Assistant): light/temperature control, automation triggers
- **Browser** (extension): save current page context, research from browsing
- **Task management** (Todoist, Linear): task tracking, project management
- **Weather/Location**: context-aware suggestions

**Decision framework:** Each integration should be evaluated on:
1. How often would you use it?
2. Can it be done via existing tools (web, shell) without dedicated integration?
3. How hard is it to build and maintain?
4. Does it require exposing credentials to Aria?

**Why it matters:** Integrations make Aria more useful but each one is maintenance burden. Pick the 2-3 highest-value ones.

---

## 12. Performance & Scaling

### Investigation: How will Aria perform as data grows?

**Question:** After a year of use, Aria will have thousands of memories, hundreds of conversations, and significant MongoDB data. Will it still be fast?

**Areas to explore:**
- MongoDB vector search performance at 10K, 50K, 100K memories
- Memory retrieval latency as corpus grows — is hybrid search still fast?
- Conversation loading time with long histories (1000+ messages)
- Embedding generation throughput for batch operations
- Background task concurrency limits
- Memory usage of the FastAPI process over time (memory leaks?)
- Index optimization for common query patterns
- Archival strategy: move old data to cold storage?
- Benchmark current system and establish baseline metrics

**Why it matters:** A personal agent that gets slower over time will eventually be abandoned. Need to ensure it stays snappy.

---

## 13. Testing Strategy for an AI Agent

### Investigation: How do you test a system whose core behavior is non-deterministic?

**Question:** Unit tests cover infrastructure, but how do you test the orchestrator, memory extraction, mode switching, and multi-turn conversations?

**Areas to explore:**
- Evaluation frameworks: can you use LLM-as-judge to evaluate Aria's responses?
- Golden conversation sets: curated multi-turn conversations with expected behaviors
- Memory extraction accuracy: given a conversation, does extraction produce correct memories?
- Mode switching correctness: does the right system prompt / tool set get activated?
- Tool call validation: does the orchestrator call the right tools with right arguments?
- Regression testing: how to detect when a model upgrade degrades behavior?
- Integration testing: full-stack tests from HTTP request to LLM call to response
- Cost of testing: each test run costs API tokens — how to minimize while maintaining coverage?
- Mock LLM responses for deterministic testing of orchestrator logic

**Why it matters:** Without testing, every change is a gamble. But testing AI systems is fundamentally different from testing traditional software.

---

## 14. Alternative Mobile Interfaces

### Investigation: If Signal proves limiting, what's the next best mobile option?

**Question:** Signal is the pragmatic choice, but what if its limitations become friction?

**Areas to explore:**
- **Progressive Web App (PWA)**: Aria's web UI as an installable mobile app. No app store, push notifications via Service Workers. How good are PWA notifications on iOS vs Android in 2026?
- **Telegram Bot**: better bot API than Signal, inline keyboards, rich formatting, but requires Telegram account
- **Matrix/Element**: open protocol, rich client, self-hosted, but smaller user base
- **WhatsApp Business API**: most people have WhatsApp, but expensive and Meta-controlled
- **Discord Bot**: if gaming is a use case, Discord is already open during gaming
- **Simple SMS via Twilio**: universal, no app needed, but expensive per message and limited formatting
- **Ntfy.sh or similar**: push notifications only (not bidirectional), but dead simple

**Decision criteria:**
1. Already installed on your devices?
2. Bidirectional (not just notifications)?
3. Rich formatting support?
4. Self-hostable?
5. Privacy characteristics?
6. Cost?

**Why it matters:** Signal is the right first choice, but having a backup plan avoids lock-in.

---

## 15. Aria Identity & Continuity

### Investigation: What makes Aria feel like "one agent" across modes and interfaces?

**Question:** If Aria has different personalities in different modes, how do you maintain a coherent identity?

**Areas to explore:**
- Core personality traits that persist across modes (e.g., always respectful, always remembers user preferences)
- Name and pronouns: does Aria refer to itself consistently?
- Memory continuity: when switching modes, does Aria acknowledge the switch? ("Switching to coding mode — I see we were just discussing your vacation plans, I'll keep that in mind")
- Cross-mode references: in coding mode, Aria might say "remember the architecture decision we discussed in our research session earlier"
- Identity vs. role: Aria is one agent playing different roles, not multiple agents
- System prompt design: a shared "identity preamble" prepended to all mode-specific system prompts
- User testing: try different approaches and see what feels right

**Why it matters:** The unified agent vision requires Aria to feel like one entity, not a collection of disconnected chatbots.

---

*Add new investigations as questions arise. Move items to the implementation plan when they're ready for action.*

*Last updated: 2026-03-14*
