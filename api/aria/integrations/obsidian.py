"""
ARIA - Obsidian vault surface (Coherence C6 + Steward §3.2/§8)

Purpose: publish ARIA's long-form outputs (research reports, analyses, plans)
into the LiveSync-materialized vault so they land on every device Ben reads on,
AND record what ARIA wrote so the VaultReader can tell "Ben edited this" from
"ARIA wrote this". The vault stops being write-only here: it becomes the
approval surface, and an approval surface is only safe if ARIA can prove which
bytes are its own.

Conflict discipline (the load-bearing constraint):
- Namespace partition: writes go ONLY under vault/<Folder>/{Design,Specs,
  Analysis,Research,Planning}/ — never .obsidian/ or .trash/.
- Atomic writes (temp file + rename) so the LiveSync bridge never sees a
  half-written note.
- Never clobber: publish() gives an existing file a timestamped sibling rather
  than overwriting it.
- Content-hash provenance: every ARIA write records {path, aria_hash,
  written_at, frontmatter} in settings.vault_hash_state_collection. The old
  mtime guard cannot do this job — the LiveSync bridge rewrites mtimes, and
  ARIA's own write looks exactly like a human's under it.
- S3 ownership: upsert_managed() writes only the frontmatter keys ARIA owns and
  preserves a human-authored `## Notes from Ben` section verbatim.

Frontmatter codec: this module owns both the parser and the serializer, because
they are one contract — the reader must be able to read back exactly what the
writer emits. It is a deliberately small, strict subset of YAML (scalars, ISO
datetimes, `- ` lists, nested maps, `[a, b]` flow lists) with no pyyaml
dependency: pyyaml is not in requirements.txt (it is only present transitively
in the current venv), and guessing at YAML edge cases on a control surface is
worse than refusing the document — a refusal is surfaced to Ben as a parse
error, a wrong guess silently changes what he approved.

Related Spec Sections:
- COHERENCE_DESIGN.md C6 (Obsidian long-form surface)
- ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md §3.2, §4.1, §8 (the vault as a
  two-way surface; CHARTER.md / STEWARD_PLAN.md / Research `accepted:`)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from aria.config import settings
from aria.shared.ownership import merge_owned

logger = logging.getLogger(__name__)

DOC_TYPES = ("Design", "Specs", "Analysis", "Research", "Planning")

# The one section of an ARIA-managed doc that belongs to Ben. Everything else in
# the body is ARIA's to rewrite; this survives verbatim across every refresh.
NOTES_HEADING = "## Notes from Ben"

# Frontmatter keys ARIA always owns on a doc it writes.
ARIA_FRONTMATTER_KEYS = ("updated", "generated_by")

# The vault's own convention puts these first, in this order, on every
# human-authored doc; matching it is what makes an ARIA doc look native in
# Obsidian's property editor instead of like machine output.
FRONTMATTER_KEY_ORDER = ("title", "status", "created", "updated", "generated_by")

FRONTMATTER_FENCE = "---"


class FrontmatterError(ValueError):
    """The document's frontmatter is not in the supported subset.

    Raised rather than guessed at: a malformed control doc must be reported to
    Ben (VaultReader emits a parse_error event), never silently reinterpreted.
    """


def _slugify_title(title: str, max_len: int = 80) -> str:
    clean = re.sub(r"[^\w\s-]", "", title).strip()
    clean = re.sub(r"\s+", " ", clean)
    return (clean or "untitled")[:max_len].strip()


def content_hash(text: str) -> str:
    """Stable content identity for a vault doc.

    Hashes the decoded text, not the raw bytes, so the writer (which builds a
    str) and the reader (which decodes UTF-8) agree without a normalization
    step in between.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_local() -> datetime:
    """Timezone-aware local timestamp, seconds precision.

    Generated at write time and always carrying an offset: the project-docs
    convention requires a real timestamp in vault frontmatter, and an offset-
    less one is ambiguous the moment it is read on the phone.
    """
    return datetime.now().astimezone().replace(microsecond=0)


# --------------------------------------------------------------- frontmatter

