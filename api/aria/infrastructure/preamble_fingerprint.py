"""Privacy-safe fingerprints for the cacheable system/tools prompt prefix.

The gateway needs to explain *why* a caller stopped reusing KV without storing
the prompt itself.  This module hashes the pieces that affect the rendered
prefix and retains only a bounded in-process signature per caller/model/path.
It is deliberately observational: no request text is rewritten.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Optional


_ISO_SUBDAY = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_MAX_TRACKED_PREFIXES = 2048
_REASONING_KEYS = (
    "reasoning_effort",
    "thinking_budget_tokens",
    "enable_thinking",
    "chat_template_kwargs",
)


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:24]


@dataclass(frozen=True)
class _Signature:
    fingerprint: str
    normalized_fingerprint: str
    system_fingerprint: str
    tools_fingerprint: str
    template_fingerprint: str
    prefix_bytes: int
    system_bytes: int
    tools_bytes: int
    tool_count: int

    def public(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "system_fingerprint": self.system_fingerprint,
            "tools_fingerprint": self.tools_fingerprint,
            "template_fingerprint": self.template_fingerprint,
            "prefix_bytes": self.prefix_bytes,
            "system_bytes": self.system_bytes,
            "tools_bytes": self.tools_bytes,
            "tool_count": self.tool_count,
        }


def _signature(body: Optional[dict]) -> Optional[_Signature]:
    if not isinstance(body, dict):
        return None

    messages = body.get("messages")
    systems: list[Any] = []
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "system":
                break
            systems.append(message.get("content"))

    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    template = {key: body[key] for key in _REASONING_KEYS if key in body}
    if not systems and not tools and not template:
        return None

    system_raw = _encoded(systems)
    tools_raw = _encoded(tools)
    template_raw = _encoded(template)
    prefix_raw = _encoded({"system": systems, "tools": tools, "template": template})
    normalized = _ISO_SUBDAY.sub("<TIME>", prefix_raw.decode("utf-8")).encode("utf-8")
    return _Signature(
        fingerprint=_digest(prefix_raw),
        normalized_fingerprint=_digest(normalized),
        system_fingerprint=_digest(system_raw),
        tools_fingerprint=_digest(tools_raw),
        template_fingerprint=_digest(template_raw),
        prefix_bytes=len(prefix_raw),
        system_bytes=len(system_raw),
        tools_bytes=len(tools_raw),
        tool_count=len(tools),
    )


class PreambleTracker:
    """Bounded arrival-ordered comparison of privacy-safe prefix signatures."""

    def __init__(self, max_entries: int = _MAX_TRACKED_PREFIXES) -> None:
        self.max_entries = max(1, int(max_entries))
        self._seen: OrderedDict[str, _Signature] = OrderedDict()

    def clear(self) -> None:
        self._seen.clear()

    def observe(self, key: str, body: Optional[dict]) -> dict[str, Any]:
        current = _signature(body)
        if current is None:
            return {"state": "absent", "change_reason": None}

        previous = self._seen.pop(key, None)
        self._seen[key] = current
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)

        result = current.public()
        if previous is None:
            result.update(state="first_seen", change_reason=None)
            return result
        result["previous_fingerprint"] = previous.fingerprint
        if previous.fingerprint == current.fingerprint:
            result.update(state="stable", change_reason=None)
            return result
        if previous.normalized_fingerprint == current.normalized_fingerprint:
            result.update(state="changed", change_reason="volatile_timestamp")
            return result

        changed: list[str] = []
        if previous.system_fingerprint != current.system_fingerprint:
            changed.append("system")
        if previous.tools_fingerprint != current.tools_fingerprint:
            changed.append("tools")
        if previous.template_fingerprint != current.template_fingerprint:
            changed.append("reasoning_template")
        result.update(
            state="changed",
            change_reason=("_and_".join(changed) + "_changed") if changed else "prefix_changed",
        )
        return result


preamble_tracker = PreambleTracker()
