"""
ARIA - APNs delivery stub for watched-shells idle alerts

Purpose: Minimal Apple Push Notification service sender invoked by the
idle notifier when `shells_apns_enabled=true`. This module is intentionally
a stub: actual APNs HTTP/2 delivery requires the `httpx` HTTP/2 extras (or
a library like `aioapns` / `pyapns2`) plus a signed JWT using the
developer-portal-issued .p8 auth key.

Flip `shells_apns_enabled=true` and fill in the apns_* config (team id,
key id, bundle id, auth-key path) once you have those set up. Until then
this logs intended deliveries without sending.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from aria.config import settings

logger = logging.getLogger(__name__)


async def send_apns_alert(
    db,
    *,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> int:
    """Deliver a push to every registered device.

    Returns the number of devices the alert was dispatched to (or attempted).
    If APNs is disabled or misconfigured, logs and returns 0.
    """
    if not settings.shells_apns_enabled:
        return 0

    cursor = db.devices.find({"platform": "ios"})
    tokens: list[str] = []
    async for doc in cursor:
        token = doc.get("token")
        if token:
            tokens.append(token)

    if not tokens:
        return 0

    payload = {
        "aps": {
            "alert": {"title": title, "body": body},
            "sound": "default",
        },
    }
    if data:
        payload["data"] = data

    # Configuration sanity check — bail out with a clear log line rather
    # than attempting HTTP/2 with missing credentials.
    required = [
        settings.apns_team_id,
        settings.apns_key_id,
        settings.apns_bundle_id,
        settings.apns_auth_key_path,
    ]
    if any(not v for v in required):
        logger.warning(
            "APNs enabled but not fully configured — skipping %d device(s). "
            "Missing any of: apns_team_id, apns_key_id, apns_bundle_id, apns_auth_key_path.",
            len(tokens),
        )
        return 0

    if not os.path.exists(settings.apns_auth_key_path):
        logger.warning(
            "APNs auth key not found at %s — skipping %d device(s).",
            settings.apns_auth_key_path,
            len(tokens),
        )
        return 0

    # Actual delivery is intentionally left to a follow-up PR — it needs
    # HTTP/2 + JWT (ES256) and is painful to unit-test without a real
    # device. The payload, tokens, and config are all accessible here
    # when ready to wire `httpx[http2]` or `aioapns`.
    logger.info(
        "[apns-stub] would deliver to %d device(s): title=%r body=%r payload=%s",
        len(tokens),
        title,
        body,
        json.dumps(payload),
    )
    _ = time.time()  # keep timing hook for future metrics
    return len(tokens)
