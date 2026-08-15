"""
ARIA - Guard Policy

Purpose: Answer "may an agent touch this path?" from a human-readable file, and
notice when the answer itself has been edited.

Two rules shape every design decision here (proposal §2 principle 12: *the
evaluator and the kill switch are unwritable by the thing they evaluate*):

1. **The policy file can only TIGHTEN, never loosen.** Deny-side lists
   (`protected_paths`, sandbox masks) are the UNION of `config.py` and
   `guard/policy.yaml`; allow-side lists (the sandbox's `ro_paths`/`rw_paths`
   exceptions) are the INTERSECTION with the compiled-in floor; numeric caps
   take the stricter of the two. So an agent that rewrites `guard/policy.yaml`
   cannot un-protect `guard/**` — the worst it can do is make its own life
   harder, and the tamper hash catches the edit anyway. Every documented
   self-improvement failure (DGM deleting its own hallucination markers, o3
   rewriting shutdown scripts) is the shape this rule exists to break.

   ⚠️ The allow-side half of that rule was NOT true until 2026-08-15: `ro_paths`
   and `rw_paths` were unioned like the deny lists, and a reviewer's policy file
   containing `ro_paths: ["~/.ssh"]` + `rw_paths: ["/home/ben"]` produced
   `--tmpfs ~/.ssh` at argv index 140 and then `--ro-bind-try ~/.ssh` at index
   526 and `--bind /home/ben /home/ben` at index 705 — a later bind wins over an
   earlier mask, so the file un-masked the SSH keys and made the whole home
   writable. A union is only tighten-only for lists that *deny*. Adding an entry
   to an *exception* list is a loosening by construction, so the file may only
   drop entries from `ro_paths`/`rw_paths`, never add them; anything it names
   that the floor does not is reported in `GuardPolicy.rejected` and ignored.
   `sandbox.py` enforces the same property a second time against the argv it
   actually emits — one layer can be wrong; both being wrong takes two mistakes.
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

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aria.config import settings

logger = logging.getLogger(__name__)

GUARD_EVENTS_COLLECTION = "guard_events"
POLICY_STATE_ID = "guard_policy"

# The accepted-hash record ALSO lives on disk, outside Mongo. Mongo is bound to
# 0.0.0.0 with no auth (CLAUDE.md, "Security posture (S4)") and the sandbox
# deliberately leaves the network open, so a session can reach :27017 and delete
# `app_state/_id=guard_policy` in one call. When that document was the only
# record of an acceptance, deleting it re-armed trust-on-first-use and the next
# boot blessed whatever policy.yaml said — edit the policy, drop one document,
# and the widened policy is "first use" with no e-stop and no alert.
#
# ~/.aria is in `guard_sandbox_tmpfs_paths`, so this file is masked inside every
# guarded session: strictly harder for a session to reach than Mongo is. The env
# override exists for tests and for a future root-owned location (proposal D3,
# `/etc/aria/guard.yaml` via `sudo make install-guard`).
GUARD_STATE_ENV = "ARIA_GUARD_STATE_PATH"
DEFAULT_GUARD_STATE_PATH = "~/.aria/guard/accepted_policy.json"

# The ONE sanctioned way out of a tamper verdict. It is quoted into every
# failure detail and every critical event, because the alternative an operator
# reaches for otherwise — deleting the accepted-hash record — IS the attack this
# module exists to detect, and it would clear the alarm by disarming the alarm.
ACCEPT_REMEDY = (
    "If this change was deliberate, bless it through the admin path (never by "
    "deleting the acceptance record — that is the re-arm attack itself):\n"
    "  HASH=$(curl -s localhost:8200/api/v1/guard/status | "
    "python3 -c 'import json,sys; print(json.load(sys.stdin)[\"policy\"][\"hash\"])')\n"
    "  curl -XPOST localhost:8200/api/v1/guard/policy/accept "
    "-H \"X-Admin-Key: $ADMIN_KEY\" -H 'Content-Type: application/json' "
    "-d \"{\\\"hash\\\": \\\"$HASH\\\"}\"\n"
    "(offline: cd ~/Development/ProjectAria/api && python3 -c "
    "'from aria.guard.policy import policy_hash; print(policy_hash())')"
)

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

# The cache is keyed by CALLER-SUPPLIED strings: `_check_allowed_paths` compiles
# whatever a charter's `allowed_paths` contains, so an unbounded dict is a slow
# memory leak in a process that never restarts. FIFO eviction, not LRU: the
# entries that matter (the policy's own patterns) are re-inserted on the next
# match anyway, and a dict preserves insertion order for free.
_REGEX_CACHE_MAX = 512
_PATTERN_MAX_LEN = 512


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

    if len(pattern) > _PATTERN_MAX_LEN:
        # Not cached and not compiled: a multi-kilobyte "pattern" is not a path
        # rule, and compiling it would be the caller choosing our CPU budget.
        raise PolicyError(
            f"path pattern is {len(pattern)} chars (max {_PATTERN_MAX_LEN})"
        )

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
    while len(_regex_cache) >= _REGEX_CACHE_MAX:
        _regex_cache.pop(next(iter(_regex_cache)))
    _regex_cache[pattern] = compiled
    return compiled


def match_any(path: str, patterns) -> Optional[str]:
    """Return the first pattern matching `path` (repo-relative), else None."""
    rel = path.replace(os.sep, "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")   # never str.lstrip("./"): that would eat the dot of ".env"
    for pattern in patterns:
        try:
            compiled = _compile(pattern)
        except PolicyError:
            # Dropping one pattern SHRINKS whatever list it came from. For the
            # caller-supplied list (`allowed_paths`) that is the fail-closed
            # direction — the path lands outside the charter — which is the only
            # list a caller can fill. Deny lists come from the hash-verified
            # policy file and never contain a pattern this long.
            logger.warning("guard: ignoring oversized path pattern (%d chars)", len(pattern))
            continue
        if compiled.match(rel):
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
    # Entries the file asked for that would have LOOSENED the sandbox. Kept
    # (rather than dropped silently) because "your policy file is trying to
    # widen the sandbox" is the single most interesting thing the guard can
    # notice about its own configuration.
    rejected: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "path": self.path,
            "hash": self.hash,
            "error": self.error,
            "protected_paths": self.protected_paths,
            "rejected": self.rejected,
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

    if policy.rejected:
        logger.error(
            "guard: %s tried to WIDEN the sandbox and was ignored: %s",
            target,
            "; ".join(f"{r['list']}: {r['path']}" for r in policy.rejected),
        )

    _cache["key"] = key
    _cache["policy"] = policy
    return policy


def normalize_path(raw: str) -> str:
    """The comparable form of a sandbox path (`~/.ssh` == `/home/ben/.ssh/`)."""
    return os.path.normpath(os.path.expanduser(str(raw).strip())) if str(raw).strip() else ""


def _narrow_exceptions(
    floor: list[str], file_values: Any, name: str, rejected: list[dict]
) -> list[str]:
    """Intersect an ALLOW list with the compiled-in floor.

    `ro_paths`/`rw_paths` are holes punched in the sandbox, so the union that
    the deny lists use is exactly backwards for them: it lets the file open a
    hole the code never sanctioned. The file may therefore only *remove*
    entries. Absent (or null) means "no opinion" and keeps the whole floor —
    otherwise deleting a key would silently break every session instead of
    tightening anything.
    """
    if file_values is None:
        return list(floor)
    if not isinstance(file_values, list):
        raise PolicyError(f"sandbox.{name} must be a list of paths")
    wanted = {normalize_path(v): v for v in file_values if isinstance(v, str) and v.strip()}
    kept = [entry for entry in floor if normalize_path(entry) in wanted]
    for norm, original in wanted.items():
        if not any(normalize_path(entry) == norm for entry in floor):
            rejected.append({"list": name, "path": original, "reason": "not in the compiled-in floor"})
    return kept


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

    rejected: list[dict] = []
    return GuardPolicy(
        source="file",
        path=target,
        protected_paths=_uniq(base.protected_paths, data.get("protected_paths") or []),
        sandbox_tmpfs_paths=_uniq(base.sandbox_tmpfs_paths, sandbox.get("tmpfs_paths") or []),
        sandbox_ro_paths=_narrow_exceptions(
            base.sandbox_ro_paths, sandbox.get("ro_paths"), "ro_paths", rejected
        ),
        sandbox_rw_paths=_narrow_exceptions(
            base.sandbox_rw_paths, sandbox.get("rw_paths"), "rw_paths", rejected
        ),
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
        rejected=rejected,
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


def guard_state_path() -> str:
    """Where the on-disk accepted-hash record lives.

    ⚠️ A test must NEVER write the production record. On 2026-08-15 a guard test
    run created `~/.aria/guard/accepted_policy.json` holding the hash of a
    tmp_path policy file; because `main.py` e-stops with `auto_thaw=False` when
    `verify_policy` is not ok, the next `systemctl --user restart aria-api`
    would have frozen every coding-session spawn until a human cleared it — a
    test taking down the system it tests. Tests are supposed to point
    `ARIA_GUARD_STATE_PATH` at tmp_path; the fallback below makes forgetting
    that harmless instead of load-bearing, because "every future test author
    remembers" is not a safety property.
    """
    override = os.environ.get(GUARD_STATE_ENV)
    if override:
        return os.path.expanduser(override)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        fallback = os.path.join(
            tempfile.gettempdir(), f"aria-guard-test-{os.getpid()}", "accepted_policy.json"
        )
        logger.warning(
            "guard: running under pytest with no %s; using %s instead of the real "
            "acceptance record", GUARD_STATE_ENV, fallback,
        )
        return fallback
    return os.path.expanduser(DEFAULT_GUARD_STATE_PATH)


def _read_state_file() -> tuple[Optional[dict], Optional[str]]:
    """(record, error) for the on-disk acceptance record.

    An absent file and an UNREADABLE one are deliberately different answers.
    Absent means "no acceptance has ever happened here" — the only situation in
    which trust-on-first-use is honest. Unreadable means the evidence exists and
    we cannot see it, which is the tamper shape, so it fails closed instead of
    being rounded down to "fresh install".
    """
    path = guard_state_path()
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{path}: {type(exc).__name__}: {exc}"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"{path}: not valid JSON ({exc})"
    if not isinstance(data, dict) or not isinstance(data.get("hash"), str) or not data["hash"]:
        return None, f"{path}: no 'hash' field"
    return data, None


def _write_state_file(policy_hash_value: str, actor: str) -> tuple[Optional[dict], Optional[str]]:
    """Append an acceptance to the on-disk record. (record, error).

    `history` is the audit trail the single Mongo document cannot be: deleting
    the document is now visible as "disk says an acceptance happened, Mongo says
    it never did". Written atomically (tmp + os.replace) at 0600 so a crash
    mid-write cannot leave a file that reads as "fresh install".
    """
    path = guard_state_path()
    now = datetime.now(timezone.utc).isoformat()
    existing, read_error = _read_state_file()
    if read_error:
        # Never overwrite unreadable evidence in place: a human accepting a
        # policy is allowed to repair the file, but the broken one is kept as
        # the record of how it got broken.
        try:
            os.replace(path, f"{path}.corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        except OSError:
            logger.warning("guard: could not preserve corrupt %s", path, exc_info=True)
    history = list((existing or {}).get("history") or [])
    history.append({"hash": policy_hash_value, "actor": actor, "at": now})
    record = {
        "hash": policy_hash_value,
        "accepted_by": actor,
        "accepted_at": now,
        "first_accepted_at": (existing or {}).get("first_accepted_at") or now,
        "history": history[-200:],
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".accepted-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(record, out, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        return None, f"{path}: {type(exc).__name__}: {exc}"
    return record, None


async def verify_policy(db, expected_hash: Optional[str] = None) -> dict:
    """Compare the enforced policy against the accepted hash (principle 12).

    Trust-on-first-use happens exactly ONCE per machine and leaves evidence in
    two places (`app_state/_id=guard_policy` and `guard_state_path()`), because
    a TOFU that can be re-armed is not a check. The attack it closes, verified
    against this code on 2026-08-15: edit `guard/policy.yaml`, delete the single
    `app_state` document over the un-authenticated Mongo the sandbox can reach,
    and the next boot "trusts on first use" — no tamper event, no e-stop.

    FAILS CLOSED. `ok=False` is what `main.py` turns into an e-stop, so every
    branch where we cannot prove the policy is the accepted one returns
    `ok=False`. The single exception is a Mongo read error *while the on-disk
    record still matches* — that is positive evidence from the harder-to-reach
    of the two stores, and e-stopping the box because Mongo hiccuped would make
    the guard the outage.
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
        "state_file": guard_state_path(),
    }

    if policy.error:
        result["ok"] = False
        result["status"] = "unparseable"
        await record_event(
            db, "policy:unparseable",
            f"{policy.path}: {policy.error} — enforcing settings defaults",
            blocked=True, severity="critical", path=policy.path,
        )

    if policy.rejected:
        # No effect on `ok`: the widening was already dropped at load time, and
        # an unauthorised edit is caught by the hash below. This event is how a
        # *blessed* file that quietly asks for more still gets read by a human.
        await record_event(
            db, "policy:rejected_widening",
            f"{policy.path} asked to widen the sandbox; ignored: "
            + "; ".join(f"{r['list']}={r['path']}" for r in policy.rejected[:10]),
            blocked=True, severity="critical", path=policy.path,
            extra={"rejected": policy.rejected},
        )

    if expected_hash is None:
        disk, disk_error = await asyncio.to_thread(_read_state_file)
        if disk_error:
            result["ok"] = False
            result["status"] = "unknown"
            result["detail"] = f"accepted-hash file unreadable: {disk_error}"
            result["remedy"] = ACCEPT_REMEDY
            await record_event(
                db, "policy:state_unreadable", f"{result['detail']}\n{ACCEPT_REMEDY}",
                blocked=True, severity="critical", path=guard_state_path(),
            )
            return result

        stored: Optional[dict] = None
        if db is not None:
            try:
                stored = await db.app_state.find_one({"_id": POLICY_STATE_ID})
            except Exception as exc:  # noqa: BLE001
                logger.warning("guard: could not read accepted policy hash", exc_info=True)
                if disk and disk["hash"] == current:
                    result["status"] = "ok_disk_only"
                    result["accepted_hash"] = disk["hash"]
                    result["detail"] = f"Mongo unreadable ({exc}); on-disk record matches"
                    return result
                result["ok"] = False
                result["status"] = "unknown"
                result["detail"] = f"accepted hash unreadable: {exc}"
                await record_event(
                    db, "policy:state_unreadable",
                    f"could not read the accepted policy hash from Mongo ({exc}) and the "
                    "on-disk record does not vouch for the current policy",
                    blocked=True, severity="critical", path=policy.path,
                )
                return result
        elif disk:
            # No database configured at all (in-process use, tests): the disk
            # record IS the store, not a second opinion about a missing one.
            stored = {"hash": disk["hash"]}

        if stored is None and disk is None:
            accepted = await accept_policy(db, current, actor="trust-on-first-use")
            result["accepted_hash"] = current
            result["status"] = "trusted_on_first_use"
            if accepted.get("state_error"):
                # Fails OPEN, loudly, and only here: a fresh install whose home
                # is not writable must still be usable, and the next boot has a
                # Mongo record to compare against anyway. The cost is that a
                # later deletion of that document re-arms TOFU on this box,
                # which is why it is a critical event and not a debug line.
                result["state_error"] = accepted["state_error"]
                await record_event(
                    db, "policy:state_unwritable",
                    f"trust-on-first-use could not write {guard_state_path()} "
                    f"({accepted['state_error']}) — deleting the Mongo record would "
                    "re-arm first-use on this box",
                    blocked=False, severity="critical", path=guard_state_path(),
                )
            return result

        if stored is None and disk is not None:
            # The re-arm. Restoring the document is deliberate: it makes the
            # attack single-use rather than repeatable, and the event below is
            # the record that it happened at all.
            result["ok"] = False
            result["status"] = "tamper"
            result["accepted_hash"] = disk["hash"]
            result["detail"] = (
                f"the accepted-hash document (app_state/_id={POLICY_STATE_ID}) is gone, "
                f"but {guard_state_path()} records an acceptance "
                f"({disk.get('accepted_by')} at {disk.get('accepted_at')}). "
                "That is a deletion, not a first run."
            )
            result["remedy"] = ACCEPT_REMEDY
            await record_event(
                db, "policy:state_reset", f"{result['detail']}\n{ACCEPT_REMEDY}",
                blocked=True, severity="critical", path=policy.path,
                extra={"disk_hash": disk["hash"], "current_hash": current},
            )
            if db is not None:
                try:
                    await db.app_state.update_one(
                        {"_id": POLICY_STATE_ID},
                        {"$set": {
                            "hash": disk["hash"],
                            "accepted_by": f"restored-from-disk({disk.get('accepted_by')})",
                            "accepted_at": datetime.now(timezone.utc),
                        }},
                        upsert=True,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("guard: could not restore the accepted hash", exc_info=True)
            return result

        result["accepted_hash"] = (stored or {}).get("hash")
        if disk is None and stored is not None and stored.get("hash"):
            # Upgrade path: this box accepted a policy before the on-disk record
            # existed. Mirror what Mongo already says rather than waiting for
            # the next manual accept — until the file exists, deleting the
            # document is still a reset button.
            _, mirror_error = await asyncio.to_thread(
                _write_state_file, str(stored["hash"]), "mirrored-from-mongo"
            )
            if mirror_error:
                logger.warning("guard: could not mirror the accepted hash to disk: %s", mirror_error)
        if disk is not None and stored is not None and disk["hash"] != stored.get("hash"):
            # Two stores, two answers: whichever is lying, we are not entitled
            # to pick the convenient one.
            result["ok"] = False
            result["status"] = "tamper"
            result["detail"] = (
                f"accepted hash differs between Mongo ({str(stored.get('hash'))[:12]}) "
                f"and {guard_state_path()} ({disk['hash'][:12]})"
            )
            result["remedy"] = ACCEPT_REMEDY
            await record_event(
                db, "policy:state_divergence", f"{result['detail']}\n{ACCEPT_REMEDY}",
                blocked=True, severity="critical", path=policy.path,
            )
            return result

    if result["accepted_hash"] and result["accepted_hash"] != current:
        result["ok"] = False
        result["status"] = "tamper"
        result["remedy"] = ACCEPT_REMEDY
        await record_event(
            db, "policy:tamper",
            f"enforced guard policy hash {current[:12]} != accepted "
            f"{str(result['accepted_hash'])[:12]} ({policy.path})\n{ACCEPT_REMEDY}",
            blocked=True, severity="critical", path=policy.path,
        )
    return result


async def accept_policy(db, policy_hash_value: str, actor: str = "api") -> dict:
    """Bless a policy hash as the new expected one (a deliberate human update).

    Writes BOTH stores. The on-disk half is what makes deleting the Mongo
    document detectable rather than a reset button.
    """
    doc = {
        "_id": POLICY_STATE_ID,
        "hash": policy_hash_value,
        "accepted_by": actor,
        "accepted_at": datetime.now(timezone.utc),
    }
    state, state_error = await asyncio.to_thread(_write_state_file, policy_hash_value, actor)
    if state_error:
        logger.error("guard: could not persist the accepted policy hash to disk: %s", state_error)
    if db is not None:
        try:
            await db.app_state.update_one(
                {"_id": POLICY_STATE_ID}, {"$set": doc}, upsert=True
            )
        except Exception:  # noqa: BLE001
            logger.warning("guard: could not store accepted policy hash", exc_info=True)
    await record_event(
        db, "policy:accepted", f"policy hash {policy_hash_value[:12]} accepted by {actor}",
        actor=actor, path=guard_state_path(),
    )
    return {**doc, "state_file": guard_state_path(),
            "state_error": state_error, "first_accepted_at": (state or {}).get("first_accepted_at")}
