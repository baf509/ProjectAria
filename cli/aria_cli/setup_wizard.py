"""
ARIA - Setup Wizard

Purpose: Interactive terminal wizard for configuring ARIA's .env file

Related Spec Sections:
- Section 10.2: Pydantic Settings
"""

import os
import secrets
import subprocess
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()

# Project root is two levels up from this file (cli/aria_cli/setup_wizard.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
INFRA_ROOT = Path("/home/ben/Dev/infrastructure")


def _mask_key(key: str) -> str:
    """Show first 8 + last 4 chars of a key for display."""
    if len(key) <= 12:
        return "***"
    return key[:8] + "..." + key[-4:]


def _read_existing_env(path: Path) -> dict[str, str]:
    """Parse an existing .env file into a dict, ignoring comments and blanks."""
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Write .env file with grouped comments."""
    groups = [
        ("MongoDB", ["MONGODB_URI", "MONGODB_DATABASE"]),
        ("LLM - Local", ["LLAMACPP_URL", "LLAMACPP_API_KEY"]),
        ("LLM - Cloud (optional)", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"]),
        ("Embeddings", ["EMBEDDING_URL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSION", "VOYAGE_API_KEY"]),
        ("Voice Services (optional)", ["TTS_URL", "STT_URL"]),
        ("Signal (optional)", ["SIGNAL_ENABLED", "SIGNAL_REST_URL", "SIGNAL_ACCOUNT", "SIGNAL_DM_POLICY"]),
        ("Telegram (optional)", [
            "TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_DM_POLICY",
            "TELEGRAM_ALLOWED_USERS", "TELEGRAM_POLL_INTERVAL_SECONDS",
        ]),
        ("Search (optional)", ["BRAVE_SEARCH_API_KEY"]),
        ("Screenshot (optional)", ["SCREENSHOT_COMMAND", "SCREENSHOT_VISION_BACKEND", "SCREENSHOT_VISION_MODEL"]),
        ("Document Generation (optional)", ["DOCGEN_OUTPUT_DIR"]),
        ("Skills (optional)", ["SKILLS_DIR"]),
        ("Autopilot (optional)", ["AUTOPILOT_MAX_STEPS", "AUTOPILOT_STEP_TIMEOUT_SECONDS"]),
        ("OODA Self-Correction (optional)", ["OODA_DEFAULT_THRESHOLD", "OODA_DEFAULT_MAX_RETRIES"]),
        ("Group Chat (optional)", ["GROUPCHAT_DEFAULT_ROUNDS", "GROUPCHAT_MAX_PERSONAS"]),
        ("API", ["API_HOST", "API_PORT", "DEBUG", "API_AUTH_ENABLED", "API_KEY", "ENCRYPTION_KEY"]),
    ]

    lines = []
    written_keys = set()
    for group_name, keys in groups:
        lines.append(f"# {group_name}")
        for key in keys:
            if key in values:
                lines.append(f"{key}={values[key]}")
                written_keys.add(key)
        lines.append("")

    # Write any remaining keys not covered by groups
    remaining = {k: v for k, v in values.items() if k not in written_keys}
    if remaining:
        lines.append("# Other")
        for key, val in remaining.items():
            lines.append(f"{key}={val}")
        lines.append("")

    path.write_text("\n".join(lines))


def _check_endpoint(url: str, timeout: float = 5.0) -> bool:
    """Test connectivity to an HTTP endpoint."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        return resp.status_code < 500
    except Exception:
        return False


def _prompt(label: str, default: str = "", password: bool = False) -> str:
    """Prompt for a value with optional default and masking."""
    if password and default:
        display_default = _mask_key(default)
        val = Prompt.ask(label, default=display_default)
        # If user just pressed Enter, keep existing value
        if val == display_default:
            return default
        return val
    return Prompt.ask(label, default=default or None) or default


def _section_prerequisites() -> None:
    """Check Docker, docker compose v2, and shared-infra network."""
    console.print(Panel("[bold]1/12  Prerequisites[/bold]", style="blue"))

    checks = []

    # Docker daemon
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
        checks.append(("Docker daemon", True))
    except Exception:
        checks.append(("Docker daemon", False))

    # docker compose v2
    try:
        result = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=10
        )
        checks.append(("docker compose v2", result.returncode == 0))
    except Exception:
        checks.append(("docker compose v2", False))

    # shared-infra network
    try:
        result = subprocess.run(
            ["docker", "network", "inspect", "shared-infra"],
            capture_output=True, timeout=10,
        )
        checks.append(("shared-infra network", result.returncode == 0))
    except Exception:
        checks.append(("shared-infra network", False))

    table = Table(show_header=False)
    table.add_column("Check")
    table.add_column("Status")
    for name, ok in checks:
        icon = "[green]OK[/green]" if ok else "[red]MISSING[/red]"
        table.add_row(name, icon)
    console.print(table)

    if not all(ok for _, ok in checks):
        console.print(
            "[yellow]Warning:[/yellow] Some prerequisites are missing. "
            "See CLAUDE.md for setup instructions. Continuing anyway...\n"
        )
    else:
        console.print()


