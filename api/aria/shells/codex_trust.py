"""
Pre-seed Codex's directory-trust entry for a workdir.

Codex shows a blocking "Do you trust the contents of this directory?" dialog
the first time it starts in a directory it hasn't seen — even under
`--dangerously-bypass-approvals-and-sandbox` (verified on codex-cli 0.146). A
shell spawned detached (no human at the keyboard) hangs on that dialog and
looks "frozen". Codex counterpart to `claude_trust.py`.

Trust is recorded in `~/.codex/config.toml` as

    [projects."<abspath>"]
    trust_level = "trusted"

We append that table before launching `codex`, so the dialog never appears.

TOML handling is deliberately asymmetric: stdlib `tomllib` parses (to detect
an existing entry and refuse to touch a corrupt file), but since there is no
stdlib writer we *append* the new table as text rather than rewriting the
whole document — this also preserves Ben's comments/formatting, which a
round-trip through a writer would destroy.

Best-effort like claude_trust: any failure is logged and swallowed. The worst
case on failure is the old behaviour (dialog appears), never a failed shell
creation.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import tomllib

from aria.config import settings

logger = logging.getLogger(__name__)


def _config_path() -> str:
    """Resolve the path to Codex's config file.

    Honours an explicit override in settings, then the CODEX_HOME env var
    that codex itself respects, then falls back to ~/.codex/config.toml.
    """
    override = getattr(settings, "shells_codex_config_path", "") or ""
    if override:
        return os.path.abspath(os.path.expanduser(override))
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return os.path.join(os.path.expanduser(codex_home), "config.toml")
    return os.path.expanduser("~/.codex/config.toml")


def _resolve_workdir(workdir: str | None) -> str:
    """The absolute directory `codex` will start in (matches tmux's cwd)."""
    if workdir:
        return os.path.abspath(os.path.expanduser(workdir))
    return os.path.expanduser("~")


def ensure_codex_trusted(workdir: str | None) -> bool:
    """Mark `workdir` as trusted in Codex's config so the trust dialog is
    skipped when a freshly spawned shell launches `codex` there.

    Returns True if the entry is now present (already-trusted counts as
    success), False if the config could not be updated. Never raises.
    """
    target = _resolve_workdir(workdir)
    path = _config_path()
    try:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            text = ""
        except OSError as exc:
            logger.warning("codex-trust: cannot read %s: %s", path, exc)
            return False

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            # Never append to a file we can't parse — codex would then refuse
            # the whole config, which is worse than one trust dialog.
            logger.warning("codex-trust: %s is not valid TOML: %s", path, exc)
            return False

        projects = data.get("projects")
        entry = projects.get(target) if isinstance(projects, dict) else None
        if isinstance(entry, dict) and entry.get("trust_level") == "trusted":
            return True  # already trusted — skip the write

        if isinstance(entry, dict):
            # The path has a table but a different trust_level (e.g. denied).
            # Appending a duplicate table would be invalid TOML, and silently
            # flipping an explicit human "no" to "yes" is not ours to do.
            logger.warning(
                "codex-trust: %s already has trust_level=%r in %s — leaving it",
                target, entry.get("trust_level"), path,
            )
            return False

        # json.dumps produces a valid TOML basic string for the quoted key.
        section = f'\n[projects.{json.dumps(target)}]\ntrust_level = "trusted"\n'
        _atomic_write(path, text + section)
        logger.info("codex-trust: marked %s trusted in %s", target, path)
        return True
    except Exception as exc:  # pragma: no cover - defensive; never block spawn
        logger.warning("codex-trust: failed to trust %s: %s", target, exc)
        return False


def _atomic_write(path: str, text: str) -> None:
    """Write `text` via a temp file + rename in the same directory, so a
    reader never sees a half-written file. Preserves the original file mode."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".config.toml.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
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
