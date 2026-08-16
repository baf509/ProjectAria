"""Tests for the vault as a two-way surface (Steward §3.2):

- the ObsidianWriter's frontmatter + content-hash provenance, and
- the VaultReader's human-edit detection built on it.

Everything runs against a fake vault in tmp_path and a fake Mongo collection —
no network, no live aria-api, no MongoDB.

The invariants here are not stylistic. This is the surface Ben answers on, from
his phone, through LiveSync: a lost or overwritten edit is the worst failure in
the system. So each test below names the way an edit could have been lost.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from aria.config import settings
from aria.integrations import obsidian as ob
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

class _FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def sort(self, field: str, direction: int = 1):
        self._docs.sort(key=lambda d: d.get(field), reverse=direction < 0)
        return self

    async def to_list(self, length: int | None = None):
        return [dict(d) for d in self._docs[: length or len(self._docs)]]


class _FakeCollection:
    """Enough of a motor collection for the hash-state store and the event log:
    find_one, an upserting $set update_one, insert_many and a sorted find."""

    def __init__(self):
        self.docs: list[dict] = []
        self.update_calls = 0

    def _match(self, flt: dict):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in flt.items()):
                return doc
        return None

    async def find_one(self, flt: dict):
        doc = self._match(flt)
        return dict(doc) if doc else None

    def find(self, flt: dict | None = None):
        return _FakeCursor(
            [d for d in self.docs if all(d.get(k) == v for k, v in (flt or {}).items())]
        )

    async def insert_many(self, docs: list[dict]):
        for doc in docs:
            doc.setdefault("_id", ObjectId())
            self.docs.append(doc)
        return SimpleNamespace(inserted_ids=[d["_id"] for d in docs])

    async def update_one(self, flt: dict, update: dict, upsert: bool = False):
        self.update_calls += 1
        doc = self._match(flt)
        if doc is None:
            if not upsert:
                return None
            doc = dict(flt)
            doc.update(update.get("$setOnInsert", {}))
            self.docs.append(doc)
        doc.update(update.get("$set", {}))
        return None


class _ExplodingCollection(_FakeCollection):
    """A collection whose writes fail — the Mongo hiccup behind D7."""

    async def update_one(self, flt: dict, update: dict, upsert: bool = False):
        self.update_calls += 1
        raise RuntimeError("connection reset by peer")

    async def insert_many(self, docs: list[dict]):
        raise RuntimeError("connection reset by peer")


class _FakeDB:
    def __init__(self, colls: dict[str, _FakeCollection] | None = None):
        self._colls: dict[str, _FakeCollection] = dict(colls or {})

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._colls.setdefault(name, _FakeCollection())

    def __getattr__(self, name: str) -> _FakeCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


@pytest.fixture(autouse=True)
def _clean_unrecorded_writes():
    """The unrecorded-write registry is module state shared by writer and
    reader; a leak across tests would fake provenance for an unrelated file."""
    ob._UNRECORDED_WRITES.clear()
    yield
    ob._UNRECORDED_WRITES.clear()


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

    def test_flow_list_splits_on_top_level_commas_only(self):
        # `inner.split(",")` made three tags out of two and — because
        # upsert_managed re-serializes what it parsed — wrote the mangled list
        # back over Ben's. The codec's one accepted-but-wrong construct.
        parsed, _ = parse_frontmatter('---\ntags: [aria, "steward, phase 3", x]\n---\n')
        assert parsed["tags"] == ["aria", "steward, phase 3", "x"]

    def test_flow_list_handles_single_quotes_and_escapes(self):
        parsed, _ = parse_frontmatter(
            "---\ntags: ['a, b', 'it''s', \"say \\\"hi\\\", ok\"]\n---\n"
        )
        assert parsed["tags"] == ["a, b", "it's", 'say "hi", ok']

    def test_flow_list_values_survive_a_write_roundtrip(self):
        fm = {"tags": ["aria", "steward, phase 3"]}
        parsed, _ = parse_frontmatter(dump_frontmatter(fm))
        assert parsed == fm

    def test_flow_list_the_codec_cannot_resolve_is_refused_not_guessed(self):
        for text in (
            '---\ntags: [aria, "unterminated]\n---\n',
            "---\ntags: [aria,,steward]\n---\n",
        ):
            with pytest.raises(FrontmatterError):
                parse_frontmatter(text)

    def test_trailing_comma_is_a_list_of_one(self):
        parsed, _ = parse_frontmatter("---\ntags: [aria, ]\n---\n")
        assert parsed["tags"] == ["aria"]

    def test_the_roundtrip_guarantee_is_value_level_only(self):
        # dump(parse(text)) == text is NOT claimed and is not true: the
        # serializer emits one canonical form per value. Pin the weaker, real
        # guarantee so nobody builds a diff-based edit detector on the stronger
        # one (the reader hashes the FILE for exactly this reason).
        text = "---\nnote: 'hello world'\ntags: [a, b]\n---\n"
        fm, _ = parse_frontmatter(text)
        rendered = dump_frontmatter(fm)
        assert rendered != text                            # form drifts...
        assert "note: hello world" in rendered              # quotes dropped
        assert "  - a" in rendered                          # flow list -> block
        assert parse_frontmatter(rendered)[0] == fm         # ...values do not
        # And the other direction: a string that would re-parse as something
        # else comes back quoted, which is drift too.
        assert 'title: "2026"' in dump_frontmatter({"title": "2026"})

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

    async def test_upsert_managed_refreshes_a_doc_aria_owns(self, enabled):
        db = _FakeDB()
        w = _writer(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        first = await w.upsert_managed(
            str(plan), {"status": "active", "plan_hash": "abc123"},
            "# Plan\n\nfirst body", managed_keys=["status", "plan_hash"],
        )
        assert first["wrote"] is True and first["reason"] == "new"

        second = await w.upsert_managed(
            str(plan), {"status": "paused", "plan_hash": "def456"},
            "# Plan\n\nsecond body", managed_keys=["status", "plan_hash"],
        )
        fm, body = parse_frontmatter(plan.read_text(encoding="utf-8"))
        assert second["wrote"] is True and second["human_edited"] is False
        assert second["reason"] == "aria-owned"
        assert fm["status"] == "paused" and fm["plan_hash"] == "def456"
        assert "second body" in body and "first body" not in body

    async def test_seed_key_is_created_once_and_never_rewritten(self, enabled):
        """D1: `approval:` has to be CREATED by ARIA (nothing else writes it)
        and then never written again — a later tick rewriting it to `pending`
        is ARIA silently revoking Ben's own approval."""
        db = _FakeDB()
        w = _writer(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"

        first = await w.upsert_managed(
            str(plan), {"status": "active", "approval": "pending"},
            "# Plan\n\nbody", managed_keys=["status"],
        )
        fm, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
        assert fm["approval"] == "pending"        # seeded: the key now exists
        assert first["seeded"] == ["approval"]

        # ARIA re-runs and proposes a different value for the same key — and,
        # for good measure, tries to claim it as managed. Neither may take.
        second = await w.upsert_managed(
            str(plan), {"status": "active", "approval": "rejected"},
            "# Plan\n\nnew body", managed_keys=["status", "approval"],
        )
        fm, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
        assert second["wrote"] is True             # the body did refresh
        assert second["seeded"] == []
        assert fm["approval"] == "pending"         # ...but the answer key did not

    async def test_seed_keys_are_all_three_control_keys(self, enabled):
        db = _FakeDB()
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        result = await _writer(enabled, db).upsert_managed(
            str(plan),
            {"status": "active", "approval": "pending", "autonomy": 1,
             "accepted": "pending"},
            "# Plan\n\nbody", managed_keys=["status"],
        )
        assert result["seeded"] == ["accepted", "approval", "autonomy"]
        fm, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
        assert fm["approval"] == "pending" and fm["autonomy"] == 1
        assert fm["accepted"] == "pending"

    async def test_upsert_managed_never_replaces_a_body_ben_edited(self, enabled):
        """D2: the old code wrote `body` unconditionally and computed
        `human_edited` afterwards, so every phone edit outside `## Notes from
        Ben` was destroyed by the next steward tick."""
        db = _FakeDB()
        w = _writer(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(str(plan), {"status": "active", "approval": "pending"},
                               "# Plan\n\nstep one", managed_keys=["status"])

        # Ben, on his phone: flips the answer and rewrites a paragraph in place.
        fm, body = parse_frontmatter(plan.read_text(encoding="utf-8"))
        fm["approval"] = "approved"
        _write_raw(plan, dump_frontmatter(fm)
                   + body.replace("step one", "step one, but do the DS4 work first"))
        bens_bytes = plan.read_bytes()

        result = await w.upsert_managed(
            str(plan), {"status": "active", "plan_hash": "zzz"},
            "# Plan\n\nstep two", managed_keys=["status", "plan_hash"],
        )
        assert plan.read_bytes() == bens_bytes     # not one byte of his doc
        assert result["wrote"] is False
        assert result["human_edited"] is True
        assert result["reason"] == "human-edited"

        # ...and ARIA's version is a proposal beside it, not a silent win.
        proposal = Path(result["proposal_path"])
        assert proposal.exists()
        p_fm, p_body = parse_frontmatter(proposal.read_text(encoding="utf-8"))
        assert "step two" in p_body
        assert p_fm["proposed_for"] == str(plan)
        # The proposal has to be safe to copy over the original, so it carries
        # his answer forward rather than resetting the seed key to `pending`.
        assert p_fm["approval"] == "approved"
        # And a human hears about it.
        review = db["scan_review"].docs
        assert len(review) == 1
        assert review[0]["kind"] == "vault_write_refused"
        assert review[0]["subject"] == str(plan)

    async def test_upsert_managed_refuses_a_doc_with_no_hash_record(self, enabled):
        """A doc ARIA has never recorded a write for is somebody else's — most
        likely one Ben hand-wrote. Unknown provenance is refused exactly like a
        known human edit; guessing is what loses an edit."""
        db = _FakeDB()
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, dump_frontmatter({"status": "active", "approval": "approved"})
                   + "\n# Plan\n\nBen's own plan\n")
        original = plan.read_bytes()

        result = await _writer(enabled, db).upsert_managed(
            str(plan), {"status": "active", "plan_hash": "abc123"},
            "# Plan\n\nARIA's plan", managed_keys=["status", "plan_hash"],
        )
        assert plan.read_bytes() == original
        assert result["wrote"] is False and result["reason"] == "unknown-provenance"
        assert Path(result["proposal_path"]).exists()

    async def test_refused_write_carries_bens_notes_and_keys_into_the_proposal(self, enabled):
        db = _FakeDB()
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, dump_frontmatter({
            "status": "active",
            "approval": "approved",      # Ben's key — ARIA does not own it
            "autonomy": 2,
        }) + "\n# Plan\n\nold body\n\n## Notes from Ben\n\ndo the DS4 work first\n")

        result = await _writer(enabled, db).upsert_managed(
            str(plan),
            {"status": "active", "plan_hash": "abc123"},
            "# Plan\n\nfresh steward body",
            managed_keys=["status", "plan_hash", "created"],
        )
        assert result["preserved_notes"] is True
        fm, body = parse_frontmatter(Path(result["proposal_path"]).read_text(encoding="utf-8"))
        assert fm["autonomy"] == 2                # human keys carried across
        assert fm["plan_hash"] == "abc123"        # ARIA's managed key applied
        assert "fresh steward body" in body
        assert extract_section(body, "## Notes from Ben") == "do the DS4 work first"

    async def test_a_second_refusal_does_not_pile_up_review_items(self, enabled):
        db = _FakeDB()
        w = _writer(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, dump_frontmatter({"status": "active"}) + "\n# Plan\n\nhis\n")
        for _ in range(3):
            await w.upsert_managed(str(plan), {"status": "active"}, "# Plan\n\nmine",
                                   managed_keys=["status"])
        assert len(db["scan_review"].docs) == 1   # deduped by (kind, subject)

    async def test_flow_list_is_not_corrupted_by_a_refresh(self, enabled):
        """The parse bug only bit because upsert_managed writes back what it
        parsed — so the regression test belongs on the write path too."""
        db = _FakeDB()
        w = _writer(enabled, db)
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        await w.upsert_managed(
            str(plan), {"status": "active", "tags": ["aria", "steward, phase 3"]},
            "# Plan\n\nbody", managed_keys=["status", "tags"],
        )
        await w.upsert_managed(str(plan), {"status": "paused"}, "# Plan\n\nbody2",
                               managed_keys=["status"])
        fm, _ = parse_frontmatter(plan.read_text(encoding="utf-8"))
        assert fm["tags"] == ["aria", "steward, phase 3"]

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

    async def test_lost_hash_record_retries_alerts_and_is_not_read_as_bens_edit(
        self, enabled
    ):
        """D7: `_record_write` used to swallow the Mongo failure with a
        logger.warning and return the digest as if it had stored it. The next
        poll then reported ARIA's own frontmatter as a `human_edit` — a control
        input nobody typed."""
        exploding = _ExplodingCollection()
        db = _FakeDB({settings.vault_hash_state_collection: exploding})
        notifier = SimpleNamespace(notify=AsyncMock())
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"

        with patch.object(ob, "_HASH_STATE_BACKOFF_SECONDS", 0), \
             patch("aria.api.deps.get_notification_service", return_value=notifier):
            result = await _writer(enabled, db).upsert_managed(
                str(plan), {"status": "active"}, "# Plan\n\nbody",
                managed_keys=["status"],
            )

        assert result["wrote"] is True
        assert result["hash_recorded"] is False          # says so, does not pretend
        assert exploding.update_calls == ob._HASH_STATE_ATTEMPTS   # retried first
        notifier.notify.assert_awaited_once()
        assert notifier.notify.await_args.kwargs["source"] == "vault"

        # The write is remembered in-process, so the reader knows whose bytes
        # these are even with no record in Mongo.
        assert ob.unrecorded_write_digest(plan) == content_hash(
            plan.read_text(encoding="utf-8")
        )
        events = await _reader(enabled, db).poll_once()
        assert [e for e in events if e["type"] == vr.EV_HUMAN_EDIT] == []

    async def test_a_recorded_write_clears_the_unrecorded_marker(self, enabled):
        db = _FakeDB()
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        ob.note_unrecorded_write(plan, "stale-digest-from-an-earlier-outage")
        await _writer(enabled, db).upsert_managed(
            str(plan), {"status": "active"}, "# Plan\n\nbody", managed_keys=["status"]
        )
        assert ob.unrecorded_write_digest(plan) is None

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

    async def test_aria_note_without_a_hash_record_is_not_an_edit_by_ben(self, enabled):
        """A note published through a writer with no db has no hash record. Its
        own `generated_by: aria` frontmatter must keep it from reading as one of
        Ben's edits — and with nothing answered on it, it is silent."""
        with patch.object(settings, "obsidian_vault_path", str(enabled)):
            path = await ObsidianWriter(str(enabled)).publish(
                "body", title="Unwired publish", project="ProjectAria",
                frontmatter={"accepted": "pending"},
            )
        assert await _reader(enabled, _FakeDB()).poll_once() == []
        assert Path(path).exists()

    async def test_first_sight_reports_a_decision_ben_already_made(self, enabled):
        """D3: `generated_by: aria` survives Ben's edit — he flips `accepted:`,
        not the provenance key. Adopting such a doc silently on first sight (the
        old behaviour) would have swallowed every answer he had already given,
        all at once, on the day `vault_reader_enabled` was flipped on."""
        db = _FakeDB()
        note = enabled / "ProjectAria" / "Research" / "2026-08-01 Qwen slot curve.md"
        _write_raw(note, dump_frontmatter({
            "title": "Qwen slot curve",
            "generated_by": "aria",       # ARIA published it...
            "accepted": True,             # ...and Ben answered it, weeks ago
        }) + "\n# Qwen slot curve\n\nfindings\n")

        events = await _reader(enabled, db).poll_once()
        accepted = [e for e in events if e["type"] == vr.EV_ACCEPTED]
        assert len(accepted) == 1
        assert accepted[0]["value"] is True
        assert accepted[0]["first_sight"] is True
        # Not claimed as an edit: ARIA wrote the doc and has no record of who
        # touched it since. The DECISION is what must not be dropped.
        assert [e for e in events if e["type"] == vr.EV_HUMAN_EDIT] == []
        assert await _reader(enabled, db).poll_once() == []      # reported once

    async def test_first_sight_reports_approval_and_autonomy_too(self, enabled):
        db = _FakeDB()
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, dump_frontmatter({
            "generated_by": "aria",
            "approval": "approved",
            "autonomy": 3,
        }) + "\n# Plan\n\nbody\n")
        events = await _reader(enabled, db).poll_once()
        by_type = {e["type"]: e for e in events}
        assert by_type[vr.EV_APPROVAL]["value"] == "approved"
        assert by_type[vr.EV_AUTONOMY]["value"] == 3
        assert all(e["first_sight"] is True for e in events)

    async def test_first_sight_of_a_hand_written_doc_still_reports_the_edit(self, enabled):
        # The suppression is narrow: only a doc ARIA's own frontmatter claims.
        db = _FakeDB()
        # STEWARD_PLAN.md, not CHARTER.md: approval is a PLAN key. A charter says
        # what a project is for and how far ARIA may go; the plan is what gets
        # approved. These tests are about persistence, not about that rule.
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, dump_frontmatter({"approval": "approved"}) + "\n# Plan\n")
        events = await _reader(enabled, db).poll_once()
        edits = [e for e in events if e["type"] == vr.EV_HUMAN_EDIT]
        assert len(edits) == 1 and edits[0]["first_sight"] is True

    # ------------------------------------------------------ event persistence

    async def test_events_survive_the_reader_that_consumed_them(self, enabled):
        """D4: a poll permanently advances each file's state, so an edit is
        reportable exactly once. With the events in one instance's memory, the
        worker's poll left `GET /vault/events` (a different instance) showing
        nothing at all — Ben's decision existed nowhere a human could look."""
        db = _FakeDB()
        # STEWARD_PLAN.md, not CHARTER.md: approval is a PLAN key. A charter says
        # what a project is for and how far ARIA may go; the plan is what gets
        # approved. These tests are about persistence, not about that rule.
        plan = enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md"
        _write_raw(plan, dump_frontmatter({"approval": "approved"}) + "\n# Plan\n")

        worker = _reader(enabled, db)
        assert await worker.poll_once()                  # the worker consumes them

        route_side = _reader(enabled, db)
        assert await route_side.poll_once() == []        # nothing left to find
        assert route_side.recent_events == []            # and its ring is empty
        persisted = await route_side.recent(50)
        assert [e["type"] for e in persisted] == [
            e["type"] for e in worker.recent_events
        ]
        assert any(e["type"] == vr.EV_APPROVAL for e in persisted)

    async def test_recent_is_newest_last_and_honours_the_limit(self, enabled):
        db = _FakeDB()
        r = _reader(enabled, db)
        charter = enabled / "ProjectAria" / "Planning" / "CHARTER.md"
        for value in (1, 2, 3):
            _write_raw(charter, dump_frontmatter({"autonomy": value}) + "\n# Charter\n")
            await r.poll_once()
        tail = await r.recent(2)
        assert len(tail) == 2
        autonomy = [e for e in tail if e["type"] == vr.EV_AUTONOMY]
        assert autonomy and autonomy[-1]["value"] == 3   # newest last

    async def test_events_are_not_mutated_by_persistence(self, enabled):
        # insert_many stamps `_id` into the dicts it is handed; those same dicts
        # are returned to the steward and JSON-serialized by the route.
        db = _FakeDB()
        charter = enabled / "ProjectAria" / "Planning" / "CHARTER.md"
        _write_raw(charter, dump_frontmatter({"autonomy": 1}) + "\n# Charter\n")
        events = await _reader(enabled, db).poll_once()
        assert events and not any("_id" in e for e in events)

    async def test_persistence_failure_does_not_swallow_the_events(self, enabled):
        db = _FakeDB({vr.EVENTS_COLLECTION: _ExplodingCollection()})
        charter = enabled / "ProjectAria" / "Planning" / "CHARTER.md"
        _write_raw(charter, dump_frontmatter({"autonomy": 1}) + "\n# Charter\n")
        r = _reader(enabled, db)
        events = await r.poll_once()
        assert [e["type"] for e in events]               # still returned...
        assert r.recent_events == events                 # ...and still cached

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

    async def test_worker_loop_delivers_persists_and_survives_a_bad_consumer(
        self, enabled
    ):
        """D10: the old version of this test appended to a list it never read,
        and called `poll_once()` — which does not invoke `on_events` at all.
        The delivery seam is in `_run`, so `_run` is what has to be driven: the
        steward is downstream of it, and a consumer that raises must not stop
        the reader or lose the tick."""
        db = _FakeDB()
        seen: list[dict] = []
        calls = {"n": 0}

        async def sink(events):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("steward blew up")     # the exception guard
            seen.extend(events)

        charter = enabled / "ProjectAria" / "Planning" / "CHARTER.md"
        _write_raw(charter, dump_frontmatter({"autonomy": 1}) + "\n# Charter\n")

        r = vr.VaultReader(db=db, vault_path=str(enabled), interval_seconds=10,
                           on_events=sink)
        r.interval = 0.05          # the 10 s floor is a production guard, not a
                                   # reason to make this test take 10 seconds
        with patch.object(vr, "SETTLE_SECONDS", 0):
            await r.start()
            assert r._task is not None
            for _ in range(200):                          # first tick: it raises
                if calls["n"] == 1:
                    break
                await asyncio.sleep(0.01)
            assert calls["n"] == 1
            assert not r._task.done()                     # the loop is still alive

            # Second tick, after a new edit: delivered this time.
            _write_raw(charter, dump_frontmatter({"autonomy": 2}) + "\n# Charter\n")
            for _ in range(200):
                if calls["n"] >= 2:
                    break
                await asyncio.sleep(0.01)
            await r.stop()

        assert r._task is None
        assert [e["type"] for e in seen] and any(
            e["type"] == vr.EV_AUTONOMY and e["value"] == 2 for e in seen
        )
        # Both ticks reached Mongo, including the one whose consumer raised.
        persisted = await r.recent(50)
        assert [e["value"] for e in persisted if e["type"] == vr.EV_AUTONOMY] == [1, 2]


