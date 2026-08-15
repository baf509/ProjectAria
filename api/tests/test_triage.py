"""Tests for the TriageWorker and the paused-shell nudge worker.

These are the two Hermes crons that moved into ARIA (steward proposal §1.2,
§3.1 item 12). The invariants below are incident-derived, not stylistic:

- an empty or ambiguous classification must leave the alert alone. Qwen3.8 is a
  reasoning model and returns EMPTY content when the budget runs out in
  `reasoning_content`; DS4 doing exactly that labelled every memory with zero
  entities. Here the same bug would silently un-raise an alert Ben needs.
- a real failure is never acked by triage: the outbox selects
  `needs_human=true & unacked`, so acking is indistinguishable from silencing.
- the diagnostic session is ALWAYS stopped, including when the poll times out —
  the cron asked its model to do that and leaked sessions.
- a "diagnose only" session that wrote to the workspace gets its proposal
  discarded and raises instead.
- triage never applies anything: the alert gains a proposal, nothing else.

No network, no Mongo, no live aria-api. Nothing here may reach
`aria.notifications.signal_rpc` — that really sends Signal messages to Ben.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from aria.notifications import signal_rpc
from aria.notifications.triage import (
    REPORT_BEGIN,
    REPORT_END,
    TriageWorker,
    build_diagnose_prompt,
    parse_classification,
    parse_proposal,
    strip_reasoning,
)
from aria.shells.nudge_worker import NudgeWorker


# ---------------------------------------------------------------------------
# Minimal in-memory Mongo stand-in (no mongomock in this venv)
# ---------------------------------------------------------------------------

def _match(doc: dict, flt: dict) -> bool:
    for key, expected in (flt or {}).items():
        actual = doc.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$ne":
                    if actual == operand:
                        return False
                elif op == "$gte":
                    if actual is None or actual < operand:
                        return False
                elif op == "$in":
                    if actual not in operand:
                        return False
                else:  # pragma: no cover - unsupported operator in a test
                    raise NotImplementedError(op)
        elif actual != expected:
            return False
    return True


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction=1):
        self._docs.sort(key=lambda d: d.get(field) or 0, reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[: int(n)]
        return self

    def __aiter__(self):
        async def _gen():
            for doc in self._docs:
                yield doc

        return _gen()


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = list(docs or [])

    async def insert_one(self, doc):
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, flt=None, *args, **kwargs):
        for doc in self.docs:
            if _match(doc, flt or {}):
                return dict(doc)
        return None

    async def update_one(self, flt, update, upsert=False, **kwargs):
        for doc in self.docs:
            if _match(doc, flt):
                for op, fields in update.items():
                    assert op == "$set", op
                    doc.update(fields)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def count_documents(self, flt=None):
        return len([d for d in self.docs if _match(d, flt or {})])

    def find(self, flt=None, *args, **kwargs):
        return _FakeCursor([dict(d) for d in self.docs if _match(d, flt or {})])


class FakeDB:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


class _ExplodingClient:
    """Any attempt to open a real connection from a test is a bug."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("no test here may talk to signal-cli")


@pytest.fixture(autouse=True)
def _no_real_signal():
    """corsair's .env carries a live break-glass account and signal-cli really
    is listening on :8090, so an unpatched send in a test messages Ben (it has
    happened). Patch the module reference INSIDE signal_rpc — patching
    signal_rpc.httpx.AsyncClient would mutate the shared httpx module."""
    with patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_ExplodingClient)):
        yield


@pytest.fixture(autouse=True)
def _stops_clear():
    """Killswitch and e-stop both open, unless a test says otherwise."""
    ks = MagicMock()
    ks.is_active = False
    estop = MagicMock()
    estop.is_active = AsyncMock(return_value=False)
    with (
        patch("aria.api.deps.get_killswitch", return_value=ks),
        patch("aria.api.deps.resolve_estop_manager", AsyncMock(return_value=estop)),
    ):
        yield SimpleNamespace(killswitch=ks, estop=estop)


