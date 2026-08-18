"""
ARIA - Extraction worker shell selection (perf review D14)

Purpose: the extraction tick swept the WHOLE fleet -- 664 shells today,
mostly stopped and long since caught up -- paying a state find_one and a
list_events for each, ~1300 queries per tick to usually do nothing.

Selection now happens in the query, via a cursor mirrored onto the shell
doc. The mirror is bookkeeping; shell_extraction_state stays authoritative,
so a lost mirror write costs one wasted sweep, never a missed extraction.
"""

import pytest

from aria.shells.service import ShellService


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


def _match(doc, q):
    for k, v in q.items():
        if k == "status":
            if doc.get("status") not in v["$in"]:
                return False
        elif k == "$expr":
            # Only the shape this code emits: line_count - last_extracted >= n
            floor = v["$gte"][1]
            pending = int(doc.get("line_count") or 0) - int(doc.get("last_extracted_line") or 0)
            if pending < floor:
                return False
    return True


class _Shells:
    def __init__(self, docs):
        self.docs = docs
        self.updates = []

    def find(self, q=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, q or {})])

    async def update_one(self, filt, update):
        self.updates.append((filt, update))
        for d in self.docs:
            if d["name"] == filt["name"]:
                d.update(update["$set"])
        return None


class _DB:
    def __init__(self, docs):
        self.shells = _Shells(docs)
        self.shell_events = None      # ShellService binds these at construction
        self.shell_snapshots = None


def _shell(name, line_count, extracted=0):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return {
        "name": name, "short_name": name, "status": "stopped",
        "created_at": now, "last_activity_at": now,
        "line_count": line_count, "last_extracted_line": extracted,
    }


@pytest.mark.asyncio
async def test_caught_up_shells_are_not_selected():
    db = _DB([
        _shell("busy", 5000, 100),      # 4900 pending
        _shell("caught-up", 5000, 5000),  # 0 pending
        _shell("trickle", 5000, 4995),    # 5 pending, under the floor
    ])
    svc = ShellService(db)
    names = [s.name for s in await svc.list_shells(status=["stopped"], pending_min=20)]
    assert names == ["busy"]


@pytest.mark.asyncio
async def test_no_pending_min_still_returns_the_whole_fleet():
    db = _DB([_shell("a", 10, 10), _shell("b", 10, 10)])
    svc = ShellService(db)
    names = sorted(s.name for s in await svc.list_shells(status=["stopped"]))
    assert names == ["a", "b"], "backfill and other callers must still see everything"


@pytest.mark.asyncio
async def test_a_shell_never_extracted_counts_all_its_lines_as_pending():
    """No mirror field at all (pre-deploy fleet) must read as fully pending."""
    doc = _shell("legacy", 900)
    doc.pop("last_extracted_line")
    db = _DB([doc])
    svc = ShellService(db)
    names = [s.name for s in await svc.list_shells(status=["stopped"], pending_min=20)]
    assert names == ["legacy"], "a shell with no mirror must not be skipped"
