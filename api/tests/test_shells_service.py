"""Tests for aria.shells.service — uses an in-memory fake Mongo."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aria.shells.service import ShellService, ShellNotFoundError, ShellStoppedError
from aria.shells.tmux import TmuxSessionNotFoundError


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query: dict, sort=None):
        matches = [d for d in self.docs if _match(d, query)]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key) or 0, reverse=(direction == -1))
        return dict(matches[0]) if matches else None

    async def insert_one(self, doc: dict):
        self.docs.append(dict(doc))

    async def insert_many(self, docs: list[dict]):
        for d in docs:
            self.docs.append(dict(d))

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        for d in self.docs:
            if _match(d, query):
                _apply_update(d, update)
                return
        if upsert:
            new_doc: dict = {}
            for k, v in query.items():
                new_doc[k] = v
            _apply_update(new_doc, update)
            self.docs.append(new_doc)

    async def find_one_and_update(
        self, query: dict, update: dict, upsert: bool = False, return_document=True
    ):
        for d in self.docs:
            if _match(d, query):
                _apply_update(d, update)
                return dict(d)
        if upsert:
            new_doc: dict = {}
            for k, v in query.items():
                new_doc[k] = v
            _apply_update(new_doc, update)
            self.docs.append(new_doc)
            return dict(new_doc)
        return None

    def find(self, query: dict):
        return FakeCursor([d for d in self.docs if _match(d, query)])


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = [dict(d) for d in docs]
        self._sort_key: tuple[str, int] | None = None
        self._limit: int | None = None

    def sort(self, key, direction: int = 1):
        self._sort_key = (key, direction)
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def __aiter__(self):
        docs = self._docs
        if self._sort_key:
            k, d = self._sort_key
            docs = sorted(docs, key=lambda x: x.get(k) or 0, reverse=(d == -1))
        if self._limit is not None:
            docs = docs[: self._limit]
        self._iter = iter(docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _match(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if isinstance(v, dict):
            if "$in" in v and doc.get(k) not in v["$in"]:
                return False
            if "$gt" in v and not (doc.get(k) is not None and doc.get(k) > v["$gt"]):
                return False
            if "$lt" in v and not (doc.get(k) is not None and doc.get(k) < v["$lt"]):
                return False
        elif doc.get(k) != v:
            return False
    return True


def _apply_update(doc: dict, update: dict) -> None:
    for op, fields in update.items():
        if op == "$set":
            for k, v in fields.items():
                _set_nested(doc, k, v)
        elif op == "$setOnInsert":
            for k, v in fields.items():
                if k not in doc:
                    _set_nested(doc, k, v)
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = doc.get(k, 0) + v


def _set_nested(doc: dict, key: str, value: Any) -> None:
    if "." not in key:
        doc[key] = value
        return
    parts = key.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


class FakeDB:
    def __init__(self):
        self.shells = FakeCollection()
        self.shell_events = FakeCollection()
        self.shell_snapshots = FakeCollection()


class FakeTmux:
    def __init__(self):
        self.sent: list[tuple[str, str, bool, bool]] = []
        self.sessions: set[str] = set()
        self.pane: dict[str, str] = {}

    async def has_session(self, name: str) -> bool:
        return name in self.sessions

    async def send_keys(self, name, text, *, append_enter=True, literal=False):
        if name not in self.sessions:
            raise TmuxSessionNotFoundError(name)
        self.sent.append((name, text, append_enter, literal))

    async def capture_pane(self, name, *, lines=10000):
        if name not in self.sessions:
            raise TmuxSessionNotFoundError(name)
        return self.pane.get(name, "")

    async def list_sessions(self, prefix=None):
        names = list(self.sessions)
        if prefix:
            names = [n for n in names if n.startswith(prefix)]
        return names


@pytest.fixture
def service():
    db = FakeDB()
    tmux = FakeTmux()
    svc = ShellService(db, tmux=tmux)  # type: ignore[arg-type]
    svc._fake_db = db  # type: ignore[attr-defined]
    svc._fake_tmux = tmux  # type: ignore[attr-defined]
    return svc


@pytest.mark.asyncio
async def test_register_and_get(service):
    shell = await service.register_shell("claude-proj", project_dir="/tmp/proj", pane_id="%1")
    assert shell.name == "claude-proj"
    assert shell.short_name == "proj"
    assert shell.status == "active"
    got = await service.get_shell("claude-proj")
    assert got is not None
    assert got.project_dir == "/tmp/proj"


@pytest.mark.asyncio
async def test_insert_events_line_numbers(service):
    await service.register_shell("claude-proj", project_dir="/tmp/proj")
    await service.insert_events_batch(
        "claude-proj",
        [
            {"kind": "output", "text_raw": "hello", "text_clean": "hello", "source": "pipe-pane"},
            {"kind": "output", "text_raw": "world", "text_clean": "world", "source": "pipe-pane"},
        ],
    )
    await service.insert_events_batch(
        "claude-proj",
        [
            {"kind": "output", "text_raw": "again", "text_clean": "again", "source": "pipe-pane"},
        ],
    )
    events = await service.list_events("claude-proj", limit=10)
    assert [e.line_number for e in events] == [1, 2, 3]
    shell = await service.get_shell("claude-proj")
    assert shell.line_count == 3


@pytest.mark.asyncio
async def test_list_events_since_line(service):
    await service.register_shell("claude-proj")
    await service.insert_events_batch(
        "claude-proj",
        [{"kind": "output", "text_raw": f"l{i}", "text_clean": f"l{i}", "source": "pipe-pane"} for i in range(5)],
    )
    events = await service.list_events("claude-proj", since_line=2, limit=10)
    assert [e.line_number for e in events] == [3, 4, 5]


@pytest.mark.asyncio
async def test_mark_stopped(service):
    await service.register_shell("claude-proj")
    await service.mark_stopped("claude-proj")
    shell = await service.get_shell("claude-proj")
    assert shell.status == "stopped"


@pytest.mark.asyncio
async def test_send_input_requires_shell(service):
    with pytest.raises(ShellNotFoundError):
        await service.send_input("claude-unknown", "hi")


@pytest.mark.asyncio
async def test_send_input_rejects_stopped(service):
    await service.register_shell("claude-proj")
    await service.mark_stopped("claude-proj")
    with pytest.raises(ShellStoppedError):
        await service.send_input("claude-proj", "hi")


@pytest.mark.asyncio
async def test_send_input_logs_event(service):
    await service.register_shell("claude-proj")
    service._fake_tmux.sessions.add("claude-proj")
    line = await service.send_input("claude-proj", "yes please")
    assert line >= 1
    events = await service.list_events("claude-proj", kinds=["input"], limit=10)
    assert len(events) == 1
    assert events[0].text_clean == "yes please"
    assert events[0].source == "send-keys"
    assert service._fake_tmux.sent[0] == ("claude-proj", "yes please", True, False)


@pytest.mark.asyncio
async def test_send_input_marks_stopped_on_missing_session(service):
    await service.register_shell("claude-proj")
    # session not in fake tmux → send_keys will raise
    with pytest.raises(ShellStoppedError):
        await service.send_input("claude-proj", "hi")
    shell = await service.get_shell("claude-proj")
    assert shell.status == "stopped"


@pytest.mark.asyncio
async def test_capture_and_snapshot(service):
    await service.register_shell("claude-proj")
    service._fake_tmux.sessions.add("claude-proj")
    service._fake_tmux.pane["claude-proj"] = "\x1b[31mhello\x1b[0m"
    snap = await service.capture_and_snapshot("claude-proj")
    assert snap is not None
    assert snap.content == "hello"
    # Same content → no new snapshot
    snap2 = await service.capture_and_snapshot("claude-proj")
    assert snap2 is None


@pytest.mark.asyncio
async def test_tail_order(service):
    await service.register_shell("claude-proj")
    await service.insert_events_batch(
        "claude-proj",
        [
            {"kind": "output", "text_raw": "a", "text_clean": "a", "source": "pipe-pane"},
            {"kind": "output", "text_raw": "b", "text_clean": "b", "source": "pipe-pane"},
            {"kind": "output", "text_raw": "c", "text_clean": "c", "source": "pipe-pane"},
        ],
    )
    tail = await service.tail("claude-proj", lines=2)
    assert [e.text_clean for e in tail] == ["b", "c"]
