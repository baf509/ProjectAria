"""Tests for Alerts v2: classification, persisted cooldowns, dedup, the
decide/delivered/heartbeat routes, and the relay watchdog + break-glass path.

The invariants under test are incident-derived, not stylistic:
- a stalled coding session must reach the queue (it was silently dropped)
- an aria-api restart must not re-fire a cooldown'd alert (37 restarts -> 31
  duplicate `selfcheck degraded` rows)
- the relay must never be declared dead on a box that never had a relay
- break-glass must never fire for an unlisted alert kind
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from httpx import ASGITransport, AsyncClient

from aria.config import settings
from aria.notifications import signal_rpc
from aria.notifications.relay import RELAY_STATE_ID, RelayWatchdog
from aria.notifications.service import NotificationService, classify


# ---------------------------------------------------------------------------
# Minimal in-memory Mongo stand-in (no mongomock in this venv)
# ---------------------------------------------------------------------------

def _match(doc: dict, flt: dict) -> bool:
    for key, expected in (flt or {}).items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in expected):
                return False
            continue
        actual = doc.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if op == "$ne":
                    if actual == operand:
                        return False
                elif op == "$in":
                    # Mongo matches $in element-wise against an array field —
                    # db.projects.relevant_paths is one, and the slug resolver
                    # queries it.
                    if isinstance(actual, list):
                        if not any(item in operand for item in actual):
                            return False
                    elif actual not in operand:
                        return False
                elif op == "$regex":
                    if actual is None or not re.search(operand, str(actual)):
                        return False
                else:  # pragma: no cover - unsupported operator in a test
                    raise NotImplementedError(op)
        elif actual != expected:
            return False
    return True


def _apply(doc: dict, update: dict) -> None:
    for op, fields in update.items():
        if op == "$set":
            doc.update(fields)
        elif op == "$inc":
            for field, delta in fields.items():
                doc[field] = (doc.get(field) or 0) + delta
        elif op == "$max":
            for field, value in fields.items():
                current = doc.get(field)
                if current is None or value > current:
                    doc[field] = value
        else:  # pragma: no cover
            raise NotImplementedError(op)


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

    async def to_list(self, length=None):
        return self._docs if length is None else self._docs[:length]


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = list(docs or [])
        self.queries: list[dict] = []

    async def insert_one(self, doc):
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, flt=None, projection=None, *args, **kwargs):
        for doc in self.docs:
            if _match(doc, flt or {}):
                return dict(doc)
        return None

    async def update_one(self, flt, update, upsert=False, **kwargs):
        for doc in self.docs:
            if _match(doc, flt):
                _apply(doc, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            doc = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            _apply(doc, update)
            self.docs.append(doc)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=doc.get("_id"))
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def find_one_and_update(self, flt, update, sort=None, return_document=None, **kwargs):
        candidates = [d for d in self.docs if _match(d, flt)]
        if sort:
            field, direction = sort[0]
            candidates.sort(key=lambda d: d.get(field) or 0, reverse=direction < 0)
        if not candidates:
            return None
        doc = candidates[0]
        _apply(doc, update)
        return dict(doc)

    def find(self, flt=None, *args, **kwargs):
        self.queries.append(dict(flt or {}))
        return _FakeCursor([dict(d) for d in self.docs if _match(d, flt or {})])


class FakeDB:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, FakeCollection())


def _patch_db(db):
    return patch("aria.db.mongodb.get_database", new=AsyncMock(return_value=db))


class _ExplodingClient:
    """Any attempt to open a real connection from a test is a bug."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("break-glass must not open a connection here")


@pytest.fixture(autouse=True)
def _no_real_signal():
    """corsair's .env carries a live SIGNAL_BREAKGLASS_ACCOUNT/RECIPIENT and the
    signal-cli daemon really is listening on :8090, so an unpatched break-glass
    path in a test sends Ben an actual Signal message (it did, once, while this
    file was being written). Every test in this module runs with the transport
    nailed shut; the ones that exercise the client patch it themselves. The
    patch swaps the module reference INSIDE signal_rpc — patching
    signal_rpc.httpx.AsyncClient would mutate the shared httpx module and blow
    up every unrelated client in the process (it did that too)."""
    with patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_ExplodingClient)):
        yield


# ---------------------------------------------------------------------------
# Classification — the drop filter is gone
# ---------------------------------------------------------------------------

class TestClassification:
    def test_session_stall_is_info_not_human(self):
        kind, severity, needs_human = classify("coding:abc123", "stalled:idle")
        assert (kind, severity, needs_human) == ("stall", "info", False)

    def test_progress_notices_are_info(self):
        for event in ("loop:ended", "loop:nudge", "budget:warn", "budget:soft_gate", "stopped"):
            _, severity, needs_human = classify("coding:abc123", event)
            assert (severity, needs_human) == ("info", False), event

    def test_bad_terminal_outcomes_are_visible_but_never_needs_human(self):
        """A session that FAILED, was killed by the budget guard or ran out its
        deadline used to land in the same silent info bucket as "completed", so
        the headline claim of Alerts v2 was false for the cases that matter.
        Severity carries it now; needs_human still must not, or the triage cron
        starts spawning fixers for lifecycle events again."""
        for event in ("error", "failed", "deadline", "budget:hard_gate", "crashed"):
            _, severity, needs_human = classify("coding:abc123", event)
            assert severity == "high", event
            assert needs_human is False, event

    def test_completion_is_not_a_failure(self):
        # "completed" contains no failure marker and must stay in the quiet lane
        # even though it is terminal.
        for event in ("completed", "complete", "resolved"):
            _, severity, needs_human = classify("coding:abc123", event)
            assert (severity, needs_human) == ("info", False), event

    def test_gate_failure_is_not_lifecycle(self):
        kind, severity, needs_human = classify("coding:gate", "gate:failed")
        assert kind == "gate" and severity == "high" and needs_human is True

    def test_unknown_source_defaults_to_raising(self):
        # A caller that forgets to classify must fail toward Ben's attention,
        # never away from it.
        _, severity, needs_human = classify("some-new-worker", "weird_thing")
        assert severity == "medium" and needs_human is True

    def test_recovery_is_informational(self):
        _, severity, needs_human = classify("selfcheck", "recovered")
        assert severity == "info" and needs_human is False


