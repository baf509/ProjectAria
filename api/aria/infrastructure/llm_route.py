"""
ARIA - Local LLM Route Selection

Phase: Infrastructure / model-server control plane
Purpose: Decide which running model server answers as "the local model", and
persist the operator's pin for that choice.

Why this is its own module: the selection rules are consumed by both the
OpenAI-compatible passthrough (`api/routes/llm_proxy.py`) and the control-plane
routes that set/report the pin (`api/routes/infrastructure.py`). Keeping them
here makes them unit-testable without standing up HTTP or docker, which matters
because the failure they guard against — a consumer silently pointed at a dead
port — is only visible in the *edges* (stale pin, stopped server, unknown name).

Related Spec Sections:
- CLAUDE.md "LLM Adapter Pattern" (the LLAMACPP_URL passthrough contract)
"""

from __future__ import annotations

from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

# Fixed-_id singleton, same pattern as C4's active-project doc in app_state.
LLM_ROUTE_DOC = "llm_route"

# Model names that explicitly mean "you choose" rather than naming a server.
# `aria-resident` is the id Hermes carries in its config.yaml.
AUTO_ALIASES = frozenset({"auto", "aria", "aria-resident", "aria-auto", "default", ""})


def _norm(value: Any) -> str:
    """Casefold and unify separators so `DS4_0731.gguf` matches `ds4-0731`."""
    if not isinstance(value, str):
        return ""
    out = value.strip().lower()
    for ch in ("_", " ", "."):
        out = out.replace(ch, "-")
    return out.strip("-")


def _names_for(server: dict) -> set[str]:
    """Every string a caller might plausibly use to name this server."""
    names = {_norm(server.get("slug"))}
    model_file = server.get("model_file")
    if isinstance(model_file, str) and model_file:
        base = model_file.rsplit("/", 1)[-1]
        names.add(_norm(base))
        if base.lower().endswith(".gguf"):
            names.add(_norm(base[: -len(".gguf")]))
    names.discard("")
    return names


def is_servable(server: dict) -> bool:
    """Running, on this box, and reachable — the bar for answering a request."""
    return (
        server.get("state") == "running"
        and bool(server.get("onbox"))
        and bool(server.get("port"))
        and bool(base_url_for(server))
    )


def base_url_for(server: dict) -> Optional[str]:
    """Prefer loopback; fall back to the tailnet form.

    The fallback is load-bearing, not cosmetic: DS4 binds 100.123.245.84:8107
    ONLY, so `localhost:8107` is connection-refused even though a listener
    exists — a gotcha that has been misdiagnosed repeatedly on this box.
    """
    endpoints = server.get("endpoints") or {}
    base = endpoints.get("local") or endpoints.get("tailnet")
    return base.rstrip("/") if isinstance(base, str) and base else None


def match_requested(servers: list[dict], requested: Optional[str]) -> tuple[Optional[dict], bool]:
    """Resolve a caller-supplied `model` to a server.

    Returns `(server, known_but_stopped)`:
      - `(server, False)` — the name identifies a running server; route there.
      - `(None, True)`    — the name identifies a server ARIA knows that is NOT
                            running. The caller asked for something specific and
                            unavailable, so this must surface as an error rather
                            than quietly answering with a different model.
      - `(None, False)`   — no opinion (an auto alias, or an unrecognized string
                            like `gpt-4`); fall through to pin/auto.
    """
    want = _norm(requested)
    if want in AUTO_ALIASES:
        return None, False

    stopped_match = False
    for server in servers:
        if want not in _names_for(server):
            continue
        if is_servable(server):
            return server, False
        stopped_match = True
    return None, stopped_match


def rank_resident(servers: list[dict]) -> Optional[dict]:
    """Auto pick: the largest resident footprint among servable servers."""
    candidates = [s for s in servers if is_servable(s)]
    if not candidates:
        return None

    def weight(server: dict) -> float:
        gib = server.get("resident_gib_estimate")
        return float(gib) if isinstance(gib, (int, float)) else 0.0

    return max(candidates, key=weight)


def select(
    servers: list[dict], requested: Optional[str] = None, pin: Optional[str] = None
) -> tuple[Optional[dict], str, bool]:
    """Apply the full precedence: request > pin > auto.

    Returns `(server, reason, requested_unavailable)`. `reason` is human-facing
    and shows up in `GET /llm/v1/backend`, so a human can see *why* a given
    model answered without inferring it from a completion.
    """
    chosen, stopped = match_requested(servers, requested)
    if chosen is not None:
        return chosen, f"requested by caller (model={requested})", False
    if stopped:
        return None, f"requested server '{requested}' is not running", True

    if pin:
        pinned, pin_stopped = match_requested(servers, pin)
        if pinned is not None:
            return pinned, f"pinned in ARIA ({pin})", False
        # Deliberately not an error: a stale pin degrades to auto so one
        # forgotten setting cannot take every consumer offline. But it must
        # SAY so — a pin that silently reads as plain auto is how a UI ends up
        # claiming a selection that nothing is honouring. Covers both a stopped
        # server and one that has left the registry entirely.
        detail = "is not running" if pin_stopped else "is not a known server"
        best = rank_resident(servers)
        return best, f"pin '{pin}' {detail} — fell back to largest resident", False

    return rank_resident(servers), "largest resident_gib among running on-box servers", False


async def read_pin(db: Optional[AsyncIOMotorDatabase]) -> Optional[str]:
    """The operator's pinned slug, or None for auto."""
    if db is None:
        return None
    doc = await db.app_state.find_one({"_id": LLM_ROUTE_DOC})
    slug = (doc or {}).get("slug")
    return slug if isinstance(slug, str) and slug else None


async def write_pin(db: AsyncIOMotorDatabase, slug: Optional[str]) -> None:
    """Pin the local-model route to `slug`, or clear it with None (= auto)."""
    await db.app_state.update_one(
        {"_id": LLM_ROUTE_DOC},
        {"$set": {"slug": slug}},
        upsert=True,
    )


async def backend_model_id(base: str) -> Optional[str]:
    """Ask a backend which model it actually has loaded.

    Ground truth beats the registry: the registry records what we *believe* is
    configured, while this is what the process reports. The two drift whenever a
    systemd unit is edited without the registry being updated to match.

    Never raises — callers fall back to the registry's slug/model_file.
    """
    if not base:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base}/models")
            for entry in (resp.json() or {}).get("data") or []:
                mid = entry.get("id")
                if isinstance(mid, str) and mid:
                    return mid
    except Exception:
        return None
    return None