# ---------------------------------------------------------------------------
# Fakes for the collaborators triage drives
# ---------------------------------------------------------------------------

GOOD_REPORT = f"""
some pane noise from the agent
{REPORT_BEGIN}
ROOT_CAUSE: aria-api lost its Mongo connection when mongod restarted.
FIX: systemctl --user restart aria-api
CONFIDENCE: high
EVIDENCE: journalctl line "ServerSelectionTimeoutError" at 19:04:11
{REPORT_END}
"""


class FakeManager:
    """Stands in for CodingSessionManager: records what triage asked for."""

    def __init__(self, output=GOOD_REPORT, diff="", status="running"):
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self.output = output
        self.diff = diff
        self.status = status
        self.get_output_calls = 0

    async def start_session(self, **kwargs):
        self.started.append(kwargs)
        return {"_id": "sess-1", "status": self.status}

    async def get_output(self, session_id, lines=50):
        self.get_output_calls += 1
        return self.output

    async def get_session(self, session_id):
        return {"_id": session_id, "status": self.status}

    async def get_diff(self, session_id):
        return self.diff

    async def stop_session(self, session_id):
        self.stopped.append(session_id)
        return True


class FakeAdapter:
    """Stands in for the local Qwen adapter (llm/base.LLMAdapter.complete)."""

    def __init__(self, content="FAILURE\nservice is down"):
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, messages, tools=None, temperature=0.7, max_tokens=4096):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        return self.content, [], {"input_tokens": 10, "output_tokens": 5}


def _alert(**overrides) -> dict:
    doc = {
        "_id": ObjectId(),
        "source": "selfcheck",
        "event_type": "degraded",
        "detail": "llm (ConnectError)",
        "message": "[selfcheck] DEGRADED: llm (ConnectError)",
        "acked": False,
        "needs_human": True,
        "severity": "high",
        "kind": "selfcheck",
        "occurrences": 1,
        "proposal": None,
        "decision": None,
        "created_at": datetime.now(timezone.utc),
    }
    doc.update(overrides)
    return doc


def _worker(db, **kwargs) -> TriageWorker:
    kwargs.setdefault("adapter", FakeAdapter())
    kwargs.setdefault("manager", FakeManager())
    kwargs.setdefault("poll_seconds", 0)
    kwargs.setdefault("deadline_seconds", 5)
    # Pinned rather than inherited from settings so these tests keep meaning the
    # same thing if the guard's worktree default is ever flipped.
    kwargs.setdefault("use_worktree", True)
    return TriageWorker(db, notifier=None, **kwargs)


# ---------------------------------------------------------------------------
# Parsing — the reasoning-model trap
# ---------------------------------------------------------------------------

class TestParsing:
    def test_empty_content_is_not_a_verdict(self):
        # The exact shape of the DS4 bug: the model spent its budget in
        # reasoning_content and returned nothing.
        assert parse_classification("") is None
        assert parse_classification("   \n ") is None

    def test_reasoning_only_is_not_a_verdict(self):
        # The adapter wraps reasoning in <think> when content follows; an
        # unterminated block means the answer was cut off mid-thought.
        assert parse_classification("<think>the alert says FAILURE maybe") is None

    def test_verdict_after_reasoning(self):
        raw = "<think>weekly roll-up, nothing broken</think>INFORMATIONAL\nweekly report"
        assert parse_classification(raw) is True
        assert strip_reasoning(raw).startswith("INFORMATIONAL")

    def test_both_words_is_ambiguous(self):
        assert parse_classification("INFORMATIONAL or FAILURE, hard to say") is None

    def test_failure_verdict(self):
        assert parse_classification("FAILURE\nmongod is unreachable") is False

    def test_proposal_parsed_from_last_block(self):
        # The pane holds our own prompt (which contains the template) above the
        # agent's answer, so the LAST block is the answer.
        pane = build_diagnose_prompt(_alert()) + "\n" + GOOD_REPORT
        parsed = parse_proposal(pane)
        assert parsed["root_cause"].startswith("aria-api lost its Mongo connection")
        assert parsed["fix"] == "systemctl --user restart aria-api"
        assert parsed["confidence"] == "high"
        assert "ServerSelectionTimeoutError" in parsed["evidence"]

    def test_prompt_template_alone_is_not_a_proposal(self):
        # An agent that only echoed the prompt has said nothing; writing that as
        # a proposal would put a placeholder in Ben's Signal message.
        assert parse_proposal(build_diagnose_prompt(_alert())) is None

    def test_report_without_root_cause_is_rejected(self):
        pane = f"{REPORT_BEGIN}\nFIX: restart it\nCONFIDENCE: low\n{REPORT_END}"
        assert parse_proposal(pane) is None

    def test_unfenced_output_is_rejected(self):
        assert parse_proposal("ROOT_CAUSE: mongod died\nFIX: restart") is None