def _section_mongodb(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure MongoDB connection."""
    console.print(Panel("[bold]2/12  MongoDB[/bold]", style="blue"))

    env["MONGODB_URI"] = _prompt(
        "MongoDB URI",
        default=existing.get("MONGODB_URI", "mongodb://mongod:27017/?directConnection=true&replicaSet=rs0"),
    )
    env["MONGODB_DATABASE"] = _prompt(
        "Database name",
        default=existing.get("MONGODB_DATABASE", "aria"),
    )

    # Test connection if mongosh available
    try:
        result = subprocess.run(
            ["mongosh", "--eval", "db.runCommand({ping:1})", env["MONGODB_URI"].replace("mongod", "localhost")],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            console.print("[green]OK[/green] MongoDB connection verified\n")
        else:
            console.print("[yellow]Warning:[/yellow] Could not connect (mongosh test failed). This is normal if MongoDB runs in Docker.\n")
    except FileNotFoundError:
        console.print("[dim]mongosh not found — skipping connection test[/dim]\n")
    except Exception:
        console.print("[dim]Connection test skipped[/dim]\n")


def _section_llm(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure LLM backends."""
    console.print(Panel("[bold]3/12  LLM Backends[/bold]", style="blue"))

    env["LLAMACPP_URL"] = _prompt(
        "llama.cpp URL",
        default=existing.get("LLAMACPP_URL", "http://llamacpp:8080/v1"),
    )
    env["LLAMACPP_API_KEY"] = _prompt(
        "llama.cpp API key (optional)",
        default=existing.get("LLAMACPP_API_KEY", ""),
        password=True,
    )

    console.print("\n[dim]Cloud LLM keys (all optional — press Enter to skip):[/dim]")
    env["ANTHROPIC_API_KEY"] = _prompt(
        "Anthropic API key",
        default=existing.get("ANTHROPIC_API_KEY", ""),
        password=True,
    )
    env["OPENAI_API_KEY"] = _prompt(
        "OpenAI API key",
        default=existing.get("OPENAI_API_KEY", ""),
        password=True,
    )
    env["OPENROUTER_API_KEY"] = _prompt(
        "OpenRouter API key",
        default=existing.get("OPENROUTER_API_KEY", ""),
        password=True,
    )
    console.print()


def _section_embeddings(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure embeddings."""
    console.print(Panel("[bold]4/12  Embeddings[/bold]", style="blue"))

    env["EMBEDDING_URL"] = _prompt(
        "Embeddings URL",
        default=existing.get("EMBEDDING_URL", "http://embeddings:8001/v1"),
    )
    # Fixed values — not editable
    env["EMBEDDING_MODEL"] = "voyageai/voyage-4-nano"
    env["EMBEDDING_DIMENSION"] = "1024"
    console.print(f"[dim]Model: {env['EMBEDDING_MODEL']} (1024-dim, fixed)[/dim]")

    env["VOYAGE_API_KEY"] = _prompt(
        "Voyage API key (optional, for cloud embeddings)",
        default=existing.get("VOYAGE_API_KEY", ""),
        password=True,
    )
    console.print()


def _section_voice(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure voice services."""
    console.print(Panel("[bold]5/12  Voice Services[/bold]", style="blue"))

    env["TTS_URL"] = _prompt(
        "TTS URL (optional)",
        default=existing.get("TTS_URL", "http://tts:8002/v1"),
    )
    env["STT_URL"] = _prompt(
        "STT URL (optional)",
        default=existing.get("STT_URL", "http://stt:8003/v1"),
    )
    console.print()


def _section_signal(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure Signal integration."""
    console.print(Panel("[bold]6/12  Signal Integration[/bold]", style="blue"))

    enabled = Confirm.ask(
        "Enable Signal integration?",
        default=existing.get("SIGNAL_ENABLED", "false").lower() == "true",
    )
    env["SIGNAL_ENABLED"] = str(enabled).lower()

    if enabled:
        env["SIGNAL_REST_URL"] = _prompt(
            "Signal REST API URL",
            default=existing.get("SIGNAL_REST_URL", "http://signal-cli:8080"),
        )
        env["SIGNAL_ACCOUNT"] = _prompt(
            "Signal phone number (e.g. +1234567890)",
            default=existing.get("SIGNAL_ACCOUNT", ""),
        )
        env["SIGNAL_DM_POLICY"] = _prompt(
            "DM policy (allowlist/open)",
            default=existing.get("SIGNAL_DM_POLICY", "allowlist"),
        )
    console.print()


def _section_telegram(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure Telegram bot integration."""
    console.print(Panel("[bold]7/12  Telegram Bot[/bold]", style="blue"))

    enabled = Confirm.ask(
        "Enable Telegram bot integration?",
        default=existing.get("TELEGRAM_ENABLED", "false").lower() == "true",
    )
    env["TELEGRAM_ENABLED"] = str(enabled).lower()

    if enabled:
        env["TELEGRAM_BOT_TOKEN"] = _prompt(
            "Telegram bot token (from @BotFather)",
            default=existing.get("TELEGRAM_BOT_TOKEN", ""),
            password=True,
        )
        env["TELEGRAM_DM_POLICY"] = _prompt(
            "DM policy (allowlist/open)",
            default=existing.get("TELEGRAM_DM_POLICY", "allowlist"),
        )
        env["TELEGRAM_ALLOWED_USERS"] = _prompt(
            "Allowed Telegram usernames (comma-separated, no @)",
            default=existing.get("TELEGRAM_ALLOWED_USERS", ""),
        )
        env["TELEGRAM_POLL_INTERVAL_SECONDS"] = _prompt(
            "Poll interval (seconds)",
            default=existing.get("TELEGRAM_POLL_INTERVAL_SECONDS", "5"),
        )
    console.print()


def _section_features(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure new feature settings."""
    console.print(Panel("[bold]10/12  Features[/bold]", style="blue"))

    console.print("[dim]Screenshot analysis (requires display + scrot):[/dim]")
    env["SCREENSHOT_COMMAND"] = _prompt(
        "Screenshot command",
        default=existing.get("SCREENSHOT_COMMAND", "scrot"),
    )
    env["SCREENSHOT_VISION_BACKEND"] = _prompt(
        "Vision LLM backend",
        default=existing.get("SCREENSHOT_VISION_BACKEND", "anthropic"),
    )
    env["SCREENSHOT_VISION_MODEL"] = _prompt(
        "Vision LLM model",
        default=existing.get("SCREENSHOT_VISION_MODEL", "claude-sonnet-4-20250514"),
    )

    console.print("\n[dim]Document generation:[/dim]")
    env["DOCGEN_OUTPUT_DIR"] = _prompt(
        "Document output directory",
        default=existing.get("DOCGEN_OUTPUT_DIR", "~/aria-documents"),
    )

    console.print("\n[dim]Skill packages:[/dim]")
    env["SKILLS_DIR"] = _prompt(
        "Skills directory",
        default=existing.get("SKILLS_DIR", "~/.aria/skills/"),
    )

    console.print("\n[dim]Autopilot mode:[/dim]")
    env["AUTOPILOT_MAX_STEPS"] = _prompt(
        "Max autopilot steps",
        default=existing.get("AUTOPILOT_MAX_STEPS", "20"),
    )
    env["AUTOPILOT_STEP_TIMEOUT_SECONDS"] = _prompt(
        "Step timeout (seconds)",
        default=existing.get("AUTOPILOT_STEP_TIMEOUT_SECONDS", "300"),
    )

    console.print("\n[dim]OODA self-correction:[/dim]")
    env["OODA_DEFAULT_THRESHOLD"] = _prompt(
        "Quality threshold (0.0-1.0)",
        default=existing.get("OODA_DEFAULT_THRESHOLD", "0.7"),
    )
    env["OODA_DEFAULT_MAX_RETRIES"] = _prompt(
        "Max retries",
        default=existing.get("OODA_DEFAULT_MAX_RETRIES", "2"),
    )

    console.print("\n[dim]Group chat / debate:[/dim]")
    env["GROUPCHAT_DEFAULT_ROUNDS"] = _prompt(
        "Default debate rounds",
        default=existing.get("GROUPCHAT_DEFAULT_ROUNDS", "3"),
    )
    env["GROUPCHAT_MAX_PERSONAS"] = _prompt(
        "Max personas per session",
        default=existing.get("GROUPCHAT_MAX_PERSONAS", "6"),
    )
    console.print()


def _section_search(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure search."""
    console.print(Panel("[bold]8/12  Web Search[/bold]", style="blue"))

    env["BRAVE_SEARCH_API_KEY"] = _prompt(
        "Brave Search API key (optional)",
        default=existing.get("BRAVE_SEARCH_API_KEY", ""),
        password=True,
    )
    console.print()


def _section_api(existing: dict[str, str], env: dict[str, str]) -> None:
    """Configure API security."""
    console.print(Panel("[bold]9/12  API Security[/bold]", style="blue"))

    env["API_HOST"] = _prompt(
        "API host",
        default=existing.get("API_HOST", "0.0.0.0"),
    )
    env["API_PORT"] = _prompt(
        "API port",
        default=existing.get("API_PORT", "8000"),
    )

    auth = Confirm.ask(
        "Enable API authentication?",
        default=existing.get("API_AUTH_ENABLED", "true").lower() == "true",
    )
    env["API_AUTH_ENABLED"] = str(auth).lower()

    default_key = existing.get("API_KEY", "") or secrets.token_hex(32)
    env["API_KEY"] = _prompt(
        "API key",
        default=default_key,
        password=True,
    )

    default_enc = existing.get("ENCRYPTION_KEY", "") or secrets.token_hex(32)
    env["ENCRYPTION_KEY"] = _prompt(
        "Encryption key",
        default=default_enc,
        password=True,
    )

    env["DEBUG"] = _prompt(
        "Debug mode",
        default=existing.get("DEBUG", "false"),
    )
    console.print()


def _section_verify(env: dict[str, str]) -> None:
    """Test connectivity to all configured endpoints."""
    console.print(Panel("[bold]12/12  Connectivity Check[/bold]", style="blue"))

    endpoints = []
    for label, key in [
        ("llama.cpp", "LLAMACPP_URL"),
        ("Embeddings", "EMBEDDING_URL"),
        ("TTS", "TTS_URL"),
        ("STT", "STT_URL"),
    ]:
        url = env.get(key, "")
        if url:
            # For Docker URLs, try localhost equivalent too
            test_url = url
            for container in ("llamacpp", "embeddings", "tts", "stt", "mongod"):
                test_url = test_url.replace(f"http://{container}:", "http://localhost:")
            endpoints.append((label, test_url))

    if not endpoints:
        console.print("[dim]No endpoints to verify[/dim]\n")
        return

    table = Table(show_header=True)
    table.add_column("Service")
    table.add_column("URL")
    table.add_column("Status")

    for label, url in endpoints:
        ok = _check_endpoint(url)
        status = "[green]OK[/green]" if ok else "[yellow]Unreachable[/yellow]"
        table.add_row(label, url, status)

    console.print(table)
    console.print("[dim]Unreachable services may be accessible from within Docker.[/dim]\n")


@click.command("setup")
@click.option("--non-interactive", is_flag=True, help="Accept all defaults without prompting")
def setup(non_interactive: bool) -> None:
    """Interactive setup wizard for ARIA configuration."""
    console.print(Panel(
        "[bold cyan]ARIA Setup Wizard[/bold cyan]\n\n"
        "This wizard will walk you through configuring ARIA.\n"
        "Press Enter to accept defaults shown in brackets.",
        style="cyan",
    ))

    existing = _read_existing_env(ENV_PATH)
    env: dict[str, str] = {}

    if non_interactive:
        # Populate all values from existing or defaults
        env.update({
            "MONGODB_URI": existing.get("MONGODB_URI", "mongodb://mongod:27017/?directConnection=true&replicaSet=rs0"),
            "MONGODB_DATABASE": existing.get("MONGODB_DATABASE", "aria"),
            "LLAMACPP_URL": existing.get("LLAMACPP_URL", "http://llamacpp:8080/v1"),
            "LLAMACPP_API_KEY": existing.get("LLAMACPP_API_KEY", ""),
            "ANTHROPIC_API_KEY": existing.get("ANTHROPIC_API_KEY", ""),
            "OPENAI_API_KEY": existing.get("OPENAI_API_KEY", ""),
            "OPENROUTER_API_KEY": existing.get("OPENROUTER_API_KEY", ""),
            "EMBEDDING_URL": existing.get("EMBEDDING_URL", "http://embeddings:8001/v1"),
            "EMBEDDING_MODEL": "voyageai/voyage-4-nano",
            "EMBEDDING_DIMENSION": "1024",
            "VOYAGE_API_KEY": existing.get("VOYAGE_API_KEY", ""),
            "TTS_URL": existing.get("TTS_URL", "http://tts:8002/v1"),
            "STT_URL": existing.get("STT_URL", "http://stt:8003/v1"),
            "SIGNAL_ENABLED": existing.get("SIGNAL_ENABLED", "false"),
            "SIGNAL_REST_URL": existing.get("SIGNAL_REST_URL", "http://signal-cli:8080"),
            "SIGNAL_ACCOUNT": existing.get("SIGNAL_ACCOUNT", ""),
            "SIGNAL_DM_POLICY": existing.get("SIGNAL_DM_POLICY", "allowlist"),
            "TELEGRAM_ENABLED": existing.get("TELEGRAM_ENABLED", "false"),
            "TELEGRAM_BOT_TOKEN": existing.get("TELEGRAM_BOT_TOKEN", ""),
            "TELEGRAM_DM_POLICY": existing.get("TELEGRAM_DM_POLICY", "allowlist"),
            "TELEGRAM_ALLOWED_USERS": existing.get("TELEGRAM_ALLOWED_USERS", ""),
            "TELEGRAM_POLL_INTERVAL_SECONDS": existing.get("TELEGRAM_POLL_INTERVAL_SECONDS", "5"),
            "BRAVE_SEARCH_API_KEY": existing.get("BRAVE_SEARCH_API_KEY", ""),
            "SCREENSHOT_COMMAND": existing.get("SCREENSHOT_COMMAND", "scrot"),
            "SCREENSHOT_VISION_BACKEND": existing.get("SCREENSHOT_VISION_BACKEND", "anthropic"),
            "SCREENSHOT_VISION_MODEL": existing.get("SCREENSHOT_VISION_MODEL", "claude-sonnet-4-20250514"),
            "DOCGEN_OUTPUT_DIR": existing.get("DOCGEN_OUTPUT_DIR", "~/aria-documents"),
            "SKILLS_DIR": existing.get("SKILLS_DIR", "~/.aria/skills/"),
            "AUTOPILOT_MAX_STEPS": existing.get("AUTOPILOT_MAX_STEPS", "20"),
            "AUTOPILOT_STEP_TIMEOUT_SECONDS": existing.get("AUTOPILOT_STEP_TIMEOUT_SECONDS", "300"),
            "OODA_DEFAULT_THRESHOLD": existing.get("OODA_DEFAULT_THRESHOLD", "0.7"),
            "OODA_DEFAULT_MAX_RETRIES": existing.get("OODA_DEFAULT_MAX_RETRIES", "2"),
            "GROUPCHAT_DEFAULT_ROUNDS": existing.get("GROUPCHAT_DEFAULT_ROUNDS", "3"),
            "GROUPCHAT_MAX_PERSONAS": existing.get("GROUPCHAT_MAX_PERSONAS", "6"),
            "API_HOST": existing.get("API_HOST", "0.0.0.0"),
            "API_PORT": existing.get("API_PORT", "8000"),
            "API_AUTH_ENABLED": existing.get("API_AUTH_ENABLED", "true"),
            "API_KEY": existing.get("API_KEY", "") or secrets.token_hex(32),
            "ENCRYPTION_KEY": existing.get("ENCRYPTION_KEY", "") or secrets.token_hex(32),
            "DEBUG": existing.get("DEBUG", "false"),
        })
        # Preserve any extra keys from existing .env
        for k, v in existing.items():
            if k not in env:
                env[k] = v
        _write_env(ENV_PATH, env)
        console.print(f"[green]OK[/green] Wrote {ENV_PATH}")
        return

    # Interactive sections
    _section_prerequisites()
    _section_mongodb(existing, env)
    _section_llm(existing, env)
    _section_embeddings(existing, env)
    _section_voice(existing, env)
    _section_signal(existing, env)
    _section_telegram(existing, env)
    _section_search(existing, env)
    _section_api(existing, env)
    _section_features(existing, env)

    # Preserve any extra keys from existing .env
    for k, v in existing.items():
        if k not in env:
            env[k] = v

    # Write .env
    console.print(Panel("[bold]11/12  Writing .env[/bold]", style="blue"))
    _write_env(ENV_PATH, env)
    console.print(f"[green]OK[/green] Wrote {ENV_PATH}\n")

    # Verify
    _section_verify(env)

    # Offer systemd install
    if Confirm.ask("Install systemd services for auto-start on boot?", default=False):
        from aria_cli.service import install_services
        install_services(dry_run=False)

    console.print(Panel(
        "[bold green]Setup complete![/bold green]\n\n"
        "Next steps:\n"
        "  1. Start shared infrastructure:  cd /home/ben/Dev/infrastructure && docker compose up -d\n"
        "  2. Start ARIA:                   cd /home/ben/Dev/ProjectAria && docker compose up -d\n"
        "  3. Verify:                        aria health",
        style="green",
    ))