@pytest.mark.asyncio
async def test_stalled_coding_session_now_enqueues():
    """The regression this whole file exists for: coding:* stall/deadline/
    budget/loop events were dropped, so a stuck agent could never surface."""
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        res = await svc.notify(
            source="coding:sess-1", event_type="stalled:idle", detail="no output for 5m"
        )
    assert res["queued"] is True
    doc = db.alerts.docs[0]
    assert doc["severity"] == "info"
    assert doc["needs_human"] is False
    assert doc["kind"] == "stall"
    assert doc["dedup_key"] == "coding:sess-1|stalled:idle"
    assert doc["occurrences"] == 1
    assert doc["delivered_at"] is None
    assert doc["decision"] is None


@pytest.mark.asyncio
async def test_mail_echo_still_dropped():
    """Classification closes the triage feedback loop for session lifecycle;
    orchestrator mail echoes stay dropped because the mailbox already holds
    them verbatim (see notifications/service.py _is_mail_echo)."""
    db = FakeDB()
    with _patch_db(db):
        res = await NotificationService().notify(
            source="agents", event_type="agent_task_done", detail="x"
        )
    assert res == {"queued": False, "reason": "informational"}
    assert db.alerts.docs == []


def _seed_projects(db) -> None:
    """db.projects as it actually is on corsair: a coarse harvested row for
    ~/Development, and the hand-created `aria` row that claims ProjectAria
    through relevant_paths (shells/harvest.py merges into it rather than minting
    a `ProjectAria` twin)."""
    db.projects.docs.extend(
        [
            {
                "_id": "PP",
                "slug": "development",
                "path": "/home/ben/Development",
                "relevant_paths": [],
            },
            {
                "_id": "PA",
                "slug": "aria",
                "path": None,
                "relevant_paths": ["/home/ben/Development/ProjectAria"],
            },
        ]
    )


@pytest.mark.asyncio
async def test_caller_supplied_classification_wins():
    db = FakeDB()
    _seed_projects(db)
    with _patch_db(db):
        await NotificationService().notify(
            source="guard",
            event_type="blocked",
            detail="rm -rf refused",
            severity="critical",
            kind="guard",
            needs_human=True,
            dedup_key="guard|blocked|sess-9",
            project_path="/home/ben/Development/ProjectAria",
        )
    doc = db.alerts.docs[0]
    assert doc["severity"] == "critical"
    assert doc["kind"] == "guard"
    assert doc["dedup_key"] == "guard|blocked|sess-9"
    # NOT the basename: this row's project is `aria` in db.projects, which is
    # the slug the cockpit queries with.
    assert doc["project_slug"] == "aria"


@pytest.mark.asyncio
async def test_project_slug_is_resolved_not_guessed_from_the_basename():
    """`GET /alerts?project=aria` returned 0 rows because every ProjectAria
    alert was filed as `ProjectAria` — a slug no db.projects row has. Most
    specific root wins, so the coarse ~/Development row must not claim it."""
    db = FakeDB()
    _seed_projects(db)
    with _patch_db(db):
        await NotificationService().notify(
            source="coding:gate",
            event_type="gate:failed",
            detail="check failed 3x",
            project_path="/home/ben/Development/ProjectAria/api",
        )
        await NotificationService().notify(
            source="selfcheck",
            event_type="degraded",
            detail="x",
            project_path="/home/ben/Development/some-other-repo",
        )
    assert db.alerts.docs[0]["project_slug"] == "aria"
    assert db.alerts.docs[1]["project_slug"] == "development"


@pytest.mark.asyncio
async def test_project_slug_falls_back_to_basename_when_unclaimed():
    """A workspace no project row claims yet (harvest will mint this same slug)
    — and the same answer when db.projects is unreachable."""
    db = FakeDB()
    with _patch_db(db):
        await NotificationService().notify(
            source="coding:s1",
            event_type="error",
            detail="x",
            project_path="/home/ben/Development/aria-projects/scratch-1/",
        )
    assert db.alerts.docs[0]["project_slug"] == "scratch-1"


# ---------------------------------------------------------------------------
# Dedup + cooldown persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeat_increments_occurrences_instead_of_duplicating():
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        first = await svc.notify(
            source="selfcheck", event_type="degraded", detail="llm down", cooldown_seconds=0
        )
        second = await svc.notify(
            source="selfcheck", event_type="degraded", detail="llm still down", cooldown_seconds=0
        )
    assert first["queued"] is True and not first.get("deduped")
    assert second["deduped"] is True and second["occurrences"] == 2
    assert len(db.alerts.docs) == 1
    assert db.alerts.docs[0]["detail"] == "llm still down"