# ---------------------------------------------------------------------------
# Classification path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_informational_alert_is_downgraded_and_acked():
    db = FakeDB()
    alert = _alert(source="shells:report", event_type="weekly report", severity="medium")
    db.alerts.docs.append(alert)
    worker = _worker(db, adapter=FakeAdapter("INFORMATIONAL\nweekly roll-up, nothing broken"))

    summary = await worker.tick_once()

    assert summary["informational"] == 1
    row = db.alerts.docs[0]
    assert row["severity"] == "info"
    assert row["needs_human"] is False
    assert row["acked"] is True
    assert row["triage"]["state"] == "informational"
    assert row["triage"]["reason"]  # acked WITH a reason, not silently


@pytest.mark.asyncio
async def test_unusable_classification_leaves_the_alert_alone():
    """The whole point of rule 7: an empty completion must not un-raise an alert.
    Ben still gets it; we just learn nothing."""
    db = FakeDB()
    alert = _alert()
    db.alerts.docs.append(alert)
    manager = FakeManager()
    worker = _worker(db, adapter=FakeAdapter(""), manager=manager)

    summary = await worker.tick_once()

    row = db.alerts.docs[0]
    assert summary["failed"] == 1
    assert row["needs_human"] is True
    assert row["acked"] is False
    assert row["severity"] == "high"
    assert row["proposal"] is None
    assert manager.started == []  # no session spawned on an unknown verdict
    assert row["triage"]["state"] == "classify_failed"


@pytest.mark.asyncio
async def test_classifier_unreachable_leaves_the_alert_alone():
    db = FakeDB()
    db.alerts.docs.append(_alert())
    exploding = MagicMock()
    exploding.complete = AsyncMock(side_effect=ConnectionError("no route to :8080"))
    worker = _worker(db, adapter=exploding)

    await worker.tick_once()

    row = db.alerts.docs[0]
    assert row["needs_human"] is True and row["acked"] is False


@pytest.mark.asyncio
async def test_critical_alert_is_never_reclassified():
    """A 27B model does not get to decide a critical row is routine."""
    db = FakeDB()
    db.alerts.docs.append(_alert(severity="critical", kind="selfcheck"))
    adapter = FakeAdapter("INFORMATIONAL\nlooks fine to me")
    manager = FakeManager()
    worker = _worker(db, adapter=adapter, manager=manager)

    await worker.tick_once()

    row = db.alerts.docs[0]
    assert adapter.calls == []           # the model was never asked
    assert row["needs_human"] is True    # and could not lower the raise
    assert row["proposal"]["root_cause"]  # it was diagnosed instead


@pytest.mark.asyncio
async def test_guard_and_estop_alerts_are_never_touched():
    db = FakeDB()
    db.alerts.docs.append(_alert(source="guard", kind="guard", event_type="blocked"))
    manager = FakeManager()
    adapter = FakeAdapter()
    worker = _worker(db, adapter=adapter, manager=manager)

    await worker.tick_once()

    row = db.alerts.docs[0]
    assert row["triage"]["state"] == "denied"
    assert manager.started == [] and adapter.calls == []
    assert row["needs_human"] is True


