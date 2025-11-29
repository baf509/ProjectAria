#!/usr/bin/env python3
"""
ARIA CLI - Main Entry Point

Phase: 1
Purpose: CLI commands for interacting with ARIA API

Related Spec Sections:
- Section 8: Phase 1 - CLI Client
"""

import json
import sys
from datetime import datetime

import click
import httpx
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.markdown import Markdown

console = Console()


class AriaClient:
    """ARIA API client."""

    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=120.0)

    def health_check(self):
        """Check API health."""
        response = self.client.get(f"{self.base_url}/api/v1/health")
        response.raise_for_status()
        return response.json()

    def list_conversations(self, limit: int = 50):
        """List conversations."""
        response = self.client.get(
            f"{self.base_url}/api/v1/conversations", params={"limit": limit}
        )
        response.raise_for_status()
        return response.json()

    def create_conversation(self, title: str = None):
        """Create a new conversation."""
        data = {}
        if title:
            data["title"] = title

        response = self.client.post(
            f"{self.base_url}/api/v1/conversations", json=data
        )
        response.raise_for_status()
        return response.json()

    def get_conversation(self, conversation_id: str):
        """Get a conversation."""
        response = self.client.get(
            f"{self.base_url}/api/v1/conversations/{conversation_id}"
        )
        response.raise_for_status()
        return response.json()

    def send_message(self, conversation_id: str, message: str):
        """Send a message and stream the response."""
        response = self.client.post(
            f"{self.base_url}/api/v1/conversations/{conversation_id}/messages",
            json={"content": message, "stream": True},
            headers={"Accept": "text/event-stream"},
        )
        response.raise_for_status()
        return response

    def list_agents(self):
        """List agents."""
        response = self.client.get(f"{self.base_url}/api/v1/agents")
        response.raise_for_status()
        return response.json()


@click.group()
@click.version_option(version="0.2.0")
def cli():
    """ARIA - Local AI Agent Platform"""
    pass


@cli.command()
def health():
    """Check ARIA API health."""
    try:
        client = AriaClient()
        result = client.health_check()
        console.print(f"[green]✓[/green] Status: {result['status']}")
        console.print(f"[green]✓[/green] Version: {result['version']}")
        console.print(f"[green]✓[/green] Database: {result['database']}")
    except Exception as e:
        console.print(f"[red]✗[/red] Error: {str(e)}", style="red")
        sys.exit(1)


@cli.group()
def conversations():
    """Manage conversations."""
    pass


@conversations.command("list")
@click.option("--limit", default=50, help="Number of conversations to show")
def list_conversations_cmd(limit):
    """List conversations."""
    try:
        client = AriaClient()
        convos = client.list_conversations(limit=limit)

        if not convos:
            console.print("No conversations found.")
            return

        table = Table(title="Conversations")
        table.add_column("ID", style="cyan")
        table.add_column("Title", style="green")
        table.add_column("Messages", justify="right")
        table.add_column("Updated", style="dim")

        for convo in convos:
            table.add_row(
                convo["id"][:8] + "...",
                convo["title"],
                str(convo["stats"]["message_count"]),
                convo["updated_at"][:10],
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@conversations.command("create")
@click.option("--title", help="Conversation title")
def create_conversation_cmd(title):
    """Create a new conversation."""
    try:
        client = AriaClient()
        convo = client.create_conversation(title=title)
        console.print(
            f"[green]✓[/green] Created conversation: {convo['id']} - {convo['title']}"
        )
        console.print(f"Use: aria chat --conversation {convo['id']} \"your message\"")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@cli.command()
@click.argument("message", required=False)
@click.option("--conversation", "-c", help="Conversation ID to continue")
@click.option("--new", is_flag=True, help="Start a new conversation")
def chat(message, conversation, new):
    """Chat with ARIA."""
    client = AriaClient()

    try:
        # Get or create conversation
        if new or not conversation:
            if new:
                convo = client.create_conversation()
                conversation = convo["id"]
                console.print(
                    f"[dim]Started new conversation: {conversation}[/dim]\\n"
                )
            else:
                # Use most recent conversation or create new one
                convos = client.list_conversations(limit=1)
                if convos:
                    conversation = convos[0]["id"]
                    console.print(
                        f"[dim]Continuing conversation: {convos[0]['title']}[/dim]\\n"
                    )
                else:
                    convo = client.create_conversation()
                    conversation = convo["id"]
                    console.print(
                        f"[dim]Started new conversation: {conversation}[/dim]\\n"
                    )

        # Interactive mode if no message provided
        if not message:
            console.print("[bold]ARIA Chat[/bold] (Ctrl+C to exit)\\n")
            while True:
                try:
                    message = console.input("[cyan]You:[/cyan] ")
                    if not message.strip():
                        continue

                    console.print("[green]ARIA:[/green] ", end="")
                    response = client.send_message(conversation, message)

                    # Stream response
                    response_text = []
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            data = json.loads(line[6:])
                            if data["type"] == "text":
                                console.print(data["content"], end="")
                                response_text.append(data["content"])
                            elif data["type"] == "error":
                                console.print(
                                    f"\\n[red]Error:[/red] {data['error']}"
                                )
                                break

                    console.print("\\n")
                except KeyboardInterrupt:
                    console.print("\\n[dim]Goodbye![/dim]")
                    break
        else:
            # One-shot mode
            console.print(f"[cyan]You:[/cyan] {message}\\n")
            console.print("[green]ARIA:[/green] ", end="")

            response = client.send_message(conversation, message)

            # Stream response
            for line in response.iter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    if data["type"] == "text":
                        console.print(data["content"], end="")
                    elif data["type"] == "error":
                        console.print(f"\\n[red]Error:[/red] {data['error']}")
                        sys.exit(1)

            console.print("\\n")

    except Exception as e:
        console.print(f"\\n[red]Error:[/red] {str(e)}")
        sys.exit(1)


@cli.group()
def agents():
    """Manage agents."""
    pass


@agents.command("list")
def list_agents_cmd():
    """List agents."""
    try:
        client = AriaClient()
        agents_list = client.list_agents()

        if not agents_list:
            console.print("No agents found.")
            return

        table = Table(title="Agents")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Description")
        table.add_column("LLM")
        table.add_column("Default", justify="center")

        for agent in agents_list:
            table.add_row(
                agent["id"][:8] + "...",
                agent["name"],
                agent["description"][:50] + "..."
                if len(agent["description"]) > 50
                else agent["description"],
                f"{agent['llm']['backend']}/{agent['llm']['model']}",
                "✓" if agent["is_default"] else "",
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
