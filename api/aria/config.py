"""
ARIA - Configuration

Phase: 1
Purpose: Application settings using pydantic-settings

Related Spec Sections:
- Section 10.2: Pydantic Settings
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # MongoDB (8.2 with replica set)
    mongodb_uri: str = "mongodb://localhost:27017/?directConnection=true&replicaSet=rs0"
    mongodb_database: str = "aria"
    mongodb_max_pool_size: int = 50
    mongodb_min_pool_size: int = 5

    # llama.cpp (local, OpenAI-compatible)
    # Default corrected 2026-07-30: was :8092, which is now the ridge-llama-proxy
    # (Wake-on-LAN). A missing/incomplete .env would therefore have pointed
    # ARIA's PRIMARY chat backend at Ridge and woken a sleeping gaming PC on
    # every call. :8103 was then the real local chat server (Qwen3.6-35B-A3B-MTP
    # ROCmFP4).
    #
    # 2026-08-05: no longer a hardcoded model port at all — it points at ARIA's
    # own `/llm/v1` passthrough (api/routes/llm_proxy.py), which resolves to
    # whichever on-box server is currently resident. Pinning a port here is what
    # broke this every time the resident model changed: DS4-0731 (:8107) landed
    # earlier today and is RAM-exclusive with :8103, so :8103 went permanently
    # down and `selfcheck` paged "llm (ConnectError)" every 10 minutes into the
    # Hermes alert-triage cron. The proxy makes that drift structurally
    # impossible. Auth is the global ARIA api_key (the middleware accepts it as
    # `Authorization: Bearer`, which is all an OpenAI client can send) —
    # LLAMACPP_API_KEY must therefore equal API_KEY.
    llamacpp_url: str = "http://localhost:8200/llm/v1"
    # Same proxy, plus one injected system line naming the model actually
    # serving the request. Conversational agents use this so that asked "what
    # model are you?" they can answer with the model rather than with
    # `aria-resident`, which is a routing alias and names nothing.
    #
    # Deliberately NOT the same as llamacpp_url: that one is LLAMACPP_URL, used
    # by health probes and by evalstack/benchmark targets, and it must keep
    # forwarding request bodies verbatim so measurements aren't taken against a
    # prompt ARIA silently edited.
    llamacpp_identified_url: str = "http://localhost:8200/llm/v1-identified"
    # When a request NAMES a registered model that isn't resident, start it —
    # stopping whatever it is RAM-exclusive with first. This is what lets a
    # consumer treat ARIA like an ordinary provider: pick a model, use it.
    #
    # It has real teeth: satisfying the request can evict a running model and
    # block the request for the couple of minutes a load takes. It fires ONLY on
    # an explicitly named model, never on the `aria-resident` alias. Set false to
    # go back to a 409 telling the caller to start it themselves.
    llm_proxy_autostart: bool = True
    # How long to hold a request open while an autostarted backend maps its
    # weights. Ling Q5_K_M takes ~110s from cold; DS4 similar. Past this the
    # request is forwarded anyway so the caller sees the backend's own error
    # rather than a silent hang. Hermes' gateway_timeout is 1800s.
    llm_proxy_autostart_timeout: float = 300.0
    llamacpp_api_key: str = ""
    # Hard wall-clock cap on a single LLM call. The SDK default (600s) lets a
    # busy/half-open local server wedge a caller for ~10min; a hang never raises
    # so retry_async can't recover it.
    llamacpp_timeout_seconds: int = 120

    # The "agentic" backend slot. REPURPOSED 2026-07-30: it no longer means the
    # retired qwen-agentic :8093 server — it is now ARIA's LOCAL CODING backend,
    # pointed at :8105 (chadrockv2, Qwen3.6-27B ROCmFP6 "STRIX QUALITY") and used
    # by agent slug=pi-coding. Overridden by AGENTIC_URL in .env; the default is
    # kept in step with it so the two can't disagree.
    agentic_url: str = "http://localhost:8105/v1"
    agentic_api_key: str = ""

    # Ridge (RTX 3090) — NInfer serving Qwen3.6-35B-A3B, reached over the tailnet
    # via corsair's ridge-llama-proxy (:8092). The proxy sends Wake-on-LAN and
    # HOLDS the request while the box boots and loads 20.8 GiB of weights, so a
    # cold call legitimately takes ~90s before the first byte — hence a timeout
    # far larger than llamacpp's. Inference is remote; the agent's tools still
    # run locally on corsair.
    #
    # NOTE: NInfer serves ONE request at a time (no continuous batching), so
    # concurrent callers queue. Keep the number of ridge-backed workers small.
    ridge_url: str = "http://100.123.245.84:8092/v1"
    ridge_api_key: str = ""
    ridge_timeout_seconds: int = 420

    # Chroma context-1 (local agentic search model served by a second llama.cpp).
    # Off by default: the container is not part of the normal stack. With this
    # false the backend is unavailable, the Search Agent tool is not registered,
    # and health stops probing :8081 (no permanent DEGRADED).
    context1_enabled: bool = False
    context1_url: str = "http://localhost:8081/v1"
    context1_api_key: str = ""
    context1_model: str = "default"
    context1_max_iterations: int = 8
    context1_max_docs: int = 20
    context1_fs_allowed_roots: list[str] = [
        "/home/ben/Development/ProjectAria",
        "/home/ben/Development/infrastructure",
    ]

    # Cloud LLMs
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openrouter_api_key: str = ""

    # Fireworks AI (Firepass) — OpenAI-compatible; hosts GLM 5.2.
    fireworks_api_key: str = ""
    fireworks_base_url: str = "https://api.fireworks.ai/inference/v1"

    # Spend circuit-breaker: if >0, the rate-limit watchdog trips the global
    # emergency stop when the last hour's priced usage exceeds this many USD.
    spend_cap_usd_per_hour: float = 0.0

    # TTS
    tts_url: str = "http://localhost:8002/v1"

    # STT
    stt_url: str = "http://localhost:8003/v1"

    # Signal
    signal_enabled: bool = False
    signal_rest_url: str = "http://localhost:8088"
    signal_account: str = ""
    signal_dm_policy: str = "allowlist"
    signal_allowed_senders: list[str] = []
    signal_attachment_dir: str = "~/.local/share/signal-cli/attachments/"
    signal_poll_interval_seconds: int = 15

    # Search / research
    brave_search_api_key: str = ""
    brave_search_url: str = "https://api.search.brave.com/res/v1/web/search"
    research_default_backend: str = "llamacpp"
    research_default_model: str = "default"
    codex_binary: str = "codex"
    claude_code_binary: str = "claude"
    # Installed Pi coding-agent executable. ARIA supervises this real
    # interactive TUI in a watched tmux shell; it does not reimplement
    # Pi's agent loop. Provider names correspond to ~/.pi/agent/models.json.
    pi_coding_binary: str = "pi"
    pi_coding_provider_llamacpp: str = "llama-cpp"
    pi_coding_provider_agentic: str = "agentic"
    pi_coding_provider_ridge: str = "ridge"
    coding_default_backend: str = "codex"
    coding_default_workspace: str = "/home/ben/Development/aria-projects"
    coding_output_lines: int = 500
    # Run ARIA-spawned coding sessions on the watched-shell substrate (a tmux
    # session that auto-adopts + captures to shell_events), so a sub-agent IS a
    # shell — unified with the fleet, drivable via the same tools, and visible in
    # the TUI/MCP. The watchdog/checkpoint/review overlay still manages it. Set
    # false to fall back to the legacy raw-subprocess substrate.
    # Multi-machine nodes (aria-node agents). The fleet can span this host plus
    # remote nodes (e.g. a MacBook). local_node_id identifies THIS host; shells
    # whose `host` differs are driven via the node command queue rather than
    # local tmux. Empty local_node_id → resolved to socket.gethostname().
    local_node_id: str = ""
    node_heartbeat_timeout_seconds: int = 45   # missed heartbeats → node offline
    node_command_ttl_seconds: int = 120        # queued command expiry (TTL)
    node_command_timeout_seconds: int = 30     # how long a remote op awaits a result
    node_command_poll_seconds: int = 20        # server-side long-poll hold for the node

    coding_use_shell_substrate: bool = True
    coding_watchdog_interval_seconds: int = 5
    coding_stall_seconds: int = 60
    coding_auto_respond_prompts: bool = False
    # Global concurrency limiter for coding sub-agents (Pi-Flow
    # --max-concurrent-subagents parity). A session holds a "slot" while it is
    # actively running; spawns beyond the cap sit in a `queued` state and launch
    # as slots free. 0 = unbounded. Size it with looping (Ralph) sessions in
    # mind — they hold a slot for their whole life.
    coding_max_concurrent_sessions: int = 4
    # laguna hosts ONE coding slot; a second concurrent laguna session evicts
    # the first's prefix. Cloud backends are not affected by this cap.
    coding_max_concurrent_laguna_sessions: int = 1
    # Ridge (NInfer) has no continuous batching -- "one request at a time,
    # concurrent callers queue" (docs/ops/LOCAL_INFERENCE_TOPOLOGY.md §3.1).
    # Same reasoning as laguna above, different resource. Cloud backends are
    # not affected by this cap either.
    coding_max_concurrent_ridge_sessions: int = 1
    # pi-code sessions land on the resident local server (DS4), where each one
    # occupies a llama.cpp SLOT for its whole life. The global cap above cannot
    # protect those slots: a claude_code session consumes a global slot but ZERO
    # local capacity, so four Claude sessions would block pi entirely while the
    # GPU idles, and four pi sessions would over-subscribe the slots reserved
    # for them. This is the cap that must track the server's -np.
    #
    # Budget at -np 1 (rewritten 2026-08-15): pi's model is DS4-XXS on the Halo
    # APU, served as exactly ONE 131K slot, and DS4 is MoE — concurrent slots
    # activate different experts, so multi-slot anti-scales (measured per-request
    # 10.06/6.97/5.58 t/s at 2/4/6 slots vs 30.69 single). Hermes moved to Qwen
    # on the R9700 on 2026-08-15, so nothing else contends for this slot, and the
    # right cap is one session with nothing reserved. The old 2+2 was budgeted
    # for a retired -np 4 deployment and over-subscribed the live server 4:1 —
    # which does not fail loudly, it just makes every turn pay a cold prefill
    # (39.5 s vs 4.2 s warm). `check_pi_slot_budget()` warns if this drifts.
    coding_max_concurrent_pi_sessions: int = 1
    # Slots on the local server NOT available to pi sub-agents (Hermes main,
    # the system pi-coding agent, background workers). Zero now that Hermes and
    # ARIA's background work both live on Qwen/R9700 rather than on pi's server.
    coding_pi_reserved_slots: int = 0

    # Master switch, independent of everything below: False refuses any
    # pool-backed coding session up front (start_session) with a clear error,
    # rather than letting it dial a model server that isn't there. Flip this
    # off when chadrock/Laguna is physically down -- e.g. 2026-07-30, Ben shut
    # the model down -- without deleting any of the config below, so turning
    # it back on later is a one-line change once the server is back.
    pool_enabled: bool = True

    # Poolside `pool` CLI, run in standalone mode against a local OpenAI-
    # compatible endpoint. The model is passed via POOLSIDE_STANDALONE_MODEL,
    # not a CLI flag -- `pool exec` has no --model.
    pool_binary: str = "/home/ben/.local/bin/pool"
    # :8102 is Chadrock (Laguna ROCmFP4, Vulkan) running --parallel 1 with pool
    # as its ONLY consumer -- that exclusivity is what keeps its prefix warm.
    #
    # The id_slot advice that used to live here is GONE, along with the
    # topology it described: laguna-slot-proxy (:8096-:8100, mapping consumers
    # to id_slot values on one shared server) is stopped and disabled, and
    # those ports have no listener. Prefix isolation now comes from one server
    # per consumer class, and on DS4 from one llama.cpp slot per agent -- not
    # from a proxy rewriting id_slot. Do not reintroduce it.
    pool_api_url: str = "http://127.0.0.1:8102"
    # Must match the alias Chadrock reports at /v1/models. llama-server ignores
    # the request model field in single-model mode, so a mismatch is silent --
    # which is exactly why it is worth pinning correctly.
    pool_model: str = "laguna-s21-rocmfp4-strixkvspine-v4"
    pool_api_key: str = "EMPTY"
    # Hard cap on how many sessions may sit queued waiting for a slot. 0 = no
    # cap. Beyond it a spawn is refused (fail loud) rather than silently queued.
    coding_queue_max: int = 64
    # Ralph loop: opt-in, per-session. When a coding session carries a
    # loop_config, the watchdog nudges it forward whenever it goes idle at its
    # prompt — re-checking the killswitch/e-stop each nudge — until it emits the
    # done token, or the nudge/deadline caps trip. Absent loop_config = no loop.
    coding_loop_idle_seconds: int = 45          # idle-at-prompt time before a nudge
    coding_loop_max_nudges: int = 40            # hard cap on nudges per session
    coding_loop_deadline_minutes: int = 180     # wall-clock cap on a looping session
    coding_loop_done_regex: str = "RALPH_DONE"  # seen in output → loop is done
    coding_loop_nudge_prompt: str = (
        "Continue the next step of the task. When the entire task is complete AND "
        "verified (tests pass), reply with exactly RALPH_DONE and stop."
    )
    # Verification Gate (Coherence C1): a Ralph-looped session's done-signal is
    # self-reported ("done" in the agent's head, not "verified"). When enabled,
    # the watchdog runs a check command in the workspace before honoring the
    # done token; failure re-nudges with the check's output instead of ending
    # the loop. Per-project override lives on `projects.check_command`
    # (fallback: this global default); missing/no-op check → skip, don't
    # block. Off by default -- opt in per the Coherence design's principle 2
    # (propose/gate, don't silently change behavior).
    coding_gate_enabled: bool = False
    coding_gate_command: str = "make check"
    coding_gate_timeout_seconds: int = 300
    coding_gate_max_retries: int = 3          # consecutive gate failures before giving up (alert, stop nudging)
    # Complexity routing: when a coding session is started with NO explicit
    # backend/model, a Sonnet-class judge classifies the task into a tier and
    # picks the model. An explicit pin always wins — routing only fills the gap.
    # Sonnet is the floor for normal routing; the sub-Sonnet fallback below is
    # reached only when the Claude subscription quota is exhausted.
    coding_routing_enabled: bool = True
    coding_routing_judge_backend: str = "anthropic"
    coding_routing_judge_model: str = "claude-sonnet-5"
    # "api"  — one small Anthropic API call, sub-second, costs a fraction of a
    #          cent. Right for the interactive desk path. Needs ANTHROPIC_API_KEY.
    # "cli"   — `claude -p` via ClaudeRunner: burns the Claude subscription
    #          instead of API tokens, but several seconds of CLI startup.
    # "auto"  — api when an Anthropic key is configured, else cli. Default, so
    #          routing works out of the box on a subscription-only box.
    coding_routing_judge_transport: str = "auto"
    coding_routing_judge_timeout_seconds: int = 20
    coding_routing_cache_ttl_seconds: int = 900   # dedupe repeat classifications
    # Tier → model. deep = planning/design/strategy; standard = scoped
    # implementation; light = research/info-gathering (often answered inline
    # by the judge with no session spawned at all).
    coding_routing_model_deep: str = "claude-opus-4-8"
    coding_routing_model_standard: str = "claude-sonnet-5"
    coding_routing_model_light: str = "claude-sonnet-5"
    # Quota-exhausted fallback — the ONLY path that goes below Sonnet. Engaged
    # when the watchdog sees quota/rate-limit text in a session's output and
    # records a cooldown in `model_availability`.
    # Quota fallback runs on the LOCAL model, so it keeps working with no key
    # and no spend. Repointed 2026-07-27: this used to be pi-code + llm
    # "agentic", but agentic is :8093 (qwen-agentic) which has been down for
    # days -- the comment claiming it pointed at :8095 was simply wrong, so the
    # fallback would itself have failed. `pool` reaches laguna directly via
    # pool_api_url. Note it consumes the single laguna coding slot, so if a
    # coding session already holds it the fallback queues rather than evicting.
    # EMPTY = NO FALLBACK, by deliberate choice (Ben, 2026-07-30): when the
    # Claude quota is exhausted a coding task should FAIL AND PAUSE, not quietly
    # continue on a weaker local model and produce work of a different standard.
    # Set this to a backend name to re-enable demotion; `pool` was the old
    # default and pointed at a server that has since been shut down, so the
    # "fallback" was a broken path pretending to be a safety net.
    coding_routing_fallback_backend: str = ""
    coding_routing_fallback_llm: str = ""
    coding_routing_fallback_model: str = ""
    coding_routing_quota_cooldown_minutes: int = 60

    infrastructure_root: str = "/home/ben/Development/infrastructure"

    # Streaming
    stream_chunk_timeout_seconds: int = 60

    # Memory
    memory_search_cache_ttl_seconds: int = 10
    memory_dedup_similarity_threshold: float = 0.95

    # Embeddings
    embedding_url: str = "http://localhost:8001/v1"
    embedding_model: str = "voyageai/voyage-4-nano"
    embedding_dimension: int = 1024
    voyage_api_key: str = ""

    # Retrieval capability switches (memory/capabilities.py). These are only
    # the *boot defaults*: the live state is a persisted doc, so a capability
    # switched off by an operator stays off across an aria-api restart rather
    # than silently coming back and resuming the alerts they turned off.
    #   embeddings_enabled=False -> never call the embeddings service; writes
    #     land with embedding_pending=true and are backfilled on re-enable.
    #   search_enabled=False     -> never emit $vectorSearch/$search; recall
    #     degrades to the mongod-native fallback scan.
    embeddings_enabled: bool = True
    search_enabled: bool = True

    # Backfill worker: drains embedding_pending memories + un-embedded ontology
    # entities. Runs on a timer and is woken immediately when embeddings are
    # re-enabled, which is what makes "turn it back on" self-healing.
    embedding_backfill_enabled: bool = True
    embedding_backfill_interval_seconds: int = 300
    embedding_backfill_batch_size: int = 100
    # Concurrent embed calls inside a batch. The embeddings service is CPU-only
    # (sentence-transformers), so a wide fan-out just queues; 4 keeps the
    # backfill from starving live memory writes of the same service.
    embedding_backfill_concurrency: int = 4

    # API
    # Port 8200 is canonical post-cutover (ProjectAria absorbed aria-shells;
    # the old :8000 is retired). The systemd unit pins --port 8200.
    api_host: str = "0.0.0.0"
    api_port: int = 8200
    api_auth_enabled: bool = True
    api_key: str = "aria-local-admin-key"
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:8200",
        "http://localhost:1420",
        "http://127.0.0.1:3000",
        "http://aria-ui:3000",
        "tauri://localhost",
        "https://tauri.localhost",
    ]
    task_default_timeout_seconds: int = 1800
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    rate_limit_requests_per_window: int = 120
    audit_logging_enabled: bool = True
    tool_execution_policy: str = "allowlist"
    # 2026-07-27: "filesystem" and "shell" added GLOBALLY so the Ridge-backed
    # coding agent (slug=pi-coding-ridge) can actually write code on this box.
    # This is a system-wide loosening, chosen deliberately over a per-agent
    # grant: EVERY agent can now write files and run shell commands, including
    # background workers. Both remain in tool_sensitive_names, so any call path
    # that passes allow_sensitive=False still refuses them.
    # Residual guardrails: shell_allowed_commands is read-only (no rm/sudo/sed —
    # see shell_denied_commands) and filesystem_denied_paths still protects
    # ~/.ssh, ~/.aws, ~/.gnupg, ~/.netrc, ~/.pgpass.
    # To revert: delete the two entries below.
    tool_allowed_names: list[str] = [
        "filesystem",
        "shell",
        "web",
        "browse_page",
        "list_coding_sessions",
        "get_coding_output",
        "get_coding_diff",
        "update_soul",
        "claude_agent",
        "pi_coding_agent",
        "deep_think",
        "search_agent",
        # 2026-08-17: the scheduled runtime-update check. Read-only by
        # construction — it queries GitHub/Docker Hub and reads local git HEADs,
        # and never pulls, builds, or restarts anything. Allowlisted so ARIA's
        # scheduler can invoke it unattended; without this the scheduled run
        # fails with "not in the tool allowlist" and would look like a working
        # job that silently reports nothing.
        "check_runtime_updates",
    ]
    # Tools whose name starts with one of these prefixes are allowed under the
    # allowlist policy — e.g. the Playwright MCP computer-use tools (browser_*).
    tool_allowed_prefixes: list[str] = ["browser_"]
    tool_denied_names: list[str] = []
    tool_sensitive_names: list[str] = ["shell", "filesystem"]
    tool_api_sensitive_enabled: bool = False
    tool_rate_limit_per_minute: int = 30
    shell_allowed_commands: list[str] = [
        "pwd",
        "ls",
        "find",
        "cat",
        "head",
        "tail",
        "rg",
        "grep",
        "git status",
        "git diff",
        "git log",
        # 2026-07-27: test runners + verifiers, so a coding agent can actually
        # CHECK its work instead of asserting success. Everything above this
        # line is read-only; these genuinely execute project code.
        #
        # These are PREFIXES. Bare interpreters ("python", "python3", "node",
        # "sh") are deliberately absent — "python3" would permit
        # `python3 -c "<anything>"`, arbitrary code execution wearing a test
        # runner's coat. `python3 -m pytest` is safe because the prefix pins the
        # module. Add the specific runner, never the interpreter.
        #
        # ONLY LIST WHAT IS INSTALLED. The shell tool execs directly (no shell),
        # so an entry that is not on the aria-api unit's PATH fails with
        # [Errno 2] and merely burns agent turns — that PATH is set in
        # ~/.config/systemd/user/aria-api.service.d/toolpath.conf, which adds
        # ~/.local/bin (pytest) and ~/.cargo/bin (cargo).
        # Not listed because NOT INSTALLED here as of 2026-07-27: ruff, mypy,
        # eslint, tsc, go, yarn, pnpm. Install one, then add it here.
        #
        # Running a project's tests inherently executes that project's code
        # (conftest.py, build.rs, package.json scripts). That is the point, and
        # is bounded by the shell tool's injection guard: no chaining, piping,
        # substitution or redirection, so each call is exactly one command.
        "pytest",
        "python3 -m pytest",
        "npm test",
        "npm run test",
        "make test",
        "cargo test",
        "cargo check",
        "cargo clippy",
    ]
    shell_denied_commands: list[str] = [
        "rm",
        "sudo",
        "shutdown",
        "reboot",
        "mkfs",
        "dd ",
        "git reset",
        "git checkout --",
        "docker system prune",
        "awk",
        "gawk",
        "mawk",
        "sed",
    ]
    # Paths the filesystem tool must never read/write/list/delete, even though
    # they are inside the user's home. Targets common credential stores so a
    # prompt-injected tool call cannot exfiltrate them.
    filesystem_denied_paths: list[str] = [
        "~/.ssh",
        "~/.aws",
        "~/.gnupg",
        "~/.netrc",
        "~/.pgpass",
        "~/.my.cnf",
        "~/.npmrc",
        "~/.pypirc",
        "~/.docker/config.json",
        "~/.kube",
        "~/.config/gh",
        "~/.config/google-chrome",
        "~/.config/chromium",
        "~/.mozilla",
        "~/.password-store",
        "~/.git-credentials",
    ]
    # OS-level containment for the shell tool, on top of the allowlist above.
    # The allowlist is a Python string check on the command text; this wraps
    # the actual subprocess in `bwrap` (bubblewrap) so a bypass of the
    # allowlist (or a project's own build script — pytest/npm test/cargo test
    # legitimately run project-controlled code, e.g. conftest.py/package.json
    # scripts/build.rs) still can't reach the network or read credential
    # files, and can't be turned off by the model itself since the wrapping
    # happens here, not inside anything a tool call can see.
    #
    # Kept close to the current effective capability (whole $HOME + /tmp stay
    # read-write, matching FilesystemTool's default `allowed_paths`) rather
    # than narrowing to a single workspace — that would need session-scoped
    # tool construction, which ARIA doesn't have yet (tools are one process-
    # wide singleton registered in main.py, shared by every agent/session).
    shell_sandbox_enabled: bool = True
    shell_sandbox_binary: str = "bwrap"
    # Reuses filesystem_denied_paths: same credential stores, but enforced by
    # masking them with an empty tmpfs (kernel-level) instead of a string
    # prefix check, so e.g. `cat ~/.ssh/id_ed25519` fails at the OS layer even
    # though "cat" is itself an allowed command.
    # Screenshot
    screenshot_command: str = "scrot"
    screenshot_vision_backend: str = "anthropic"
    screenshot_vision_model: str = "claude-sonnet-4-20250514"

    # Document generation
    docgen_output_dir: str = "~/Development/ProjectAria/aria-documents"

    # Skills
    skills_dir: str = "~/.aria/skills/"

    # Group chat
    groupchat_default_rounds: int = 3
    groupchat_max_personas: int = 6

    # Soul
    soul_file: str = "~/.aria/SOUL.md"

    # Heartbeat
    heartbeat_enabled: bool = False
    heartbeat_file: str = "~/.aria/HEARTBEAT.md"
    heartbeat_interval_minutes: int = 30
    heartbeat_active_hours_start: int = 9
    heartbeat_active_hours_end: int = 22
    heartbeat_backend: str = "openrouter"
    heartbeat_model: str = "deepseek/deepseek-v4-flash"
    heartbeat_ok_keyword: str = "HEARTBEAT_OK"

    # Dream Cycle
    dream_enabled: bool = False
    dream_interval_hours: int = 6
    dream_active_hours_start: int = 1   # run during quiet hours (1am-5am)
    dream_active_hours_end: int = 5
    dream_min_conversations: int = 3  # skip dream if fewer conversations since last run
    dream_max_memories: int = 50
    dream_max_conversations: int = 5
    dream_max_journal_entries: int = 10
    dream_claude_model: str = ""        # optional model override for claude CLI
    dream_timeout_seconds: int = 300    # max time for claude subprocess

    # Claude Runner — route background LLM tasks through Claude Code CLI
    # Uses subscription tokens instead of API tokens for heavy lifting
    use_claude_runner: bool = True       # set False to use API tokens for all tasks
    claude_runner_timeout_seconds: int = 120  # default timeout for non-dream tasks
    claude_runner_skip_permissions: bool = True  # allow background tasks full tool access

    # Deep Think — delegate reasoning to Claude Opus via CLI
    # The orchestrator model handles routing/memory, Claude does the thinking
    deep_think_enabled: bool = True          # inject delegation instructions into system prompt
    deep_think_model: str = ""               # optional model override (e.g. "claude-opus-4-20250514")
    deep_think_timeout_seconds: int = 180    # max time for a deep_think call

    # Ambient Awareness
    awareness_enabled: bool = False
    awareness_poll_interval_seconds: int = 120       # how often sensors run
    awareness_analysis_interval_minutes: int = 30    # how often ClaudeRunner analyzes
    awareness_observation_ttl_hours: int = 24        # auto-expire old observations
    awareness_watch_dirs: list[str] = ["/home/ben/Development/ProjectAria"]
    awareness_cpu_warn_percent: float = 90.0
    awareness_memory_warn_percent: float = 85.0
    awareness_disk_warn_percent: float = 90.0
    awareness_check_docker: bool = True
    awareness_inject_context: bool = True            # inject observations into LLM context
    awareness_session_lookback_hours: float = 48     # how far back to scan Claude sessions

    # Autopilot
    autopilot_max_steps: int = 20
    autopilot_step_timeout_seconds: int = 300

    # OODA
    ooda_default_threshold: float = 0.7
    ooda_default_max_retries: int = 2

    # Watched Shells
    shells_enabled: bool = True
    shells_tmux_session_prefix: str = "claude-"
    shells_capture_batch_size: int = 50
    shells_capture_flush_ms: int = 500
    shells_capture_max_buffer: int = 10000
    shells_snapshot_interval_seconds: int = 30
    shells_snapshot_lines: int = 10000
    shells_idle_threshold_seconds: int = 60
    shells_reconcile_interval_seconds: int = 120
    shells_idle_notifier_enabled: bool = True
    shells_idle_notifier_interval_seconds: int = 30
    shells_idle_prompt_patterns: list[str] = [
        r"\?\s*$",
        r">\s*$",
        r"Human:\s*$",
        r"\[y/n\]\s*$",
        r"(?i)press.*to continue",
    ]
    shells_include_in_chat_context: bool = True
    shells_context_max_tokens: int = 2000
    shells_context_lookback_hours: int = 24
    shells_context_lines_per_shell: int = 20
    shells_extraction_enabled: bool = True
    # Was hardcoded `"agentic"`, i.e. AGENTIC_URL -> the on-demand local coding
    # server, which is STOPPED by default -- so this fired every 10 minutes at a
    # port with no listener. It is cheap bulk classification, which is what
    # gemma-aux exists for, and keeping it off the big server leaves that
    # server's slots for Hermes and the coding agents.
    shells_extraction_backend: str = "llamacpp"
    shells_extraction_model: str = "gemma-4-e4b-Q4"
    shells_extraction_interval_minutes: int = 10
    shells_extraction_min_events: int = 20
    # Per-shell wall-clock bound on one extraction call. Belt-and-suspenders
    # over llamacpp_timeout_seconds so a single stuck call can't wedge the
    # worker's heartbeat past this (selfcheck watches for a stalled cursor).
    shells_extraction_timeout_seconds: int = 240
    shells_input_rate_limit_per_minute: int = 30
    shells_retention_days: int = 0  # 0 = keep forever
    shells_auto_archive_days: int = 7
    # Command spawned inside a new shell when launch_claude=True.
    # --dangerously-skip-permissions matches the long-running ARIA workflow
    # where the user has already approved the agent for filesystem/shell use;
    # override via env var SHELLS_CLAUDE_LAUNCH_COMMAND if you need different
    # flags or a different binary entirely.
    #
    # Wrapped in `bash -lc` so the login shell sources ~/.profile / ~/.bashrc
    # and PATH includes ~/.local/bin (or wherever the user installed claude).
    # Without this, the systemd-user context that runs the API has a minimal
    # PATH and the spawned tmux pane exits with status 127 ("command not
    # found") the instant it starts, killing the session before any client
    # can attach.
    #
    # The shim (scripts/aria-claude-launch) resumes the directory's most recent
    # thread with --continue when Claude Code history exists, so a session that
    # had to be respawned picks up where it left off instead of coming back
    # empty. Referenced by absolute path for the same PATH reason as above.
    shells_claude_launch_command: str = (
        "bash -lc '~/.local/bin/aria-claude-launch'"
    )

    # Planning subsystem (tasks + projects)
    # Ambient capture runs an LLM call after each non-private conversation
    # turn. Disable to require manual /api/v1/todos creation only.
    planning_ambient_capture_enabled: bool = True
    # Backend/model for ambient task extraction. Decoupled from the
    # conversation's chat model so the hot path can use a cheap fast model.
    planning_ambient_backend: str = "openrouter"
    planning_ambient_model: str = "deepseek/deepseek-v4-flash"
    # Default geometry for new tmux sessions. tmux's built-in default is 80x24,
    # which makes Claude Code's TUI render at a width that mobile clients can't
    # display without ugly wrapping. Mobile/widget clients should call
    # POST /shells/{name}/resize on view appear with their actual cols/rows.
    shells_default_cols: int = 120
    shells_default_rows: int = 40
    shells_min_cols: int = 20
    shells_min_rows: int = 10
    shells_max_cols: int = 500
    shells_max_rows: int = 200

    # Scrollback retention is a per-shell TOKEN budget, not a time TTL. The
    # prune worker keeps only the most recent ~N tokens of raw events per
    # shell. Derived data (memories, projects, tasks) is never touched.
    shells_prune_enabled: bool = True
    shells_event_token_budget: int = 150000  # ~600KB of recent scrollback/shell
    shells_prune_interval_hours: int = 6

    # Idle-session reaper (vault/ProjectAria/Design/ARCHITECTURE.md (Coherence C9)): capture-then-reap ARIA
    # coding sessions idle > N days — the agent is first asked to save its
    # learnings (reply with the done token), then the shell is killed. Default
    # OFF (destructive); tag a shell `keep` to protect it.
    shells_reap_enabled: bool = False
    shells_reap_idle_days: int = 7
    shells_reap_interval_hours: int = 6
    shells_reap_save_timeout_minutes: int = 30
    shells_reap_done_token: str = "REAP_SAVED"
    shells_reap_protected_tag: str = "keep"

    # Nudge-paused-shells: POST /shells/{name}/nudge wakes a watched shell
    # sitting at a prompt (activity_state=blocked). Attempts persist on the
    # shell doc across calls; after max_attempts consecutive failed nudges the
    # endpoint raises an alert (source="shells:nudge") that rides the normal
    # Hermes triage -> Signal confirm path. Driven by a Hermes cron sweep.
    shells_nudge_default_text: str = (
        "You appear to be paused. If your task isn't finished, continue "
        "working on it now; if it is finished, print a brief status summary."
    )
    shells_nudge_max_attempts: int = 3
    shells_nudge_wait_ms: int = 4000
    shells_nudge_min_idle_seconds: int = 300   # never nudge a freshly-paused shell
    shells_nudge_min_interval_minutes: int = 10  # debounce between nudges of one shell
    shells_nudge_protected_tag: str = "no-nudge"

    # Pre-seed Claude Code's folder-trust flag before launching a shell so the
    # blocking "Do you trust the files in this folder?" dialog never appears.
    shells_claude_autotrust: bool = True
    shells_claude_config_path: str = ""  # defaults to ~/.claude.json if empty
    shells_codex_config_path: str = ""   # defaults to ~/.codex/config.toml if empty

    # Auto-adopt: discover externally-started claude-* tmux sessions and watch
    # them without an explicit create_shell call. Hook-based in real time (see
    # scripts/aria-tmux-hook.conf), with this poll reconciler as a backstop.
    shells_adopt_enabled: bool = True
    shells_adopt_interval_seconds: int = 15
    # pipe-pane shim the reconciler starts capture with (writes the pidfile the
    # capture process is tracked by). Matches scripts/aria-shell-capture.
    shells_capture_shim: str = "/home/ben/.local/bin/aria-shell-capture"

    # Project registry harvester — derives the projects collection from git
    # repos + Claude/pi sessions + live shells. Never hand-maintained.
    projects_harvest_enabled: bool = True
    projects_harvest_interval_minutes: int = 30

    # Shared Services (SHARED_SERVICES_DESIGN.md) — S2 scan/reconcile worker.
    # Default OFF; enable once verified. Consumers register emitters (coherence
    # C2 machine-scan → memory; ontology entity attributes).
    shared_scan_enabled: bool = False
    shared_scan_interval_seconds: int = 300

    # Coherence C2: GitChangeEmitter on the S2 scan worker above — per-repo
    # "what changed in my code" memories (new commits since last seen).
    # Read-only (rev-parse/diff --shortstat/log), riding the same worker/
    # interval as shared_scan_enabled; has no effect if that's off. Empty
    # roots falls back to the project harvester's repo roots
    # (shells/harvest.py _find_git_repos + EXTRA_REPO_ROOTS).
    git_scan_enabled: bool = True
    git_scan_roots: list[str] = []
    git_scan_min_change_lines: int = 10

    # Ontology Memory Map (vault/ProjectAria/Design/ARCHITECTURE.md (Ontology Memory Map)
    # machines/services/projects and its cross-link into aria.memories.
    #
    # `ontology_enabled` runs the PROJECTION (§4a) — deriving service/project/
    # machine entities from the registries and db.projects. It is cheap,
    # LLM-free and idempotent, so it rides the S2 scan worker.
    ontology_enabled: bool = False
    # Re-embed entities during projection. Costs ~0.8s/entity on the CPU
    # embedding service (measured 2026-08-07: 100 entities in 78s), so the
    # periodic pass leaves it off and only explicit runs re-embed.
    ontology_projection_embed: bool = False
    # Forward-only entity extraction on newly created memories (§7, phase 5c).
    # Separate flag from ontology_enabled because this one spends inference on
    # every memory written, whereas the projection spends none.
    ontology_extraction_enabled: bool = False
    # Local by default and deliberately so: this host has had no cloud fallback
    # since 2026-07-26, and `llamacpp` resolves to ARIA's own /llm/v1
    # passthrough, which follows whichever server is resident. Naming a port
    # here would break every time the resident model changes.
    ontology_extraction_backend: str = "llamacpp"
    # Names gemma-aux explicitly rather than following the resident model. Two
    # measured reasons (2026-08-07): the resident server is DS4, a REASONING
    # model that spends its whole token budget thinking before emitting JSON —
    # and gemma is the box's designated auxiliary model, so extraction does not
    # compete with interactive inference for the GPU. The passthrough resolves
    # this string via llm_route.match_requested, so it is a selector, not a URL.
    ontology_extraction_model: str = "gemma-4-e4b-Q4"
    # Require the memory text to actually contain an entity's name before
    # accepting the LLM's proposal. Measured on the first sample, this rejected
    # a wrong-quant model server, an unrelated container, and two entities that
    # appeared nowhere in the text. Costs some true positives on purpose: a
    # wrong edge in a knowledge graph is worse than a missing one.
    ontology_extraction_verify: bool = True

    # Coherence C6: Obsidian vault as the long-form human⇄agent surface.
    # ARIA writes plain markdown under vault/<RepoName>/{Design,Specs,Analysis,
    # Research,Planning}/; the LiveSync bridge propagates to every device.
    # Write-only by default — ARIA never reads Ben's own notes into memory
    # unless explicitly pointed at a folder.
    obsidian_enabled: bool = False
    obsidian_vault_path: str = "/home/ben/Obsidian/vault"
    # Skip (don't overwrite) a vault file a human touched within this window —
    # the ObsidianWriter's conflict guard for co-drafted docs.
    obsidian_human_edit_guard_minutes: int = 10
    # Auto-publish finished research reports into vault/<folder>/Research/
    # (decision log #5: auto for research/analysis). Requires obsidian_enabled.
    obsidian_auto_publish_research: bool = True
    # Folder for outputs not attributable to a project's repo.
    obsidian_default_folder: str = "ARIA"

    # Coherence C3: Linear backlog reconciliation. Per-project OPT-IN — only
    # repo/project slugs mapped here are synced/reconciled (map value = Linear
    # project id). Auto-resolve is threshold-gated to clearly-done tickets
    # (cited evidence, logged + reversible); ambiguous ones surface proposals.
    linear_enabled: bool = False
    linear_api_key: str = ""
    linear_sync_interval_minutes: int = 30
    linear_project_map: dict[str, str] = {}
    linear_reconcile_auto_resolve: bool = True
    linear_reconcile_propose_confidence: float = 0.75
    linear_reconcile_auto_confidence: float = 0.9

    # Self-monitoring: periodically verify DB / LLM / embeddings / extraction
    # and raise an alert (with cooldown) when something silently broke.
    selfcheck_enabled: bool = True
    selfcheck_interval_minutes: int = 10
    selfcheck_alert_cooldown_minutes: int = 60

    # Weekly heartbeat report so silence is never ambiguous (healthy vs the
    # monitor itself being dead). weekday: Mon=0..Sun=6; hour is local.
    report_enabled: bool = True
    report_weekday: int = 6
    report_hour: int = 9

    # =====================================================================
    # Steward layer (approved 2026-08-15; vault:
    # ProjectAria/Planning/ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md)
    #
    # ARIA becomes the steward of a set of chartered projects: it plans, does
    # proactive research, supervises every agent, and improves itself — while
    # Hermes is reduced to Ben's channel. Every knob below defaults OFF or to a
    # conservative value: the design principle is that a phase is enabled only
    # after its gate passes, so a fresh checkout never starts acting on its own.
    # =====================================================================

    # --- Alerts v2 / relay -------------------------------------------------
    # Alerts gain severity/kind/needs_human/dedup_key/decision. Cooldowns move
    # into Mongo because the in-memory dict reset on every aria-api restart —
    # with 37 restarts since 08-11 that turned "once per state transition" into
    # 31 duplicate `selfcheck degraded` rows.
    alerts_cooldown_persist: bool = True
    # The relay (Hermes 'ARIA outbox' cron) heartbeats through MCP. No heartbeat
    # for this long means Ben is not receiving anything — the failure that has
    # silently happened three times (2026-06-29, 07-28, 08-10).
    alert_relay_heartbeat_timeout_minutes: int = 20
    alert_relay_watchdog_enabled: bool = True
    alert_relay_watchdog_interval_seconds: int = 120
    # Break-glass (decision D5): the ONE sanctioned exception to "ARIA never
    # pushes". Only relay:dead and estop may take this path, and only via the
    # signal-cli JSON-RPC daemon Hermes already runs. ARIA's legacy SignalService
    # points at a REST API on :8088 that is not running — do not confuse them.
    alert_breakglass_enabled: bool = True
    alert_breakglass_kinds: list[str] = ["relay:dead", "estop"]
    signal_cli_rpc_url: str = "http://127.0.0.1:8090/api/v1/rpc"
    signal_breakglass_recipient: str = ""      # set in .env; empty disables sending
    signal_breakglass_account: str = ""        # the sending signal-cli account
    # An unanswered urgent decision expires to HOLD — never to APPLY.
    alert_decision_timeout_minutes: int = 240

    # --- Guard (sandbox + git rollback substrate) --------------------------
    # The guard holds the git pen: agents never commit, push or merge. See the
    # proposal §7. Enabled per-mechanism so a broken sandbox cannot block work.
    guard_enabled: bool = True
    # Wrap coding sessions in bwrap. OFF until the red-team drill passes on this
    # box (proposal P2 gate) — a sandbox that breaks pi's auth or npm cache is
    # worse than none, because it fails at agent-start with no diagnosis.
    guard_sandbox_enabled: bool = False
    guard_sandbox_backends: list[str] = ["pi-code", "claude_code", "codex"]
    # Masked with an empty tmpfs inside a session: credentials and blast radius.
    guard_sandbox_tmpfs_paths: list[str] = [
        "~/.ssh", "~/.gnupg", "~/.config/gh", "~/.docker",
        "~/.config/corsair-backup", "~/.hermes", "~/.aria", "~/git-safe",
    ]
    # Read-only inside a session: pi needs its provider table, Claude its creds.
    guard_sandbox_ro_paths: list[str] = ["~/.pi/agent/models.json", "~/.claude"]
    # Writable inside a session besides the worktree itself. pi streams its
    # structured transcript here, and losing it would blind the supervisor.
    guard_sandbox_rw_paths: list[str] = ["~/.pi/agent/sessions"]
    # Resource envelope per session. 4G, not 6G: MemAvailable runs ~11.9 GiB and
    # the co-resident model floor is 7 GiB, so the real slack is ~5 GiB.
    guard_session_memory_max: str = "4G"
    guard_session_cpu_quota: str = "400%"
    guard_min_mem_available_gib: float = 9.0
    # Git protocol.
    guard_worktree_default: bool = True        # every ARIA session gets its own worktree
    guard_checkpoint_enabled: bool = True      # real commits, not Mongo metadata
    guard_checkpoint_interval_seconds: int = 600
    guard_mirror_root: str = "~/git-safe"      # bare mirrors; push target for aria/* branches
    guard_branch_prefix: str = "aria"
    # Merge gate. A merge is proposed to Ben (A<=2) or auto (A3) only if all of
    # these pass. Gate work runs in ARIA's process, outside the sandbox.
    guard_merge_gate_enabled: bool = True
    guard_diff_max_lines: int = 400
    guard_diff_max_files: int = 20
    guard_gitleaks_enabled: bool = True
    guard_gitleaks_binary: str = "gitleaks"
    # Tamper protection (principle 12): paths no agent may modify at any
    # autonomy level. A touch is an immediate raise, not a merge rejection.
    guard_protected_paths: list[str] = [
        "api/aria/guard/**",
        "api/aria/agents/watchdog.py",
        "api/aria/agents/estop.py",
        "api/aria/core/killswitch.py",
        "api/aria/notifications/service.py",
        "api/aria/config.py",
        ".env",
        ".env.example",
        "**/.claude/settings*.json",
        "**/settings.local.json",
        "CLAUDE.md",
        "**/hooks/**",
        "api/tests/**",
        "**/evalstack/**",
    ]
    guard_policy_path: str = "guard/policy.yaml"

    # --- Charters / steward -------------------------------------------------
    steward_enabled: bool = False
    steward_interval_minutes: int = 30
    # Explicit slug, never the /llm/v1 "largest resident" auto-route: that
    # resolves to DS4, which is pi's single slot, and every steward tick would
    # evict a coding agent's warm prefix (4.2 s warm vs 39.5 s cold).
    steward_backend: str = "llamacpp"
    steward_model: str = "qwen3.8-27b-rocmfp4-r9700"
    steward_endpoint: str = "http://127.0.0.1:8080/v1"
    steward_max_actions_per_tick: int = 2
    # ⚠️ Qwen3.8 is a REASONING model: it emits `reasoning_content` before
    # `content`, so a tight token budget returns finish_reason="length" with an
    # EMPTY content string (measured 2026-08-15: a one-line reply needed 41
    # completion tokens, 17 of them reasoning; at max_tokens=24 content was ""
    # while reasoning_content was full). Every steward/research/triage call must
    # budget generously AND treat empty content as a failure — writing the empty
    # result is exactly how DS4 silently labelled every memory with zero
    # entities (see CLAUDE.md, Ontology Memory Map).
    steward_max_tokens: int = 2048
    steward_idle_days_before_pause_proposal: int = 21
    # Default per-project budget when a charter does not set one (decision D13).
    steward_default_sessions_per_day: int = 3
    steward_default_session_minutes: int = 60
    steward_default_cloud_usd_per_day: float = 2.0
    steward_default_research_runs_per_week: int = 2
    steward_default_lines_merge: int = 400

    # --- Vault (the shared notepad, read AND write) -------------------------
    # ARIA has always written to the vault and never read it. The reader is what
    # makes a phone edit of `approval:` an actual control input.
    vault_reader_enabled: bool = False
    vault_reader_interval_seconds: int = 60
    # Human-edit detection is by content hash, not mtime: the LiveSync bridge
    # rewrites mtimes, so the existing 10-minute mtime guard cannot tell a human
    # edit from a sync echo or from ARIA's own write.
    vault_hash_state_collection: str = "vault_docs"

    # --- Triage + the nudger's timer (both moved out of Hermes crons) ------
    # These two loops used to be cron PROMPTS executed by a 4B model. When that
    # model went away on 2026-08-10 the jobs errored, the gateway paused them,
    # and Ben received nothing for five days. They are ARIA workers now; the
    # flags stay off until each is exercised once.
    triage_enabled: bool = False
    triage_interval_seconds: int = 300
    triage_max_alerts_per_tick: int = 3
    triage_max_diagnoses_per_hour: int = 4
    # --- Triage CLASSIFIER (2026-08-17: moved off steward_model to gemma) ---
    # Deliberately its own setting rather than reusing steward_model, which also
    # drives the Steward service (charters, plans — currently ENABLED) and
    # agents/review.py. Repointing steward_model would have moved those onto a
    # 4B model too, which is not what was asked for and not what they need.
    #
    # Only the INFORMATIONAL-vs-FAILURE call runs here. The DIAGNOSE session is
    # separate and untouched (triage_diagnose_backend = "claude_code").
    # Classification is a bounded binary judgement over a short alert message —
    # well within a 4B model — and moving it off :8080 keeps triage from
    # competing with Hermes's interactive model.
    #
    # ⚠️ Yes, a 4B model on gemma :8104 is what this loop used to run on before
    # 2026-08-10. It failed because gemma was ABSENT (its systemd unit gated on a
    # retired :18211 endpoint and exited 1 every boot — repaired 2026-08-17), not
    # because a 4B classified badly. The failure mode is also safe by design: an
    # unusable classification returns None and LEAVES THE ALERT ALONE, so Ben
    # still receives it. A dead classifier costs a downgrade, never a delivery.
    #
    # One thing that genuinely improves here: gemma runs with `--reasoning off
    # --reasoning-budget 0`, so the empty-content reasoning-model trap described
    # under steward_max_tokens does not apply to it at all.
    triage_classify_backend: str = "llamacpp"
    triage_classify_model: str = "gemma-4-e4b-it"
    triage_classify_endpoint: str = "http://127.0.0.1:8104/v1"
    triage_classify_max_tokens: int = 512
    shells_nudge_worker_enabled: bool = False
    shells_nudge_worker_interval_minutes: int = 15

    # --- Research planner ---------------------------------------------------
    research_planner_enabled: bool = False
    research_topic_cooldown_days: int = 30
    research_daytime_prefill_cap_tokens: int = 8000
    research_night_window: str = "01:00-07:00"

    # --- Meta supervisor ----------------------------------------------------
    meta_supervisor_enabled: bool = False
    meta_supervisor_interval_seconds: int = 30
    # Stuck signals. Thresholds follow OpenHands' stuck detector, which are the
    # only published, tested values for this.
    meta_no_diff_nudges: int = 2               # nudges with no worktree diff change
    meta_repeated_error_threshold: int = 3     # identical error line repeats
    meta_tool_loop_threshold: int = 4          # identical (tool, args) repeats
    meta_alternating_loop_threshold: int = 6   # A-B-A-B tool cycles
    meta_monologue_threshold: int = 3          # turns with no tool call
    meta_crash_grace_seconds: int = 60         # exit faster than this + no diff = failed
    # Escalation ladder: how far it may climb before Ben is involved.
    meta_ladder_max_rung: int = 4              # 0 log,1 nudge,2 restart,3 re-route,4 decompose,5 raise
    meta_raises_per_project_per_day: int = 3   # 4th within 24h → auto-pause that project
    meta_worker_liveness_enabled: bool = True

    # --- Outcomes / self-improvement ---------------------------------------
    outcome_scoring_enabled: bool = True
    # Different-family review: a verifier cascade only helps when the verifiers
    # are uncorrelated, so local diffs are reviewed by the cloud tier.
    outcome_review_family: str = "cloud"
    improver_enabled: bool = False
    improver_interval_hours: int = 168          # weekly
    improver_max_proposals_per_run: int = 1
    # Only these may be rewritten by an improvement proposal. Everything in
    # guard_protected_paths is immutable regardless.
    improver_mutable_paths: list[str] = [
        "api/prompts/*.md",
    ]
    improver_eval_suite: str = "agentic_core"

    # --- Admin key split ----------------------------------------------------
    # The global API_KEY is readable by anything running as ben, including a
    # coding agent — so it must not be enough to deactivate the killswitch,
    # repoint an agent, or start/stop a model server. Those move behind
    # ADMIN_KEY, which is never placed in a session environment.
    admin_key: str = ""

    debug: bool = False

    class Config:
        env_file = (".env", "../.env")
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


settings = Settings()
