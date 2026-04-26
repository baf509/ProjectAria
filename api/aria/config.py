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
    llamacpp_url: str = "http://localhost:8080/v1"
    llamacpp_api_key: str = ""

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
        "sed",
        "awk",
        "rg",
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
    heartbeat_backend: str = ""
    heartbeat_model: str = ""
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
    shells_input_rate_limit_per_minute: int = 30
    shells_retention_days: int = 0  # 0 = keep forever
    shells_auto_archive_days: int = 7

    # Planning subsystem (tasks + projects)
    # Ambient capture runs an LLM call after each non-private conversation
    # turn. Disable to require manual /api/v1/todos creation only.
    planning_ambient_capture_enabled: bool = True
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

    # APNs (Phase 6 — mobile push). Disabled by default; set
    # `shells_apns_enabled=true` and configure the key + team/bundle IDs to
    # start sending idle alerts to registered iOS devices.
    shells_apns_enabled: bool = False
    apns_team_id: str = ""
    apns_key_id: str = ""
    apns_bundle_id: str = "dev.aria.AriaMobile"
    apns_auth_key_path: str = ""
    apns_use_sandbox: bool = True

    debug: bool = False

    class Config:
        env_file = (".env", "../.env")
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


settings = Settings()
