"""
Pre-seed Claude Code's folder-trust flag for a workdir.

Claude Code shows a blocking "Do you trust the files in this folder?" dialog
the first time it starts in a directory it hasn't seen. `--dangerously-skip-
permissions` suppresses the *permission* prompts but NOT this *trust* gate, so
a shell we spawn detached (no human at the keyboard) hangs forever on the
dialog and looks "frozen".

Trust is recorded in `~/.claude.json` as
`projects[<abspath>].hasTrustDialogAccepted = true`. We set that flag for the
workdir *before* launching `claude`, so the dialog never appears.

This is best-effort: any failure is logged and swallowed. The worst case on
failure is the old behaviour (dialog appears), never a failed shell creation.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from aria.config import settings

logger = logging.getLogger(__name__)


def _config_path() -> str:
    """Resolve the path to Claude Code's main config file.

    Honours an explicit override in settings, then the CLAUDE_CONFIG_DIR env
    var that Claude Code itself respects, then falls back to ~/.claude.json.
    """
    override = getattr(settings, "shells_claude_config_path", "") or ""
    if override:
        return os.path.abspath(os.path.expanduser(override))
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir:
        return os.path.join(os.path.expanduser(config_dir), ".claude.json")
    return os.path.expanduser("~/.claude.json")


def _resolve_workdir(workdir: str | None) -> str:
    """The absolute directory `claude` will start in (matches tmux's cwd)."""
    if workdir:
        return os.path.abspath(os.path.expanduser(workdir))
    return os.path.expanduser("~")


def ensure_trusted(workdir: str | None) -> bool:
    """Mark `workdir` as trusted in Claude Code's config so the trust dialog
    is skipped when a freshly spawned shell launches `claude` there.

    Returns True if the flag is now set (already-trusted counts as success),
    False if the config could not be updated. Never raises.
    """
    target = _resolve_workdir(workdir)
    path = _config_path()
    try:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("claude-trust: cannot read %s: %s", path, exc)
            return False

        if not isinstance(data, dict):
            logger.warning("claude-trust: %s is not a JSON object", path)
            return False

        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects

        entry = projects.get(target)
        if not isinstance(entry, dict):
            entry = {}
            projects[target] = entry

        if entry.get("hasTrustDialogAccepted") is True:
            return True  # already trusted — skip the write to avoid clobbering
                         # concurrent writes from live claude processes.

        entry["hasTrustDialogAccepted"] = True
        entry.setdefault("hasCompletedProjectOnboarding", True)

        _atomic_write(path, data)
        logger.info("claude-trust: marked %s trusted in %s", target, path)
        return True
    except Exception as exc:  # pragma: no cover - defensive; never block spawn
        logger.warning("claude-trust: failed to trust %s: %s", target, exc)
        return False


def _atomic_write(path: str, data: dict) -> None:
    """Write JSON to `path` via a temp file + rename in the same directory, so
    a reader never sees a half-written file. Preserves the original file mode."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".claude.json.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