@pytest.mark.asyncio
async def test_paused_shell_alerts_go_straight_to_ben():
    """The cron carved `shells:nudge` out of its diagnose step: the shell is
    waiting for a human instruction, and a cloud session can only restate the
    alert. It reaches Ben untouched."""
    db = FakeDB()
    db.alerts.docs.append(
        _alert(source="shells:nudge", kind="shells-nudge", event_type="nudge:exhausted")
    )
    manager = FakeManager()
    adapter = FakeAdapter()
    worker = _worker(db, manager=manager, adapter=adapter)

    await worker.tick_once()

    row = db.alerts.docs[0]
    assert manager.started == [] and adapter.calls == []
    assert row["needs_human"] is True and row["acked"] is False
    assert row["triage"]["reason"] == "needs_human_instruction"


@pytest.mark.asyncio
async def test_triage_never_triages_its_own_output():
    """The cron's diagnostic sessions raised alerts that spawned more diagnostic
    sessions; 31 rows later nobody was reading the queue."""
    db = FakeDB()
    db.alerts.docs.append(_alert(source="triage:diagnose", kind="triage"))
    manager = FakeManager()
    worker = _worker(db, manager=manager)

    await worker.tick_once()

    assert manager.started == []
    assert db.alerts.docs[0]["triage"]["state"] == "denied"


# ---------------------------------------------------------------------------
# Diagnosis path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failure_gets_a_proposal_and_stays_needing_a_human():
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager()
    worker = _worker(db, manager=manager)

    summary = await worker.tick_once()

    assert summary["diagnosed"] == 1
    row = db.alerts.docs[0]
    proposal = row["proposal"]
    assert proposal["root_cause"].startswith("aria-api lost its Mongo connection")
    assert proposal["fix"] == "systemctl --user restart aria-api"
    assert proposal["confidence"] == "high"
    assert proposal["evidence"]
    # Propose, never apply: the alert is still Ben's decision, and triage did
    # not ack it — acking would drop it out of the outbox's selection.
    assert row["needs_human"] is True
    assert row["acked"] is False
    assert row["decision"] is None
    assert manager.stopped == ["sess-1"]


@pytest.mark.asyncio
async def test_diagnostic_session_is_stopped_even_when_polling_times_out():
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager(output="the agent rambled and never printed a report")
    worker = _worker(db, manager=manager, deadline_seconds=0)

    summary = await worker.tick_once()

    assert manager.stopped == ["sess-1"]   # ALWAYS stop — a leak is the old bug
    assert summary["failed"] == 1
    row = db.alerts.docs[0]
    assert row["proposal"] is None         # never write an empty proposal
    assert row["needs_human"] is True
    assert db.triage_runs.docs[0]["outcome"] == "unparseable"


@pytest.mark.asyncio
async def test_session_start_failure_is_not_fatal_to_the_alert():
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager()
    manager.start_session = AsyncMock(side_effect=RuntimeError("queue full"))
    worker = _worker(db, manager=manager)

    await worker.tick_once()

    row = db.alerts.docs[0]
    assert row["proposal"] is None and row["needs_human"] is True
    assert db.triage_runs.docs[0]["outcome"] == "error"


@pytest.mark.asyncio
async def test_worktree_session_that_writes_is_reported_not_quoted():
    """A diagnostic agent drifting into action is the documented failure mode.
    The tripwire is the tree itself, not a promise in the prompt: a session
    started in a fresh worktree has a clean diff, so ANY diff is a write."""
    db = FakeDB()
    db.alerts.docs.append(_alert())
    notifier = MagicMock()
    notifier.notify = AsyncMock(return_value={"queued": True})
    manager = FakeManager(diff="diff --git a/api/aria/main.py b/api/aria/main.py\n+oops")
    worker = _worker(db, manager=manager, use_worktree=True)
    worker.notifier = notifier

    await worker.tick_once()

    row = db.alerts.docs[0]
    assert row["proposal"] is None            # the analysis is not trusted
    assert manager.stopped == ["sess-1"]
    assert manager.started[0]["create_worktree"] is True
    notifier.notify.assert_awaited_once()
    kwargs = notifier.notify.await_args.kwargs
    assert kwargs["needs_human"] is True
    assert kwargs["event_type"] == "diagnose:wrote_to_workspace"
    assert db.triage_runs.docs[0]["outcome"] == "wrote_to_workspace"


