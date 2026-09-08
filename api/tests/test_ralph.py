"""Real Git + real verifier processes, deterministic workers, isolated fake Mongo.

TrustedProcessRuntime is TEST ONLY: fixture programs are trusted, and it makes
no sandbox claim. Production containment has separate container integration tests.
"""
import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from mongomock_motor import AsyncMongoMockClient

from aria.llm.base import ToolCall
from aria.ralph.config import RalphSettings, load_project
from aria.ralph.git import GitWorkspace, OwnershipError, TargetLock, git
from aria.ralph.models import CreateRun, Plan, TaskSpec
from aria.ralph.runtime import process
from aria.ralph.service import RalphService
from aria.ralph.worker import AgentWorker


class TrustedProcessRuntime:
    def __init__(self):
        self.containers, self.stopped, self.calls = {}, [], []

    async def preflight(self, image):
        pass

    async def start(self, name, workspace, policy, **kwargs):
        self.containers[name] = (workspace, kwargs.get("assets"))

    async def execute(self, name, argv, timeout):
        workspace, assets = self.containers[name]
        argv = [str(a).replace("/workspace", str(workspace)).replace("/checks", str(assets)) for a in argv]
        self.calls.append((name, argv))
        code, output = await process(argv, timeout=timeout, env={"PATH": "/usr/bin:/bin", "HOME": str(workspace)})
        return {"exit_code": code, "output": output.decode(errors="replace")}

    async def stop(self, name):
        self.containers.pop(name, None)
        self.stopped.append(name)


class FakeWorker:
    def __init__(self, values=(1, 2), callback=None):
        self.values, self.callback, self.calls = list(values), callback, []

    async def run(self, **kw):
        self.calls.append({k: copy.deepcopy(kw[k]) for k in ("session_id", "task", "handoff")})
        await kw["meter"]("reserve", None)
        await kw["meter"]("usage", {"total_tokens": 10})
        if self.callback:
            result = await self.callback(kw)
            if result is not None:
                return result
        value = self.values[min(len(self.calls) - 1, len(self.values) - 1)]
        await kw["execute"]([sys.executable, "-c", f"from pathlib import Path; Path('/workspace/answer.txt').write_text('{value}')"])
        return {"outcome": "ready", "changed": "Updated answer", "checks": "Agent says done", "handoff": "Inspect answer.txt and repair the value."}


def task(task_id="fix", **kwargs):
    return {"id": task_id, "description": "Make answer equal two", "specification_ref": "spec#answer",
            "acceptance_criteria": ["answer.txt contains 2"], "dependencies": [],
            "allowed_paths": ["answer.txt"], "out_of_scope": ["No changes to other files"],
            "check_ids": ["answer"], **kwargs}


@pytest.fixture
async def fixture(tmp_path):
    repo, assets = tmp_path / "repo", tmp_path / "trusted"
    repo.mkdir()
    assets.mkdir()
    (repo / "answer.txt").write_text("0")
    (repo / "AGENTS.md").write_text("Inspect before changing code.")
    await git("init", repo)
    await git("-C", repo, "add", ".")
    await git("-C", repo, "commit", "-m", "fixture baseline")
    (assets / "check.py").write_text("from pathlib import Path\nassert Path(__import__('sys').argv[1]).read_text() == '2', 'answer must be two'\n")
    policy = {"projects": {"fixture": {
        "repository": str(repo), "assets": str(assets), "image": "sha256:" + "a" * 64,
        "checks": {"answer": {"argv": [sys.executable, "/checks/check.py", "/workspace/answer.txt"], "version": "1"}},
        "regression_check_ids": ["answer"], "final_check_ids": ["answer"],
    }}}
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy))
    settings = RalphSettings(enabled=True, policy_file=str(policy_path), state_dir=str(tmp_path / "state"))
    db = AsyncMongoMockClient(tz_aware=True).ralph_test
    runtime, worker = TrustedProcessRuntime(), FakeWorker()
    service = RalphService(db, settings=settings, runtime=runtime, worker=worker)
    await service.store.initialize()
    return service, repo, policy_path


async def create(service, *, tasks=None, limits=None):
    run = await service.create(CreateRun(project="fixture", specification="The answer must be two.",
                                         plan={"version": 1, "tasks": tasks or [task()]}, limits=limits or {}))
    return await service.approve(run["_id"], run["version"])


