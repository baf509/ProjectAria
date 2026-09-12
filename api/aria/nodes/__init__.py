"""
ARIA - Multi-machine nodes (aria-node agents)

Purpose: let the watched-shell fleet span this host plus remote nodes (e.g. a
MacBook). A remote node registers its tmux shells + coding sessions into the one
central brain over the API and is driven back through a pull-based command queue.
See vault/ProjectAria/Design/ARCHITECTURE.md (Multi-machine fleet)
"""

from __future__ import annotations

import socket

from aria.config import settings


def local_node_id() -> str:
    """Identifier for THIS host (the API process). Shells/sessions whose `host`
    differs are remote and driven via the node command queue, not local tmux."""
    return settings.local_node_id or socket.gethostname()


def is_remote_host(host: str | None) -> bool:
    """True if `host` names a machine other than the one running the API."""
    return bool(host) and host != local_node_id()


def canonical_shell_host(host: str | None) -> str:
    """Resolve physical shell ownership without changing command-queue routing."""
    host = host or local_node_id()
    return settings.node_shell_host_aliases.get(host, host)


def _machine_name(value: str) -> str:
    """Compare machine names without tripping over how each source spells them.

    `local_node_id()` falls back to `socket.gethostname()` ("Bens-MacBook-Pro.local")
    while node ids are registry slugs ("bens-macbook-pro"), so a literal comparison
    of the two never matches — not even for this machine's own node.
    """
    return value.strip().lower().removesuffix(".local")


def is_same_machine(host: str | None) -> bool:
    """True if `host` names a node running on the SAME physical machine as the API.

    Distinct from `is_remote_host`, and deliberately not a replacement for it.
    That question is "does this go through the node command queue rather than
    local tmux", and the answer stays yes for a co-located node like
    `mac-agents`. This question is "is the workspace on this filesystem", which
    is what the guard needs before it can cut a worktree or read a repository.
    Conflating the two is why the guard skipped every session on this host.
    """
    if not host:
        return True
    canonical = _machine_name(canonical_shell_host(host))
    return canonical in {_machine_name(local_node_id()),
                         _machine_name(settings.local_node_id or ""),
                         _machine_name(socket.gethostname())} - {""}
