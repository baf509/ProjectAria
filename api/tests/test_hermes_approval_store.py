from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sqlite3


_MODULE_PATH = Path(__file__).parents[2] / "integrations" / "hermes" / "approval_store.py"
_SPEC = importlib.util.spec_from_file_location("aria_hermes_approval_store", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
ApprovalStore = _MODULE.ApprovalStore


def test_approval_store_persists_addressable_records(tmp_path):
    path = tmp_path / "state.db"
    store = ApprovalStore(path)
    operation_id = store.create(
        "signal:ben",
        {"description": "remove a shell", "command": "aria shells rm demo"},
        ttl_seconds=300,
        operation_id="a1b2c3d4",
    )

    reopened = ApprovalStore(path)
    pending = reopened.pending("signal:ben")
    assert operation_id == "a1b2c3d4"
    assert pending[0]["operation_id"] == operation_id
    assert pending[0]["summary"] == "remove a shell"

    reopened.finalize(operation_id, "approved", "once")
    assert reopened.pending("signal:ben") == []


def test_approval_store_expires_and_finalizes_restart_orphans(tmp_path):
    path = tmp_path / "state.db"
    store = ApprovalStore(path)
    store.create("s", {"description": "old"}, ttl_seconds=300, operation_id="old")
    store.create("s", {"description": "live"}, ttl_seconds=300, operation_id="live")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE pending_approvals SET expires_at=? WHERE operation_id='old'",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )

    assert store.expire() == 1
    assert store.finalize_orphans({"live"}) == 0
    assert [row["operation_id"] for row in store.pending("s")] == ["live"]
    assert store.finalize_orphans(set()) == 1
    assert store.pending("s") == []