@pytest.mark.asyncio
async def test_dedup_escalates_severity():
    """The break-glass mute. `POST /api/v1/notifications/send
    {"source":"relay","event_type":"dead"}` needs only the global API key and
    pre-creates the `relay|dead` row at whatever classify() defaults to (high
    here); the RelayWatchdog's real critical then deduped INTO it, and
    `?severity=critical` — the query the break-glass path runs — returned
    nothing. The first occurrence must not cap the row."""
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        planted = await svc.notify(
            source="relay", event_type="dead", detail="x", cooldown_seconds=0
        )
        assert db.alerts.docs[0]["severity"] == "high"

        real = await svc.notify(
            source="relay",
            event_type="dead",
            detail="no heartbeat for 45m",
            severity="critical",
            kind="relay",
            needs_human=True,
            dedup_key="relay|dead",
            cooldown_seconds=0,
        )
    assert real["deduped"] is True
    assert real["alert_id"] == planted["alert_id"]
    assert len(db.alerts.docs) == 1
    row = db.alerts.docs[0]
    assert row["severity"] == "critical"
    assert row["needs_human"] is True
    assert real["severity"] == "critical"


@pytest.mark.asyncio
async def test_dedup_never_downgrades_severity():
    """Severity ranks explicitly because BSON would not: sorted as strings,
    "critical" < "info" < "low" < "medium", so a $max would let any later repeat
    walk a critical row back down."""
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        await svc.notify(
            source="relay", event_type="dead", detail="dead",
            severity="critical", needs_human=True, cooldown_seconds=0,
        )
        for lower in ("info", "low", "medium", "high"):
            await svc.notify(
                source="relay", event_type="dead", detail="still dead",
                severity=lower, cooldown_seconds=0,
            )
            assert db.alerts.docs[0]["severity"] == "critical", lower
    assert db.alerts.docs[0]["needs_human"] is True


@pytest.mark.asyncio
async def test_dedup_freezes_kind_by_design():
    """kind is the row's ROUTING identity — the break-glass allow-list, the
    triage selectors and the digest grouping are all keyed on it. Escalating
    severity under a consumer is fine; moving the row to another lane is not.
    A genuinely different kind of event carries its own dedup_key."""
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        await svc.notify(
            source="relay", event_type="dead", detail="x", kind="relay", cooldown_seconds=0
        )
        await svc.notify(
            source="relay", event_type="dead", detail="x",
            kind="estop", severity="critical", cooldown_seconds=0,
        )
    assert db.alerts.docs[0]["kind"] == "relay"
    assert db.alerts.docs[0]["severity"] == "critical"


@pytest.mark.asyncio
async def test_acked_alert_starts_a_new_row():
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        await svc.notify(source="selfcheck", event_type="degraded", detail="a", cooldown_seconds=0)
        db.alerts.docs[0]["acked"] = True
        await svc.notify(source="selfcheck", event_type="degraded", detail="b", cooldown_seconds=0)
    assert len(db.alerts.docs) == 2


@pytest.mark.asyncio
async def test_cooldown_survives_a_restart():
    """A new NotificationService instance (= a restarted aria-api) must see the
    cooldown written by the previous process. In-memory only, 37 restarts since
    08-11 produced 31 duplicate `selfcheck degraded` rows."""
    db = FakeDB()
    with _patch_db(db):
        first = await NotificationService().notify(
            source="selfcheck", event_type="degraded", detail="llm down", cooldown_seconds=3600
        )
        assert first["queued"] is True
        assert db.alert_cooldowns.docs[0]["_id"] == "selfcheck|degraded"

        # Simulate the restart: brand-new instance, empty in-memory dict, and
        # the previous row acked so dedup cannot mask the cooldown.
        db.alerts.docs[0]["acked"] = True
        restarted = NotificationService()
        assert restarted._cooldowns == {}
        again = await restarted.notify(
            source="selfcheck", event_type="degraded", detail="llm down", cooldown_seconds=3600
        )
    assert again == {"queued": False, "reason": "cooldown"}
    assert len(db.alerts.docs) == 1


@pytest.mark.asyncio
async def test_expired_persisted_cooldown_allows_send():
    db = FakeDB()
    db.alert_cooldowns.docs.append(
        {
            "_id": "selfcheck|degraded",
            "last_sent_at": datetime.now(timezone.utc) - timedelta(hours=2),
        }
    )
    with _patch_db(db):
        res = await NotificationService().notify(
            source="selfcheck", event_type="degraded", detail="x", cooldown_seconds=600
        )
    assert res["queued"] is True


