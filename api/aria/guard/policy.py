"""
ARIA - Guard Policy

Purpose: Answer "may an agent touch this path?" from a human-readable file, and
notice when the answer itself has been edited.

Two rules shape every design decision here (proposal §2 principle 12: *the
evaluator and the kill switch are unwritable by the thing they evaluate*):

1. **The policy file can only TIGHTEN, never loosen.** Deny-side lists
   (`protected_paths`, sandbox masks) are the UNION of `config.py` and
   `guard/policy.yaml`; numeric caps take the stricter of the two. So an agent
   that rewrites `guard/policy.yaml` cannot un-protect `guard/**` — the worst it
   can do is make its own life harder, and the tamper hash catches the edit
   anyway. Every documented self-improvement failure (DGM deleting its own
   hallucination markers, o3 rewriting shutdown scripts) is the shape this rule
   exists to break.
2. **A policy we cannot parse is not a policy we obey.** A malformed file falls
   back to the compiled-in `settings.guard_protected_paths` and records a
   critical guard event, rather than being partially applied — corrupting the
   file must not be a way to shrink the deny list.

**Parser choice (deliberate).** `pyyaml` is present in the venv but is NOT in
`api/requirements.txt` — it arrived as somebody's transitive dependency and could
vanish on the next `pip install -r`. Guard code that stops loading its policy
because an undeclared package disappeared is exactly the silent-degradation class
this whole subsystem exists to prevent, and adding the dependency is out of scope
(approved-libs rule). So the file is real YAML *syntax* (readable, editable,
highlighted) parsed by the strict ~90-line subset parser below: nested mappings,
sequences of scalars, and scalars. Anything outside that subset (flow
collections, anchors, multi-line strings, tabs) raises rather than being guessed
at — see rule 2.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aria.config import settings

logger = logging.getLogger(__name__)

GUARD_EVENTS_COLLECTION = "guard_events"
POLICY_STATE_ID = "guard_policy"

# Protected no matter what any file or setting says. These are the paths whose
# whole purpose is to constrain the agent, so letting the constraint list decide
# whether they are constrained would be circular. `.git/**` is here because a
# writable hook directory is remote code execution against ARIA's own git
# operations (the Amazon Q wiper incident, proposal §13).
_IMMUTABLE_FLOOR: tuple[str, ...] = (
    "guard/**",
    "api/aria/guard/**",
    ".git/**",
)


class PolicyError(Exception):
    """Raised when guard/policy.yaml exists but cannot be understood."""


# ---------------------------------------------------------------------------
# Minimal strict YAML subset parser
# ---------------------------------------------------------------------------

_SCALAR_TRUE = {"true", "yes", "on"}
_SCALAR_FALSE = {"false", "no", "off"}
_SCALAR_NULL = {"null", "~", ""}


def _parse_scalar(raw: str, lineno: int) -> Any:
    text = raw.strip()
    if text[:1] in {'"', "'"}:
        quote = text[0]
        end = text.find(quote, 1)
        if end == -1:
            raise PolicyError(f"line {lineno}: unterminated {quote} string")
        value = text[1:end]
        rest = text[end + 1:].strip()
        if rest and not rest.startswith("#"):
            raise PolicyError(f"line {lineno}: trailing text after quoted value: {rest!r}")
        return value
    # Unquoted: an inline comment needs whitespace before '#', so that a value
    # like `refs/wip/#1` is not silently truncated.
    cut = re.search(r"\s#", text)
    if cut:
        text = text[: cut.start()].strip()
    lowered = text.lower()
    if lowered in _SCALAR_TRUE:
        return True
    if lowered in _SCALAR_FALSE:
        return False
    if lowered in _SCALAR_NULL:
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    if text[:1] in "[{&*|>":
        raise PolicyError(
            f"line {lineno}: {text[:1]!r} starts a YAML feature this parser "
            "does not support (flow collections, anchors, block scalars). "
            "Use the plain key/list/scalar subset."
        )
    return text


def _tokenize(text: str) -> list[tuple[int, int, str]]:
    """(lineno, indent, content) for every significant line."""
    out: list[tuple[int, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise PolicyError(f"line {lineno}: tab used for indentation")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        out.append((lineno, indent, stripped))
    return out


def _parse_block(tokens: list[tuple[int, int, str]], idx: int, indent: int) -> tuple[Any, int]:
    lineno, first_indent, first = tokens[idx]
    if first.startswith("- "):
        items: list[Any] = []
        while idx < len(tokens):
            lineno, cur_indent, content = tokens[idx]
            if cur_indent < indent or not content.startswith("- "):
                break
            if cur_indent > indent:
                raise PolicyError(f"line {lineno}: unexpected indentation in list")
            items.append(_parse_scalar(content[2:], lineno))
            idx += 1
        return items, idx

    mapping: dict[str, Any] = {}
    while idx < len(tokens):
        lineno, cur_indent, content = tokens[idx]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise PolicyError(f"line {lineno}: unexpected indentation")
        if content.startswith("- "):
            break
        if ":" not in content:
            raise PolicyError(f"line {lineno}: expected 'key: value' or 'key:', got {content!r}")
        key, _, rest = content.partition(":")
        key = key.strip()
        rest = rest.strip()
        idx += 1
        if rest and not rest.startswith("#"):
            mapping[key] = _parse_scalar(rest, lineno)
            continue
        if idx < len(tokens) and tokens[idx][1] > cur_indent:
            value, idx = _parse_block(tokens, idx, tokens[idx][1])
            mapping[key] = value
        elif idx < len(tokens) and tokens[idx][1] == cur_indent and tokens[idx][2].startswith("- "):
            # Sequence at the same indentation as its key (valid YAML).
            value, idx = _parse_block(tokens, idx, cur_indent)
            mapping[key] = value
        else:
            mapping[key] = None
    return mapping, idx


def parse_simple_yaml(text: str) -> dict:
    """Parse the supported subset. Raises PolicyError on anything else."""
    tokens = _tokenize(text)
    if not tokens:
        return {}
    value, idx = _parse_block(tokens, 0, tokens[0][1])
    if idx != len(tokens):
        raise PolicyError(f"line {tokens[idx][0]}: could not parse remainder of file")
    if not isinstance(value, dict):
        raise PolicyError("policy file must be a mapping at the top level")
    return value


# ---------------------------------------------------------------------------
# Path matching
# ---------------------------------------------------------------------------

_regex_cache: dict[str, re.Pattern] = {}


def _compile(pattern: str) -> re.Pattern:
    """Glob → regex with gitignore-ish semantics.

    `fnmatch` alone is wrong here in two ways that matter: its `*` happily
    crosses `/` (so `api/*` would match `api/aria/guard/x.py`), and it has no
    concept of `**`, so `api/aria/guard/**` would not match the directory itself
    and `**/hooks/**` would not match a top-level `hooks/`.
    """
    cached = _regex_cache.get(pattern)
    if cached is not None:
        return cached

    p = pattern.strip().lstrip("/")
    # A pattern with no slash matches that basename at any depth (gitignore
    # semantics), so `.env` also covers `api/.env` — the fail-closed direction.
    any_depth = "/" not in p.strip("/")
    trailing_tree = p.endswith("/**")
    if trailing_tree:
        p = p[:-3]

    parts: list[str] = []
    if any_depth:
        parts.append(r"(?:.*/)?")
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            parts.append(r"(?:[^/]*/)*")
            i += 3
        elif p.startswith("**", i):
            parts.append(r".*")
            i += 2
        elif p[i] == "*":
            parts.append(r"[^/]*")
            i += 1
        elif p[i] == "?":
            parts.append(r"[^/]")
            i += 1
        else:
            parts.append(re.escape(p[i]))
            i += 1
    if trailing_tree:
        parts.append(r"(?:/.*)?")

    compiled = re.compile("".join(parts) + r"\Z")
    _regex_cache[pattern] = compiled
    return compiled


def match_any(path: str, patterns) -> Optional[str]:
    """Return the first pattern matching `path` (repo-relative), else None."""
    rel = path.replace(os.sep, "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")   # never str.lstrip("./"): that would eat the dot of ".env"
    for pattern in patterns:
        if _compile(pattern).match(rel):
            return pattern
    return None


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

@dataclass
class GuardPolicy:
    source: str                       # "file" | "settings"
    path: Optional[str]
    protected_paths: list[str]
    sandbox_tmpfs_paths: list[str]
    sandbox_ro_paths: list[str]
    sandbox_rw_paths: list[str]
    diff_max_lines: int
    diff_max_files: int
    gitleaks_enabled: bool
    checkpoint_max_file_bytes: int
    checkpoint_max_total_bytes: int
    hash: str
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "path": self.path,
            "hash": self.hash,
            "error": self.error,
            "protected_paths": self.protected_paths,
            "sandbox": {
                "tmpfs": self.sandbox_tmpfs_paths,
                "ro": self.sandbox_ro_paths,
                "rw": self.sandbox_rw_paths,
            },
            "merge_gate": {
                "diff_max_lines": self.diff_max_lines,
                "diff_max_files": self.diff_max_files,
                "gitleaks_enabled": self.gitleaks_enabled,
            },
            "checkpoint": {
                "max_file_bytes": self.checkpoint_max_file_bytes,
                "max_total_bytes": self.checkpoint_max_total_bytes,
            },
        }


# Defaults for the two knobs config.py has no field for. They are the values
# git-safety-net.sh settled on after a naive `git add -A` hashed 18 GB of
# unignored model weights on 2026-08-15 and put 6 GB of loose objects in .git.
DEFAULT_CHECKPOINT_MAX_FILE_BYTES = 10 * 1024 * 1024      # 10 MiB
DEFAULT_CHECKPOINT_MAX_TOTAL_BYTES = 512 * 1024 * 1024    # 512 MiB

_cache: dict[str, Any] = {"key": None, "policy": None}


def repo_root() -> str:
    """The ProjectAria checkout this code is running from.

    Derived from __file__ (api/aria/guard/policy.py → three parents up) rather
    than from cwd: aria-api runs as a systemd unit whose cwd is not the repo.
    """
    return str(Path(__file__).resolve().parents[3])


def policy_file_path() -> str:
    configured = os.path.expanduser(settings.guard_policy_path)
    if os.path.isabs(configured):
        return configured
    return os.path.join(repo_root(), configured)


def _uniq(*lists) -> list[str]:
    seen: dict[str, None] = {}
    for items in lists:
        for item in items or []:
            if isinstance(item, str) and item.strip():
                seen.setdefault(item.strip(), None)
    return list(seen)


def _settings_policy(error: Optional[str], path: Optional[str]) -> GuardPolicy:
    """The compiled-in policy: what applies when no file exists, and the floor
    a file can only tighten."""
    payload = {
        "protected_paths": _uniq(_IMMUTABLE_FLOOR, settings.guard_protected_paths),
        "tmpfs": list(settings.guard_sandbox_tmpfs_paths),
        "ro": list(settings.guard_sandbox_ro_paths),
        "rw": list(settings.guard_sandbox_rw_paths),
        "diff_max_lines": settings.guard_diff_max_lines,
        "diff_max_files": settings.guard_diff_max_files,
        "gitleaks": settings.guard_gitleaks_enabled,
    }
    digest = hashlib.sha256(
        ("settings:" + json.dumps(payload, sort_keys=True)).encode("utf-8")
    ).hexdigest()
    return GuardPolicy(
        source="settings",
        path=path,
        protected_paths=payload["protected_paths"],
        sandbox_tmpfs_paths=payload["tmpfs"],
        sandbox_ro_paths=payload["ro"],
        sandbox_rw_paths=payload["rw"],
        diff_max_lines=payload["diff_max_lines"],
        diff_max_files=payload["diff_max_files"],
        gitleaks_enabled=payload["gitleaks"],
        checkpoint_max_file_bytes=DEFAULT_CHECKPOINT_MAX_FILE_BYTES,
        checkpoint_max_total_bytes=DEFAULT_CHECKPOINT_MAX_TOTAL_BYTES,
        hash=digest,
        error=error,
    )


def load_policy(path: Optional[str] = None, *, force: bool = False) -> GuardPolicy:
    """Load the enforced policy, cached on (path, mtime, size).

    Never raises: an unreadable or malformed file degrades to the compiled-in
    settings policy with `.error` set, which callers surface as a critical guard
    event. Refusing to answer `is_protected()` would fail *open* at every call
    site, which is the opposite of what this module is for.
    """
    target = path or policy_file_path()
    try:
        stat = os.stat(target)
        key = (target, stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (target, None, None)

    if not force and _cache["key"] == key and _cache["policy"] is not None:
        return _cache["policy"]

    base = _settings_policy(None, target)
    policy = base
    if key[1] is not None:
        try:
            raw_bytes = Path(target).read_bytes()
            data = parse_simple_yaml(raw_bytes.decode("utf-8"))
            policy = _merge_file_policy(base, data, target, raw_bytes)
        except (PolicyError, OSError, UnicodeDecodeError) as exc:
            logger.error(
                "guard: %s is unreadable/malformed (%s) — falling back to "
                "settings.guard_protected_paths. The deny list is NOT reduced.",
                target, exc,
            )
            policy = _settings_policy(f"{type(exc).__name__}: {exc}", target)

    _cache["key"] = key
    _cache["policy"] = policy
    return policy


def _merge_file_policy(
    base: GuardPolicy, data: dict, target: str, raw_bytes: bytes
) -> GuardPolicy:
    """Apply the file on top of the settings floor — tighten-only (see module
    docstring rule 1)."""
    sandbox = data.get("sandbox") or {}
    gate = data.get("merge_gate") or {}
    checkpoint = data.get("checkpoint") or {}
    if not isinstance(sandbox, dict) or not isinstance(gate, dict) or not isinstance(checkpoint, dict):
        raise PolicyError("sandbox/merge_gate/checkpoint must be mappings")

    def _min_int(file_value, floor_value: int, name: str) -> int:
        if file_value is None:
            return floor_value
        if not isinstance(file_value, int) or isinstance(file_value, bool) or file_value < 0:
            raise PolicyError(f"{name} must be a non-negative integer")
        return min(file_value, floor_value)

    return GuardPolicy(
        source="file",
        path=target,
        protected_paths=_uniq(base.protected_paths, data.get("protected_paths") or []),
        sandbox_tmpfs_paths=_uniq(base.sandbox_tmpfs_paths, sandbox.get("tmpfs_paths") or []),
        sandbox_ro_paths=_uniq(base.sandbox_ro_paths, sandbox.get("ro_paths") or []),
        sandbox_rw_paths=_uniq(base.sandbox_rw_paths, sandbox.get("rw_paths") or []),
        diff_max_lines=_min_int(gate.get("diff_max_lines"), base.diff_max_lines, "diff_max_lines"),
        diff_max_files=_min_int(gate.get("diff_max_files"), base.diff_max_files, "diff_max_files"),
        # A file may turn gitleaks ON when settings has it off, but not off when
        # settings has it on — same tighten-only rule.
        gitleaks_enabled=bool(gate.get("gitleaks_enabled")) or base.gitleaks_enabled,
        checkpoint_max_file_bytes=_min_int(
            checkpoint.get("max_file_bytes"), base.checkpoint_max_file_bytes, "max_file_bytes"
        ),
        checkpoint_max_total_bytes=_min_int(
            checkpoint.get("max_total_bytes"), base.checkpoint_max_total_bytes, "max_total_bytes"
        ),
        hash=hashlib.sha256(b"file:" + raw_bytes).hexdigest(),
        raw=data,
    )


def is_protected(
    path: str, repo_root_path: Optional[str] = None, policy: Optional[GuardPolicy] = None
) -> bool:
    """Is `path` off-limits to every agent at every autonomy level?

    `path` may be repo-relative or absolute. An absolute path that is not inside
    `repo_root_path` is protected — a diff should never name one, and "I don't
    know where this is" must not read as "allowed".
    """
    policy = policy or load_policy()
    rel = _relativize(path, repo_root_path)
    if rel is None:
        return True
    return match_any(rel, policy.protected_paths) is not None


def protecting_pattern(
    path: str, repo_root_path: Optional[str] = None, policy: Optional[GuardPolicy] = None
) -> Optional[str]:
    """Which rule protects `path` (for an explainable gate verdict)."""
    policy = policy or load_policy()
    rel = _relativize(path, repo_root_path)
    if rel is None:
        return "<outside repo root>"
    return match_any(rel, policy.protected_paths)


def _relativize(path: str, repo_root_path: Optional[str]) -> Optional[str]:
    normalized = os.path.normpath(path)
    if not os.path.isabs(normalized):
        # `../../.ssh/id_ed25519` escapes the repo just as surely as an absolute
        # path does, so it gets the same fail-closed answer.
        if normalized == ".." or normalized.startswith(".." + os.sep):
            return None
        return normalized.replace(os.sep, "/")
    if not repo_root_path:
        return None
    root = os.path.normpath(os.path.abspath(repo_root_path))
    try:
        rel = os.path.relpath(normalized, root)
    except ValueError:
        return None
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return rel.replace(os.sep, "/")


def policy_hash() -> str:
    """Hash of the enforced policy — file bytes when a file is in force, the
    canonical settings payload otherwise."""
    return load_policy().hash


# ---------------------------------------------------------------------------
# guard_events + tamper detection
# ---------------------------------------------------------------------------

async def record_event(
    db,
    kind: str,
    detail: str,
    *,
    session_id: Optional[str] = None,
    path: Optional[str] = None,
    blocked: bool = False,
    severity: str = "info",
    actor: str = "guard",
    extra: Optional[dict] = None,
) -> dict:
    """Append to `guard_events`. Never raises.

    A guard event that cannot be written must still be visible, so the log line
    is emitted first and at WARNING/ERROR for anything blocked or critical —
    losing the record of a blocked action to a Mongo hiccup would make the guard
    silently unaccountable, the failure class this subsystem exists to close.
    """
    event = {
        "kind": kind,
        "detail": detail,
        "session_id": session_id,
        "path": path,
        "blocked": bool(blocked),
        "severity": severity,
        "actor": actor,
        "at": datetime.now(timezone.utc),
    }
    if extra:
        event.update(extra)

    log = logger.error if severity == "critical" else (logger.warning if blocked else logger.info)
    log("guard event %s: %s (session=%s path=%s blocked=%s)",
        kind, detail, session_id, path, blocked)

    if db is None:
        return event
    try:
        await db[GUARD_EVENTS_COLLECTION].insert_one(dict(event))
    except Exception:  # noqa: BLE001 — telemetry must never break the guard
        logger.warning("guard: could not persist event %s", kind, exc_info=True)
    return event


async def verify_policy(db, expected_hash: Optional[str] = None) -> dict:
    """Compare the enforced policy against the accepted hash (principle 12).

    Trust-on-first-use: with no stored hash we accept the current one and say so.
    That is the honest position — there is no earlier state to compare against —
    and it means installing the guard does not require a manual blessing step
    that would get skipped.
    """
    policy = load_policy(force=True)
    current = policy.hash
    result: dict[str, Any] = {
        "ok": True,
        "status": "ok",
        "current_hash": current,
        "accepted_hash": expected_hash,
        "source": policy.source,
        "path": policy.path,
        "parse_error": policy.error,
    }

    if policy.error:
        result["ok"] = False
        result["status"] = "unparseable"
        await record_event(
            db, "policy:unparseable",
            f"{policy.path}: {policy.error} — enforcing settings defaults",
            blocked=True, severity="critical", path=policy.path,
        )

    if expected_hash is None:
        stored = None
        if db is not None:
            try:
                stored = await db.app_state.find_one({"_id": POLICY_STATE_ID})
            except Exception:  # noqa: BLE001
                logger.warning("guard: could not read accepted policy hash", exc_info=True)
                result["status"] = "unknown"
                result["detail"] = "accepted hash unreadable"
                return result
        if not stored:
            await accept_policy(db, current, actor="trust-on-first-use")
            result["accepted_hash"] = current
            result["status"] = "trusted_on_first_use"
            return result
        result["accepted_hash"] = stored.get("hash")

    if result["accepted_hash"] and result["accepted_hash"] != current:
        result["ok"] = False
        result["status"] = "tamper"
        await record_event(
            db, "policy:tamper",
            f"enforced guard policy hash {current[:12]} != accepted "
            f"{str(result['accepted_hash'])[:12]} ({policy.path})",
            blocked=True, severity="critical", path=policy.path,
        )
    return result


async def accept_policy(db, policy_hash_value: str, actor: str = "api") -> dict:
    """Bless a policy hash as the new expected one (a deliberate human update)."""
    doc = {
        "_id": POLICY_STATE_ID,
        "hash": policy_hash_value,
        "accepted_by": actor,
        "accepted_at": datetime.now(timezone.utc),
    }
    if db is not None:
        try:
            await db.app_state.update_one(
                {"_id": POLICY_STATE_ID}, {"$set": doc}, upsert=True
            )
        except Exception:  # noqa: BLE001
            logger.warning("guard: could not store accepted policy hash", exc_info=True)
    await record_event(
        db, "policy:accepted", f"policy hash {policy_hash_value[:12]} accepted by {actor}",
        actor=actor,
    )
    return doc
