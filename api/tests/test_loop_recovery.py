"""Controller failure and trust-boundary regressions."""
import asyncio
import json
from pathlib import Path

import pytest

from aria.loop.config import load_project
from aria.loop.git import GitWorkspace, OwnershipError, git
from aria.loop.models import CreateRun
from aria.loop.runtime import InfrastructureError
from aria.loop.service import LoopService
from aria.loop.worker import AgentWorker, ContextLimit
from tests.test_loop import fixture, create, finish, task, FakeWorker


def stop_once(exc, transcript):
    """Log one model turn, then stop the first attempt the way a real limit does."""
    stopped = []

    async def callback(kw):
        if stopped:
            return None
        stopped.append(True)
        await kw["log"]("model", {"session_id": kw["session_id"], "content": transcript})
        raise exc

    return callback


async def test_export_ignore_cannot_hide_accepted_files(fixture):
    service, repo, _ = fixture
    (repo / ".gitattributes").write_text("answer.txt export-ignore\n")
    await git("-C", repo, "add", ".")
    await git("-C", repo, "commit", "-m", "attribute trap")
    service.worker = FakeWorker([2])
    run = await finish(service, (await create(service))["_id"])
    assert run["state"] == "ready_for_review", run["stop_reason"]
    candidate = next((service.root / run["_id"]).glob("verify-*/answer.txt"))
    assert candidate.read_text() == "2"
    ref = (await git("--git-dir", run["checkpoint_repository"], "rev-parse", run["checkpoint_ref"])).decode().strip()
    assert ref == run["final_revision"]


async def test_reservation_cannot_be_bypassed_with_other_state_root(fixture):
    service, _, _ = fixture
    first = await create(service)
    await service.store.reserve_target(first, service.root, "occupied")
    settings = service.settings.model_copy(update={"state_dir": str(service.root / "different")})
    competing = LoopService(service.db, settings=settings, runtime=service.runtime)
    second = await create(service)
    with pytest.raises(OwnershipError):
        await competing.control(second["_id"], "start")
    # Crash after reservation but before claiming the run is recoverable.
    recovered = await service.recover(first["_id"])
    assert recovered["state"] == "paused"
    assert (await service.db.loop_targets.find_one({"_id": first["target"]}))["owner"] is None


async def test_run_deadline_survives_resume(fixture):
    service, _, _ = fixture
    run = await create(service)
    await service.db.loop_runs.update_one({"_id": run["_id"]}, {"$set": {
        "state": "paused", "started_at": run["created_at"], "deadline": 1,
    }})
    result = await finish(service, run["_id"], "resume")
    assert result["state"] == "budget_exhausted" and result["stop_reason"] == "overall_wall_time"
    assert not service.worker.calls


async def test_worker_wall_time_has_finite_retry(fixture):
    service, _, _ = fixture
    async def hang(kw):
        await asyncio.Event().wait()
    service.worker = FakeWorker([2], hang)
    result = await finish(service, (await create(service, limits={"attempt_seconds": 1, "attempts_per_task": 1}))["_id"])
    assert result["state"] == "blocked"
    assert result["attempts"][0]["execution_outcome"] == "TimeoutError"
    assert not service.runtime.containers


async def test_global_estop_stops_before_worker(fixture):
    service, _, _ = fixture
    await service.db.estop.insert_one({"_id": "global", "active": True})
    result = await finish(service, (await create(service))["_id"])
    assert result["state"] == "paused" and result["stop_reason"] == "global_emergency_stop"
    assert not service.worker.calls


async def test_provider_failure_is_distinct_and_not_retried(fixture):
    service, _, _ = fixture
    calls = []
    class BrokenProvider:
        async def complete(self, *args, **kwargs):
            calls.append(1)
            raise ConnectionError("model connection lost")
    service.worker = AgentWorker(lambda *_: BrokenProvider())
    result = await finish(service, (await create(service))["_id"])
    assert result["state"] == "failed" and result["stop_reason"].startswith("model_execution_failure")
    assert calls == [1] and result["usage"]["unknown_calls"] == 1


async def test_failed_container_termination_keeps_repository_fenced(fixture):
    service, _, _ = fixture
    original = service.runtime.stop
    async def failed_stop(name):
        raise InfrastructureError("termination unavailable")
    service.runtime.stop = failed_stop
    run = await create(service)
    result = await finish(service, run["_id"])
    assert result["state"] == "failed"
    assert result["accepted_revision"] == result["starting_revision"]
    second = await create(service)
    with pytest.raises(OwnershipError):
        await service.control(second["_id"], "start")
    service.runtime.stop = original
    recovered = await service.recover(run["_id"])
    assert recovered["state"] == "paused"
    assert not service.runtime.containers


