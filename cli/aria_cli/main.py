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


@cli.group()
def memories():
    """Manage memories."""
    pass


@memories.command("list")
@click.option("--limit", default=50, help="Number of memories to show")
@click.option("--type", help="Filter by content type")
def list_memories_cmd(limit, type):
    """List memories."""
    try:
        client = AriaClient()
        params = {"limit": limit}
        if type:
            params["content_type"] = type

        response = client.client.get(
            f"{client.base_url}/api/v1/memories", params=params
        )
        response.raise_for_status()
        memories_list = response.json()

        if not memories_list:
            console.print("No memories found.")
            return

        table = Table(title="Memories")
        table.add_column("ID", style="cyan")
        table.add_column("Type", style="green")
        table.add_column("Content")
        table.add_column("Importance", justify="right")
        table.add_column("Categories")

        for memory in memories_list:
            table.add_row(
                memory["id"][:8] + "...",
                memory["content_type"],
                memory["content"][:60] + "..."
                if len(memory["content"]) > 60
                else memory["content"],
                f"{memory['importance']:.2f}",
                ", ".join(memory.get("categories", [])[:2]),
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@memories.command("search")
@click.argument("query")
@click.option("--limit", default=10, help="Number of results")
def search_memories_cmd(query, limit):
    """Search memories."""
    try:
        client = AriaClient()
        response = client.client.post(
            f"{client.base_url}/api/v1/memories/search",
            json={"query": query, "limit": limit},
        )
        response.raise_for_status()
        results = response.json()

        if not results:
            console.print("No memories found.")
            return

        console.print(f"[bold]Found {len(results)} memories:[/bold]\n")

        for i, memory in enumerate(results, 1):
            console.print(f"[cyan]{i}.[/cyan] [{memory['content_type']}]")
            console.print(f"   {memory['content']}")
            console.print(
                f"   [dim]Importance: {memory['importance']:.2f} | "
                f"Categories: {', '.join(memory.get('categories', []))}"
                f"[/dim]\n"
            )

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@memories.command("add")
@click.argument("content")
@click.option("--type", default="fact", help="Memory type (fact, preference, event, skill)")
@click.option("--importance", default=0.5, type=float, help="Importance (0.0-1.0)")
@click.option("--categories", help="Comma-separated categories")
def add_memory_cmd(content, type, importance, categories):
    """Add a new memory manually."""
    try:
        client = AriaClient()
        data = {
            "content": content,
            "content_type": type,
            "importance": importance,
            "categories": categories.split(",") if categories else [],
        }

        response = client.client.post(
            f"{client.base_url}/api/v1/memories", json=data
        )
        response.raise_for_status()
        memory = response.json()

        console.print(
            f"[green]✓[/green] Created memory: {memory['id'][:8]}..."
        )
        console.print(f"   Type: {memory['content_type']}")
        console.print(f"   Content: {memory['content']}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@memories.command("extract")
@click.argument("conversation_id")
def extract_memories_cmd(conversation_id):
    """Extract memories from a conversation."""
    try:
        client = AriaClient()
        response = client.client.post(
            f"{client.base_url}/api/v1/memories/extract/{conversation_id}"
        )
        response.raise_for_status()
        result = response.json()

        console.print(f"[green]✓[/green] {result['message']}")
        console.print(
            f"   Conversation: {result['conversation_id']}"
        )
        console.print(
            "[dim]Extraction is running in the background...[/dim]"
        )

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@cli.group()
def tools():
    """Manage tools."""
    pass


@tools.command("list")
@click.option("--type", help="Filter by tool type (builtin, mcp)")
def list_tools_cmd(type):
    """List available tools."""
    try:
        client = AriaClient()
        params = {}
        if type:
            params["tool_type"] = type

        response = client.client.get(
            f"{client.base_url}/api/v1/tools", params=params
        )
        response.raise_for_status()
        tools_list = response.json()

        if not tools_list:
            console.print("No tools found.")
            return

        table = Table(title="Tools")
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="green")
        table.add_column("Description")
        table.add_column("Parameters", justify="right")

        for tool in tools_list:
            table.add_row(
                tool["name"],
                tool["type"],
                tool["description"][:60] + "..."
                if len(tool["description"]) > 60
                else tool["description"],
                str(len(tool["parameters"])),
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@tools.command("info")
@click.argument("tool_name")
def tool_info_cmd(tool_name):
    """Show detailed information about a tool."""
    try:
        client = AriaClient()
        response = client.client.get(
            f"{client.base_url}/api/v1/tools/{tool_name}"
        )
        response.raise_for_status()
        tool = response.json()

        console.print(f"[bold cyan]{tool['name']}[/bold cyan] ({tool['type']})")
        console.print(f"\n{tool['description']}\n")

        if tool["parameters"]:
            console.print("[bold]Parameters:[/bold]")
            for param in tool["parameters"]:
                required = "[red]*[/red]" if param["required"] else ""
                console.print(f"  {required}[cyan]{param['name']}[/cyan] ({param['type']})")
                console.print(f"    {param['description']}")
                if param.get("default"):
                    console.print(f"    [dim]Default: {param['default']}[/dim]")
                console.print()

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@tools.command("execute")
@click.argument("tool_name")
@click.argument("arguments", required=False)
def execute_tool_cmd(tool_name, arguments):
    """Execute a tool with JSON arguments."""
    try:
        client = AriaClient()

        # Parse arguments
        args = {}
        if arguments:
            args = json.loads(arguments)

        console.print(f"[dim]Executing {tool_name}...[/dim]\n")

        response = client.client.post(
            f"{client.base_url}/api/v1/tools/execute",
            json={"tool_name": tool_name, "arguments": args},
        )
        response.raise_for_status()
        result = response.json()

        if result["status"] == "success":
            console.print(f"[green]✓[/green] Tool executed successfully")
            console.print(f"[dim]Duration: {result['duration_ms']}ms[/dim]\n")
            console.print("[bold]Output:[/bold]")
            console.print(result["output"])
        else:
            console.print(f"[red]✗[/red] Tool execution failed")
            console.print(f"[red]Error:[/red] {result['error']}")

    except json.JSONDecodeError:
        console.print("[red]Error:[/red] Invalid JSON arguments")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@cli.group()
def mcp():
    """Manage MCP servers."""
    pass


@mcp.command("list")
def list_mcp_servers_cmd():
    """List MCP servers."""
    try:
        client = AriaClient()
        response = client.client.get(
            f"{client.base_url}/api/v1/mcp/servers"
        )
        response.raise_for_status()
        servers = response.json()

        if not servers:
            console.print("No MCP servers configured.")
            return

        table = Table(title="MCP Servers")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Version")
        table.add_column("Connected", justify="center")
        table.add_column("Tools", justify="right")

        for server in servers:
            table.add_row(
                server["id"],
                server.get("name", "Unknown"),
                server.get("version", "-"),
                "✓" if server["connected"] else "✗",
                str(server["tool_count"]),
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@mcp.command("add")
@click.argument("server_id")
@click.argument("command", nargs=-1, required=True)
def add_mcp_server_cmd(server_id, command):
    """Add an MCP server."""
    try:
        client = AriaClient()
        response = client.client.post(
            f"{client.base_url}/api/v1/mcp/servers",
            json={"server_id": server_id, "command": list(command)},
        )
        response.raise_for_status()
        result = response.json()

        console.print(f"[green]✓[/green] {result['message']}")
        console.print(f"   Tools registered: {result['tool_count']}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@mcp.command("remove")
@click.argument("server_id")
def remove_mcp_server_cmd(server_id):
    """Remove an MCP server."""
    try:
        client = AriaClient()
        response = client.client.delete(
            f"{client.base_url}/api/v1/mcp/servers/{server_id}"
        )
        response.raise_for_status()

        console.print(f"[green]✓[/green] Removed MCP server: {server_id}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@mcp.command("tools")
@click.argument("server_id")
def list_mcp_server_tools_cmd(server_id):
    """List tools provided by an MCP server."""
    try:
        client = AriaClient()
        response = client.client.get(
            f"{client.base_url}/api/v1/mcp/servers/{server_id}/tools"
        )
        response.raise_for_status()
        tools_list = response.json()

        if not tools_list:
            console.print(f"No tools found for server: {server_id}")
            return

        console.print(f"[bold]Tools from {server_id}:[/bold]\n")

        for tool in tools_list:
            console.print(f"[cyan]{tool['name']}[/cyan]")
            console.print(f"  {tool['description']}")
            console.print()

    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
