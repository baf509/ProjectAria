"""Route tests for the Guard's operator surface.

`test_guard.py` never imported this module or built a client, and that gap is
exactly where the two worst defects of the first review lived:

  - `GET /guard/sessions/{id}/merge-gate` took a `check_command` QUERY
    PARAMETER and handed it to `create_subprocess_shell` as ben with aria-api's
    whole environment, echoing 1500 bytes of output back — an unauthenticated
    remote shell that returned ADMIN_KEY to `?check_command=env`;
  - `require_admin` referenced `settings` without importing it, so the merge
    route raised NameError → 500 instead of 403. A unit test of `GitGuard`
    cannot see either one.

So these tests exercise the ASGI app: the admin gate on the two irreversible
routes, and a structural assertion that no guard route accepts a caller-supplied
command in any form.

The app is a bare FastAPI with only this router mounted — deliberately not
`aria.main.app`, which would drag in the whole service and its middleware for
questions about six routes. The global X-API-Key middleware lives in main.py and
is tested there; what is unique to the guard is the ADMIN_KEY split.
"""

from __future__ import annotations

import os
import typing

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from aria.api import deps
from aria.api.routes import guard as guard_routes
from aria.config import settings
from aria.guard import policy as guard_policy

ADMIN_KEY = "test-admin-key"


@pytest.fixture(autouse=True)
def guard_state_isolation(tmp_path, monkeypatch):
    """No test may write the production acceptance record — a wrong value there
    e-stops aria-api at boot. See test_guard.py's module docstring."""
    monkeypatch.setenv(
        guard_policy.GUARD_STATE_ENV, str(tmp_path / "guard-state" / "accepted.json")
    )


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def count_documents(self, query):
        return len([d for d in self.docs if all(d.get(k) == v for k, v in query.items())])

    async def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return None

    async def insert_one(self, doc):
        self.docs.append(doc)
        return type("R", (), {"inserted_id": len(self.docs)})()

    async def update_one(self, query, update, upsert=False):
        existing = await self.find_one(query)
        if existing is None:
            if not upsert:
                return None
            existing = dict(query)
            self.docs.append(existing)
        existing.update(update.get("$set", {}))
        return type("R", (), {"modified_count": 1})()

    def find(self, query=None):
        rows = [d for d in self.docs
                if all(d.get(k) == v for k, v in (query or {}).items())]

        class Cursor:
            def sort(self, *_a, **_k):
                return self

            def limit(self, *_a, **_k):
                return self

            async def to_list(self, length=None):
                return rows

        return Cursor()


class FakeDB:
    def __init__(self):
        self._collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._collections.setdefault(name, FakeCollection())

    def __getattr__(self, name):
        return self[name]


class FakeGuard:
    """Records what the routes asked of the git protocol."""

    def __init__(self):
        self.mirror_root = "/tmp/git-safe"
        self.calls: list[tuple] = []

    async def checkpoint(self, session_id, reason="manual"):
        self.calls.append(("checkpoint", session_id, reason))
        return {"ok": True, "committed": True, "sha": "abc123", "session_id": session_id}

    async def rollback(self, session_id, to="start"):
        self.calls.append(("rollback", session_id, to))
        return {"ok": True, "session_id": session_id, "target": to}

    async def merge_gate(self, session_id, **kwargs):
        self.calls.append(("merge_gate", session_id, kwargs))
        return {"session_id": session_id, "passed": True, "checks": []}

    async def merge(self, session_id, squash=True, actor="api"):
        self.calls.append(("merge", session_id, squash, actor))
        return {"ok": True, "merged": True, "sha": "def456", "session_id": session_id}


@pytest.fixture
async def client(monkeypatch):
    fake_guard = FakeGuard()
    db = FakeDB()
    monkeypatch.setattr(settings, "admin_key", ADMIN_KEY)
    monkeypatch.setattr(guard_routes, "get_git_guard", lambda _db=None: fake_guard)

    app = FastAPI()
    app.include_router(guard_routes.router, prefix="/api/v1")
    app.dependency_overrides[deps.get_db] = lambda: db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        ac.guard = fake_guard          # type: ignore[attr-defined]
        ac.db = db                     # type: ignore[attr-defined]
        yield ac


