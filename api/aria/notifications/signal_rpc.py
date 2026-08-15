"""
ARIA - Break-glass Signal client (signal-cli JSON-RPC)

Purpose: the ONE sanctioned path where ARIA sends Ben a message directly
(steward proposal §6.4 / decision D5). Normal alerts go to the `alerts`
collection and Hermes relays them; this module exists for the case where that
relay is the thing that is broken — verified silently dead three times
(2026-06-29→07-26, 07-28, 08-10→08-15).

Transport notes (verified 2026-08-15, do not "simplify"):
- The signal-cli daemon Hermes owns answers JSON-RPC on
  http://127.0.0.1:8090/api/v1/rpc. It is the only live sender on this box.
- Do NOT shell out to the signal-cli BINARY: it blocks on the daemon's config
  lock and hangs forever (~/.hermes/skills/devops/signal-cli-delivery/SKILL.md).
- aria/signal/service.py targets a signal-cli REST wrapper on :8088 that is not
  running. It is a different, dead transport — not a fallback for this one.

Everything here fails closed and silent: an unreachable daemon must never raise
into a watchdog tick, and an unlisted alert kind must never be sent at all.

⚠️ This module SENDS. corsair's .env carries a live account/recipient and the
daemon is running, so any test that reaches `send_breakglass` with real settings
puts a real message on Ben's phone (this happened once, 2026-08-15T22:39Z, from
an unpatched watchdog test). Tests must patch the module reference
`aria.notifications.signal_rpc.httpx` — patching `signal_rpc.httpx.AsyncClient`
mutates the shared httpx module and breaks every other client in the process.
"""

from __future__ import annotations

import itertools
import logging
from typing import Optional

import httpx

from aria.config import settings

logger = logging.getLogger(__name__)

_RPC_TIMEOUT_SECONDS = 10.0

_request_ids = itertools.count(1)


def breakglass_allowed(kind: str) -> tuple[bool, str]:
    """Fail-closed gate for the direct-send path. Returns (allowed, reason).

    `kind` is matched against settings.alert_breakglass_kinds both whole and by
    its family prefix, because the allow-list is written in the proposal's
    "<kind>:<event>" form ("relay:dead") while alert rows carry the bare family
    ("relay"). A bare family only matches a bare entry, so listing "relay:dead"
    does NOT authorise "relay:recovered".
    """
    if not settings.alert_breakglass_enabled:
        return False, "disabled"
    allowed = list(settings.alert_breakglass_kinds or [])
    if kind not in allowed:
        return False, f"kind_not_allowed:{kind}"
    if not settings.signal_breakglass_account or not settings.signal_breakglass_recipient:
        return False, "unconfigured"
    if not settings.signal_cli_rpc_url:
        return False, "unconfigured"
    return True, "ok"


async def send_breakglass(message: str, *, kind: str) -> dict:
    """Send one direct Signal message via the signal-cli JSON-RPC daemon.

    Returns {"sent": bool, "reason": str}; never raises. Callers are watchdog
    ticks — a Signal outage must not take the watchdog down with it."""
    ok, reason = breakglass_allowed(kind)
    if not ok:
        logger.info("break-glass suppressed (%s) for kind=%s", reason, kind)
        return {"sent": False, "reason": reason}

    payload = {
        "jsonrpc": "2.0",
        "method": "send",
        "params": {
            "account": settings.signal_breakglass_account,
            "recipient": [settings.signal_breakglass_recipient],
            "message": message,
        },
        "id": next(_request_ids),
    }
    try:
        async with httpx.AsyncClient(timeout=_RPC_TIMEOUT_SECONDS) as client:
            resp = await client.post(settings.signal_cli_rpc_url, json=payload)
    except Exception as exc:
        logger.warning("break-glass send failed (transport): %s", exc)
        return {"sent": False, "reason": "transport_error", "detail": str(exc)[:200]}

    if resp.status_code >= 400:
        logger.warning("break-glass send failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return {"sent": False, "reason": f"http_{resp.status_code}"}

    body: Optional[dict]
    try:
        body = resp.json()
    except Exception:
        body = None
    # signal-cli answers 200 with a JSON-RPC error object for unknown methods
    # and unregistered accounts, so HTTP status alone is not delivery evidence.
    if isinstance(body, dict) and body.get("error"):
        detail = str(body["error"])[:200]
        logger.warning("break-glass send rejected by daemon: %s", detail)
        return {"sent": False, "reason": "rpc_error", "detail": detail}

    logger.warning("break-glass Signal message SENT (kind=%s)", kind)
    return {"sent": True, "reason": "ok", "result": (body or {}).get("result")}