@pytest.mark.asyncio
async def test_live_checkout_write_is_detected_against_the_baseline():
    """Without a worktree the live tree is usually already dirty (Ben's own
    work), so only a CHANGE since the spawn counts as the session writing."""
    db = FakeDB()
    db.alerts.docs.append(_alert())
    notifier = MagicMock()
    notifier.notify = AsyncMock(return_value={"queued": True})
    manager = FakeManager(diff="diff --git a/x b/x\n+written by the agent")
    worker = _worker(db, manager=manager, use_worktree=False)
    worker.notifier = notifier

    with patch(
        "aria.notifications.triage.TriageWorker._diff_hash",
        AsyncMock(return_value="baseline-hash-of-bens-dirty-tree"),
    ):
        await worker.tick_once()

    assert db.alerts.docs[0]["proposal"] is None
    notifier.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_clean_worktree_diff_is_not_a_violation():
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager(diff="")
    worker = _worker(db, manager=manager, use_worktree=True)

    await worker.tick_once()

    assert db.alerts.docs[0]["proposal"]["root_cause"]


@pytest.mark.asyncio
async def test_unreadable_baseline_does_not_accuse():
    """git unavailable / not a repo: the tripwire cannot speak, and silence is
    not an accusation."""
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager(diff="diff --git a/x b/x")
    worker = _worker(db, manager=manager, use_worktree=False)

    with patch(
        "aria.notifications.triage.TriageWorker._diff_hash", AsyncMock(return_value=None)
    ):
        await worker.tick_once()

    assert db.alerts.docs[0]["proposal"]["root_cause"]


# ---------------------------------------------------------------------------
# Budget + safety gates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hourly_diagnosis_budget_defers_without_burning_an_attempt():
    db = FakeDB()
    db.alerts.docs.append(_alert())
    now = datetime.now(timezone.utc)
    for _ in range(2):
        db.triage_runs.docs.append({"kind": "diagnose", "started_at": now})
    manager = FakeManager()
    worker = _worker(db, manager=manager, max_diagnoses_per_hour=2)

    summary = await worker.tick_once()

    assert summary["skipped"] == 1
    assert manager.started == []
    row = db.alerts.docs[0]
    assert row["triage"]["state"] == "deferred"
    assert row["triage"]["attempts"] == 0   # deferral is not the alert's fault
    assert row["needs_human"] is True


@pytest.mark.asyncio
async def test_old_diagnoses_do_not_count_against_the_budget():
    db = FakeDB()
    db.alerts.docs.append(_alert())
    stale = datetime.now(timezone.utc) - timedelta(hours=3)
    db.triage_runs.docs.append({"kind": "diagnose", "started_at": stale})
    manager = FakeManager()
    worker = _worker(db, manager=manager, max_diagnoses_per_hour=1)

    await worker.tick_once()

    assert manager.started  # the stale run is outside the window


@pytest.mark.asyncio
async def test_killswitch_blocks_the_whole_tick(_stops_clear):
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager()
    adapter = FakeAdapter()
    worker = _worker(db, manager=manager, adapter=adapter)
    _stops_clear.killswitch.is_active = True

    summary = await worker.tick_once()

    assert summary["reason"] == "stop_engaged"
    assert manager.started == [] and adapter.calls == []
    assert "triage" not in db.alerts.docs[0]


@pytest.mark.asyncio
async def test_estop_blocks_the_whole_tick(_stops_clear):
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager()
    worker = _worker(db, manager=manager)
    _stops_clear.estop.is_active = AsyncMock(return_value=True)

    summary = await worker.tick_once()

    assert summary["reason"] == "stop_engaged"
    assert manager.started == []


