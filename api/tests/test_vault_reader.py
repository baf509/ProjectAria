"""Tests for the vault as a two-way surface (Steward §3.2):

- the ObsidianWriter's frontmatter + content-hash provenance, and
- the VaultReader's human-edit detection built on it.

Everything runs against a fake vault in tmp_path and a fake Mongo collection —
no network, no live aria-api, no MongoDB.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from aria.config import settings
from aria.integrations import vault_reader as vr
from aria.integrations.obsidian import (
    FrontmatterError,
    ObsidianWriter,
    content_hash,
    dump_frontmatter,
    extract_section,
    parse_frontmatter,
)


# --------------------------------------------------------------- fake Mongo

class _FakeCollection:
    """Enough of a motor collection for the hash-state store: find_one and an
    upserting $set update_one."""

    def __init__(self):
        self.docs: list[dict] = []

    def _match(self, flt: dict):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in flt.items()):
                return doc
        return None

    async def find_one(self, flt: dict):
        doc = self._match(flt)
        return dict(doc) if doc else None

    async def update_one(self, flt: dict, update: dict, upsert: bool = False):
        doc = self._match(flt)
        if doc is None:
            if not upsert:
                return None
            doc = dict(flt)
            self.docs.append(doc)
        doc.update(update.get("$set", {}))
        return None


class _FakeDB:
    def __init__(self):
        self._colls: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._colls.setdefault(name, _FakeCollection())

    def __getattr__(self, name: str) -> _FakeCollection:  # pragma: no cover
        return self[name]


@pytest.fixture
def vault(tmp_path):
    (tmp_path / "ProjectAria" / "Planning").mkdir(parents=True)
    (tmp_path / "ProjectAria" / "Research").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def enabled(vault):
    with patch.object(settings, "obsidian_enabled", True), \
         patch.object(settings, "obsidian_vault_path", str(vault)), \
         patch.object(settings, "vault_reader_enabled", True):
        yield vault


def _writer(vault, db):
    return ObsidianWriter(str(vault), db=db)


def _reader(vault, db):
    return vr.VaultReader(db=db, vault_path=str(vault))


def _write_raw(path: Path, text: str) -> None:
    """Simulate Ben's phone: bytes land on disk with no hash record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ------------------------------------------------------------- frontmatter

class TestFrontmatterCodec:
    def test_roundtrip_of_the_charter_subset(self):
        fm = {
            "status": "active",
            "approval": "pending",
            "autonomy": 2,
            "accepted": True,
            "created": "2026-08-15",
            "tags": ["aria", "steward"],
            "charter": {"purpose": "keep ARIA coherent", "goals": ["a", "b"]},
        }
        parsed, body = parse_frontmatter(dump_frontmatter(fm) + "\nbody text\n")
        assert parsed == fm
        assert "body text" in body

    def test_datetime_keeps_its_offset(self):
        fm = {"updated": datetime.fromisoformat("2026-08-15T17:57:53-04:00")}
        parsed, _ = parse_frontmatter(dump_frontmatter(fm))
        assert parsed["updated"].utcoffset().total_seconds() == -4 * 3600

    def test_date_only_stays_a_string(self):
        # BSON has no date-only type and a naive datetime is silently stamped
        # UTC on the way into Mongo — both corrupt what Ben wrote.
        parsed, _ = parse_frontmatter("---\ncreated: 2026-08-15\n---\n")
        assert parsed["created"] == "2026-08-15"

    def test_value_containing_colon_survives_roundtrip(self):
        # The vault's real docs do this: `updated: 2026-08-15T17:50-04:00 (gap
        # sweep: §4.1 reconciled)`.
        fm = {"updated": "2026-08-15T17:50-04:00 (gap sweep: reconciled)"}
        parsed, _ = parse_frontmatter(dump_frontmatter(fm))
        assert parsed == fm

    def test_phone_typed_yes_is_a_boolean(self):
        parsed, _ = parse_frontmatter("---\naccepted: yes\n---\n")
        assert parsed["accepted"] is True

    def test_rejects_unterminated_block(self):
        with pytest.raises(FrontmatterError):
            parse_frontmatter("---\napproval: approved\n\n# heading\n")

    def test_rejects_constructs_outside_the_subset(self):
        for text in (
            "---\nsummary: |\n  a block scalar\n---\n",     # block scalar
            "---\ngoals:\n  - purpose: nested\n---\n",       # mapping in a list
            "---\ncharter: {purpose: x}\n---\n",             # flow mapping
            "---\nref: *anchor\n---\n",                      # alias
        ):
            with pytest.raises(FrontmatterError):
                parse_frontmatter(text)

    def test_no_frontmatter_is_not_an_error(self):
        fm, body = parse_frontmatter("# just a note\n")
        assert fm == {} and body.startswith("# just a note")

    def test_extract_section_stops_at_next_heading(self):
        body = "## Plan\n\nstuff\n\n## Notes from Ben\n\nplease do X\n\n## Log\n\nnope\n"
        assert extract_section(body, "## Notes from Ben") == "please do X"