@pytest.mark.asyncio
async def test_future_cooldown_stamp_does_not_silence_the_alert_class(caplog):
    """Mongo is bound 0.0.0.0 with no auth (CLAUDE.md S4), so one document —
    writable by any coding agent on the tailnet — used to mute an alert class
    permanently and across restarts: `now - last_sent >= cooldown` can never
    become true when last_sent is in the future."""
    db = FakeDB()
    db.alert_cooldowns.docs.append(
        {
            "_id": "selfcheck|degraded",
            "last_sent_at": datetime.now(timezone.utc) + timedelta(hours=4),
        }
    )
    with _patch_db(db), caplog.at_level(logging.WARNING, logger="aria.notifications.service"):
        res = await NotificationService().notify(
            source="selfcheck", event_type="degraded", detail="llm down", cooldown_seconds=600
        )
    assert res["queued"] is True
    assert any("FUTURE" in r.getMessage() for r in caplog.records)
    # And the poisoned stamp is overwritten on the way out, so it self-heals.
    assert db.alert_cooldowns.docs[0]["last_sent_at"] <= datetime.now(timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(lambda now: now + timedelta(days=365), id="future"),
        pytest.param(lambda now: now - timedelta(days=400), id="ancient"),
        pytest.param(lambda now: "2026-08-15T12:00:00", id="not-a-datetime"),
    ],
)
async def test_implausible_cooldown_is_loud_not_debug(caplog, stored):
    """Bad *data* has to be as visible as a bad read. A silently-ignored stamp
    is indistinguishable from a working cooldown, which is how "this alert
    stopped firing" goes unnoticed for days."""
    db = FakeDB()
    db.alert_cooldowns.docs.append(
        {"_id": "selfcheck|degraded", "last_sent_at": stored(datetime.now(timezone.utc))}
    )
    with _patch_db(db), caplog.at_level(logging.WARNING, logger="aria.notifications.service"):
        res = await NotificationService().notify(
            source="selfcheck", event_type="degraded", detail="x", cooldown_seconds=600
        )
    assert res["queued"] is True
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and "selfcheck" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_poisoned_in_memory_cooldown_is_dropped():
    """The cache is not a trusted source either — _load_cooldown is not the only
    writer of that dict."""
    svc = NotificationService()
    svc._cooldowns[("selfcheck", "degraded")] = datetime.now(timezone.utc) + timedelta(hours=4)
    db = FakeDB()
    with _patch_db(db):
        res = await svc.notify(
            source="selfcheck", event_type="degraded", detail="x", cooldown_seconds=600
        )
    assert res["queued"] is True


@pytest.mark.asyncio
async def test_cooldown_store_failure_falls_back_to_memory():
    """The cooldown store is a convenience path: if it is unreadable the alert
    goes through (fail open), and the in-process dict still dampens repeats."""
    db = FakeDB()

    async def _boom(*args, **kwargs):
        raise RuntimeError("no cooldown collection")

    db.alert_cooldowns.find_one = _boom  # type: ignore[assignment]
    db.alert_cooldowns.update_one = _boom  # type: ignore[assignment]
    svc = NotificationService()
    with _patch_db(db):
        first = await svc.notify(
            source="selfcheck", event_type="degraded", detail="x", cooldown_seconds=3600
        )
        db.alerts.docs[0]["acked"] = True
        second = await svc.notify(
            source="selfcheck", event_type="degraded", detail="x", cooldown_seconds=3600
        )
    assert first["queued"] is True
    assert second == {"queued": False, "reason": "cooldown"}


# ---------------------------------------------------------------------------
# Bounded life for the info lane (C4 regression)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_info_rows_expire_and_attention_rows_do_not():
    """Nothing acks a session lifecycle row and nothing delivers it, so without
    an expiry they accumulate forever — inflating every project's cockpit
    attention score and eventually filling its 300-row read with noise."""
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        await svc.notify(
            source="coding:s1", event_type="stalled:idle", detail="idle", cooldown_seconds=0
        )
        await svc.notify(
            source="selfcheck", event_type="degraded", detail="llm down", cooldown_seconds=0
        )
    info, real = db.alerts.docs
    assert info["severity"] == "info"
    assert info["expires_at"] is not None
    assert info["expires_at"] > datetime.now(timezone.utc)
    # Anything that needs a human is out of the TTL index's reach entirely.
    assert real["expires_at"] is None


@pytest.mark.asyncio
async def test_escalated_row_loses_its_expiry():
    """A row that has become real must not evaporate under the TTL sweep."""
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        await svc.notify(
            source="coding:s1", event_type="stalled:idle", detail="idle", cooldown_seconds=0
        )
        assert db.alerts.docs[0]["expires_at"] is not None
        await svc.notify(
            source="coding:s1", event_type="stalled:idle", detail="wedged",
            severity="high", cooldown_seconds=0,
        )
    assert db.alerts.docs[0]["severity"] == "high"
    assert db.alerts.docs[0]["expires_at"] is None


@pytest.mark.asyncio
async def test_recurring_info_row_expiry_tracks_the_last_occurrence():
    db = FakeDB()
    svc = NotificationService()
    with _patch_db(db):
        await svc.notify(
            source="coding:s1", event_type="stalled:idle", detail="idle", cooldown_seconds=0
        )
        first = db.alerts.docs[0]["expires_at"]
        db.alerts.docs[0]["expires_at"] = first - timedelta(days=6)
        await svc.notify(
            source="coding:s1", event_type="stalled:idle", detail="idle", cooldown_seconds=0
        )
    assert db.alerts.docs[0]["expires_at"] > first - timedelta(days=6)


