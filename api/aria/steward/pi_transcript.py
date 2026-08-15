"""
ARIA - Pi Transcript Reader

Purpose: read pi's structured session JSONL — the only place a local coding
agent's tool calls, per-turn token usage and provider errors are actually
recorded — and turn it into the stuck signals the MetaSupervisor acts on.

Why this exists: until now ARIA supervised pi through a tmux pane, i.e. by
md5-ing 100 lines of rendered text. Every signal below (same tool with the same
arguments four times, an A-B-A-B tool cycle, three turns of pure monologue, a
provider that timed out on every request) is invisible in that pane but is one
`jq` away in the transcript pi already writes. ARIA had never opened the file.

THE SCHEMA (read off the real files on corsair, 2026-08-15 — not guessed;
`~/.pi/agent/sessions/--home-ben-Development-ProjectAria--/2026-08-09T12-48-58-338Z_019fe691-...jsonl`
and the infrastructure smoke session):

  Path      ~/.pi/agent/sessions/<cwd-slug>/<ISO-ts>_<session-uuid>.jsonl
            cwd-slug = "--" + cwd.strip("/").replace("/", "-") + "--"
            session-uuid = the ARIA coding-session id (session.py passes it as
            `--session-id`, backends/pi_code.py:46), which is what lets us find
            the transcript for a session at all.

  One JSON object per line, `type` discriminated:
    {"type":"session","version":3,"id":<uuid>,"timestamp":<iso>,"cwd":<path>}
    {"type":"model_change","id","parentId","timestamp","provider","modelId"}
    {"type":"thinking_level_change","id","parentId","timestamp","thinkingLevel"}
    {"type":"message","id","parentId","timestamp","message":{...}}

  message.role is one of user | assistant | toolResult:
    user       {"role","content":[{"type":"text","text"}],"timestamp":<ms>}
    assistant  {"role","content":[ {"type":"thinking","thinking",
                                    "thinkingSignature"}
                                 | {"type":"text","text"}
                                 | {"type":"toolCall","id","name",
                                    "arguments":{...}} ],
                "api","provider","model","stopReason","rawStopReason",
                "responseId","timestamp":<ms>,
                "usage":{"input","output","cacheRead","cacheWrite","reasoning",
                         "totalTokens","cost":{...}},
                "errorMessage": <only when stopReason == "error">}
    toolResult {"role","content":[{"type":"text","text"}],"isError":<bool>,
                "toolCallId","toolName","timestamp":<ms>,"details":{...}}

  `arguments` is a real JSON object, not a string. `stopReason` observed:
  stop | toolUse | aborted | error. An `error` turn carries content=[] and an
  `errorMessage` ("Request timed out." — four in a row on the live file, which
  is exactly the repeated-error signal this module extracts).

Parsing is deliberately tolerant: the file is being appended to *while* we read
it, so the last line is routinely a partial object; unknown `type`s and unknown
keys are ignored rather than raising; a missing file is None, not an error. A
supervisor that crashes on a half-written line is worse than no supervisor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

PI_SESSIONS_ROOT = Path("~/.pi/agent/sessions").expanduser()

# Read at most this much of a transcript. Long sessions grow without bound and
# every signal here is about *recent* behaviour, so the tail is the honest
# window; `PiTranscript.truncated` says when we took one.
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024

_WS = re.compile(r"\s+")


def cwd_slug(path: str | os.PathLike) -> str:
    """pi's directory name for a working directory.

    Verified against every directory currently in ~/.pi/agent/sessions:
    /home/ben -> --home-ben--, /home/ben/Development/ProjectAria ->
    --home-ben-Development-ProjectAria--.
    """
    text = str(path).strip()
    return "--" + text.strip("/").replace("/", "-") + "--"


def find_transcript(
    session_id: str,
    workspace: Optional[str] = None,
    root: Optional[Path] = None,
) -> Optional[Path]:
    """Locate the transcript for an ARIA coding-session id.

    The cwd slug is tried first (one stat instead of a walk), then a glob across
    every session directory. The fallback is not paranoia: a session that runs
    in a guard worktree has a cwd of `<repo>/.worktrees/<project>-<sid8>`, not
    the workspace the caller knows about, so the slug guess misses exactly the
    sessions the supervisor cares most about.
    """
    base = Path(root) if root is not None else PI_SESSIONS_ROOT
    if not session_id:
        return None
    pattern = f"*_{session_id}.jsonl"
    try:
        if workspace:
            direct = base / cwd_slug(workspace)
            matches = sorted(direct.glob(pattern))
            if matches:
                return matches[-1]
        matches = sorted(base.glob(f"*/{pattern}"))
    except OSError as exc:  # unreadable sessions root — not fatal, just blind
        logger.debug("pi transcript lookup failed for %s: %s", session_id, exc)
        return None
    return matches[-1] if matches else None


def _hash_args(arguments: Any) -> str:
    """Stable short hash of a tool call's arguments.

    Sorted-key JSON so `{"a":1,"b":2}` and `{"b":2,"a":1}` are the same call —
    the loop detector compares argument *identity*, and pi's serialisation order
    is not guaranteed across turns.
    """
    try:
        blob = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(arguments)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]


def _norm_error(text: str) -> str:
    """One comparable line out of an error blob: first non-empty line, squashed
    whitespace, capped. Errors arrive both as a provider `errorMessage` and as a
    multi-line tool stderr; both have to compare equal to themselves."""
    for line in (text or "").splitlines():
        line = _WS.sub(" ", line).strip()
        if line:
            return line[:300]
    return ""


def _ts(value: Any) -> Optional[datetime]:
    """pi writes ISO strings at the record level and epoch-ms inside `message`."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