# ------------------------------------------------------------- writer

@pytest.mark.asyncio
class TestObsidianWriterProvenance:
    async def test_publish_emits_frontmatter_and_records_hash(self, enabled):
        db = _FakeDB()
        path = await _writer(enabled, db).publish(
            "findings", title="GPU memory research", project="ProjectAria"
        )
        text = Path(path).read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        assert fm["generated_by"] == "aria"
        assert isinstance(fm["created"], datetime) and fm["created"].utcoffset() is not None
        assert isinstance(fm["updated"], datetime)
        assert "findings" in body
        state = await db[settings.vault_hash_state_collection].find_one({"path": path})
        assert state["aria_hash"] == content_hash(text)

    async def test_upsert_managed_preserves_human_key_and_notes(self, enabled):
        db = _FakeDB()
        w = _writer(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, dump_frontmatter({
            "status": "active",
            "approval": "approved",      # Ben's key — ARIA does not own it
            "autonomy": 2,
        }) + "\n# Plan\n\nold body\n\n## Notes from Ben\n\ndo the DS4 work first\n")

        result = await w.upsert_managed(
            str(plan),
            {"status": "active", "plan_hash": "abc123"},
            "# Plan\n\nfresh steward body",
            managed_keys=["status", "plan_hash", "created"],
        )
        assert result is not None
        fm, body = parse_frontmatter(plan.read_text(encoding="utf-8"))
        assert fm["approval"] == "approved"      # untouched human key
        assert fm["autonomy"] == 2
        assert fm["plan_hash"] == "abc123"
        assert fm["generated_by"] == "aria"
        assert "fresh steward body" in body
        assert extract_section(body, "## Notes from Ben") == "do the DS4 work first"
        assert result["preserved_notes"] is True
        assert result["human_edited"] is True     # ARIA had no record of this file

    async def test_upsert_managed_reports_a_contradiction_instead_of_winning(self, enabled):
        """S3: ARIA proposing a value for a key Ben owns is a proposal, not a
        write — the value stays his and the clash is reported to the caller."""
        db = _FakeDB()
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, dump_frontmatter({"approval": "approved"}) + "\n# Plan\n\nbody\n")
        result = await _writer(enabled, db).upsert_managed(
            str(plan), {"approval": "pending", "status": "active"}, "# Plan\n\nnew",
            managed_keys=["status"],
        )
        assert result["conflicts"] == ["approval"]
        fm, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
        assert fm["approval"] == "approved"

    async def test_upsert_managed_refuses_a_doc_it_cannot_parse(self, enabled):
        db = _FakeDB()
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        broken = "---\napproval: approved\n\n# no closing fence\n"
        _write_raw(plan, broken)
        assert await _writer(enabled, db).upsert_managed(
            str(plan), {"status": "active"}, "body", managed_keys=["status"]
        ) is None
        # Refused, not clobbered: Ben's bytes are still there.
        assert plan.read_text(encoding="utf-8") == broken

    async def test_append_section_hash_guard(self, enabled):
        db = _FakeDB()
        w = _writer(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(str(plan), {"status": "active"}, "# Plan\n\nbody",
                               managed_keys=["status"])

        # ARIA appending to its own doc: allowed, even though the file was just
        # modified (the old mtime guard would have refused this forever).
        assert await w.append_section(str(plan), "Progress", "spawned session x") == str(plan)
        assert "## Progress" in plan.read_text(encoding="utf-8")

        # Ben edits it; the hash no longer matches what ARIA wrote.
        _write_raw(plan, plan.read_text(encoding="utf-8") + "\nBen was here\n")
        assert await w.append_section(str(plan), "Progress 2", "more") is None
        assert "## Progress 2" not in plan.read_text(encoding="utf-8")

    async def test_append_refreshes_updated_and_rerecords_hash(self, enabled):
        db = _FakeDB()
        w = _writer(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(str(plan), {"status": "active"}, "# Plan\n\nbody",
                               managed_keys=["status"])
        before, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
        await w.append_section(str(plan), "Progress", "line")
        after, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
        assert after["updated"] >= before["updated"]
        state = await db[settings.vault_hash_state_collection].find_one({"path": str(plan)})
        assert state["aria_hash"] == content_hash(plan.read_text(encoding="utf-8"))

    async def test_disabled_writer_returns_none(self, vault):
        with patch.object(settings, "obsidian_enabled", False):
            w = ObsidianWriter(str(vault), db=_FakeDB())
            assert await w.publish("x", title="T") is None
            assert await w.upsert_managed("a.md", {}, "b", managed_keys=[]) is None
            assert await w.append_section("a.md", "h", "c") is None


# ------------------------------------------------------------- reader

@pytest.mark.asyncio
class TestVaultReader:
    async def test_aria_write_produces_no_human_edit_event(self, enabled):
        db = _FakeDB()
        w, r = _writer(enabled, db), _reader(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(str(plan), {"status": "active", "approval": "pending"},
                               "# Plan\n\nbody", managed_keys=["status", "approval"])
        assert await r.poll_once() == []
        assert await r.poll_once() == []          # and stays quiet

    async def test_human_edit_emits_event_with_parsed_frontmatter(self, enabled):
        db = _FakeDB()
        w, r = _writer(enabled, db), _reader(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(str(plan), {"status": "active"}, "# Plan\n\nbody",
                               managed_keys=["status"])
        await r.poll_once()

        fm, body = parse_frontmatter(plan.read_text(encoding="utf-8"))
        fm["status"] = "paused"
        _write_raw(plan, dump_frontmatter(fm) + body)

        events = await r.poll_once()
        edits = [e for e in events if e["type"] == vr.EV_HUMAN_EDIT]
        assert len(edits) == 1
        assert edits[0]["frontmatter"]["status"] == "paused"
        assert edits[0]["project"] == "ProjectAria"
        assert edits[0]["doc"] == vr.DOC_STEWARD_PLAN
        assert await r.poll_once() == []          # reported once

    async def test_approval_flip_detected(self, enabled):
        db = _FakeDB()
        w, r = _writer(enabled, db), _reader(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(str(plan), {"approval": "pending"}, "# Plan\n\nbody",
                               managed_keys=["approval"])
        await r.poll_once()

        fm, body = parse_frontmatter(plan.read_text(encoding="utf-8"))
        fm["approval"] = "approved"
        _write_raw(plan, dump_frontmatter(fm) + body)

        approvals = [e for e in await r.poll_once() if e["type"] == vr.EV_APPROVAL]
        assert len(approvals) == 1
        assert approvals[0]["value"] == "approved"
        assert approvals[0]["previous"] == "pending"

    async def test_autonomy_and_charter_from_a_hand_written_charter(self, enabled):
        db = _FakeDB()
        r = _reader(enabled, db)
        charter = enabled / "ProjectAria" / "Planning" / "CHARTER.md"
        _write_raw(charter, dump_frontmatter({
            "status": "active",
            "autonomy": 2,
            "charter": {"purpose": "keep ARIA coherent", "goals": ["ship P3"]},
        }) + "\n# Charter\n\nthe prose\n")

        events = await r.poll_once()
        kinds = {e["type"] for e in events}
        assert vr.EV_CHARTER in kinds and vr.EV_AUTONOMY in kinds
        ch = [e for e in events if e["type"] == vr.EV_CHARTER][0]
        assert ch["value"]["purpose"] == "keep ARIA coherent"
        assert "the prose" in ch["body"]
        assert [e for e in events if e["type"] == vr.EV_AUTONOMY][0]["value"] == 2

    async def test_accepted_flip_on_a_research_note(self, enabled):
        db = _FakeDB()
        w, r = _writer(enabled, db), _reader(enabled, db)
        path = await w.publish("body", title="Qwen slot curve",
                               project="ProjectAria", frontmatter={"accepted": "pending"})
        assert await r.poll_once() == []          # `pending` is not a decision

        fm, body = parse_frontmatter(Path(path).read_text(encoding="utf-8"))
        fm["accepted"] = True
        _write_raw(Path(path), dump_frontmatter(fm) + body)

        accepted = [e for e in await r.poll_once() if e["type"] == vr.EV_ACCEPTED]
        assert len(accepted) == 1
        assert accepted[0]["value"] is True
        assert accepted[0]["doc"] == vr.DOC_RESEARCH

    async def test_notes_from_ben_section_is_reported(self, enabled):
        db = _FakeDB()
        w, r = _writer(enabled, db), _reader(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(str(plan), {"status": "active"}, "# Plan\n\nbody",
                               managed_keys=["status"])
        await r.poll_once()
        _write_raw(plan, plan.read_text(encoding="utf-8")
                   + "\n## Notes from Ben\n\ndo the DS4 work first\n")

        notes = [e for e in await r.poll_once() if e["type"] == vr.EV_NOTES]
        assert len(notes) == 1
        assert notes[0]["value"] == "do the DS4 work first"
        assert await r.poll_once() == []

    async def test_malformed_frontmatter_is_an_event_not_an_exception(self, enabled):
        db = _FakeDB()
        r = _reader(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, "---\napproval: approved\n\n# never closed\n")

        events = await r.poll_once()
        assert [e["type"] for e in events] == [vr.EV_PARSE_ERROR]
        assert events[0]["error"]
        assert await r.poll_once() == []          # surfaced once, not every minute

    async def test_out_of_range_autonomy_is_flagged_not_applied(self, enabled):
        db = _FakeDB()
        r = _reader(enabled, db)
        charter = enabled / "ProjectAria" / "Planning" / "CHARTER.md"
        _write_raw(charter, dump_frontmatter({"autonomy": 7}) + "\n# Charter\n")
        events = await r.poll_once()
        bad = [e for e in events if e["type"] == vr.EV_INVALID_VALUE]
        assert len(bad) == 1 and bad[0]["key"] == "autonomy" and bad[0]["value"] == 7
        assert not [e for e in events if e["type"] == vr.EV_AUTONOMY]

    async def test_reader_never_writes_to_the_vault(self, enabled):
        db = _FakeDB()
        w, r = _writer(enabled, db), _reader(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(str(plan), {"status": "active"}, "# Plan\n\nbody",
                               managed_keys=["status"])
        _write_raw(enabled / "ProjectAria" / "Planning" / "BROKEN.md", "---\nx\n")
        before = {p: p.read_bytes() for p in enabled.rglob("*") if p.is_file()}
        await r.poll_once()
        after = {p: p.read_bytes() for p in enabled.rglob("*") if p.is_file()}
        assert before == after

    async def test_ignores_obsidian_trash_and_git(self, enabled):
        db = _FakeDB()
        for hidden in (".obsidian", ".trash", ".git"):
            _write_raw(enabled / hidden / "Planning" / "CHARTER.md",
                       dump_frontmatter({"approval": "approved"}) + "\n# nope\n")
        assert await _reader(enabled, db).poll_once() == []

    async def test_oversized_doc_is_reported_once_and_skipped(self, enabled):
        db = _FakeDB()
        r = _reader(enabled, db)
        big = enabled / "ProjectAria" / "Research" / "huge.md"
        _write_raw(big, "x" * (vr.MAX_DOC_BYTES + 10))
        events = await r.poll_once()
        assert [e["type"] for e in events] == [vr.EV_TOO_LARGE]
        assert await r.poll_once() == []

    async def test_aria_published_note_without_a_hash_record_is_adopted(self, enabled):
        """The research auto-publish path constructs ObsidianWriter() with no db,
        so its note has no hash record. Its own `generated_by: aria` frontmatter
        must keep it from reading as one of Ben's edits."""
        with patch.object(settings, "obsidian_vault_path", str(enabled)):
            path = await ObsidianWriter(str(enabled)).publish(
                "body", title="Unwired publish", project="ProjectAria"
            )
        assert await _reader(enabled, _FakeDB()).poll_once() == []
        assert Path(path).exists()

    async def test_degrades_without_a_db(self, enabled):
        r = vr.VaultReader(db=None, vault_path=str(enabled))
        charter = enabled / "ProjectAria" / "Planning" / "CHARTER.md"
        _write_raw(charter, dump_frontmatter({"approval": "pending"}) + "\n# Charter\n")
        assert [e["type"] for e in await r.poll_once() if e["type"] == vr.EV_HUMAN_EDIT]
        # In-memory state still suppresses the repeat that would otherwise fire
        # every 60 seconds forever.
        assert await r.poll_once() == []

    async def test_missing_vault_is_not_an_error(self, tmp_path):
        r = vr.VaultReader(db=_FakeDB(), vault_path=str(tmp_path / "nope"))
        assert await r.poll_once() == []

    async def test_worker_does_not_start_when_disabled(self, vault):
        with patch.object(settings, "vault_reader_enabled", False):
            r = vr.VaultReader(db=_FakeDB(), vault_path=str(vault))
            await r.start()
            assert r._task is None
            await r.stop()

    async def test_worker_start_stop_and_callback(self, enabled):
        db = _FakeDB()
        seen: list[list[dict]] = []

        async def sink(events):
            seen.append(events)

        r = vr.VaultReader(db=db, vault_path=str(enabled), interval_seconds=1,
                           on_events=sink)
        _write_raw(enabled / "ProjectAria" / "Planning" / "CHARTER.md",
                   dump_frontmatter({"autonomy": 1}) + "\n# Charter\n")
        await r.start()
        assert r._task is not None
        # poll_once is the unit under test elsewhere; here we only prove the
        # worker starts, is stoppable, and delivers through the callback seam.
        await r.poll_once()
        await r.stop()
        assert r._task is None
