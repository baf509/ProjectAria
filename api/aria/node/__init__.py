"""
ARIA - Node agent (runs on a remote machine to join the fleet)

`aria-node` is a thin, outbound-only daemon. It registers this machine with the
central ARIA API, captures its local `claude-*` tmux shells (pushing events +
snapshots), and long-polls a command queue to drive them on the central brain's
behalf — so a MacBook's shells and coding sessions appear in, and are drivable
from, the one corsair fleet. It talks ONLY to the API (no Mongo), over the
tailnet, using the same X-API-Key.

Run it with:  python -m aria.node   (see agent.main for env/flags)
"""

from aria.node.agent import NodeAgent, main

__all__ = ["NodeAgent", "main"]
