#!/usr/bin/env python3
"""
ARIA CLI - Main Entry Point

Phase: 1
Purpose: CLI commands for interacting with ARIA API

Related Spec Sections:
- Section 8: Phase 1 - CLI Client
"""

import json
import os
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
        headers = {}
        api_key = os.getenv("ARIA_API_KEY")
        if api_key:
            headers["X-API-Key"] = api_key
        self.client = httpx.Client(timeout=120.0, headers=headers)

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

    def search_conversations(self, query: str, limit: int = 50):
        """Search conversations."""
        response = self.client.get(
            f"{self.base_url}/api/v1/conversations",
            params={"q": query, "limit": limit},
        )
        response.raise_for_status()
        return response.json()

    def export_conversation(self, conversation_id: str, format: str = "markdown"):
        """Export a conversation."""
        response = self.client.get(
            f"{self.base_url}/api/v1/conversations/{conversation_id}/export",
            params={"format": format},
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

    def request(self, method: str, path: str, **kwargs):
        response = self.client.request(method, f"{self.base_url}/api/v1{path}", **kwargs)
        response.raise_for_status()
        return response


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
@click.option("--query", help="Search query")
def list_conversations_cmd(limit, query):
    """List conversations."""
    try:
        client = AriaClient()
        convos = client.search_conversations(query, limit=limit) if query else client.list_conversations(limit=limit)

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
                str(convo.get("updated_at", ""))[:10],
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@conversations.command("export")
@click.argument("conversation_id")
@click.option("--format", "export_format", default="markdown", type=click.Choice(["markdown", "json"]))
def export_conversation_cmd(conversation_id, export_format):
    """Export a conversation."""
    try:
        client = AriaClient()
        exported = client.export_conversation(conversation_id, export_format)
        if export_format == "markdown":
            console.print(Markdown(exported["content"]))
        else:
            console.print_json(data=exported)
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
                    f"[dim]Started new conversation: {conversation}[/dim]\n"
                )
            else:
                # Use most recent conversation or create new one
                convos = client.list_conversations(limit=1)
                if convos:
                    conversation = convos[0]["id"]
                    console.print(
                        f"[dim]Continuing conversation: {convos[0]['title']}[/dim]\n"
                    )
                else:
                    convo = client.create_conversation()
                    conversation = convo["id"]
                    console.print(
                        f"[dim]Started new conversation: {conversation}[/dim]\n"
                    )

        # Interactive mode if no message provided
        if not message:
            console.print("[bold]ARIA Chat[/bold] (Ctrl+C to exit)\n")
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
                            try:
                                data = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            if data["type"] == "text":
                                console.print(data["content"], end="")
                                response_text.append(data["content"])
                            elif data["type"] == "error":
                                console.print(
                                    f"\\n[red]Error:[/red] {data['error']}"
                                )
                                break

                    console.print("\n")
                except KeyboardInterrupt:
                    console.print("\\n[dim]Goodbye![/dim]")
                    break
        else:
            # One-shot mode
            console.print(f"[cyan]You:[/cyan] {message}\n")
            console.print("[green]ARIA:[/green] ", end="")

            response = client.send_message(conversation, message)

            # Stream response
            for line in response.iter_lines():
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if data["type"] == "text":
                        console.print(data["content"], end="")
                    elif data["type"] == "error":
                        console.print(f"\\n[red]Error:[/red] {data['error']}")
                        sys.exit(1)

            console.print("\n")

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