# ---------------------------------------------------------------------------
# Failed sessions: visible, and structurally unable to loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_session_enqueues_a_visible_row():
    db = FakeDB()
    with _patch_db(db):
        res = await NotificationService().notify(
            source="coding:sess-7", event_type="error", detail="Session exited with code 1"
        )
    assert res["queued"] is True
    doc = db.alerts.docs[0]
    assert doc["severity"] == "high"
    assert doc["needs_human"] is False
    # No expiry: a failed session is not disposable record-keeping.
    assert doc["expires_at"] is None


@pytest.mark.asyncio
async def test_no_session_lifecycle_event_can_re_enter_the_triage_lane():
    """The 31-row incident: the triage cron selected an alert, spawned a fixer
    agent, the fixer's own lifecycle became an alert, and triage selected that
    too. Every consumer that ACTS (relay, triage, break-glass) selects
    needs_human=true; no coding:<session_id> event of any severity sets it, so
    a fixer that fails, blows its budget and times out re-triggers nothing."""
    db = FakeDB()
    svc = NotificationService()
    events = [
        "stopped", "completed", "error", "failed", "crashed", "deadline",
        "budget:hard_gate", "budget:warn", "stalled:idle", "loop:ended", "loop:nudge",
    ]
    with _patch_db(db):
        for i, event in enumerate(events):
            await svc.notify(
                source=f"coding:fixer-{i}", event_type=event, detail="x", cooldown_seconds=0
            )
    assert len(db.alerts.docs) == len(events)
    assert all(doc["needs_human"] is False for doc in db.alerts.docs)
    # The relay's own selector, verbatim (notifications/relay.py _pending_alerts).
    assert [d for d in db.alerts.docs if _match(d, {"needs_human": True, "acked": False})] == []


@pytest.mark.asyncio
async def test_db_down_never_raises_into_caller():
    with patch("aria.db.mongodb.get_database", new=AsyncMock(side_effect=RuntimeError("no db"))):
        res = await NotificationService().notify(
            source="selfcheck", event_type="degraded", detail="x"
        )
    assert res["queued"] is False and res["reason"] == "enqueue_failed"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
async def alerts_client():
    from aria.main import app
    from aria.api import deps

    db = FakeDB()
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
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            ac.db = db  # type: ignore[attr-defined]
            yield ac
    app.dependency_overrides.clear()
    app.state.relay_watchdog = None


def _alert_doc(**over) -> dict:
    doc = {
        "_id": ObjectId(),
        "source": "selfcheck",
        "event_type": "degraded",
        "detail": "llm down",
        "message": "[selfcheck] DEGRADED: llm down",
        "acked": False,
        "created_at": datetime.now(timezone.utc),
        "acked_at": None,
        "project_path": None,
        "project_slug": None,
        "severity": "high",
        "kind": "selfcheck",
        "needs_human": True,
        "dedup_key": "selfcheck|degraded",
        "occurrences": 1,
        "delivered_at": None,
        "proposal": None,
        "decision": None,
    }
    doc.update(over)
    return doc


