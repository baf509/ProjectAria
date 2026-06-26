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
    llamacpp_url: str = "http://localhost:8092/v1"
    llamacpp_api_key: str = ""
    # Hard wall-clock cap on a single LLM call. The SDK default (600s) lets a
    # busy/half-open local server wedge a caller for ~10min; a hang never raises
    # so retry_async can't recover it.
    llamacpp_timeout_seconds: int = 120

    # Chroma context-1 (local agentic search model served by a second llama.cpp)
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
    coding_default_backend: str = "codex"
    coding_default_workspace: str = "/home/ben/Development/aria-projects"
    coding_output_lines: int = 500
    # Run ARIA-spawned coding sessions on the watched-shell substrate (a tmux
    # session that auto-adopts + captures to shell_events), so a sub-agent IS a
    # shell — unified with the fleet, drivable via the same tools, and visible in
    # the TUI/MCP. The watchdog/checkpoint/review overlay still manages it. Set
    # false to fall back to the legacy raw-subprocess substrate.
    coding_use_shell_substrate: bool = True
    coding_watchdog_interval_seconds: int = 5
    coding_stall_seconds: int = 60
    coding_auto_respond_prompts: bool = False
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

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_auth_enabled: bool = True
    api_key: str = "aria-local-admin-key"
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
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
    tool_allowed_names: list[str] = [
        "web",
        "list_coding_sessions",
        "get_coding_output",
        "get_coding_diff",
        "list_llamacpp_models",
        "update_soul",
        "claude_agent",
        "pi_coding_agent",
        "deep_think",
        "search_agent",
    ]
    tool_denied_names: list[str] = []
    tool_sensitive_names: list[str] = ["shell", "filesystem", "switch_llamacpp_model"]
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
    # Screenshot
    screenshot_command: str = "scrot"
    screenshot_vision_backend: str = "anthropic"
    screenshot_vision_model: str = "claude-sonnet-4-20250514"

    # Document generation
    docgen_output_dir: str = "~/aria-documents"

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

    # Telegram
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_allowed_users: list[str] = []
    telegram_dm_policy: str = "allowlist"
    telegram_poll_interval_seconds: int = 5

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
    shells_claude_launch_command: str = (
        "bash -lc 'claude --dangerously-skip-permissions'"
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

    # Pre-seed Claude Code's folder-trust flag before launching a shell so the
    # blocking "Do you trust the files in this folder?" dialog never appears.
    shells_claude_autotrust: bool = True
    shells_claude_config_path: str = ""  # defaults to ~/.claude.json if empty

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

    debug: bool = False

    class Config:
        env_file = (".env", "../.env")
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


settings = Settings()
