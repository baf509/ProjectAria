"""Bounded benchmark execution must survive API restarts and retain outcomes."""
import asyncio
import json
from pathlib import Path
import signal
import sys
from unittest.mock import AsyncMock

import pytest
from aria.benchmarks.service import BenchmarkError, BenchmarkService
from aria.api.routes.benchmarks import _bound_conflicts


@pytest.mark.asyncio
async def test_supervisor_timeout_persists_without_api(tmp_path):
    from aria.benchmarks import runner
    marker = tmp_path / "done.json"
    process = await asyncio.create_subprocess_exec(
        sys.executable, runner.__file__, "0.2", str(marker),
        sys.executable, "-c", "import time; time.sleep(30)")
    assert await asyncio.wait_for(process.wait(), 5) == 124
    assert json.loads(marker.read_text())["status"] == "timed_out"


@pytest.mark.asyncio
async def test_supervisor_cancel_persists(tmp_path):
    from aria.benchmarks import runner
    marker = tmp_path / "done.json"
    ready = tmp_path / "child-ready"
    process = await asyncio.create_subprocess_exec(
        sys.executable, runner.__file__, "30", str(marker), sys.executable, "-c",
        f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(30)")
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(.02)
    assert ready.exists()
    process.send_signal(signal.SIGTERM)
    await asyncio.wait_for(process.wait(), 5)
    assert json.loads(marker.read_text())["status"] == "cancelled"


@pytest.mark.asyncio
async def test_new_service_recovers_finished_run(tmp_path):
    svc = BenchmarkService(tmp_path)
    run_dir = svc.results / "run-1"
    run_dir.mkdir(parents=True)
    completion = run_dir / "aria-completion.json"
    completion.write_text(json.dumps({"status": "timed_out", "returncode": 124, "finished_at": 42}))
    log = run_dir / "aria-bench.log"
    log.write_text("finished\n")
    svc._write_registry({"runs": {"run-1": {"run_id": "run-1", "status": "running", "pid": -1,
        "results_dir": str(run_dir), "completion": str(completion), "log": str(log)}}})
    restarted = BenchmarkService(tmp_path)
    assert (await restarted.get_run("run-1"))["status"] == "timed_out"
    assert (await restarted.list_runs())[0]["returncode"] == 124


@pytest.mark.asyncio
async def test_endpoint_only_targets_cannot_conflict_with_model_lifecycle():
    svc = AsyncMock()
    svc.list_targets.return_value = [{"name": "red-radiance", "manages_lifecycle": False}]
    manager = AsyncMock()
    assert await _bound_conflicts(manager, None, ["red-radiance"], svc) == []
    manager.status.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_managed_targets_keep_the_bound_model_guard():
    svc = AsyncMock()
    svc.list_targets.return_value = [
        {"name": "red", "model": "Red", "manages_lifecycle": False},
        {"name": "other", "model": "Other", "manages_lifecycle": True}]
    manager = AsyncMock()
    manager.status.return_value = [{"slug": "Other", "state": "running", "bound_agent": "hermes"}]
    assert await _bound_conflicts(manager, None, ["red"], svc) == ["Other (bound to hermes)"]


@pytest.mark.asyncio
async def test_run_ids_cannot_escape_results(tmp_path, monkeypatch):
    svc = BenchmarkService(tmp_path)
    monkeypatch.setattr(svc, "_require", lambda: "/unused")
    monkeypatch.setattr(svc, "list_suites", AsyncMock(return_value=[{"name": "performance"}]))
    monkeypatch.setattr(svc, "list_targets", AsyncMock(return_value=[{"name": "red"}]))
    with pytest.raises(BenchmarkError, match="Invalid run_id"):
        await svc.start_run(["performance"], ["red"], run_id="../escape")
    assert not svc.results.exists()


@pytest.mark.asyncio
async def test_uninstalled_suite_refused_before_launch(tmp_path, monkeypatch):
    svc = BenchmarkService(tmp_path)
    monkeypatch.setattr(svc, "_require", lambda: "/unused")
    monkeypatch.setattr(svc, "list_suites", AsyncMock(return_value=[{
        "name": "code", "available": False, "unavailable_runners": ["inspect"]}]))
    with pytest.raises(BenchmarkError, match="dependencies unavailable"):
        await svc.start_run(["code"], ["red"])