@pytest.mark.asyncio
async def test_list_filters_needs_human_and_undelivered(alerts_client):
    keep = _alert_doc()
    alerts_client.db.alerts.docs.extend(
        [
            keep,
            _alert_doc(needs_human=False, severity="info", kind="stall"),
            _alert_doc(delivered_at=datetime.now(timezone.utc)),
        ]
    )
    resp = await alerts_client.get("/api/v1/alerts?needs_human=true&undelivered=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["alerts"][0]["id"] == str(keep["_id"])


@pytest.mark.asyncio
async def test_list_filters_by_project_slug_or_path(alerts_client):
    by_slug = _alert_doc(project_slug="ProjectAria")
    by_path = _alert_doc(project_path="/home/ben/Development/ProjectAria")
    alerts_client.db.alerts.docs.extend([by_slug, by_path, _alert_doc(project_slug="Hermes")])
    resp = await alerts_client.get("/api/v1/alerts?project=ProjectAria")
    ids = {a["id"] for a in resp.json()["alerts"]}
    assert ids == {str(by_slug["_id"]), str(by_path["_id"])}


@pytest.mark.asyncio
async def test_list_by_slug_finds_rows_filed_under_the_old_basename(alerts_client):
    """History: every ProjectAria alert written before the slug resolver landed
    carries project_slug="ProjectAria", and the cockpit asks for `aria`. Nothing
    can re-emit those rows, so the reader matches the project's real roots."""
    _seed_projects(alerts_client.db)
    legacy_slug = _alert_doc(project_slug="ProjectAria")
    by_path = _alert_doc(project_path="/home/ben/Development/ProjectAria/api")
    current = _alert_doc(project_slug="aria")
    alerts_client.db.alerts.docs.extend(
        [legacy_slug, by_path, current, _alert_doc(project_slug="hermes")]
    )
    resp = await alerts_client.get("/api/v1/alerts?project=aria")
    ids = {a["id"] for a in resp.json()["alerts"]}
    assert ids == {str(legacy_slug["_id"]), str(by_path["_id"]), str(current["_id"])}


@pytest.mark.asyncio
async def test_list_pre_v2_rows_are_not_needs_human(alerts_client):
    legacy = {
        "_id": ObjectId(),
        "source": "selfcheck",
        "event_type": "degraded",
        "detail": "old row",
        "acked": False,
        "created_at": datetime.now(timezone.utc),
    }
    alerts_client.db.alerts.docs.append(legacy)
    assert (await alerts_client.get("/api/v1/alerts?needs_human=true")).json()["count"] == 0
    assert (await alerts_client.get("/api/v1/alerts?needs_human=false")).json()["count"] == 1


@pytest.mark.asyncio
async def test_decide_apply_records_decision_and_acks(alerts_client):
    doc = _alert_doc()
    alerts_client.db.alerts.docs.append(doc)
    resp = await alerts_client.post(
        f"/api/v1/alerts/{doc['_id']}/decide",
        json={"action": "apply", "by": "ben", "note": "go ahead"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "APPLY"
    assert body["alert"]["decision"]["value"] == "APPLY"
    assert body["alert"]["decision"]["by"] == "ben"
    stored = alerts_client.db.alerts.docs[0]
    assert stored["acked"] is True
    assert stored["needs_human"] is False
    assert "false_raise" not in stored


@pytest.mark.asyncio
async def test_decide_ignore_marks_false_raise(alerts_client):
    doc = _alert_doc()
    alerts_client.db.alerts.docs.append(doc)
    watchdog = MagicMock()
    watchdog.refresh_inbox = AsyncMock(return_value="/vault/STEWARD_INBOX.md")
    alerts_client._transport.app.state.relay_watchdog = watchdog
    resp = await alerts_client.post(
        f"/api/v1/alerts/{doc['_id']}/decide", json={"action": "IGNORE", "by": "ben"}
    )
    assert resp.status_code == 200
    assert alerts_client.db.alerts.docs[0]["false_raise"] is True
    watchdog.refresh_inbox.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_ack_refreshes_steward_inbox(alerts_client):
    doc = _alert_doc()
    alerts_client.db.alerts.docs.append(doc)
    watchdog = MagicMock()
    watchdog.refresh_inbox = AsyncMock(return_value="/vault/STEWARD_INBOX.md")
    alerts_client._transport.app.state.relay_watchdog = watchdog
    resp = await alerts_client.post(f"/api/v1/alerts/{doc['_id']}/ack")
    assert resp.status_code == 200
    watchdog.refresh_inbox.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_decide_rejects_unknown_action(alerts_client):
    doc = _alert_doc()
    alerts_client.db.alerts.docs.append(doc)
    resp = await alerts_client.post(
        f"/api/v1/alerts/{doc['_id']}/decide", json={"action": "MAYBE"}
    )
    assert resp.status_code == 400
    assert alerts_client.db.alerts.docs[0]["decision"] is None


@pytest.mark.asyncio
async def test_decide_unknown_alert_404(alerts_client):
    resp = await alerts_client.post(
        f"/api/v1/alerts/{ObjectId()}/decide", json={"action": "HOLD"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delivered_sets_delivered_at_without_acking(alerts_client):
    doc = _alert_doc()
    alerts_client.db.alerts.docs.append(doc)
    resp = await alerts_client.post(
        f"/api/v1/alerts/{doc['_id']}/delivered", json={"by": "hermes-outbox"}
    )
    assert resp.status_code == 200
    stored = alerts_client.db.alerts.docs[0]
    assert stored["delivered_at"] is not None
    assert stored["delivered_by"] == "hermes-outbox"
    assert stored["acked"] is False


@pytest.mark.asyncio
async def test_relay_heartbeat_records_state(alerts_client):
    resp = await alerts_client.post("/api/v1/alerts/relay-heartbeat", json={"source": "hermes"})
    assert resp.status_code == 200
    state = alerts_client.db.app_state.docs[0]
    assert state["_id"] == RELAY_STATE_ID
    assert state["source"] == "hermes"
    assert state["heartbeat_count"] == 1
    assert state["last_heartbeat_at"] is not None


# ---------------------------------------------------------------------------
# Relay watchdog
# ---------------------------------------------------------------------------

def _watchdog(db, tmp_path, clock, notifier=None):
    return RelayWatchdog(
        db,
        notifier or _notifier(),
        interval_seconds=60,
        timeout_minutes=20,
        now=clock,
    )


def _notifier():
    n = MagicMock()
    n.notify = AsyncMock(return_value={"queued": True})
    return n


@pytest.mark.asyncio
async def test_no_death_before_first_heartbeat(tmp_path):
    """A box where the relay cron was never installed must not page. There is no
    heartbeat row at all here, and an alert is pending."""
    db = FakeDB()
    db.alerts.docs.append(_alert_doc())
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    wd = _watchdog(db, tmp_path, lambda: now)
    with patch.object(settings, "obsidian_vault_path", str(tmp_path)):
        result = await wd.evaluate_once()
    assert result["reason"] == "no_heartbeat_yet"
    assert result["dead"] is False
    wd.notifier.notify.assert_not_awaited()
    assert not (tmp_path / "ProjectAria" / "Planning" / "STEWARD_INBOX.md").exists()


@pytest.mark.asyncio
async def test_fresh_heartbeat_is_alive(tmp_path):
    db = FakeDB()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    db.app_state.docs.append(
        {"_id": RELAY_STATE_ID, "last_heartbeat_at": now - timedelta(minutes=3)}
    )
    wd = _watchdog(db, tmp_path, lambda: now)
    with patch.object(settings, "obsidian_vault_path", str(tmp_path)):
        result = await wd.evaluate_once()
    assert result["reason"] == "alive" and result["dead"] is False
    wd.notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_relay_alerts_and_writes_inbox(tmp_path):
    db = FakeDB()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    db.app_state.docs.append(
        {
            "_id": RELAY_STATE_ID,
            "last_heartbeat_at": now - timedelta(minutes=45),
            "heartbeat_count": 12,
        }
    )
    db.alerts.docs.append(
        _alert_doc(detail="pi session wedged", kind="stall", severity="high", occurrences=3)
    )
    wd = _watchdog(db, tmp_path, lambda: now)
    send = AsyncMock(return_value={"sent": False, "reason": "unconfigured"})
    with (
        patch.object(settings, "obsidian_vault_path", str(tmp_path)),
        patch.object(signal_rpc, "send_breakglass", send),
    ):
        result = await wd.evaluate_once()

    assert result["dead"] is True and result["pending"] == 1
    kwargs = wd.notifier.notify.await_args.kwargs
    assert kwargs["source"] == "relay"
    assert kwargs["event_type"] == "dead"
    assert kwargs["severity"] == "critical"
    assert kwargs["needs_human"] is True
    assert kwargs["dedup_key"] == "relay|dead"

    inbox = tmp_path / "ProjectAria" / "Planning" / "STEWARD_INBOX.md"
    assert inbox.exists()
    text = inbox.read_text(encoding="utf-8")
    assert "Signal relay is not delivering" in text
    assert "pi session wedged" in text
    assert "×3" in text
    assert str(db.alerts.docs[0]["_id"]) in text
    # dead_since is persisted so a later heartbeat can be recognised as recovery
    assert db.app_state.docs[0]["dead_since"] == now
    # One break-glass attempt, tagged with the fully-qualified allow-list key.
    assert send.await_args.kwargs["kind"] == "relay:dead"
    assert result["breakglass"]["sent"] is False


@pytest.mark.asyncio
async def test_dead_relay_breakglass_is_rate_limited(tmp_path):
    db = FakeDB()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    db.app_state.docs.append(
        {
            "_id": RELAY_STATE_ID,
            "last_heartbeat_at": now - timedelta(minutes=45),
            "last_breakglass_at": now - timedelta(minutes=5),
        }
    )
    wd = _watchdog(db, tmp_path, lambda: now)
    send = AsyncMock(return_value={"sent": True, "reason": "ok"})
    with (
        patch.object(settings, "obsidian_vault_path", str(tmp_path)),
        patch.object(signal_rpc, "send_breakglass", send),
    ):
        result = await wd.evaluate_once()
    assert result["breakglass"] == {"sent": False, "reason": "rate_limited"}
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_relay_sends_one_breakglass_when_window_open(tmp_path):
    db = FakeDB()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    db.app_state.docs.append(
        {
            "_id": RELAY_STATE_ID,
            "last_heartbeat_at": now - timedelta(minutes=45),
            "last_breakglass_at": now - timedelta(hours=3),
        }
    )
    wd = _watchdog(db, tmp_path, lambda: now)
    send = AsyncMock(return_value={"sent": True, "reason": "ok"})
    with (
        patch.object(settings, "obsidian_vault_path", str(tmp_path)),
        patch.object(signal_rpc, "send_breakglass", send),
    ):
        await wd.evaluate_once()
    assert send.await_args.kwargs["kind"] == "relay:dead"
    assert db.app_state.docs[0]["last_breakglass_at"] == now


@pytest.mark.asyncio
async def test_heartbeat_after_death_raises_recovery(tmp_path):
    db = FakeDB()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    db.app_state.docs.append(
        {
            "_id": RELAY_STATE_ID,
            "last_heartbeat_at": now - timedelta(minutes=90),
            "dead_since": now - timedelta(minutes=70),
        }
    )
    wd = _watchdog(db, tmp_path, lambda: now)
    inbox = tmp_path / "ProjectAria" / "Planning" / "STEWARD_INBOX.md"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        "> [!warning] Signal relay is not delivering\n",
        encoding="utf-8",
    )
    with patch.object(settings, "obsidian_vault_path", str(tmp_path)):
        state = await wd.record_heartbeat("hermes")
    assert state["recovered"] is True
    kwargs = wd.notifier.notify.await_args.kwargs
    assert kwargs["event_type"] == "recovered"
    assert kwargs["severity"] == "info"
    assert kwargs["needs_human"] is False
    assert db.app_state.docs[0]["dead_since"] is None
    assert wd.status()["dead"] is False
    refreshed = inbox.read_text(encoding="utf-8")
    assert "Signal relay is not delivering" not in refreshed
    assert "No alerts are waiting on a human." in refreshed


@pytest.mark.asyncio
async def test_heartbeat_without_prior_death_is_quiet(tmp_path):
    db = FakeDB()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    wd = _watchdog(db, tmp_path, lambda: now)
    await wd.record_heartbeat("hermes")
    wd.notifier.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_refresh_preserves_active_relay_warning(tmp_path):
    db = FakeDB()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    db.app_state.docs.append(
        {
            "_id": RELAY_STATE_ID,
            "last_heartbeat_at": now - timedelta(minutes=45),
            "dead_since": now - timedelta(minutes=20),
        }
    )
    db.alerts.docs.append(_alert_doc(detail="still waiting"))
    wd = _watchdog(db, tmp_path, lambda: now)
    with patch.object(settings, "obsidian_vault_path", str(tmp_path)):
        await wd.refresh_inbox()
    text = (tmp_path / "ProjectAria" / "Planning" / "STEWARD_INBOX.md").read_text()
    assert "Signal relay is not delivering" in text
    assert "still waiting" in text


@pytest.mark.asyncio
async def test_inbox_write_failure_does_not_break_the_tick(tmp_path):
    """The vault is a fallback; a fallback that can fail the caller is not one."""
    db = FakeDB()
    now = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    db.app_state.docs.append(
        {"_id": RELAY_STATE_ID, "last_heartbeat_at": now - timedelta(minutes=45)}
    )
    wd = _watchdog(db, tmp_path, lambda: now)
    # A file where the ProjectAria directory should be: mkdir will fail.
    (tmp_path / "ProjectAria").write_text("not a directory", encoding="utf-8")
    with patch.object(settings, "obsidian_vault_path", str(tmp_path)):
        result = await wd.evaluate_once()
    assert result["dead"] is True
    assert result["inbox_path"] is None


# ---------------------------------------------------------------------------
# Break-glass client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breakglass_refuses_unlisted_kind():
    with (
        patch.object(settings, "alert_breakglass_enabled", True),
        patch.object(settings, "alert_breakglass_kinds", ["relay:dead", "estop"]),
        patch.object(settings, "signal_breakglass_account", "+15550001111"),
        patch.object(settings, "signal_breakglass_recipient", "+15550002222"),
        patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_ExplodingClient)),
    ):
        res = await signal_rpc.send_breakglass("stuck agent", kind="stall")
    assert res["sent"] is False
    assert res["reason"] == "kind_not_allowed:stall"


@pytest.mark.asyncio
async def test_breakglass_refuses_when_unconfigured():
    with (
        patch.object(settings, "alert_breakglass_enabled", True),
        patch.object(settings, "signal_breakglass_account", ""),
        patch.object(settings, "signal_breakglass_recipient", ""),
        patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_ExplodingClient)),
    ):
        res = await signal_rpc.send_breakglass("relay dead", kind="relay:dead")
    assert res == {"sent": False, "reason": "unconfigured"}