# ------------------------------------------------------------- wiring

@pytest.mark.asyncio
class TestWiring:
    """deps + routes: one reader, one writer factory, no phantom attributes."""

    @pytest.fixture(autouse=True)
    def _reset_deps(self):
        from aria.api import deps
        from aria.main import app

        deps._vault_reader = None
        app.state.vault_reader = None
        yield
        deps._vault_reader = None
        app.state.vault_reader = None
        app.dependency_overrides.clear()

    @staticmethod
    def _request(app_state=None):
        return SimpleNamespace(
            app=SimpleNamespace(state=app_state or SimpleNamespace())
        )

    async def test_get_vault_reader_returns_the_worker_instance(self, enabled):
        """D4: the worker (main.py) and POST /vault/poll must be the SAME
        reader. Two instances race for each edit — whichever polls first
        advances the file state and the other sees nothing."""
        from aria.api.deps import get_vault_reader

        db = _FakeDB()
        worker = vr.VaultReader(db=db, vault_path=str(enabled))
        request = self._request(SimpleNamespace(vault_reader=worker))
        assert await get_vault_reader(request, db) is worker

    async def test_get_vault_reader_is_a_singleton_and_really_binds_the_db(self, enabled):
        """D8: `_vault_reader.db = db` used to set an attribute nothing read —
        VaultReader keeps its handle on `self.state`, so a reader built without
        a db stayed dbless while looking wired."""
        from aria.api.deps import get_vault_reader

        db = _FakeDB()
        request = self._request()
        first = await get_vault_reader(request, db)
        second = await get_vault_reader(request, db)
        assert first is second
        assert request.app.state.vault_reader is first    # published for the worker
        assert first.state.db is db                       # the handle that is read
        assert first.db is db

    async def test_get_obsidian_writer_carries_the_db(self):
        from aria.api.deps import get_obsidian_writer

        db = _FakeDB()
        writer = await get_obsidian_writer(db)
        assert writer.db is db

    async def test_publish_route_uses_the_writer_dependency(self, enabled):
        """D9: the dep existed with a load-bearing docstring and zero callers
        while the route built its own writer. A second construction site is a
        second chance to forget the db handle."""
        from httpx import ASGITransport, AsyncClient
        from unittest.mock import MagicMock

        from aria.api import deps
        from aria.main import app

        db = _FakeDB()
        writer = ObsidianWriter(str(enabled), db=db)
        used = {"n": 0}

        async def _dep():
            used["n"] += 1
            return writer

        app.dependency_overrides[deps.get_db] = lambda: db
        app.dependency_overrides[deps.get_obsidian_writer] = _dep
        rl = MagicMock()
        rl.check = MagicMock(return_value=(True, 100))
        ks = MagicMock()
        ks.is_active = False
        estop = MagicMock()
        estop.is_active = AsyncMock(return_value=False)
        with (
            patch("aria.main.settings") as mock_settings,
            patch("aria.main.get_rate_limiter", return_value=rl),
            patch("aria.api.deps.get_killswitch", return_value=ks),
            patch("aria.api.deps.resolve_estop_manager", AsyncMock(return_value=estop)),
        ):
            mock_settings.api_auth_enabled = False
            mock_settings.cors_origins = ["*"]
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/obsidian/publish",
                    json={"content": "findings", "title": "R", "doc_type": "Research",
                          "project": "ProjectAria"},
                )
        assert resp.status_code == 200, resp.text
        assert used["n"] == 1
        # The hash record is the point of routing through the dep.
        state = await db[settings.vault_hash_state_collection].find_one(
            {"path": resp.json()["path"]}
        )
        assert state["aria_hash"]

    async def test_vault_events_route_reads_the_collection(self, enabled):
        """D4: the endpoint used to return one instance's in-memory ring, which
        is empty whenever the worker is the thing that polled."""
        from httpx import ASGITransport, AsyncClient
        from unittest.mock import MagicMock

        from aria.api import deps
        from aria.main import app

        db = _FakeDB()
        # The plan carries the approval; the charter carries autonomy. This
        # test only needs an EV_APPROVAL to exist so the route can return it.
        _write_raw(enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md",
                   dump_frontmatter({"approval": "approved"}) + "\n# Plan\n")
        worker = vr.VaultReader(db=db, vault_path=str(enabled))
        assert await worker.poll_once()
        worker.recent_events = []                 # the ring is only a cache
        app.state.vault_reader = worker
        app.dependency_overrides[deps.get_db] = lambda: db

        rl = MagicMock()
        rl.check = MagicMock(return_value=(True, 100))
        ks = MagicMock()
        ks.is_active = False
        estop = MagicMock()
        estop.is_active = AsyncMock(return_value=False)
        with (
            patch("aria.main.settings") as mock_settings,
            patch("aria.main.get_rate_limiter", return_value=rl),
            patch("aria.api.deps.get_killswitch", return_value=ks),
            patch("aria.api.deps.resolve_estop_manager", AsyncMock(return_value=estop)),
        ):
            mock_settings.api_auth_enabled = False
            mock_settings.cors_origins = ["*"]
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/v1/vault/events?limit=10")
        assert resp.status_code == 200, resp.text
        types = [e["type"] for e in resp.json()["events"]]
        assert vr.EV_APPROVAL in types


