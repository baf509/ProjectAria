"""
ARIA - Conversation message archiving (perf review D17)

Purpose: `conversations.messages` grew without bound and every $push rewrote
the whole document, so each turn cost more than the last.

The rules this locks:
  - overflow is ARCHIVED, never dropped (the transcript is the record);
  - the archive is written before the inline array is trimmed;
  - the trim is by exact message id, so a message arriving mid-archive is
    not silently dropped by a slice;
  - reads page across the seam, so the API's pagination is unchanged;
  - memory_processed flags survive the move.
"""

from datetime import datetime, timezone

import pytest
from bson import ObjectId

from aria.db.conversation_archive import (
    archive_overflow,
    archived_count,
    messages_page,
)


def _msg(i: int, processed: bool = False) -> dict:
    return {
        "id": f"m{i:04d}",
        "role": "user" if i % 2 == 0 else "assistant",
        "content": f"message {i}",
        "created_at": datetime.now(timezone.utc),
        "memory_processed": processed,
    }


class _Conversations:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, flt, projection=None):
        if self.doc is None or flt["_id"] != self.doc["_id"]:
            return None
        out = dict(self.doc)
        if projection and projection.get("messages") == 0:
            out.pop("messages", None)
        return out

    async def update_one(self, flt, update):
        pull = update.get("$pull", {}).get("messages", {})
        ids = set(pull.get("id", {}).get("$in", []))
        self.doc["messages"] = [m for m in self.doc["messages"] if m.get("id") not in ids]
        for field, delta in (update.get("$inc") or {}).items():
            self.doc[field] = int(self.doc.get(field) or 0) + delta


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()

    async def to_list(self, length=None):
        return self._docs


class _Archives:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)

    def find(self, flt, projection=None):
        return _Cursor([d for d in self.docs if d["conversation_id"] == flt["conversation_id"]])


class _DB:
    def __init__(self, doc):
        self.conversations = _Conversations(doc)
        self.conversation_archives = _Archives()


@pytest.fixture
def convo():
    oid = ObjectId()
    return oid, {"_id": oid, "messages": [_msg(i) for i in range(250)]}


@pytest.mark.asyncio
async def test_overflow_is_archived_not_dropped(convo):
    oid, doc = convo
    db = _DB(doc)
    moved = await archive_overflow(db, oid, cap=200)
    assert moved == 50
    assert len(doc["messages"]) == 200
    assert doc["messages"][0]["id"] == "m0050", "the newest 200 must be kept inline"
    assert await archived_count(db, oid) == 50
    assert doc["archived_message_count"] == 50, \
        "the read path uses this counter to skip the archive lookup entirely"
    archived_ids = [m["id"] for m in db.conversation_archives.docs[0]["messages"]]
    assert archived_ids == [f"m{i:04d}" for i in range(50)]


@pytest.mark.asyncio
async def test_no_op_under_the_cap(convo):
    oid, doc = convo
    doc["messages"] = [_msg(i) for i in range(10)]
    db = _DB(doc)
    assert await archive_overflow(db, oid, cap=200) == 0
    assert db.conversation_archives.docs == []


@pytest.mark.asyncio
async def test_memory_processed_flags_survive_the_move(convo):
    oid, doc = convo
    doc["messages"] = [_msg(i, processed=(i % 2 == 0)) for i in range(250)]
    db = _DB(doc)
    await archive_overflow(db, oid, cap=200)
    archived = db.conversation_archives.docs[0]["messages"]
    assert [m["memory_processed"] for m in archived] == [i % 2 == 0 for i in range(50)]


@pytest.mark.asyncio
async def test_reads_page_across_the_seam(convo):
    oid, doc = convo
    full_before = [m["id"] for m in doc["messages"]]
    db = _DB(doc)
    await archive_overflow(db, oid, cap=200)

    # Walk the whole history back in pages of 40 and reassemble it.
    seen: list[str] = []
    for skip in range(0, 250, 40):
        page = await messages_page(db, oid, msg_limit=40, msg_skip=skip)
        seen = [m["id"] for m in page] + seen
    assert seen == full_before, "paging across the archive seam lost or reordered messages"


@pytest.mark.asyncio
async def test_recent_window_does_not_touch_the_archive(convo):
    oid, doc = convo
    db = _DB(doc)
    await archive_overflow(db, oid, cap=200)
    db.conversation_archives.docs.clear()   # any read would now come back empty
    page = await messages_page(db, oid, msg_limit=20, msg_skip=0)
    assert [m["id"] for m in page] == [f"m{i:04d}" for i in range(230, 250)]


@pytest.mark.asyncio
async def test_messages_without_ids_are_left_inline(convo):
    oid, doc = convo
    doc["messages"] = [{"role": "user", "content": f"no id {i}"} for i in range(250)]
    db = _DB(doc)
    assert await archive_overflow(db, oid, cap=200) == 0, \
        "a $pull with no ids to match must not run"
    assert len(doc["messages"]) == 250


@pytest.mark.asyncio
async def test_unknown_conversation_and_disabled_cap(convo):
    oid, doc = convo
    db = _DB(doc)
    assert await archive_overflow(db, oid, cap=0) == 0
    assert await archive_overflow(db, ObjectId(), cap=200) == 0
    assert await messages_page(db, ObjectId(), msg_limit=10, msg_skip=0) is None