@pytest.mark.asyncio
async def test_breakglass_refuses_when_disabled():
    with (
        patch.object(settings, "alert_breakglass_enabled", False),
        patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_ExplodingClient)),
    ):
        res = await signal_rpc.send_breakglass("relay dead", kind="relay:dead")
    assert res == {"sent": False, "reason": "disabled"}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the JSON-RPC envelope; the wire format is the contract with the
    signal-cli daemon (verified live on :8090) and is worth pinning."""

    calls: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")
        self.response = _FakeResponse({"jsonrpc": "2.0", "result": {"timestamp": 1}, "id": 1})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        _FakeClient.calls.append((url, json))
        return self.response


@pytest.mark.asyncio
async def test_breakglass_sends_jsonrpc_envelope():
    _FakeClient.calls.clear()
    with (
        patch.object(settings, "alert_breakglass_enabled", True),
        patch.object(settings, "alert_breakglass_kinds", ["relay:dead"]),
        patch.object(settings, "signal_breakglass_account", "+15550001111"),
        patch.object(settings, "signal_breakglass_recipient", "+15550002222"),
        patch.object(settings, "signal_cli_rpc_url", "http://127.0.0.1:8090/api/v1/rpc"),
        patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_FakeClient)),
    ):
        res = await signal_rpc.send_breakglass("relay dead", kind="relay:dead")
    assert res["sent"] is True
    url, payload = _FakeClient.calls[0]
    assert url == "http://127.0.0.1:8090/api/v1/rpc"
    assert payload["jsonrpc"] == "2.0" and payload["method"] == "send"
    assert payload["params"]["account"] == "+15550001111"
    assert payload["params"]["recipient"] == ["+15550002222"]
    assert payload["params"]["message"] == "relay dead"


@pytest.mark.asyncio
async def test_breakglass_treats_rpc_error_as_not_sent():
    """signal-cli answers HTTP 200 with a JSON-RPC error object for an
    unregistered account — status alone is not delivery evidence."""

    class _ErrClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.response = _FakeResponse({"jsonrpc": "2.0", "error": {"code": -32601}})

    with (
        patch.object(settings, "alert_breakglass_enabled", True),
        patch.object(settings, "alert_breakglass_kinds", ["relay:dead"]),
        patch.object(settings, "signal_breakglass_account", "+15550001111"),
        patch.object(settings, "signal_breakglass_recipient", "+15550002222"),
        patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_ErrClient)),
    ):
        res = await signal_rpc.send_breakglass("relay dead", kind="relay:dead")
    assert res["sent"] is False and res["reason"] == "rpc_error"


@pytest.mark.asyncio
async def test_breakglass_transport_error_never_raises():
    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            raise OSError("connection refused")

    with (
        patch.object(settings, "alert_breakglass_enabled", True),
        patch.object(settings, "alert_breakglass_kinds", ["relay:dead"]),
        patch.object(settings, "signal_breakglass_account", "+15550001111"),
        patch.object(settings, "signal_breakglass_recipient", "+15550002222"),
        patch.object(signal_rpc, "httpx", SimpleNamespace(AsyncClient=_Boom)),
    ):
        res = await signal_rpc.send_breakglass("relay dead", kind="relay:dead")
    assert res["sent"] is False and res["reason"] == "transport_error"