@dataclass
class PiToolCall:
    """One tool invocation. `args_hash` is what the loop detectors compare."""
    name: str
    args_hash: str
    call_id: str = ""
    arguments: dict = field(default_factory=dict)
    at: Optional[datetime] = None
    is_error: Optional[bool] = None  # filled in from the matching toolResult

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.args_hash)


@dataclass
class PiUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    total: int = 0

    def add(self, raw: dict) -> None:
        if not isinstance(raw, dict):
            return
        self.input += int(raw.get("input") or 0)
        self.output += int(raw.get("output") or 0)
        self.cache_read += int(raw.get("cacheRead") or 0)
        self.cache_write += int(raw.get("cacheWrite") or 0)
        self.reasoning += int(raw.get("reasoning") or 0)
        self.total += int(raw.get("totalTokens") or 0)

    def to_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "reasoning": self.reasoning,
            "total": self.total,
        }


@dataclass
class PiTurn:
    """One assistant turn: what it said, what it called, what it cost."""
    index: int
    at: Optional[datetime] = None
    text: str = ""
    thinking_chars: int = 0
    tool_calls: list[PiToolCall] = field(default_factory=list)
    stop_reason: str = ""
    error_message: str = ""
    usage: dict = field(default_factory=dict)

    @property
    def has_tool_call(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class PiTranscript:
    """Parsed transcript + the derived stuck signals."""
    session_id: str = ""
    path: Optional[Path] = None
    cwd: str = ""
    provider: str = ""
    model: str = ""
    turns: list[PiTurn] = field(default_factory=list)
    tool_calls: list[PiToolCall] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    usage: PiUsage = field(default_factory=PiUsage)
    user_messages: int = 0
    malformed_lines: int = 0
    truncated: bool = False
    last_activity_at: Optional[datetime] = None

    # -------------------------------------------------------------- signals

    def recent_tool_calls(self, limit: int = 20) -> list[tuple[str, str]]:
        """The (tool, args_hash) tail, oldest first — the supervisor's input."""
        return [call.key for call in self.tool_calls[-max(0, limit):]]

    def repeating_tool_call(self, threshold: int) -> Optional[tuple[str, str, int]]:
        """The last `threshold` tool calls are the same (tool, args).

        Consecutive-identical, not most-common-overall: OpenHands' detector uses
        the tail because a tool called four times across a long session is
        normal work, while four in a row is a loop.
        """
        if threshold <= 0 or len(self.tool_calls) < threshold:
            return None
        tail = self.tool_calls[-threshold:]
        first = tail[0].key
        if all(call.key == first for call in tail):
            return (first[0], first[1], threshold)
        return None

    def alternating_tool_pair(self, threshold: int) -> Optional[tuple[str, str, int]]:
        """The last `threshold` calls alternate between exactly two distinct
        calls (A-B-A-B-A-B). `threshold` is rounded down to an even count."""
        span = threshold - (threshold % 2)
        if span < 4 or len(self.tool_calls) < span:
            return None
        tail = [call.key for call in self.tool_calls[-span:]]
        a, b = tail[0], tail[1]
        if a == b:
            return None
        for i, key in enumerate(tail):
            if key != (a if i % 2 == 0 else b):
                return None
        return (a[0], b[0], span)

    def trailing_monologue_turns(self) -> int:
        """Assistant turns since the last tool call.

        Errored/aborted turns don't count: a provider timeout produces a turn
        with no tool call and no text, and calling that "monologue" would label
        a dead endpoint as a reasoning loop and send the ladder down the wrong
        branch.
        """
        count = 0
        for turn in reversed(self.turns):
            if turn.has_tool_call:
                break
            if turn.stop_reason in ("error", "aborted"):
                break
            count += 1
        return count

    def repeated_error(self, threshold: int) -> Optional[tuple[str, int]]:
        """The last `threshold` errors are the same line."""
        if threshold <= 0 or len(self.errors) < threshold:
            return None
        tail = self.errors[-threshold:]
        if tail[0] and all(line == tail[0] for line in tail):
            return (tail[0], threshold)
        return None

    def to_summary(self) -> dict:
        """Small, JSON-safe view for alerts and the /steward surfaces."""
        return {
            "path": str(self.path) if self.path else None,
            "provider": self.provider,
            "model": self.model,
            "turns": len(self.turns),
            "tool_calls": len(self.tool_calls),
            "errors": len(self.errors),
            "usage": self.usage.to_dict(),
            "malformed_lines": self.malformed_lines,
            "truncated": self.truncated,
            "last_activity_at": self.last_activity_at,
        }


def parse_lines(lines: Iterable[str], *, session_id: str = "") -> PiTranscript:
    """Parse JSONL records into a PiTranscript. Never raises on bad input."""
    transcript = PiTranscript(session_id=session_id)
    by_call_id: dict[str, PiToolCall] = {}
    turn_index = 0

    raw_lines = [line for line in lines]
    for position, raw in enumerate(raw_lines):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # The last line of a live transcript is routinely half-written; that
            # is not corruption and must not be counted as one.
            if position < len(raw_lines) - 1:
                transcript.malformed_lines += 1
            continue
        if not isinstance(record, dict):
            transcript.malformed_lines += 1
            continue

        rtype = record.get("type")
        if rtype == "session":
            transcript.cwd = str(record.get("cwd") or "")
            transcript.session_id = str(record.get("id") or session_id)
            continue
        if rtype == "model_change":
            transcript.provider = str(record.get("provider") or transcript.provider)
            transcript.model = str(record.get("modelId") or transcript.model)
            continue
        if rtype != "message":
            continue  # unknown record types are ignored on purpose

        message = record.get("message")
        if not isinstance(message, dict):
            transcript.malformed_lines += 1
            continue

        at = _ts(message.get("timestamp")) or _ts(record.get("timestamp"))
        if at and (transcript.last_activity_at is None or at > transcript.last_activity_at):
            transcript.last_activity_at = at
        role = message.get("role")

        if role == "user":
            transcript.user_messages += 1
            continue

        if role == "toolResult":
            call = by_call_id.get(str(message.get("toolCallId") or ""))
            is_error = bool(message.get("isError"))
            if call is not None:
                call.is_error = is_error
            if is_error:
                transcript.errors.append(_norm_error(_text_of(message.get("content"))))
            continue

        if role != "assistant":
            continue

        turn = PiTurn(
            index=turn_index,
            at=at,
            stop_reason=str(message.get("stopReason") or ""),
            error_message=str(message.get("errorMessage") or ""),
            usage=dict(message.get("usage") or {}),
        )
        turn_index += 1
        transcript.usage.add(message.get("usage") or {})

        for part in message.get("content") or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                turn.text += str(part.get("text") or "")
            elif ptype == "thinking":
                turn.thinking_chars += len(str(part.get("thinking") or ""))
            elif ptype == "toolCall":
                arguments = part.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {"_raw": arguments}
                call = PiToolCall(
                    name=str(part.get("name") or "?"),
                    args_hash=_hash_args(arguments),
                    call_id=str(part.get("id") or ""),
                    arguments=arguments,
                    at=at,
                )
                turn.tool_calls.append(call)
                transcript.tool_calls.append(call)
                if call.call_id:
                    by_call_id[call.call_id] = call

        if turn.error_message:
            transcript.errors.append(_norm_error(turn.error_message))
        transcript.turns.append(turn)

    return transcript


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text") or "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _read_tail(path: Path, max_bytes: int) -> tuple[list[str], bool]:
    size = path.stat().st_size
    truncated = size > max_bytes
    with path.open("rb") as fh:
        if truncated:
            fh.seek(size - max_bytes)
        blob = fh.read()
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]  # the seek landed mid-record
    return lines, truncated


async def load_transcript(
    session_id: str,
    workspace: Optional[str] = None,
    *,
    root: Optional[Path] = None,
    max_bytes: int = MAX_TRANSCRIPT_BYTES,
) -> Optional[PiTranscript]:
    """Find and parse a session's transcript. None when there isn't one.

    Blocking file work runs in a thread — this is called from the supervisor
    tick, and a slow NFS-ish stat must not stall the event loop.
    """
    def _load() -> Optional[PiTranscript]:
        path = find_transcript(session_id, workspace, root)
        if path is None:
            return None
        try:
            lines, truncated = _read_tail(path, max_bytes)
        except OSError as exc:
            logger.debug("pi transcript unreadable (%s): %s", path, exc)
            return None
        transcript = parse_lines(lines, session_id=session_id)
        transcript.path = path
        transcript.truncated = truncated
        return transcript

    return await asyncio.to_thread(_load)