@pytest.mark.asyncio
async def test_unreadable_safety_gate_fails_closed(_stops_clear):
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager()
    worker = _worker(db, manager=manager)
    _stops_clear.estop.is_active = AsyncMock(side_effect=RuntimeError("mongo down"))

    summary = await worker.tick_once()

    assert summary["reason"] == "stop_engaged"
    assert manager.started == []


@pytest.mark.asyncio
async def test_unreadable_budget_refuses_to_spend():
    db = FakeDB()
    db.alerts.docs.append(_alert())
    manager = FakeManager()
    worker = _worker(db, manager=manager)
    broken = FakeCollection()
    broken.count_documents = AsyncMock(side_effect=RuntimeError("mongo down"))
    db._colls["triage_runs"] = broken

    await worker.tick_once()

    assert manager.started == []  # an unknown spend is not a licence to spend


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_selection_skips_acked_info_and_already_proposed_rows():
    db = FakeDB()
    db.alerts.docs.extend([
        _alert(acked=True),
        _alert(needs_human=False, severity="info"),
        _alert(proposal={"root_cause": "already known"}),
        _alert(triage={"state": "diagnosed", "attempts": 1}),
        _alert(triage={"state": "classify_failed", "attempts": 2}),
    ])
    worker = _worker(db, max_attempts=2)

    candidates = await worker._candidates()

    assert candidates == []


@pytest.mark.asyncio
async def test_per_tick_cap_is_honoured():
    db = FakeDB()
    for _ in range(4):
        db.alerts.docs.append(_alert())
    manager = FakeManager()
    worker = _worker(db, manager=manager, max_alerts_per_tick=2, max_diagnoses_per_hour=99)

    summary = await worker.tick_once()

    assert summary["considered"] == 2
    assert len(manager.started) == 2


@pytest.mark.asyncio
async def test_diagnose_prompt_carries_the_alert_verbatim_and_forbids_writes():
    prompt = build_diagnose_prompt(_alert(message="[selfcheck] DEGRADED: llm (ConnectError)"))
    assert "DIAGNOSE ONLY" in prompt
    assert "[selfcheck] DEGRADED: llm (ConnectError)" in prompt
    assert "infrastructure/running" in prompt   # not a hard-coded port table
    assert "Do not apply the fix" in prompt


@pytest.mark.asyncio
async def test_worker_does_not_start_when_disabled():
    db = FakeDB()
    worker = _worker(db)
    with patch("aria.notifications.triage.settings") as mock_settings:
        mock_settings.triage_enabled = False
        await worker.start()
    assert worker.status()["running"] is False


# ---------------------------------------------------------------------------
# Paused-shell nudge worker (the ex-cron timer)
# ---------------------------------------------------------------------------

class _FakeShellService:
    def __init__(self, rows):
        self.rows = rows
        self.nudged: list[str] = []

    async def fleet_overview(self, **kw):
        return self.rows


