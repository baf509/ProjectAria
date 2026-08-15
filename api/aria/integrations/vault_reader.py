"""
ARIA - Vault reader (Steward §3.2 / §4.1)

Purpose: read Ben's Obsidian vault back. ARIA has always written to the vault
and never read it, which made a plan doc a *report*. Reading it makes the same
doc a *control input*: a phone edit of `approval:` or `autonomy:` in
LiveSync-synced markdown reaches the steward within one poll, before execution
happens.

Shape:
- A 60-second poller, not inotify — inotify would be a new dependency and a new
  failure mode (watch exhaustion, missed events across the CouchDB bridge's
  rename-heavy writes) for a surface where 60 s of latency is irrelevant.
- Human-edit detection is by CONTENT HASH against what the ObsidianWriter
  recorded. mtime cannot do it: the LiveSync bridge rewrites mtimes, so ARIA's
  own write and a sync echo look exactly like Ben typing.
- Read-only, always. The reader never writes to the vault — not even to fix a
  malformed doc. Its only writes are its own state in Mongo.
- Structured events are RETURNED, never acted on. The steward decides; this
  module only reports what changed. A parse failure is itself an event, so a
  half-typed charter is surfaced to Ben instead of silently dropping his edit.

Related Spec Sections:
- ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md §3.1 #8, §3.2, §4.1, §5 step 5
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from aria.config import settings
from aria.integrations.obsidian import (
    NOTES_HEADING,
    FrontmatterError,
    content_hash,
    extract_section,
    parse_frontmatter,
)

logger = logging.getLogger(__name__)

# Documents that carry control state. Everything else in the vault is prose
# ARIA has no business reading.
PLANNING_DOCS = ("CHARTER.md", "STEWARD_PLAN.md")
RESEARCH_DIR = "Research"
PLANNING_DIR = "Planning"

# Never descend into these: .obsidian is the app's own config (it churns every
# time Ben opens a note), .trash is deleted content, and .git belongs to the
# vault's autocommit — the LiveSync bridge is already replicating it by mistake.
IGNORED_DIRS = {".obsidian", ".trash", ".git", ".stfolder", ".stversions", ".sync"}

# A control doc is a few KB. The cap exists so a stray export or an attachment
# renamed to .md cannot pull hundreds of MB into the poller's memory every
# minute; oversized files are reported, not silently skipped.
MAX_DOC_BYTES = 256 * 1024

APPROVAL_VALUES = ("pending", "approved", "rejected")
AUTONOMY_RANGE = (0, 1, 2, 3)

# Event types (the steward's vocabulary).
EV_HUMAN_EDIT = "human_edit"
EV_CHARTER = "charter"
EV_APPROVAL = "approval"
EV_AUTONOMY = "autonomy"
EV_ACCEPTED = "accepted"
EV_NOTES = "notes"
EV_PARSE_ERROR = "parse_error"
EV_INVALID_VALUE = "invalid_value"
EV_TOO_LARGE = "too_large"

DOC_CHARTER = "charter"
DOC_STEWARD_PLAN = "steward_plan"
DOC_RESEARCH = "research"


class _StateStore:
    """Per-file poller state, in Mongo when available and in memory otherwise.

    The in-memory fallback is not a nicety: without any state the reader would
    re-emit the same human_edit for every doc every 60 seconds, which is worse
    than not running at all.
    """

    def __init__(self, db):
        self.db = db
        self._mem: dict[str, dict] = {}

    @property
    def _coll(self):
        return None if self.db is None else self.db[settings.vault_hash_state_collection]

    async def get(self, path: str) -> dict:
        coll = self._coll
        if coll is None:
            return dict(self._mem.get(path) or {})
        try:
            return await coll.find_one({"path": path}) or {}
        except Exception as exc:  # pragma: no cover - Mongo hiccup
            logger.warning("vault_reader: state read failed for %s: %s", path, exc)
            return dict(self._mem.get(path) or {})

    async def set(self, path: str, fields: dict) -> None:
        """Writes ONLY reader-owned fields — `aria_hash`/`written_at` belong to
        the ObsidianWriter and are never touched here (S3)."""
        payload = dict(fields)
        payload["path"] = path
        self._mem.setdefault(path, {}).update(payload)
        coll = self._coll
        if coll is None:
            return
        try:
            await coll.update_one({"path": path}, {"$set": payload}, upsert=True)
        except Exception as exc:  # pragma: no cover - Mongo hiccup
            logger.warning("vault_reader: state write failed for %s: %s", path, exc)


def _doc_kind(path: Path) -> Optional[str]:
    if path.parent.name == PLANNING_DIR:
        if path.name == "CHARTER.md":
            return DOC_CHARTER
        if path.name == "STEWARD_PLAN.md":
            return DOC_STEWARD_PLAN
        return None
    if path.parent.name == RESEARCH_DIR:
        return DOC_RESEARCH
    return None


def _norm_approval(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


class VaultReader:
    """Polls the vault's control docs and reports what a human changed."""

    def __init__(
        self,
        db=None,
        vault_path: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        on_events: Optional[Callable[[list[dict]], Awaitable[None]]] = None,
    ):
        self.vault = Path(vault_path or settings.obsidian_vault_path)
        self.state = _StateStore(db)
        self.interval = max(10, int(interval_seconds or settings.vault_reader_interval_seconds))
        self.on_events = on_events
        self.recent_events: list[dict] = []
        self.last_poll_at: Optional[datetime] = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- worker

    def enabled(self) -> bool:
        return bool(settings.vault_reader_enabled) and self.vault.is_dir()

    async def start(self) -> None:
        if self._task is not None:
            return
        if not self.enabled():
            logger.info(
                "vault reader not started (enabled=%s, vault=%s)",
                settings.vault_reader_enabled, self.vault,
            )
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="vault.reader")
        logger.info("vault reader started (every %ds, vault=%s)", self.interval, self.vault)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:  # pragma: no cover
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=15)  # settle on boot
        except asyncio.TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                events = await self.poll_once()
                if events and self.on_events is not None:
                    try:
                        await self.on_events(events)
                    except Exception as exc:  # pragma: no cover
                        # A consumer that blows up must not stop the reader: the
                        # events are still in recent_events and the file state is
                        # already advanced, so the next edit is still detected.
                        logger.warning("vault reader consumer failed: %s", exc)
            except Exception as exc:  # pragma: no cover
                logger.warning("vault reader tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    # --------------------------------------------------------------- scan

    def _scan_paths(self) -> list[Path]:
        """Blocking directory walk — always called through asyncio.to_thread."""
        found: list[Path] = []
        try:
            entries = sorted(self.vault.iterdir())
        except (OSError, FileNotFoundError) as exc:
            logger.warning("vault reader: cannot list %s: %s", self.vault, exc)
            return found
        for project_dir in entries:
            name = project_dir.name
            if name.startswith(".") or name in IGNORED_DIRS:
                continue
            if not project_dir.is_dir():
                continue
            for doc in PLANNING_DOCS:
                candidate = project_dir / PLANNING_DIR / doc
                if self._is_readable_doc(candidate):
                    found.append(candidate)
            research = project_dir / RESEARCH_DIR
            if research.is_dir():
                try:
                    for candidate in sorted(research.iterdir()):
                        if candidate.suffix == ".md" and self._is_readable_doc(candidate):
                            found.append(candidate)
                except OSError as exc:
                    logger.warning("vault reader: cannot list %s: %s", research, exc)
        return found

    def _is_readable_doc(self, path: Path) -> bool:
        if path.name.startswith("."):
            return False
        if not path.is_file():
            return False
        if path.is_symlink():
            # A symlink could point anywhere; the reader's entire safety story
            # is "it only ever reads the vault".
            try:
                path.resolve().relative_to(self.vault.resolve())
            except (ValueError, OSError):
                return False
        return True

    def _read(self, path: Path) -> tuple[Optional[str], Optional[str]]:
        try:
            size = path.stat().st_size
        except OSError as exc:
            return None, f"stat failed: {exc}"
        if size > MAX_DOC_BYTES:
            return None, f"too large ({size} bytes > {MAX_DOC_BYTES})"
        try:
            return path.read_text(encoding="utf-8"), None
        except UnicodeDecodeError:
            return None, "not valid UTF-8"
        except OSError as exc:
            return None, f"read failed: {exc}"

    # -------------------------------------------------------------- poll

    async def poll_once(self) -> list[dict]:
        """Scan the control docs once and return the change events found.

        Callable on demand (the worker flag only gates the loop) so a steward
        tick or an operator endpoint can force a read without waiting 60 s.
        Returns [] rather than raising when the vault is missing.
        """
        if not self.vault.is_dir():
            return []
        paths = await asyncio.to_thread(self._scan_paths)
        events: list[dict] = []
        for path in paths:
            try:
                events.extend(await self._examine(path))
            except Exception as exc:  # pragma: no cover - defensive
                # One bad document must never stop the sweep; the rest of Ben's
                # edits still have to land.
                logger.warning("vault reader: %s failed: %s", path, exc)
        self.last_poll_at = datetime.now(timezone.utc)
        if events:
            self.recent_events = (self.recent_events + events)[-200:]
            logger.info(
                "vault reader: %d event(s): %s",
                len(events), ", ".join(sorted({e["type"] for e in events})),
            )
        return events

    def _base_event(self, path: Path, kind: str, doc: Optional[str]) -> dict:
        try:
            rel = str(path.relative_to(self.vault))
            project = path.relative_to(self.vault).parts[0]
        except ValueError:  # pragma: no cover - path always under vault
            rel, project = str(path), ""
        return {
            "type": kind,
            "path": str(path),
            "rel_path": rel,
            "project": project,
            "doc": doc,
            "at": datetime.now(timezone.utc),
        }

    async def _examine(self, path: Path) -> list[dict]:
        doc = _doc_kind(path)
        text, read_error = await asyncio.to_thread(self._read, path)
        state = await self.state.get(str(path))

        if text is None:
            kind = EV_TOO_LARGE if "too large" in (read_error or "") else EV_PARSE_ERROR
            ev = self._base_event(path, kind, doc)
            ev["error"] = read_error
            # Keyed on the error string, not content (we never read it), so an
            # oversized file is surfaced once rather than every minute.
            if state.get("last_read_error") == read_error:
                return []
            await self.state.set(str(path), {"last_read_error": read_error,
                                             "last_event_at": ev["at"]})
            return [ev]

        digest = content_hash(text)
        aria_hash = state.get("aria_hash")
        last_hash = state.get("last_hash")
        first_sight = not last_hash and not aria_hash

        if digest == aria_hash:
            # ARIA's own bytes. Sync the reader's bookkeeping so a later human
            # edit is measured against what ARIA actually wrote, and say nothing.
            if last_hash != digest:
                await self.state.set(str(path), {
                    "last_hash": digest,
                    "last_seen_frontmatter": state.get("frontmatter") or {},
                    "doc": doc,
                    "last_read_error": None,
                    "last_polled_at": datetime.now(timezone.utc),
                })
            return []

        if digest == last_hash:
            return []  # unchanged since the previous poll

        try:
            frontmatter, body = parse_frontmatter(text)
        except FrontmatterError as exc:
            ev = self._base_event(path, EV_PARSE_ERROR, doc)
            ev["error"] = str(exc)
            # last_hash advances even on failure: the same broken bytes are
            # reported once. Fixing the doc changes the hash and re-reports.
            await self.state.set(str(path), {
                "last_hash": digest,
                "last_parse_error": str(exc),
                "last_event_at": ev["at"],
                "doc": doc,
            })
            logger.info("vault reader: parse error in %s: %s", path, exc)
            return [ev]

        if first_sight and frontmatter.get("generated_by") == "aria":
            # ARIA published this through a writer with no db handle, so there
            # is no hash record — its own frontmatter signature says so. Adopt
            # it silently instead of reporting ARIA's research note as an edit
            # by Ben.
            await self.state.set(str(path), {
                "last_hash": digest,
                "last_seen_frontmatter": frontmatter,
                "last_parse_error": None,
                "doc": doc,
                "last_polled_at": datetime.now(timezone.utc),
            })
            return []

        previous = state.get("last_seen_frontmatter")
        if previous is None:
            previous = state.get("frontmatter") or {}
        events = self._change_events(path, doc, frontmatter, previous, body)

        notes = extract_section(body, NOTES_HEADING)
        notes_hash = content_hash(notes) if notes else None
        if notes and notes_hash != state.get("last_notes_hash"):
            ev = self._base_event(path, EV_NOTES, doc)
            ev["value"] = notes
            events.append(ev)

        await self.state.set(str(path), {
            "last_hash": digest,
            "last_seen_frontmatter": frontmatter,
            "last_notes_hash": notes_hash,
            "last_parse_error": None,
            "last_read_error": None,
            "last_event_at": datetime.now(timezone.utc),
            "last_event_types": sorted({e["type"] for e in events}),
            "doc": doc,
            "last_polled_at": datetime.now(timezone.utc),
        })
        return events

    def _change_events(
        self, path: Path, doc: Optional[str], fm: dict, previous: dict, body: str,
    ) -> list[dict]:
        events: list[dict] = []

        edit = self._base_event(path, EV_HUMAN_EDIT, doc)
        edit["frontmatter"] = fm
        events.append(edit)

        # --- charter -------------------------------------------------------
        charter = fm.get("charter")
        if doc == DOC_CHARTER or (isinstance(charter, dict) and charter != previous.get("charter")):
            ev = self._base_event(path, EV_CHARTER, doc)
            ev["frontmatter"] = fm
            ev["value"] = charter if isinstance(charter, dict) else fm
            ev["body"] = body.strip()
            events.append(ev)

        # --- approval ------------------------------------------------------
        if "approval" in fm:
            value = _norm_approval(fm.get("approval"))
            prior = _norm_approval(previous.get("approval"))
            if value not in APPROVAL_VALUES:
                events.append(self._invalid(path, doc, "approval", fm.get("approval"),
                                            f"expected one of {APPROVAL_VALUES}"))
            elif value != prior:
                ev = self._base_event(path, EV_APPROVAL, doc)
                ev["value"] = value
                ev["previous"] = prior
                ev["frontmatter"] = fm
                events.append(ev)

        # --- autonomy ------------------------------------------------------
        if "autonomy" in fm:
            value = fm.get("autonomy")
            prior = previous.get("autonomy")
            if not isinstance(value, int) or isinstance(value, bool) or value not in AUTONOMY_RANGE:
                events.append(self._invalid(path, doc, "autonomy", value,
                                            "expected an integer 0..3"))
            elif value != prior:
                ev = self._base_event(path, EV_AUTONOMY, doc)
                ev["value"] = value
                ev["previous"] = prior
                ev["frontmatter"] = fm
                events.append(ev)

        # --- accepted (research notes) --------------------------------------
        if "accepted" in fm:
            value = fm.get("accepted")
            prior = previous.get("accepted")
            if isinstance(value, str) and value.strip().lower() == "pending":
                pass  # the value ARIA publishes with; not a decision yet
            elif not isinstance(value, bool):
                events.append(self._invalid(path, doc, "accepted", value,
                                            "expected true/false (or pending)"))
            elif value != prior:
                ev = self._base_event(path, EV_ACCEPTED, doc)
                ev["value"] = value
                ev["previous"] = prior if isinstance(prior, bool) else None
                ev["frontmatter"] = fm
                events.append(ev)

        return events

    def _invalid(self, path: Path, doc, key: str, value, reason: str) -> dict:
        ev = self._base_event(path, EV_INVALID_VALUE, doc)
        ev["key"] = key
        ev["value"] = value
        ev["error"] = reason
        logger.info("vault reader: invalid %s=%r in %s (%s)", key, value, path, reason)
        return ev