# ---------------------------------------------------------------------------
# The admin split
# ---------------------------------------------------------------------------

class TestAdminGate:
    async def test_merge_requires_the_admin_key(self, client):
        """API_KEY is readable by anything running as ben — including a coding
        agent — so it cannot be what authorises an irreversible merge."""
        assert (await client.post("/api/v1/guard/sessions/s1/merge")).status_code == 403
        assert (await client.post(
            "/api/v1/guard/sessions/s1/merge", headers={"X-Admin-Key": "wrong"}
        )).status_code == 403
        assert ("merge", "s1", True, "admin-api") not in client.guard.calls

        ok = await client.post(
            "/api/v1/guard/sessions/s1/merge", headers={"X-Admin-Key": ADMIN_KEY}
        )
        assert ok.status_code == 200 and ok.json()["merged"] is True

    async def test_policy_accept_requires_the_admin_key(self, client):
        current = guard_policy.policy_hash()
        assert (await client.post(
            "/api/v1/guard/policy/accept", json={"hash": current}
        )).status_code == 403

        ok = await client.post(
            "/api/v1/guard/policy/accept", json={"hash": current},
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert ok.status_code == 200
        assert ok.json()["hash"] == current

    async def test_accept_refuses_a_hash_that_is_not_the_enforced_one(self, client):
        """Accepting "whatever is on disk now" would let a caller who never read
        the file wave through someone else's edit."""
        response = await client.post(
            "/api/v1/guard/policy/accept", json={"hash": "0" * 64},
            headers={"X-Admin-Key": ADMIN_KEY},
        )
        assert response.status_code == 409
        assert "hash mismatch" in response.json()["detail"]

    async def test_an_unset_admin_key_refuses_rather_than_falling_back(
        self, client, monkeypatch
    ):
        """Fails CLOSED: silently accepting the global key when ADMIN_KEY is
        unconfigured would make the whole split cosmetic."""
        monkeypatch.setattr(settings, "admin_key", "")
        response = await client.post(
            "/api/v1/guard/sessions/s1/merge", headers={"X-Admin-Key": "anything"}
        )
        assert response.status_code == 503
        assert "ADMIN_KEY is not configured" in response.json()["detail"]

    async def test_read_and_checkpoint_routes_do_not_need_the_admin_key(self, client):
        """The admin key is for irreversible actions only — gating a checkpoint
        behind it would mean a session's work stops being captured whenever the
        key is not to hand, which is the opposite of a safety net."""
        assert (await client.get("/api/v1/guard/status")).status_code == 200
        assert (await client.get("/api/v1/guard/events")).status_code == 200
        assert (await client.get("/api/v1/guard/checkpoints")).status_code == 200
        assert (await client.post("/api/v1/guard/sessions/s1/checkpoint")).status_code == 200
        assert (await client.get("/api/v1/guard/sessions/s1/merge-gate")).status_code == 200


# ---------------------------------------------------------------------------
# No caller-supplied commands, anywhere
# ---------------------------------------------------------------------------

_COMMAND_ISH = ("command", "cmd", "shell", "script", "exec", "argv", "args")


def _field_names(route) -> list[str]:
    names: list[str] = []
    for name, param in typing.get_type_hints(route.endpoint, include_extras=True).items():
        names.append(name)
        if isinstance(param, type) and issubclass(param, BaseModel):
            names += list(param.model_fields)
    return names


class TestNoCallerSuppliedCommands:
    def test_no_route_accepts_a_command(self):
        """The gate's command must come from the project/config, never from the
        caller: `?check_command=env` was a remote shell that returned ADMIN_KEY
        to anything which could reach the MCP surface. The thing being verified
        must not choose the verification — so this asserts on the SHAPE of every
        route, not on one route's behaviour.
        """
        offenders = []
        for route in guard_routes.router.routes:
            for name in _field_names(route):
                if any(token in name.lower() for token in _COMMAND_ISH):
                    offenders.append(f"{route.path}:{name}")
        assert offenders == []

    async def test_the_gate_route_ignores_a_check_command_query_parameter(self, client):
        """Belt and braces against the exact URL that leaked the key: FastAPI
        ignores unknown query parameters, so what matters is that nothing
        reaches merge_gate()."""
        response = await client.get(
            "/api/v1/guard/sessions/s1/merge-gate",
            params={"check_command": "echo $ADMIN_KEY"},
        )
        assert response.status_code == 200
        assert client.guard.calls == [("merge_gate", "s1", {})]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStatus:
    async def test_status_reports_the_policy_verdict_and_preflight(self, client):
        body = (await client.get("/api/v1/guard/status")).json()
        assert body["policy"]["hash"] == guard_policy.policy_hash()
        assert body["policy"]["verification"]["current_hash"] == body["policy"]["hash"]
        assert "spawn_allowed" in body["preflight"]
        # The tighten-only report is part of the operator surface: a policy file
        # asking to widen the sandbox must be visible without reading logs.
        assert body["policy"]["rejected"] == []

    async def test_status_survives_a_dead_mongo(self, client, monkeypatch):
        """Convenience path, fails OPEN: the cockpit must still render, and the
        tamper verdict itself is computed elsewhere (and fails closed)."""
        class BrokenDB(FakeDB):
            def __getitem__(self, name):
                raise RuntimeError("mongo is down")

        app = FastAPI()
        app.include_router(guard_routes.router, prefix="/api/v1")
        app.dependency_overrides[deps.get_db] = lambda: BrokenDB()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            body = (await ac.get("/api/v1/guard/status")).json()
        assert "error" in body["counts"]

    async def test_a_tampered_policy_status_names_the_blessed_remedy(
        self, client, tmp_path, monkeypatch
    ):
        """An operator who cannot find the sanctioned command deletes the state
        file instead — which IS the re-arm attack."""
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(policy_file))
        await client.get("/api/v1/guard/status")           # trust on first use
        policy_file.write_text("protected_paths:\n  - docs/**\n  - evil/**\n")

        verification = (await client.get("/api/v1/guard/status")).json()["policy"]["verification"]
        assert verification["ok"] is False and verification["status"] == "tamper"
        assert "/api/v1/guard/policy/accept" in verification["remedy"]
        assert "never by deleting the acceptance record" in verification["remedy"]