async def finish(service, run_id, action="start"):
    await service.control(run_id, action)
    job = service.jobs[run_id]
    await job
    await asyncio.sleep(0)
    return await service.inspect(run_id)


async def test_failed_candidate_repaired_real_git_real_process_verification(fixture):
    service, repo, _ = fixture
    run = await finish(service, (await create(service))["_id"])
    assert run["state"] == "ready_for_review", run["stop_reason"]
    assert [a["outcome"] for a in run["attempts"]] == ["verification_failed", "accepted", "accepted"]
    assert len({c["session_id"] for c in service.worker.calls}) == 2
    assert all(c["task"]["id"] == "fix" for c in service.worker.calls)
    assert "answer must be two" in service.worker.calls[1]["handoff"]
    assert (repo / "answer.txt").read_text() == "0"
    assert run["tasks"][0]["accepted_commit"] == run["final_revision"]
    assert run["attempts"][1]["checks"][0]["candidate_revision"] == run["final_revision"]
    content = await git("--git-dir", run["checkpoint_repository"], "show", run["final_revision"] + ":answer.txt")
    assert content == b"2"
    assert run["usage"]["attempts"] == 2 and run["usage"]["reported_tokens"] == 20
    assert run["metrics"]["verification_failures"] == 1
    assert not service.runtime.containers
    assert await service.db.ralph_logs.count_documents({"kind": "verification"}) == 3