# --------------------------------------------------- research auto-publish

@pytest.mark.asyncio
class TestResearchAutoPublish:
    """The research → vault → Ben → steward loop (D5). It lives in this file
    because what is under test is the vault contract, not the research run: a
    report published without a db handle and without `accepted:` is a broadcast
    with no return path — the reader reads ARIA's own note back as an edit by
    Ben, and there is no key for him to answer."""

    async def _run(self, db, enabled):
        from aria.research.models import ResearchConfig
        from aria.research.service import ResearchService

        service = ResearchService.__new__(ResearchService)
        service.db = db
        service.task_runner = SimpleNamespace(update_task=AsyncMock())
        service._research_branch = AsyncMock()
        service._synthesize_report = AsyncMock(return_value="the findings")
        service._persist_report_memories = AsyncMock()
        service._update_run = AsyncMock()
        service.get_run = AsyncMock(return_value={"task_id": "task-1"})
        with patch.object(settings, "obsidian_auto_publish_research", True):
            await service._run_research(
                "research-1", ResearchConfig(query="Qwen slot curve")
            )
        return service

    async def test_published_report_is_answerable_and_has_provenance(self, enabled):
        db = _FakeDB()
        service = await self._run(db, enabled)

        published = [
            call.kwargs["extra_updates"]["vault_path"]
            for call in service._update_run.await_args_list
            if "vault_path" in (call.kwargs.get("extra_updates") or {})
        ]
        assert len(published) == 1
        note = Path(published[0])
        fm, _ = parse_frontmatter(note.read_text(encoding="utf-8"))
        assert fm["accepted"] == "pending"        # the key Ben flips
        assert fm["research_id"] == "research-1"
        assert fm["generated_by"] == "aria"

        # db= was passed: there is a hash record, so the note is not read back
        # as one of Ben's edits.
        state = await db[settings.vault_hash_state_collection].find_one(
            {"path": str(note)}
        )
        assert state["aria_hash"] == content_hash(note.read_text(encoding="utf-8"))

    async def test_the_reader_sees_bens_answer_on_the_published_note(self, enabled):
        db = _FakeDB()
        service = await self._run(db, enabled)
        note = Path(
            [
                call.kwargs["extra_updates"]["vault_path"]
                for call in service._update_run.await_args_list
                if "vault_path" in (call.kwargs.get("extra_updates") or {})
            ][0]
        )
        # The note lands in the default folder; the reader scans <project>/Research.
        r = _reader(enabled, db)
        assert await r.poll_once() == []                  # ARIA's own bytes: quiet

        fm, body = parse_frontmatter(note.read_text(encoding="utf-8"))
        fm["accepted"] = True                             # Ben, on his phone
        _write_raw(note, dump_frontmatter(fm) + body)

        accepted = [e for e in await r.poll_once() if e["type"] == vr.EV_ACCEPTED]
        assert len(accepted) == 1 and accepted[0]["value"] is True