def _row(name, **overrides):
    row = {
        "name": name,
        "activity_state": "blocked",
        "awaiting_input": True,
        "idle_seconds": 900,
        "prompt_line": "> ",
        "project_dir": "/tmp/demo",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_nudge_worker_calls_the_shared_path_for_each_blocked_shell():
    """The cron's only job was this loop; the worker must call the SAME code
    path, so deleting the cron loses nothing."""
    db = FakeDB()
    shells = _FakeShellService([_row("claude-a"), _row("claude-b"), _row("claude-c", activity_state="working", awaiting_input=False)])
    notifier = MagicMock()
    notifier.notify = AsyncMock()
    worker = NudgeWorker(db, shell_service=shells, notifier=notifier)

    calls = []

    async def _fake_once(name, **kwargs):
        calls.append(name)
        return {"nudged": True, "attempts": 1, "escalated": False}

    with patch("aria.api.routes.shell_nudge.nudge_shell_once", _fake_once):
        summary = await worker.tick_once()

    assert calls == ["claude-a", "claude-b"]  # the working shell is left alone
    assert summary["blocked"] == 2 and summary["nudged"] == 2


@pytest.mark.asyncio
async def test_nudge_worker_counts_escalations_and_skips():
    db = FakeDB()
    shells = _FakeShellService([_row("claude-a"), _row("claude-b")])
    notifier = MagicMock()
    worker = NudgeWorker(db, shell_service=shells, notifier=notifier)

    async def _fake_once(name, **kwargs):
        if name == "claude-a":
            return {"nudged": True, "attempts": 3, "escalated": True}
        return {"nudged": False, "reason": "recently_nudged", "attempts": 1}

    with patch("aria.api.routes.shell_nudge.nudge_shell_once", _fake_once):
        summary = await worker.tick_once()

    assert summary["escalated"] == 1
    assert summary["skipped"] == 1
    assert summary["reasons"] == {"recently_nudged": 1}


@pytest.mark.asyncio
async def test_nudge_worker_survives_one_bad_shell():
    db = FakeDB()
    shells = _FakeShellService([_row("claude-a"), _row("claude-b")])
    worker = NudgeWorker(db, shell_service=shells, notifier=MagicMock())

    async def _fake_once(name, **kwargs):
        if name == "claude-a":
            raise RuntimeError("tmux pane vanished")
        return {"nudged": True, "attempts": 1, "escalated": False}

    with patch("aria.api.routes.shell_nudge.nudge_shell_once", _fake_once):
        summary = await worker.tick_once()

    assert summary["nudged"] == 1 and summary["skipped"] == 1


@pytest.mark.asyncio
async def test_nudge_worker_refuses_under_a_stop(_stops_clear):
    db = FakeDB()
    shells = _FakeShellService([_row("claude-a")])
    worker = NudgeWorker(db, shell_service=shells, notifier=MagicMock())
    _stops_clear.killswitch.is_active = True

    async def _fake_once(name, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("nudged a shell under an engaged stop")

    with patch("aria.api.routes.shell_nudge.nudge_shell_once", _fake_once):
        summary = await worker.tick_once()

    assert summary["reason"] == "stop_engaged"


@pytest.mark.asyncio
async def test_nudge_worker_refuses_without_a_notifier():
    """Sweeping with no way to raise the third strike would burn the attempts
    counter and lose the escalation silently."""
    db = FakeDB()
    shells = _FakeShellService([_row("claude-a")])
    worker = NudgeWorker(db, shell_service=shells, notifier=None)
    worker._resolve_notifier = lambda: None

    summary = await worker.tick_once()

    assert summary["reason"] == "no_notifier"


@pytest.mark.asyncio
async def test_nudge_worker_does_not_start_when_disabled():
    db = FakeDB()
    worker = NudgeWorker(db, shell_service=_FakeShellService([]), notifier=MagicMock())
    with patch("aria.shells.nudge_worker.settings") as mock_settings:
        mock_settings.shells_nudge_worker_enabled = False
        await worker.start()
    assert worker.status()["running"] is False


@pytest.mark.asyncio
async def test_nudge_worker_stop_is_idempotent():
    db = FakeDB()
    worker = NudgeWorker(db, shell_service=_FakeShellService([]), notifier=MagicMock())
    await worker.stop()          # never started
    with patch("aria.shells.nudge_worker.settings") as mock_settings:
        mock_settings.shells_nudge_worker_enabled = True
        mock_settings.shells_nudge_worker_interval_minutes = 15
        await worker.start()
    assert worker.status()["running"] is True
    await worker.stop()
    await worker.stop()
    assert worker.status()["running"] is False


@pytest.mark.asyncio
async def test_triage_worker_stop_cancels_a_running_tick():
    db = FakeDB()
    worker = _worker(db)
    with patch("aria.notifications.triage.settings") as mock_settings:
        mock_settings.triage_enabled = True
        await worker.start()
        assert worker.status()["running"] is True
        await asyncio.sleep(0)
        await worker.stop()
    assert worker.status()["running"] is False