@agents.command("switch")
@click.argument("conversation_id")
@click.argument("agent_slug")
def switch_agent_mode_cmd(conversation_id, agent_slug):
    """Switch a conversation to a different mode."""
    try:
        client = AriaClient()
        result = client.request(
            "POST",
            f"/conversations/{conversation_id}/switch-mode",
            json={"agent_slug": agent_slug},
        ).json()
        console.print(f"[green]✓[/green] Switched to {agent_slug}")
        console.print(f"Conversation: {result['title']}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@agents.command("create")
@click.argument("name")
@click.argument("slug")
@click.option("--description", default="", help="Short description")
@click.option("--system-prompt", required=True, help="System prompt")
@click.option("--backend", default="llamacpp", help="LLM backend")
@click.option("--model", default="default", help="Model name")
@click.option("--category", default="chat", help="Mode category")
@click.option("--temperature", default=0.7, type=float, help="Sampling temperature")
def create_agent_cmd(name, slug, description, system_prompt, backend, model, category, temperature):
    """Create an agent/mode."""
    try:
        client = AriaClient()
        agent = client.request(
            "POST",
            "/agents",
            json={
                "name": name,
                "slug": slug,
                "description": description,
                "system_prompt": system_prompt,
                "mode_category": category,
                "llm": {
                    "backend": backend,
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": 4096,
                },
            },
        ).json()
        console.print(f"[green]✓[/green] Created mode {agent['name']} ({agent['slug']})")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@agents.command("update")
@click.argument("agent_id")
@click.option("--name", help="New agent name")
@click.option("--system-prompt", help="New system prompt")
@click.option("--backend", help="LLM backend")
@click.option("--model", help="Model name")
@click.option("--temperature", type=float, help="Sampling temperature")
def update_agent_cmd(agent_id, name, system_prompt, backend, model, temperature):
    """Update an agent/mode."""
    try:
        client = AriaClient()
        data = {}
        if name is not None:
            data["name"] = name
        if system_prompt is not None:
            data["system_prompt"] = system_prompt
        if backend is not None:
            data["backend"] = backend
        if model is not None:
            data["model"] = model
        if temperature is not None:
            data["temperature"] = temperature

        if not data:
            console.print("[red]Error:[/red] No fields provided to update")
            sys.exit(1)

        result = client.request("PATCH", f"/agents/{agent_id}", json=data).json()
        console.print(f"[green]✓[/green] Updated agent: {result['name']} ({result['id'][:8]}...)")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@agents.command("delete")
@click.argument("agent_id")
def delete_agent_cmd(agent_id):
    """Delete an agent/mode."""
    try:
        client = AriaClient()
        client.request("DELETE", f"/agents/{agent_id}")
        console.print(f"[green]✓[/green] Deleted agent: {agent_id}")
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


@memories.command("delete")
@click.argument("memory_id")
def delete_memory_cmd(memory_id):
    """Delete a memory."""
    try:
        client = AriaClient()
        client.request("DELETE", f"/memories/{memory_id}")
        console.print(f"[green]✓[/green] Deleted memory: {memory_id}")
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


@cli.group()
def research():
    """Manage research runs."""
    pass


@research.command("start")
@click.argument("query")
@click.option("--depth", default=2, help="Research depth")
@click.option("--breadth", default=3, help="Research breadth")
def start_research_cmd(query, depth, breadth):
    try:
        client = AriaClient()
        result = client.request("POST", "/research", json={"query": query, "depth": depth, "breadth": breadth}).json()
        console.print(f"[green]✓[/green] Started research {result['research_id']}")
        console.print(f"Task: {result['task_id']}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@research.command("list")
def list_research_cmd():
    try:
        client = AriaClient()
        runs = client.request("GET", "/research").json()
        table = Table(title="Research Runs")
        table.add_column("ID", style="cyan")
        table.add_column("Query", style="green")
        table.add_column("Status")
        table.add_column("Progress")
        for run in runs:
            progress = run["progress"]
            table.add_row(
                run["id"][:8] + "...",
                run["query"][:48],
                run["status"],
                f"{progress['queries_completed']}/{progress['queries_total']}",
            )
        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@research.command("report")
@click.argument("research_id")
def research_report_cmd(research_id):
    try:
        client = AriaClient()
        report = client.request("GET", f"/research/{research_id}/report").json()
        console.print(Markdown(report.get("report_text") or "_No report available yet._"))
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@cli.group()
def usage():
    """Inspect usage and token totals."""
    pass


@usage.command("summary")
@click.option("--days", default=7, help="Trailing day window")
def usage_summary_cmd(days):
    try:
        client = AriaClient()
        summary = client.request("GET", "/usage/summary", params={"days": days}).json()
        console.print(summary)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@usage.command("by-model")
@click.option("--days", default=7, help="Trailing day window")
def usage_by_model_cmd(days):
    try:
        client = AriaClient()
        rows = client.request("GET", "/usage/by-model", params={"days": days}).json()
        table = Table(title="Usage By Model")
        table.add_column("Model", style="cyan")
        table.add_column("Requests", justify="right")
        table.add_column("Tokens", justify="right")
        for row in rows:
            table.add_row(str(row["_id"]), str(row["requests"]), str(row["total_tokens"]))
        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@cli.group()
def code():
    """Manage coding sessions."""
    pass


@code.command("start")
@click.argument("workspace")
@click.argument("prompt")
@click.option("--backend", help="Backend name")
@click.option("--model", help="Model override")
def code_start_cmd(workspace, prompt, backend, model):
    try:
        client = AriaClient()
        session = client.request(
            "POST",
            "/coding/sessions",
            json={"workspace": workspace, "prompt": prompt, "backend": backend, "model": model},
        ).json()
        console.print(f"[green]✓[/green] Started session {session['id']}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@cli.group()
def workflows():
    """Manage workflows."""
    pass


@cli.group()
def admin():
    """Inspect security and cutover status."""
    pass


@workflows.command("list")
def list_workflows_cmd():
    try:
        client = AriaClient()
        rows = client.request("GET", "/workflows").json()
        table = Table(title="Workflows")
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="green")
        table.add_column("Steps", justify="right")
        for row in rows:
            table.add_row(row["_id"][:8] + "...", row["name"], str(len(row.get("steps", []))))
        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@workflows.command("create")
@click.argument("name")
@click.argument("steps_json")
@click.option("--description", default="", help="Workflow description")
def create_workflow_cmd(name, steps_json, description):
    try:
        client = AriaClient()
        steps = json.loads(steps_json)
        workflow = client.request(
            "POST",
            "/workflows",
            json={"name": name, "description": description, "steps": steps},
        ).json()
        console.print(f"[green]✓[/green] Created workflow {workflow['_id']}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@workflows.command("update")
@click.argument("workflow_id")
@click.option("--name", help="New workflow name")
@click.option("--description", help="Workflow description")
@click.option("--steps", "steps_json", help="Steps as JSON array")
def update_workflow_cmd(workflow_id, name, description, steps_json):
    """Update a workflow."""
    try:
        client = AriaClient()
        data = {}
        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if steps_json is not None:
            data["steps"] = json.loads(steps_json)

        if not data:
            console.print("[red]Error:[/red] No fields provided to update")
            sys.exit(1)

        result = client.request("PATCH", f"/workflows/{workflow_id}", json=data).json()
        console.print(f"[green]✓[/green] Updated workflow: {result.get('name', workflow_id)}")
    except json.JSONDecodeError:
        console.print("[red]Error:[/red] Invalid JSON for steps")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@workflows.command("delete")
@click.argument("workflow_id")
def delete_workflow_cmd(workflow_id):
    """Delete a workflow."""
    try:
        client = AriaClient()
        client.request("DELETE", f"/workflows/{workflow_id}")
        console.print(f"[green]✓[/green] Deleted workflow: {workflow_id}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@workflows.command("run")
@click.argument("workflow_id")
@click.option("--dry-run", is_flag=True, help="Do not execute, only simulate")
def run_workflow_cmd(workflow_id, dry_run):
    try:
        client = AriaClient()
        result = client.request("POST", f"/workflows/{workflow_id}/run", json={"dry_run": dry_run}).json()
        console.print(f"[green]✓[/green] Started workflow run {result['run_id']}")
        console.print(f"Task: {result['task_id']}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@workflows.command("status")
@click.argument("workflow_id")
def workflow_status_cmd(workflow_id):
    try:
        client = AriaClient()
        status = client.request("GET", f"/workflows/{workflow_id}/status").json()
        console.print_json(data=status)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@admin.command("audit")
@click.option("--hours", default=24, help="Lookback window in hours")
@click.option("--limit", default=20, help="Number of recent events")
def admin_audit_cmd(hours, limit):
    try:
        client = AriaClient()
        payload = client.request("GET", "/admin/audit", params={"hours": hours, "limit": limit}).json()
        console.print_json(data=payload)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@admin.command("cutover")
def admin_cutover_cmd():
    try:
        client = AriaClient()
        payload = client.request("GET", "/admin/cutover").json()
        console.print_json(data=payload)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@code.command("status")
def code_status_cmd():
    try:
        client = AriaClient()
        sessions = client.request("GET", "/coding/sessions").json()
        table = Table(title="Coding Sessions")
        table.add_column("ID", style="cyan")
        table.add_column("Backend")
        table.add_column("Status")
        table.add_column("Workspace")
        for session in sessions:
            table.add_row(session["id"][:8] + "...", session["backend"], session["status"], session["workspace"])
        console.print(table)
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@code.command("stop")
@click.argument("session_id")
def code_stop_cmd(session_id):
    try:
        client = AriaClient()
        client.request("POST", f"/coding/sessions/{session_id}/stop")
        console.print(f"[green]✓[/green] Stopped {session_id}")
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


@cli.group("killswitch")
def killswitch_group():
    """Emergency killswitch controls."""
    pass


@killswitch_group.command("activate")
@click.option("--reason", default="Manual CLI activation", help="Reason for activation")
def killswitch_activate_cmd(reason):
    """Activate the emergency killswitch."""
    try:
        client = AriaClient()
        result = client.request("POST", "/killswitch/activate", json={"reason": reason}).json()
        console.print(f"[red]⚠ Killswitch ACTIVATED[/red]")
        console.print(f"Reason: {result.get('reason')}")
        console.print(f"Cancelled tasks: {result.get('cancelled_tasks', 0)}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@killswitch_group.command("deactivate")
def killswitch_deactivate_cmd():
    """Deactivate the killswitch."""
    try:
        client = AriaClient()
        client.request("POST", "/killswitch/deactivate")
        console.print(f"[green]✓[/green] Killswitch deactivated")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@killswitch_group.command("status")
def killswitch_status_cmd():
    """Check killswitch status."""
    try:
        client = AriaClient()
        status = client.request("GET", "/killswitch/status").json()
        if status["active"]:
            console.print(f"[red]⚠ ACTIVE[/red] — {status.get('reason')}")
            console.print(f"Since: {status.get('activated_at')}")
        else:
            console.print(f"[green]✓[/green] Killswitch is inactive")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@cli.group("autopilot")
def autopilot_group():
    """Autopilot mode controls."""
    pass


@autopilot_group.command("start")
@click.argument("goal")
@click.option("--mode", default="safe", type=click.Choice(["safe", "unrestricted"]))
@click.option("--backend", default="llamacpp", help="LLM backend")
@click.option("--model", default="default", help="Model name")
def autopilot_start_cmd(goal, mode, backend, model):
    """Start an autopilot session."""
    try:
        client = AriaClient()
        result = client.request(
            "POST",
            "/autopilot/start",
            json={"goal": goal, "mode": mode, "backend": backend, "model": model},
        ).json()
        console.print(f"[green]✓[/green] Autopilot started: {result['session_id']}")
        console.print(f"Task: {result['task_id']}")
        console.print(f"Steps: {result['step_count']}")
        for step in result.get("steps", []):
            console.print(f"  {step['index']+1}. [{step['action']}] {step['name']}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@autopilot_group.command("status")
@click.argument("session_id")
def autopilot_status_cmd(session_id):
    """Check autopilot session status."""
    try:
        client = AriaClient()
        session = client.request("GET", f"/autopilot/sessions/{session_id}").json()
        console.print(f"Goal: {session['goal']}")
        console.print(f"Mode: {session['mode']} | Status: {session['status']}")
        for step in session.get("steps", []):
            status_icon = {"completed": "✓", "failed": "✗", "running": "►", "awaiting_approval": "⏸", "pending": "○"}.get(step["status"], "?")
            console.print(f"  {status_icon} {step['index']+1}. {step['name']} [{step['status']}]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@autopilot_group.command("approve")
@click.argument("session_id")
@click.argument("step_index", type=int)
def autopilot_approve_cmd(session_id, step_index):
    """Approve a pending step in safe mode."""
    try:
        client = AriaClient()
        client.request(
            "POST",
            f"/autopilot/sessions/{session_id}/approve",
            json={"step_index": step_index},
        )
        console.print(f"[green]✓[/green] Approved step {step_index}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


@autopilot_group.command("stop")
@click.argument("session_id")
def autopilot_stop_cmd(session_id):
    """Stop an autopilot session."""
    try:
        client = AriaClient()
        client.request("POST", f"/autopilot/sessions/{session_id}/stop")
        console.print(f"[green]✓[/green] Autopilot stopped")
    except Exception as e:
        console.print(f"[red]Error:[/red] {str(e)}")
        sys.exit(1)


from aria_cli.setup_wizard import setup
from aria_cli.service import service

cli.add_command(setup)
cli.add_command(service)


if __name__ == "__main__":
    cli()