async def test_interrupted_final_verification_is_not_rerolled(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([2])
    original = service._verify
    async def interrupted(run, attempt, *args, **kwargs):
        if kwargs.get("final"):
            await service.store.save(run)
            raise asyncio.CancelledError()
        return await original(run, attempt, *args, **kwargs)
    service._verify = interrupted
    run = await create(service)
    result = await finish(service, run["_id"])
    assert result["state"] == "paused"
    assert result["tasks"][0]["state"] == "verified"
    service._verify = original
    result = await finish(service, run["_id"], "resume")
    assert result["state"] == "blocked" and result["stop_reason"].startswith("final_verification_uncertain")


async def test_cancel_during_verification_rejects_candidate(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([2])
    original = service.runtime.execute
    entered = asyncio.Event()
    async def execution(name, argv, timeout):
        if "-v-" in name:
            entered.set()
            await asyncio.Event().wait()
        return await original(name, argv, timeout)
    service.runtime.execute = execution
    run = await create(service)
    await service.control(run["_id"], "start")
    job = service.jobs[run["_id"]]
    # Repository setup can be slow while a production UI build is running;
    # this is a startup barrier, independent of the verification timeout.
    waiter = asyncio.create_task(entered.wait())
    try:
        await asyncio.wait({waiter, job}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        assert entered.is_set(), (await service.inspect(run["_id"]))["stop_reason"]
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
    await service.control(run["_id"], "cancel")
    await asyncio.wait_for(job, 5)
    result = await service.inspect(run["_id"])
    assert result["state"] == "cancelled" and result["tasks"][0]["state"] != "verified"
    assert not service.runtime.containers


async def test_malicious_planner_has_no_write_tool(fixture):
    from aria.llm.base import ToolCall
    service, _, _ = fixture
    class Planner:
        async def complete(self, *args, **kwargs):
            return "implement", [ToolCall("bad", "shell", {"command": "rm -rf /workspace"})], {"total_tokens": 1}
    service.worker = AgentWorker(lambda *_: Planner())
    run = await service.create(CreateRun(project="fixture", specification="Make addition work"))
    result = await finish(service, run["_id"], "plan")
    assert result["state"] == "blocked" and not service.runtime.calls


async def test_policy_assets_inside_repository_rejected(fixture):
    service, repo, policy_path = fixture
    policy = json.loads(policy_path.read_text())
    policy["projects"]["fixture"]["assets"] = str(repo)
    policy_path.write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="outside"):
        load_project(service.settings, "fixture")


async def test_turn_limit_is_controller_owned_even_for_replacement_worker(fixture):
    service, _, _ = fixture
    class Worker:
        async def run(self, **kw):
            for _ in range(10):
                await kw["meter"]("reserve", None)
                await kw["meter"]("usage", {"total_tokens": 1})
    service.worker = Worker()
    result = await finish(service, (await create(service, limits={"turns": 2, "attempts_per_task": 1}))["_id"])
    assert result["usage"]["turns"] == 2
    assert result["state"] == "blocked"


async def test_missing_verifier_executable_is_not_an_implementation_retry(fixture):
    service, _, policy_path = fixture
    policy = json.loads(policy_path.read_text())
    policy["projects"]["fixture"]["checks"]["answer"]["argv"] = ["/bin/sh", "-c", "exit 127"]
    policy_path.write_text(json.dumps(policy))
    service.worker = FakeWorker([2])
    result = await finish(service, (await create(service))["_id"])
    assert result["state"] == "blocked" and result["stop_reason"].startswith("invalid_verifier_configuration")
    assert len(service.worker.calls) == 1


async def test_invalid_provider_configuration_is_not_retried(fixture):
    service, _, _ = fixture
    def invalid(*args):
        raise ValueError("Provider credential not configured")
    service.worker = AgentWorker(invalid)
    result = await finish(service, (await create(service))["_id"])
    assert result["state"] == "blocked" and result["stop_reason"].startswith("invalid_configuration")
    assert result["usage"]["attempts"] == 1 and result["usage"]["turns"] == 0


async def test_process_timeout_keeps_partial_evidence():
    import sys
    from aria.loop.runtime import ProcessTimeout, process
    with pytest.raises(ProcessTimeout) as error:
        await process([sys.executable, "-u", "-c", "import time; print('useful failure'); time.sleep(60)"], timeout=1)
    assert b"useful failure" in error.value.output


async def test_completed_process_does_not_signal_reaped_group(monkeypatch):
    from aria.loop import runtime

    def no_longer_owned(*args):
        raise PermissionError("The completed process group is no longer owned")

    monkeypatch.setattr(runtime.os, "killpg", no_longer_owned)
    code, output = await runtime.process(["/bin/echo", "completed"])
    assert code == 0 and output == b"completed\n"


async def test_timeout_kills_descendants_after_leader_exits(tmp_path):
    import sys
    import psutil
    from aria.loop.runtime import process

    pidfile = tmp_path / "descendant"
    program = (
        "import subprocess; p=subprocess.Popen(['/bin/sleep','60']); "
        f"open({str(pidfile)!r},'w').write(str(p.pid))"
    )
    with pytest.raises(TimeoutError):
        await process([sys.executable, "-c", program], timeout=2)
    pid = int(pidfile.read_text())
    await asyncio.sleep(.1)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_existing_aria_settings_configure_service(fixture, monkeypatch):
    from aria.config import settings
    service, _, policy_path = fixture
    monkeypatch.setattr(settings, "loop_enabled", True)
    monkeypatch.setattr(settings, "loop_policy_file", str(policy_path))
    monkeypatch.setattr(settings, "loop_state_dir", str(service.root))
    configured = LoopService(service.db, runtime=service.runtime)
    assert configured.settings.enabled
    assert configured.root == service.root
    assert configured.settings.policy_file == str(policy_path)


async def test_attempt_stopped_at_a_limit_hands_forward_what_it_established(fixture):
    service, _, _ = fixture
    summary = "Rewrote the whole file; ruled out the greedy path."
    service.worker = FakeWorker([2], summary=summary, summary_usage={"total_tokens": 7}, callback=stop_once(
        ContextLimit("Attempt context limit reached; use compact handoff on next attempt"),
        "Rewriting answer.txt wholesale; the greedy path does not terminate.",
    ))
    run = await finish(service, (await create(service))["_id"])
    assert run["state"] == "ready_for_review", run["stop_reason"]
    assert [a["outcome"] for a in run["attempts"]] == ["execution_failed", "accepted", "accepted"]
    # The next fresh worker inherits the stop reason AND what the attempt established.
    handoff = service.worker.calls[1]["handoff"]
    assert "Attempt context limit reached" in handoff and summary in handoff
    assert "advisory, unverified" in handoff
    assert "greedy path does not terminate" in service.worker.summarized[0]
    # Salvage is a controller call, not a worker turn, and its tokens are accounted.
    assert run["usage"]["turns"] == 2 and run["usage"]["unknown_calls"] == 0
    assert run["usage"]["reported_tokens"] == 27
    assert run["attempts"][0]["reported_tokens"] == 17


async def test_wall_time_stop_is_salvaged_and_unaccountable_usage_stays_unknown(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([2], summary="Partial refactor; the adapter seam is wrong.", callback=stop_once(
        TimeoutError("Attempt or verification wall time exceeded"), "Partial refactor in progress.",
    ))
    run = await finish(service, (await create(service))["_id"])
    assert run["state"] == "ready_for_review", run["stop_reason"]
    assert "adapter seam is wrong" in service.worker.calls[1]["handoff"]
    # A total the provider never reported must not be silently counted as zero.
    assert run["usage"]["unknown_calls"] == 1 and run["usage"]["reported_tokens"] == 20


async def test_failed_salvage_leaves_the_original_stop_reason_intact(fixture):
    service, _, _ = fixture
    service.worker = FakeWorker([2], summary=RuntimeError("backend unavailable"), callback=stop_once(
        ContextLimit("Attempt context limit reached; use compact handoff on next attempt"), "Partial work.",
    ))
    run = await finish(service, (await create(service))["_id"])
    assert run["state"] == "ready_for_review", run["stop_reason"]
    assert service.worker.calls[1]["handoff"] == "Attempt context limit reached; use compact handoff on next attempt"
    failure = await service.db.loop_logs.find_one({"kind": "salvage_failed"})
    assert "backend unavailable" in failure["content"]


async def test_salvage_is_skipped_when_the_attempt_logged_no_model_output(fixture):
    service, _, _ = fixture
    stopped = []

    async def stop_without_logging(kw):
        if stopped:
            return None
        stopped.append(True)
        raise ContextLimit("Controller-enforced agent turn limit")

    service.worker = FakeWorker([2], summary="unused", callback=stop_without_logging)
    run = await finish(service, (await create(service))["_id"])
    assert run["state"] == "ready_for_review", run["stop_reason"]
    assert service.worker.summarized == []
    assert service.worker.calls[1]["handoff"] == "Controller-enforced agent turn limit"
