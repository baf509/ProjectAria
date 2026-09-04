"""Durable Hermes approval records stored in the gateway state database.

The blocked tool thread is still the execution authority. This table makes the
human control request addressable and auditable across session transitions and
records restart/expiry as explicit final outcomes; it never auto-approves work.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Optional


def _default_path() -> Path:
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return home / "state.db"


class ApprovalStore:
    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else _default_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _ensure(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_approvals ("
                "operation_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, "
                "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, "
                "risk_class TEXT NOT NULL, summary TEXT NOT NULL, "
                "detail_json TEXT NOT NULL, state TEXT NOT NULL, "
                "decision TEXT, decided_at TEXT)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS pending_approvals_session_state "
                "ON pending_approvals(session_key,state,created_at)"
            )

    def create(
        self,
        session_key: str,
        detail: dict[str, Any],
        *,
        ttl_seconds: float,
        operation_id: Optional[str] = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        op_id = operation_id or secrets.token_hex(4)
        expires = datetime.fromtimestamp(now.timestamp() + max(ttl_seconds, 0), timezone.utc)
        summary = str(detail.get("description") or "dangerous operation")[:500]
        risk = str(detail.get("risk_class") or detail.get("severity") or "manual")[:80]
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending_approvals "
                "(operation_id,session_key,created_at,expires_at,risk_class,summary,detail_json,state) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    op_id,
                    session_key,
                    now.isoformat(),
                    expires.isoformat(),
                    risk,
                    summary,
                    json.dumps(detail, default=str, separators=(",", ":")),
                    "pending",
                ),
            )
        return op_id

    def finalize(self, operation_id: str, state: str, decision: Optional[str] = None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE pending_approvals SET state=?, decision=?, decided_at=? "
                "WHERE operation_id=? AND state='pending'",
                (state, decision, datetime.now(timezone.utc).isoformat(), operation_id),
            )

    def pending(self, session_key: str) -> list[dict[str, Any]]:
        self.expire()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM pending_approvals WHERE session_key=? AND state='pending' "
                "ORDER BY created_at",
                (session_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def expire(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                "UPDATE pending_approvals SET state='expired', decision='timeout', decided_at=? "
                "WHERE state='pending' AND expires_at<=?",
                (now, now),
            )
            return cursor.rowcount

    def finalize_orphans(self, live_operation_ids: set[str]) -> int:
        """Safely finalize requests whose execution waiter did not survive restart."""
        self.expire()
        with closing(self._connect()) as conn, conn:
            if live_operation_ids:
                marks = ",".join("?" for _ in live_operation_ids)
                cursor = conn.execute(
                    f"UPDATE pending_approvals SET state='interrupted', decision='gateway_restart', "
                    f"decided_at=? WHERE state='pending' AND operation_id NOT IN ({marks})",
                    (datetime.now(timezone.utc).isoformat(), *sorted(live_operation_ids)),
                )
            else:
                cursor = conn.execute(
                    "UPDATE pending_approvals SET state='interrupted', decision='gateway_restart', "
                    "decided_at=? WHERE state='pending'",
                    (datetime.now(timezone.utc).isoformat(),),
                )
            return cursor.rowcount