class TestRouteWiring:
    def test_the_router_is_mounted_on_the_real_app(self):
        """A router nobody includes is a safety surface that does not exist."""
        from aria.main import app

        # Current FastAPI keeps included routers lazy as `_IncludedRouter`
        # entries with no `.path`; OpenAPI expansion is the stable public view
        # of the routes the real application actually exposes.
        paths = set(app.openapi()["paths"])
        assert "/api/v1/guard/status" in paths
        assert "/api/v1/guard/sessions/{session_id}/merge" in paths

    def test_the_irreversible_routes_carry_require_admin(self):
        """Structural, so moving `require_admin` to deps.py (as the module
        docstring plans) cannot silently drop it from a route."""
        admin_routes = {
            route.path
            for route in guard_routes.router.routes
            if any(
                getattr(d.dependency, "__name__", "") == "require_admin"
                for d in getattr(route, "dependencies", [])
            )
        }
        assert "/guard/sessions/{session_id}/merge" in admin_routes
        assert "/guard/policy/accept" in admin_routes

    def test_no_dead_imports(self):
        """`hmac` and `Header` were imported and unused after the query-parameter
        shell was removed — dead imports in a security-relevant module make it
        harder to see what the module actually depends on."""
        source = open(os.path.join(os.path.dirname(guard_routes.__file__), "guard.py")).read()
        for name in ("hmac", "Header"):
            assert f"import {name}" not in source and f", {name}" not in source
