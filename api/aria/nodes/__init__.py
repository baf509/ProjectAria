"""
ARIA - Multi-machine nodes (aria-node agents)

Purpose: let the watched-shell fleet span this host plus remote nodes (e.g. a
MacBook). A remote node registers its tmux shells + coding sessions into the one
central brain over the API and is driven back through a pull-based command queue.
See MULTI_MACHINE_FLEET_DESIGN.md.
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
