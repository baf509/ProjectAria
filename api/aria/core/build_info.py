"""
ARIA - Build info

Phase: Steward / self-stewardship
Purpose: Tell ARIA which commit it is actually RUNNING, so it can notice when
         the code on disk has moved past the code in memory.

Related Spec Sections:
- ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md §8 (self-improvement)

Why this exists
---------------
ARIA is the only project it stewards that is also ITSELF. Every other project's
changes take effect when the gate passes and the merge lands; ARIA's take effect
on the next `systemctl --user restart aria-api` — and `aria-api` is deliberately
`manageable=False` in the service registry, because a process that restarts
itself mid-request drops the request that asked for it.

Until 2026-08-19 `/` and `/health` reported a hardcoded `version: "0.2.0"`, so a
running ARIA could not tell whether the file it just read on disk was the code
it was executing. That is the actual open loop in ARIA stewarding itself: not
that changes go unverified (the guard runs `check_command` in the worktree
before any merge), but that a MERGED change is invisible to the process it
changes until someone restarts it. A steward that cannot see the gap will
re-propose work that is already done, or report a fix as live when it is not.

The UI has had this right the whole time: `/api/build` reports its build sha and
`make ui-deploy` refuses to claim success unless the running sha matches. This
is the same idea for the API, sampled at process start rather than build time
because aria-api runs natively from the working tree.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The repo root: api/aria/core/build_info.py -> up four.
REPO_ROOT = Path(__file__).resolve().parents[3]

VERSION = "0.2.0"


def _git(*args: str, cwd: Optional[Path] = None) -> Optional[str]:
    """Run a git command, or return None. Never raises: build info is
    diagnostic, and a box without git must still serve /health."""
    try:
        proc = subprocess.run(
            ("git", *args),
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            timeout=5,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("build_info: git %s failed: %s", " ".join(args), exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _sample() -> dict:
    return {
        "version": VERSION,
        "commit": _git("rev-parse", "--short", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit_at": _git("log", "-1", "--format=%cI"),
        # A dirty tree at startup means the running code does not correspond to
        # ANY commit, which is worth saying out loud rather than implying the
        # sha is the whole truth.
        "dirty": bool(_git("status", "--porcelain")),
    }


# Sampled once, at import — i.e. as close as we can get to "the code that was
# loaded". Re-reading it later would report the DISK, which is the very thing
# this module exists to distinguish from the process.
STARTED = _sample()
STARTED_AT = datetime.now(timezone.utc)


def running() -> dict:
    """What this process is executing, as sampled at startup."""
    return dict(STARTED)


def drift() -> dict:
    """How far the working tree has moved since this process started.

    `behind` counts commits on the current branch that the running process does
    not contain. Zero is the normal state; non-zero means "restart aria-api to
    apply", not "something is broken" — so callers should report it, not page.
    """
    started_commit = STARTED.get("commit")
    head = _git("rev-parse", "--short", "HEAD")
    info = {
        "running_commit": started_commit,
        "head_commit": head,
        "started_at": STARTED_AT.isoformat(),
        "stale": False,
        "behind": 0,
    }
    if not started_commit or not head:
        return info
    if head == started_commit:
        return info
    info["stale"] = True
    count = _git("rev-list", "--count", f"{started_commit}..HEAD")
    try:
        info["behind"] = int(count) if count is not None else 0
    except ValueError:
        info["behind"] = 0
    return info