async def test_dependency_order_and_only_selected_task(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([2])
    run = await create(service, tasks=[task("second", dependencies=["first"]), task("first")])
    result = await finish(service, run["_id"])
    assert result["state"] == "ready_for_review"
    assert [c["task"]["id"] for c in service.worker.calls] == ["first", "second"]
    assert "tasks" not in service.worker.calls[0]["task"]


async def test_magic_done_and_unchanged_candidate_cannot_pass(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([1])
    run = await finish(service, (await create(service))["_id"])
    assert run["state"] == "blocked", run["stop_reason"]
    assert "unchanged_candidate" in run["stop_reason"]
    assert all(t["state"] != "verified" for t in run["tasks"])
    assert sum("-v-" in name for name, _ in service.runtime.calls) == 1
    assert Path(run["tasks"][0]["workspace"]).joinpath("answer.txt").read_text() == "1"


@pytest.mark.parametrize("path", ["AGENTS.md", "unrelated.txt", ".env", ".git/config"])
async def test_scope_protected_files_and_git_metadata_block(fixture, path):
    service, _, _ = fixture
    async def alter(kw):
        await kw["execute"]([sys.executable, "-c", f"from pathlib import Path; p=Path('/workspace/{path}'); p.parent.mkdir(exist_ok=True); p.write_text('tampered')"])
    service.worker = FakeWorker([2], alter)
    run = await finish(service, (await create(service, limits={"attempts_per_task": 1}))["_id"])
    assert run["state"] == "blocked", run["stop_reason"]
    assert not any(t["state"] == "verified" for t in run["tasks"])


async def test_worker_cannot_write_authoritative_state_in_report(fixture):
    service, _, _ = fixture
    async def lie(kw):
        return {"outcome": "ready", "changed": "", "checks": "", "handoff": "", "state": "verified"}
    service.worker = FakeWorker([2], lie)
    run = await finish(service, (await create(service, limits={"attempts_per_task": 1}))["_id"])
    assert run["state"] == "blocked"
    assert run["attempts"][0]["outcome"] == "execution_failed"


async def test_failed_task_never_contaminates_unrelated_task(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([1])
    run = await finish(service, (await create(service, tasks=[task("bad"), task("unrelated")], limits={"attempts_per_task": 1}))["_id"])
    assert run["state"] == "blocked"
    assert [c["task"]["id"] for c in service.worker.calls] == ["bad"]
    assert run["tasks"][1]["workspace"] is None


async def test_attempt_budget_exhaustion_is_not_success(fixture):
    service, _, _ = fixture
    run = await finish(service, (await create(service, limits={"attempts": 1}))["_id"])
    assert run["state"] == "budget_exhausted" and run["stop_reason"] == "total_attempt_limit"
    assert len(service.worker.calls) == 1


async def test_pause_finishes_attempt_then_resume_fresh_session_cumulative_budget(fixture):
    service, _, _ = fixture
    async def pause(kw):
        if len(service.worker.calls) == 1:
            await service.control(run["_id"], "pause")
    service.worker = FakeWorker([1, 2], pause)
    run = await create(service)
    result = await finish(service, run["_id"])
    assert result["state"] == "paused" and result["usage"]["attempts"] == 1
    result = await finish(service, run["_id"], "resume")
    assert result["state"] == "ready_for_review"
    assert result["usage"]["attempts"] == 2
    assert len({c["session_id"] for c in service.worker.calls}) == 2


async def test_cancel_active_worker_stops_without_acceptance(fixture):
    service, _, _ = fixture
    entered = asyncio.Event()
    async def wait(kw):
        entered.set()
        await asyncio.Event().wait()
    service.worker = FakeWorker([2], wait)
    run = await create(service)
    await service.control(run["_id"], "start")
    job = service.jobs[run["_id"]]
    await asyncio.wait_for(entered.wait(), 30)
    await service.control(run["_id"], "cancel")
    await asyncio.wait_for(job, 5)
    result = await service.inspect(run["_id"])
    assert result["state"] == "cancelled"
    assert not service.runtime.containers
    assert result["accepted_revision"] == result["starting_revision"]


async def test_final_integration_failure_is_not_success(fixture):
    service, _, policy_path = fixture
    policy = json.loads(policy_path.read_text())
    policy["projects"]["fixture"]["checks"]["integration"] = {"argv": [sys.executable, "-c", "raise SystemExit(1)"], "version": "1"}
    policy["projects"]["fixture"]["final_check_ids"] = ["integration"]
    policy_path.write_text(json.dumps(policy))
    service.worker = FakeWorker([2])
    run = await finish(service, (await create(service))["_id"])
    assert run["tasks"][0]["state"] == "verified"
    assert run["state"] == "blocked" and run["stop_reason"] == "final_integration_failed"
    assert run["final_revision"] is None


async def test_competing_runs_and_stale_controller_fencing(fixture):
    service, _, _ = fixture
    first, second = await create(service), await create(service)
    lock = TargetLock(service.root / "locks", first["target"]).acquire()
    try:
        with pytest.raises(OwnershipError):
            await service.control(second["_id"], "start")
    finally:
        lock.release()
    stale = await service.store.get(first["_id"])
    await service.db.ralph_runs.update_one({"_id": first["_id"]}, {"$set": {"owner": "replacement"}})
    stale["state"] = "ready_for_review"
    with pytest.raises(OwnershipError):
        await service.store.save(stale, acceptance=True)
    assert (await service.store.get(first["_id"]))["state"] == "approved"


async def test_cancel_wins_atomic_acceptance_race(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([2])
    original = service.store.event
    async def race(run, kind, detail, **kwargs):
        if kind == "accepted":
            await service.control(run["_id"], "cancel")
        return await original(run, kind, detail, **kwargs)
    service.store.event = race
    result = await finish(service, (await create(service))["_id"])
    assert result["accepted_revision"] == result["starting_revision"]
    assert result["tasks"][0]["state"] != "verified"


class Crash(BaseException):
    pass


@pytest.mark.parametrize("after", [False, True])
async def test_restart_around_acceptance_is_idempotent(fixture, after):
    service, _, _ = fixture
    service.worker = FakeWorker([2])
    original = service.store.event
    async def crash(run, kind, detail, **kwargs):
        if kind == "accepted" and not after:
            raise Crash()
        result = await original(run, kind, detail, **kwargs)
        if kind == "accepted" and after:
            raise Crash()
        return result
    service.store.event = crash
    run = await create(service)
    with pytest.raises(Crash):
        await finish(service, run["_id"])
    await asyncio.sleep(0)
    replacement = RalphService(service.db, settings=service.settings, runtime=service.runtime, worker=FakeWorker([2]))
    await replacement.recover(run["_id"])
    recovered = await replacement.recover(run["_id"])
    assert recovered["state"] == "paused"
    assert recovered["tasks"][0]["state"] == ("verified" if after else "pending")
    assert recovered["usage"]["attempts"] == 1
    result = await finish(replacement, run["_id"], "resume")
    assert result["state"] == ("ready_for_review" if after else "blocked")
    assert sum(a["outcome"] == "accepted" and a["task_id"] == "fix" for a in result["attempts"]) == (1 if after else 0)


async def test_policy_change_requires_new_approval(fixture):
    service, _, policy_path = fixture
    run = await create(service)
    policy = json.loads(policy_path.read_text())
    policy["projects"]["fixture"]["checks"]["answer"]["argv"] = ["true"]
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(Exception, match="invalid_configuration"):
        await service.control(run["_id"], "start")


def test_plan_validation_dependencies_and_paths():
    for tasks in ([task("a", dependencies=["b"])], [task("a", dependencies=["a"])], [task("a"), task("a")]):
        with pytest.raises(ValueError):
            Plan(tasks=tasks)
    for path in ("../secret", "/etc", ".git/config", "src/../../secret"):
        with pytest.raises(ValueError):
            TaskSpec(**task(allowed_paths=[path]))


async def test_unregistered_workspace_and_admin_authorization(fixture, monkeypatch):
    from aria.api.routes.ralph import router, get_ralph
    from aria.config import settings
    service, _, _ = fixture
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_ralph] = lambda: service
    monkeypatch.setattr(settings, "admin_key", "operator-only-test-key")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        request = {"project": "fixture", "specification": "Fix answer", "plan": {"tasks": [task()]}}
        response = await client.post("/ralph/runs", json=request)
        assert response.status_code == 403
        response = await client.post("/ralph/runs", json={**request, "project": "unregistered"}, headers={"X-Admin-Key": settings.admin_key})
        assert response.status_code == 400
        response = await client.post("/ralph/runs", json=request, headers={"X-Admin-Key": settings.admin_key})
        assert response.status_code == 201
        run = response.json()
        response = await client.post(f"/ralph/runs/{run['_id']}/approve", json={"expected_version": run["version"] + 1}, headers={"X-Admin-Key": settings.admin_key})
        assert response.status_code == 409


async def test_real_inner_loop_keeps_context_and_starts_fresh(fixture):
    service, _, _ = fixture
    histories = []
    class Provider:
        async def complete(self, messages, **kwargs):
            histories.append(copy.deepcopy(messages))
            if len(messages) == 2:
                return "Inspect and implement", [ToolCall("c1", "shell", {"command": "printf 2 > /workspace/answer.txt"})], {"total_tokens": 10}
            return json.dumps({"outcome": "ready", "changed": "answer", "checks": "none", "handoff": ""}), [], {}
    service.worker = AgentWorker(lambda *_: Provider())
    run = await finish(service, (await create(service, tasks=[task("first"), task("second", dependencies=["first"])]))["_id"])
    assert run["state"] == "ready_for_review", run["stop_reason"]
    assert [len(h) for h in histories] == [2, 4, 2, 4]
    assert json.loads(histories[0][1].content)["session_id"] != json.loads(histories[2][1].content)["session_id"]
    assert run["metrics"]["tokens"] is None


async def test_unknown_tokens_stop_when_token_budget_configured(fixture):
    service, _, _ = fixture
    class Provider:
        async def complete(self, *args, **kwargs):
            return "{}", [], {}
    service.worker = AgentWorker(lambda *_: Provider())
    run = await finish(service, (await create(service, limits={"tokens": 1000}))["_id"])
    assert run["state"] == "budget_exhausted" and "accounting_unknown" in run["stop_reason"]
    assert run["usage"]["unknown_calls"] == 1


async def test_human_acceptance_is_explicit(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([2])
    run = await finish(service, (await create(service, tasks=[task(human_review=["Review visual clarity"])]))["_id"])
    assert run["state"] == "human_review_required" and run["human_review"]


async def test_planner_inspects_repository_and_requires_approval(fixture):
    service, repo, _ = fixture
    class Planner:
        async def complete(self, messages, **kwargs):
            if len(messages) == 2:
                return "Inspect", [ToolCall("read", "read_file", {"path": "answer.txt"})], {"total_tokens": 1}
            return json.dumps({"version": 1, "tasks": [task()]}), [], {"total_tokens": 1}
    service.worker = AgentWorker(lambda *_: Planner())
    run = await service.create(CreateRun(project="fixture", specification="Answer should be two"))
    result = await finish(service, run["_id"], "plan")
    assert result["state"] == "draft" and result["approved_at"] is None
    assert (repo / "answer.txt").read_text() == "0"
    with pytest.raises(ValueError):
        await service.control(run["_id"], "start")


async def test_real_process_timeout_kills_descendant(tmp_path):
    import psutil
    pidfile = tmp_path / "pid"
    script = f"import subprocess,time; p=subprocess.Popen(['sleep','60']); open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(60)"
    with pytest.raises(TimeoutError):
        await process([sys.executable, "-c", script], timeout=0.5)
    pid = int(pidfile.read_text())
    await asyncio.sleep(0.1)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