class TestThereIsExactlyOneApproval:
    """`approval:` is a PLAN key. Nothing else may act as a second gate.

    The charter answers "what is this project for, and how far may ARIA go"
    (`autonomy`); the plan answers "may ARIA do THIS". Before 2026-08-15 an
    `approval:` on a charter was accepted, mirrored into the plan's Mongo
    record, and logged as "steward plan marked 'approved'" — while the gate kept
    reading the plan FILE and saw `pending`. Two surfaces, one of which lied.
    """

    @pytest.mark.asyncio
    async def test_approval_on_a_charter_is_refused_and_explained(self, enabled):
        db = _FakeDB()
        _write_raw(enabled / "ProjectAria" / "Planning" / "CHARTER.md",
                   dump_frontmatter({"approval": "approved", "autonomy": 2})
                   + "\n# Charter\n")
        reader = vr.VaultReader(db=db, vault_path=str(enabled))
        events = await reader.poll_once()
        kinds = [e["type"] for e in events]

        # It must NOT read as an approval...
        assert vr.EV_APPROVAL not in kinds
        # ...and it must not be silently dropped either: Ben edited a key
        # expecting it to mean something, so he is told it does not.
        bad = [e for e in events if e["type"] == vr.EV_INVALID_VALUE
               and e.get("key") == "approval"]
        assert len(bad) == 1
        assert "STEWARD_PLAN.md" in bad[0]["error"]

        # autonomy on the charter is untouched — that IS a charter key.
        assert vr.EV_AUTONOMY in kinds

    @pytest.mark.asyncio
    async def test_approval_on_the_plan_is_the_one_that_counts(self, enabled):
        db = _FakeDB()
        _write_raw(enabled / "ProjectAria" / "Planning" / "STEWARD_PLAN.md",
                   dump_frontmatter({"approval": "approved"}) + "\n# Plan\n")
        reader = vr.VaultReader(db=db, vault_path=str(enabled))
        events = await reader.poll_once()
        approvals = [e for e in events if e["type"] == vr.EV_APPROVAL]
        assert len(approvals) == 1
        assert approvals[0]["value"] == "approved"
        assert approvals[0]["doc"] == vr.DOC_STEWARD_PLAN
