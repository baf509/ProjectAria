"""
ARIA - Systemd Service Management

Purpose: Install, manage, and monitor systemd services for ARIA and shared infrastructure

Related Spec Sections:
- Section 10: Deployment
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
INFRA_ROOT = Path("/home/ben/Dev/infrastructure")

UNIT_DIR = Path("/etc/systemd/system")
INFRA_UNIT = "aria-infra.service"
ARIA_UNIT = "aria.service"


def _generate_infra_unit() -> str:
    """Generate the aria-infra.service unit file."""
    return textwrap.dedent(f"""\
        [Unit]
        Description=ARIA Shared Infrastructure (MongoDB, llama.cpp, Embeddings)
        After=docker.service
        Requires=docker.service

        [Service]
        Type=oneshot
        RemainAfterExit=yes
        User=ben
        WorkingDirectory={INFRA_ROOT}
        ExecStartPre=/usr/bin/docker network create shared-infra --driver bridge || true
        ExecStart=/usr/bin/docker compose up -d
        ExecStop=/usr/bin/docker compose down
        TimeoutStartSec=120

        [Install]
        WantedBy=multi-user.target
    """)


def _generate_aria_unit() -> str:
    """Generate the aria.service unit file."""
    return textwrap.dedent(f"""\
        [Unit]
        Description=ARIA AI Agent Platform
        After=docker.service aria-infra.service
        Wants=aria-infra.service
        Requires=docker.service

        [Service]
        Type=oneshot
        RemainAfterExit=yes
        User=ben
        WorkingDirectory={PROJECT_ROOT}
        ExecStart=/usr/bin/docker compose up -d
        ExecStop=/usr/bin/docker compose down
        TimeoutStartSec=120

        [Install]
        WantedBy=multi-user.target
    """)


def _run_sudo(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command with sudo."""
    return subprocess.run(["sudo"] + args, check=check, text=True, capture_output=True)


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run systemctl with sudo."""
    return _run_sudo(["systemctl"] + list(args), check=check)


def install_services(dry_run: bool = False) -> None:
    """Install and enable systemd services. Callable from setup wizard."""
    infra_content = _generate_infra_unit()
    aria_content = _generate_aria_unit()

    if dry_run:
        console.print(Panel(f"[bold]{INFRA_UNIT}[/bold]\n\n{infra_content}", style="cyan"))
        console.print(Panel(f"[bold]{ARIA_UNIT}[/bold]\n\n{aria_content}", style="cyan"))
        console.print("[dim]Dry run — no files written[/dim]")
        return

    console.print("[yellow]Installing systemd services requires sudo.[/yellow]")

    # Write unit files via sudo tee
    for name, content in [(INFRA_UNIT, infra_content), (ARIA_UNIT, aria_content)]:
        target = UNIT_DIR / name
        proc = subprocess.run(
            ["sudo", "tee", str(target)],
            input=content, text=True, capture_output=True,
        )
        if proc.returncode != 0:
            console.print(f"[red]Error writing {target}:[/red] {proc.stderr}")
            return
        console.print(f"[green]OK[/green] Wrote {target}")

    # Reload and enable
    _systemctl("daemon-reload")
    _systemctl("enable", INFRA_UNIT)
    _systemctl("enable", ARIA_UNIT)
    console.print("[green]OK[/green] Services enabled for auto-start on boot")


@click.group("service")
def service():
    """Manage ARIA systemd services."""
    pass


@service.command("install")
@click.option("--dry-run", is_flag=True, help="Show generated unit files without installing")
def install_cmd(dry_run: bool) -> None:
    """Install and enable systemd services."""
    install_services(dry_run=dry_run)


@service.command("uninstall")
def uninstall_cmd() -> None:
    """Disable, stop, and remove systemd services."""
    console.print("[yellow]Removing systemd services requires sudo.[/yellow]")

    for unit in (ARIA_UNIT, INFRA_UNIT):
        _systemctl("disable", "--now", unit, check=False)
        target = UNIT_DIR / unit
        _run_sudo(["rm", "-f", str(target)], check=False)
        console.print(f"[green]OK[/green] Removed {unit}")

    _systemctl("daemon-reload")
    console.print("[green]OK[/green] Systemd reloaded")


def _resolve_targets(target: str) -> list[str]:
    """Map a target name to systemd unit names."""
    if target == "all":
        return [INFRA_UNIT, ARIA_UNIT]
    elif target == "infra":
        return [INFRA_UNIT]
    elif target == "aria":
        return [ARIA_UNIT]
    else:
        console.print(f"[red]Unknown target:[/red] {target} (use all, infra, or aria)")
        sys.exit(1)


@service.command("start")
@click.argument("target", default="all", type=click.Choice(["all", "infra", "aria"]))
def start_cmd(target: str) -> None:
    """Start services via systemctl."""
    for unit in _resolve_targets(target):
        try:
            _systemctl("start", unit)
            console.print(f"[green]OK[/green] Started {unit}")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Error starting {unit}:[/red] {e.stderr}")


@service.command("stop")
@click.argument("target", default="all", type=click.Choice(["all", "infra", "aria"]))
def stop_cmd(target: str) -> None:
    """Stop services via systemctl."""
    for unit in reversed(_resolve_targets(target)):
        try:
            _systemctl("stop", unit)
            console.print(f"[green]OK[/green] Stopped {unit}")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Error stopping {unit}:[/red] {e.stderr}")


@service.command("restart")
@click.argument("target", default="all", type=click.Choice(["all", "infra", "aria"]))
def restart_cmd(target: str) -> None:
    """Restart services via systemctl."""
    for unit in _resolve_targets(target):
        try:
            _systemctl("restart", unit)
            console.print(f"[green]OK[/green] Restarted {unit}")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Error restarting {unit}:[/red] {e.stderr}")


@service.command("status")
def status_cmd() -> None:
    """Show systemd state and docker compose status."""
    table = Table(title="Systemd Units")
    table.add_column("Unit")
    table.add_column("Loaded")
    table.add_column("Active")
    table.add_column("Enabled")

    for unit in (INFRA_UNIT, ARIA_UNIT):
        loaded = "unknown"
        active = "unknown"
        enabled = "unknown"

        result = subprocess.run(
            ["systemctl", "show", unit, "--property=LoadState,ActiveState,UnitFileState"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            props = {}
            for line in result.stdout.strip().splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    props[k] = v
            loaded = props.get("LoadState", "unknown")
            active = props.get("ActiveState", "unknown")
            enabled = props.get("UnitFileState", "unknown")

        active_style = "green" if active == "active" else "red" if active == "failed" else "yellow"
        table.add_row(unit, loaded, f"[{active_style}]{active}[/{active_style}]", enabled)

    console.print(table)

    # Docker compose status for each project
    for label, workdir in [("Infrastructure", INFRA_ROOT), ("ARIA", PROJECT_ROOT)]:
        if not (workdir / "docker-compose.yml").exists():
            continue
        console.print(f"\n[bold]{label}[/bold] containers:")
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "table"],
            capture_output=True, text=True, cwd=workdir,
        )
        if result.returncode == 0 and result.stdout.strip():
            console.print(result.stdout.strip())
        else:
            console.print("[dim]No containers running[/dim]")


@service.command("logs")
@click.option("-f", "--follow", is_flag=True, help="Follow log output")
@click.option("-n", "--lines", default=50, help="Number of lines to show")
@click.argument("target", default="all", type=click.Choice(["all", "infra", "aria"]))
def logs_cmd(follow: bool, lines: int, target: str) -> None:
    """Tail docker compose logs."""
    targets = []
    if target in ("all", "infra"):
        targets.append(("Infrastructure", INFRA_ROOT))
    if target in ("all", "aria"):
        targets.append(("ARIA", PROJECT_ROOT))

    for label, workdir in targets:
        if not (workdir / "docker-compose.yml").exists():
            console.print(f"[yellow]{label}:[/yellow] docker-compose.yml not found at {workdir}")
            continue

        console.print(f"\n[bold]--- {label} logs ---[/bold]")
        cmd = ["docker", "compose", "logs", f"--tail={lines}"]
        if follow:
            cmd.append("--follow")

        try:
            # For follow mode, run interactively; otherwise capture
            if follow:
                subprocess.run(cmd, cwd=workdir)
            else:
                result = subprocess.run(cmd, capture_output=True, text=True, cwd=workdir)
                if result.stdout:
                    console.print(result.stdout.rstrip())
                if result.stderr:
                    console.print(result.stderr.rstrip())
        except KeyboardInterrupt:
            pass