_KEY_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_.\-/ ]*?)\s*:(?:[ \t]+(?P<val>.*))?$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?$")
# Only *full* datetimes with an explicit offset become datetime objects — see
# _parse_scalar for why date-only values deliberately stay strings.
_DT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})$"
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}


def split_document(text: str) -> tuple[Optional[str], str]:
    """Split raw markdown into (frontmatter_block_text | None, body)."""
    if not text.startswith(FRONTMATTER_FENCE):
        return None, text
    lines = text.split("\n")
    if lines[0].strip() != FRONTMATTER_FENCE:
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() in (FRONTMATTER_FENCE, "..."):
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    # A document that opens a frontmatter fence and never closes it is broken,
    # not frontmatter-less: treating it as body would silently drop every key
    # Ben typed, which on an approval surface means dropping his decision.
    raise FrontmatterError("frontmatter block is not terminated by '---'")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a document into (frontmatter dict, body). Raises FrontmatterError."""
    block, body = split_document(text)
    if block is None:
        return {}, body
    toks = _tokenize(block.split("\n"))
    if not toks:
        return {}, body
    if toks[0][0] != 0:
        raise FrontmatterError("line 1: frontmatter must start at column 0")
    value, idx = _parse_block(toks, 0, 0)
    if idx != len(toks):
        raise FrontmatterError(f"line {toks[idx][2]}: unexpected content")
    if not isinstance(value, dict):
        raise FrontmatterError("frontmatter must be a mapping, not a list")
    return value, body


def _tokenize(lines: list[str]) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    for lineno, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        stripped = raw.lstrip(" ")
        if stripped.startswith("#"):
            continue
        indent = len(raw) - len(stripped)
        if "\t" in raw[:indent]:
            raise FrontmatterError(f"line {lineno}: tab indentation is not valid YAML")
        out.append((indent, raw.strip(), lineno))
    return out


def _parse_block(toks, i: int, indent: int):
    text = toks[i][1]
    if text == "-" or text.startswith("- "):
        return _parse_seq(toks, i, indent)
    return _parse_map(toks, i, indent)


def _parse_map(toks, i: int, indent: int) -> tuple[dict, int]:
    result: dict = {}
    while i < len(toks):
        ind, text, lineno = toks[i]
        if ind < indent:
            break
        if ind > indent:
            raise FrontmatterError(f"line {lineno}: unexpected indentation")
        m = _KEY_RE.match(text)
        if not m:
            raise FrontmatterError(f"line {lineno}: expected 'key: value'")
        key = m.group("key").strip()
        raw_val = m.group("val")
        if raw_val is None or not raw_val.strip():
            if i + 1 < len(toks) and toks[i + 1][0] > indent:
                child, i = _parse_block(toks, i + 1, toks[i + 1][0])
                result[key] = child
                continue
            result[key] = None
            i += 1
            continue
        result[key] = _parse_scalar(raw_val, lineno)
        i += 1
    return result, i


def _parse_seq(toks, i: int, indent: int) -> tuple[list, int]:
    items: list = []
    while i < len(toks):
        ind, text, lineno = toks[i]
        if ind < indent:
            break
        if ind > indent:
            raise FrontmatterError(f"line {lineno}: unexpected indentation")
        if not text.startswith("- "):
            if text == "-":
                raise FrontmatterError(f"line {lineno}: empty list entry")
            break
        body = text[2:].strip()
        if _KEY_RE.match(body):
            # `- key: value` is a list of mappings in real YAML. We refuse it
            # rather than flatten it to a string: the charter subset has no use
            # for it, and a wrong shape here would reach the steward as data.
            raise FrontmatterError(
                f"line {lineno}: mappings inside lists are not supported"
            )
        items.append(_parse_scalar(body, lineno))
        i += 1
    return items, i


def _unescape(body: str) -> str:
    out: list[str] = []
    k = 0
    while k < len(body):
        ch = body[k]
        if ch == "\\" and k + 1 < len(body):
            nxt = body[k + 1]
            if nxt not in _ESCAPES:
                raise FrontmatterError(f"unsupported escape '\\{nxt}'")
            out.append(_ESCAPES[nxt])
            k += 2
            continue
        out.append(ch)
        k += 1
    return "".join(out)


def _parse_scalar(raw: str, lineno: int) -> Any:
    s = raw.strip()
    if s in ("", "~", "null", "Null", "NULL"):
        return None
    if s[0] in "|>":
        raise FrontmatterError(f"line {lineno}: block scalars are not supported")
    if s[0] in "&*!":
        raise FrontmatterError(f"line {lineno}: YAML anchors/tags are not supported")
    if s[0] == "{":
        raise FrontmatterError(f"line {lineno}: inline mappings are not supported")
    if s[0] == "[":
        if not s.endswith("]"):
            raise FrontmatterError(f"line {lineno}: unterminated inline list")
        inner = s[1:-1].strip()
        if not inner:
            return []
        if "[" in inner or "{" in inner:
            raise FrontmatterError(f"line {lineno}: nested inline collections")
        return [_parse_scalar(part, lineno) for part in inner.split(",")]
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return _unescape(s[1:-1])
        except FrontmatterError as exc:
            raise FrontmatterError(f"line {lineno}: {exc}") from None
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    low = s.lower()
    # yes/no are YAML 1.1 booleans and exactly what a phone keyboard produces
    # for `accepted:`; treating them as strings would drop Ben's answer.
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    if _DT_RE.match(s):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return s
    # Deliberately NOT parsed into a date/naive datetime: BSON has no date-only
    # type (pymongo rejects datetime.date outright) and a naive datetime is
    # silently stamped UTC on the way into Mongo. Both would corrupt the value
    # Ben wrote, so `created: 2026-08-15` stays the string it looks like.
    return s


def _needs_quoting(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if s[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True
    if ": " in s or s.endswith(":") or " #" in s or "\n" in s:
        return True
    low = s.lower()
    if low in ("true", "false", "yes", "no", "null", "~", "on", "off"):
        return True
    return bool(_INT_RE.match(s) or _FLOAT_RE.match(s) or _DT_RE.match(s))


def _dump_scalar(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, str):
        if not _needs_quoting(v):
            return v
        esc = (
            v.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return f'"{esc}"'
    raise ValueError(f"unsupported frontmatter value of type {type(v).__name__}")


def dump_frontmatter(fm: dict) -> str:
    """Serialize a frontmatter dict into a `---` fenced block (trailing \\n)."""
    lines = [FRONTMATTER_FENCE]
    _dump_map(fm, 0, lines)
    lines.append(FRONTMATTER_FENCE)
    return "\n".join(lines) + "\n"


def _dump_map(d: dict, indent: int, lines: list[str]) -> None:
    pad = " " * indent
    for k, v in d.items():
        key = str(k)
        if isinstance(v, dict):
            if not v:
                lines.append(f"{pad}{key}:")
                continue
            lines.append(f"{pad}{key}:")
            _dump_map(v, indent + 2, lines)
        elif isinstance(v, (list, tuple)):
            if not v:
                lines.append(f"{pad}{key}: []")
                continue
            lines.append(f"{pad}{key}:")
            for item in v:
                lines.append(f"{pad}  - {_dump_scalar(item)}")
        else:
            rendered = _dump_scalar(v)
            lines.append(f"{pad}{key}: {rendered}".rstrip())


def extract_section(body: str, heading: str) -> Optional[str]:
    """Return the text under `heading` (heading line excluded), or None."""
    want = heading.strip().lower()
    lines = body.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower() == want:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r"^#{1,2} ", lines[j]):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


class ObsidianWriter:
    """Atomic, guard-railed markdown publisher into the Obsidian vault.

    `db` is optional so every existing caller (research auto-publish, POST
    /obsidian/publish, MCP publish_to_obsidian) keeps working unchanged — but
    without it no content hash is recorded, and the VaultReader will read
    ARIA's own note as a human-authored one the first time it sees it. Wire the
    db wherever the writer is constructed.
    """

    def __init__(self, vault_path: Optional[str] = None, db=None):
        self.vault = Path(vault_path or settings.obsidian_vault_path)
        self.db = db

    # ------------------------------------------------------------- guards

    def enabled(self) -> bool:
        return bool(settings.obsidian_enabled) and self.vault.is_dir()

    def _folder_for(self, project: Optional[str], doc_type: str) -> Path:
        """vault/<RepoName>/<DocType>/ — `project` may be a repo path (its
        basename is the vault folder, per the project-docs convention) or a
        bare folder name; None falls back to the configured default folder."""
        if doc_type not in DOC_TYPES:
            raise ValueError(f"doc_type must be one of {DOC_TYPES}, got {doc_type!r}")
        name = (
            os.path.basename(project.rstrip("/")) if project else settings.obsidian_default_folder
        )
        if not name or name.startswith("."):
            name = settings.obsidian_default_folder
        return self.vault / name / doc_type

    @staticmethod
    def _recently_modified(path: Path) -> bool:
        try:
            age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
        except FileNotFoundError:
            return False
        return age < settings.obsidian_human_edit_guard_minutes * 60

    # ------------------------------------------------------- hash provenance

    def _state_collection(self):
        if self.db is None:
            return None
        return self.db[settings.vault_hash_state_collection]

    async def _load_state(self, path: Path) -> Optional[dict]:
        coll = self._state_collection()
        if coll is None:
            return None
        try:
            return await coll.find_one({"path": str(path)})
        except Exception as exc:  # pragma: no cover - Mongo hiccup
            logger.warning("obsidian: hash-state read failed for %s: %s", path, exc)
            return None

    async def _record_write(
        self, path: Path, text: str, frontmatter: Optional[dict] = None,
        *, conflicts: Optional[list[str]] = None,
    ) -> str:
        """Record the hash of what ARIA just wrote. Best-effort, but loudly:
        a lost record makes the next VaultReader poll report ARIA's own write
        as one of Ben's edits, which is a false control input."""
        digest = content_hash(text)
        coll = self._state_collection()
        if coll is None:
            return digest
        doc = {
            "path": str(path),
            "aria_hash": digest,
            "written_at": datetime.now(timezone.utc),
            "frontmatter": frontmatter or {},
        }
        if conflicts:
            doc["frontmatter_conflicts"] = conflicts
        try:
            await coll.update_one({"path": str(path)}, {"$set": doc}, upsert=True)
        except Exception as exc:  # pragma: no cover - Mongo hiccup
            logger.warning("obsidian: hash-state write failed for %s: %s", path, exc)
        return digest

    # ------------------------------------------------------------- writes

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _ordered(fm: dict) -> dict:
        ordered = {k: fm[k] for k in FRONTMATTER_KEY_ORDER if k in fm}
        ordered.update({k: v for k, v in fm.items() if k not in ordered})
        return ordered

    def _compose(self, *, title: str, body: str, frontmatter: dict,
                 banner: bool = True) -> str:
        head = f"{dump_frontmatter(self._ordered(frontmatter))}\n"
        if not banner:
            # A managed doc carries `generated_by` + `updated` in its
            # frontmatter, which is the same statement in the vault's own
            # vocabulary; a second prose banner on a doc Ben edits every day is
            # just noise ARIA re-writes on every tick.
            if body.lstrip().startswith("# "):
                return head + body.rstrip() + "\n"
            return head + f"# {title}\n\n{body.rstrip()}\n"
        stamp = frontmatter.get("updated")
        stamp = stamp.isoformat() if isinstance(stamp, datetime) else str(stamp)
        return (
            head
            + f"# {title}\n\n"
            f"> Published by ARIA on {stamp}.\n\n"
            f"{body.rstrip()}\n"
        )

    async def publish(
        self,
        content: str,
        *,
        title: str,
        doc_type: str = "Research",
        project: Optional[str] = None,
        frontmatter: Optional[dict] = None,
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """Write a new markdown doc into the vault; returns its path, or None
        when publishing is disabled/unavailable. Never raises into the caller's
        main flow — a vault problem must not fail a research run.

        `frontmatter` extends (and may override) the always-present created /
        updated / generated_by keys; `generated_by: aria` is forced, because it
        is the marker the reader uses to recognise ARIA's own output when no
        hash record exists.
        """
        if not self.enabled():
            return None
        try:
            folder = self._folder_for(project, doc_type)
            now = now_local()
            base = filename[:-3] if filename and filename.endswith(".md") else (
                filename or f"{now.strftime('%Y-%m-%d')} {_slugify_title(title)}"
            )
            path = folder / f"{base}.md"
            if path.exists():
                # Never clobber — most likely a same-day re-publish; a sibling
                # with a time suffix keeps both.
                path = folder / f"{base} {now.strftime('%H%M%S')}.md"

            fm: dict = {"title": title}
            fm.update(frontmatter or {})
            fm.setdefault("created", now)
            fm["updated"] = now
            fm["generated_by"] = "aria"
            doc = self._compose(title=title, body=content, frontmatter=fm)

            def _write() -> None:
                folder.mkdir(parents=True, exist_ok=True)
                self._atomic_write(path, doc)

            await asyncio.to_thread(_write)
            await self._record_write(path, doc, fm)
            logger.info("obsidian: published %s", path)
            return str(path)
        except Exception as exc:
            logger.warning("obsidian publish failed for '%s': %s", title, exc)
            return None

    async def upsert_managed(
        self,
        path: str,
        frontmatter: dict,
        body: str,
        managed_keys: Iterable[str],
        *,
        project: Optional[str] = None,
        doc_type: str = "Planning",
        title: Optional[str] = None,
    ) -> Optional[dict]:
        """Write/refresh a doc ARIA owns, preserving what Ben owns.

        Preserved: every frontmatter key outside `managed_keys` (S3 — a phone
        edit of `approval:` must survive the next steward tick that rewrites
        the plan), and a `## Notes from Ben` section verbatim.

        Returns {path, hash, conflicts, human_edited, preserved_notes} or None
        (disabled / unwritable / a doc whose existing frontmatter cannot be
        parsed — that last case is refused rather than overwritten, because a
        merge we cannot compute is a merge we must not guess at).
        """
        if not self.enabled():
            return None
        try:
            target = Path(path)
            if not target.is_absolute():
                target = self._folder_for(project, doc_type) / path

            existing_text = await asyncio.to_thread(
                lambda: target.read_text(encoding="utf-8") if target.exists() else None
            )
            existing_fm: dict = {}
            existing_body = ""
            if existing_text is not None:
                try:
                    existing_fm, existing_body = parse_frontmatter(existing_text)
                except FrontmatterError as exc:
                    logger.warning(
                        "obsidian: refusing to rewrite %s — unparseable frontmatter (%s)",
                        target, exc,
                    )
                    return None

            state = await self._load_state(target)
            managed = set(managed_keys) | set(ARIA_FRONTMATTER_KEYS)
            now = now_local()
            observed = dict(frontmatter or {})
            observed["updated"] = now
            observed["generated_by"] = "aria"
            # merge_owned only reports a contradiction when it can see WHO set
            # the existing value, and a vault doc carries no provenance block —
            # so reconstruct it from ARIA's own last write: a key ARIA did not
            # write is Ben's, by definition of this surface. Without this every
            # conflict list would come back empty and a charter key Ben changed
            # under ARIA would go unreported.
            aria_written = set((state or {}).get("frontmatter") or {})
            provenance = {
                key: {"actor": "aria-obsidian" if key in aria_written else "human"}
                for key in existing_fm
            }
            set_update, conflicts = merge_owned(
                {**existing_fm, "source": provenance},
                observed,
                worker_fields=managed,
                actor="aria-obsidian",
            )
            merged = dict(existing_fm)
            # merge_owned's provenance belongs in Mongo, not in Ben's document:
            # a `source:` block in the frontmatter would show up in Obsidian's
            # property editor as noise he did not write.
            for key, value in set_update.items():
                if key in ("source", "last_verified_at"):
                    continue
                merged[key] = value
            merged.setdefault("created", existing_fm.get("created") or now)
            merged["updated"] = now
            merged["generated_by"] = "aria"

            notes = extract_section(existing_body, NOTES_HEADING) if existing_body else None
            new_body = body.rstrip()
            preserved = False
            if notes and extract_section(new_body, NOTES_HEADING) is None:
                new_body = f"{new_body}\n\n{NOTES_HEADING}\n\n{notes}\n"
                preserved = True

            heading = title or merged.get("title") or target.stem
            doc = self._compose(
                title=str(heading), body=new_body, frontmatter=merged, banner=False
            )

            human_edited = bool(
                existing_text is not None
                and (state or {}).get("aria_hash") != content_hash(existing_text)
            )

            def _write() -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write(target, doc)

            await asyncio.to_thread(_write)
            digest = await self._record_write(target, doc, merged, conflicts=conflicts)
            if conflicts:
                logger.info(
                    "obsidian: %s human-owned frontmatter key(s) left untouched in %s: %s",
                    len(conflicts), target, ", ".join(conflicts),
                )
            return {
                "path": str(target),
                "hash": digest,
                "conflicts": conflicts,
                "human_edited": human_edited,
                "preserved_notes": preserved,
            }
        except Exception as exc:
            logger.warning("obsidian upsert_managed failed for '%s': %s", path, exc)
            return None

    async def _append_allowed(self, path: Path) -> tuple[bool, str]:
        """Hash-based human-edit guard for an ARIA-managed doc.

        The old mtime window could not tell Ben's edit from ARIA's own write or
        from a LiveSync mtime rewrite. With a hash record we can: ARIA may append
        only to content it can prove it produced. With no db wired there is no
        record to check, so we fall back to the historical mtime window rather
        than refusing every append forever.
        """
        if not path.exists():
            return True, "new"
        if self.db is None:
            if self._recently_modified(path):
                return False, "recently-modified (mtime fallback, no hash state)"
            return True, "mtime-fallback"
        state = await self._load_state(path)
        aria_hash = (state or {}).get("aria_hash")
        if not aria_hash:
            return False, "unknown-provenance"
        current = await asyncio.to_thread(
            lambda: content_hash(path.read_text(encoding="utf-8"))
        )
        if current != aria_hash:
            return False, "human-edited"
        return True, "aria-owned"

    async def append_section(
        self,
        rel_or_abs_path: str,
        heading: str,
        content: str,
        *,
        project: Optional[str] = None,
        doc_type: str = "Analysis",
    ) -> Optional[str]:
        """Append a timestamped `## heading` section to an ARIA-managed doc
        (creating it if absent). This is how the steward's progress log lands in
        STEWARD_PLAN.md. Skips — returning None — when the file's content hash
        says a human, not ARIA, wrote what is there now.
        """
        if not self.enabled():
            return None
        try:
            path = Path(rel_or_abs_path)
            if not path.is_absolute():
                path = self._folder_for(project, doc_type) / rel_or_abs_path
            allowed, reason = await self._append_allowed(path)
            if not allowed:
                logger.info("obsidian: skipping %s (%s)", path, reason)
                return None
            now = now_local()
            section = f"\n\n## {heading}\n\n*({now.isoformat()})*\n\n{content.rstrip()}\n"

            def _write() -> tuple[str, dict]:
                path.parent.mkdir(parents=True, exist_ok=True)
                existing = path.read_text(encoding="utf-8") if path.exists() else ""
                try:
                    fm, body = parse_frontmatter(existing) if existing else ({}, "")
                except FrontmatterError:
                    # Never mangle a doc we cannot parse — append raw and leave
                    # the frontmatter exactly as Ben left it.
                    fm, body = {}, existing
                if fm:
                    # The vault convention is that `updated:` means something;
                    # an append that leaves it stale makes the doc lie about
                    # its own freshness on the phone.
                    fm["updated"] = now
                    text = dump_frontmatter(fm) + body.rstrip() + section
                else:
                    text = existing.rstrip() + section
                self._atomic_write(path, text)
                return text, fm

            text, fm_written = await asyncio.to_thread(_write)
            await self._record_write(path, text, fm_written)
            return str(path)
        except Exception as exc:
            logger.warning("obsidian append failed for '%s': %s", rel_or_abs_path, exc)
            return None
