"""Opt-in real production sandbox tests, using an already-present digest-pinned image.

LOOP_TEST_IMAGE=sha256:... DOCKER_HOST=... pytest tests/test_loop_containers.py
No image pulls, service restarts, paid calls, or changes to source repositories.
"""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from aria.llm.base import ToolCall
from aria.loop.config import load_project
from aria.loop.runtime import ContainerRuntime, process
from aria.loop.worker import AgentWorker
from tests.test_loop import fixture, create, finish


pytestmark = pytest.mark.skipif(not os.getenv("LOOP_TEST_IMAGE"), reason="Set LOOP_TEST_IMAGE to run real container containment checks")


def configure(service, policy_path):
    policy = json.loads(policy_path.read_text())
    project = policy["projects"]["fixture"]
    project["image"] = os.environ["LOOP_TEST_IMAGE"]
    project["checks"]["answer"]["argv"][0] = "/usr/bin/python3"
    project["memory_mb"] = 256
    project["cpus"] = 1
    policy_path.write_text(json.dumps(policy))
    runtime = ContainerRuntime(state_root=service.root)
    service.runtime = runtime
    return runtime, load_project(service.settings, "fixture")[0]


async def test_container_vertical_slice_bad_candidate_repaired(fixture):
    service, repo, policy_path = fixture
    configure(service, policy_path)
    calls = []
    class Provider:
        async def complete(self, messages, **kwargs):
            if len(messages) == 2:
                calls.append(json.loads(messages[1].content))
                value = len(calls)
                return "Implement", [ToolCall("write", "shell", {"command": f"printf {value} > /workspace/answer.txt"})], {"total_tokens": 5}
            return json.dumps({"outcome": "ready", "changed": "answer", "checks": "none", "handoff": "Repair answer"}), [], {"total_tokens": 5}
    service.worker = AgentWorker(lambda *_: Provider())
    run = await finish(service, (await create(service))["_id"])
    assert run["state"] == "ready_for_review", run["stop_reason"]
    assert [a["outcome"] for a in run["attempts"]] == ["verification_failed", "accepted", "accepted"]
    assert len({c["session_id"] for c in calls}) == 2
    assert (repo / "answer.txt").read_text() == "0"
    assert Path(run["tasks"][0]["workspace"]).joinpath("answer.txt").read_text() == "2"


async def test_container_boundary_readonly_verifier_no_credentials_or_network(fixture, monkeypatch):
    service, _, policy_path = fixture
    runtime, policy = configure(service, policy_path)
    workspace = service.root / "probe"
    workspace.mkdir(parents=True)
    (workspace / "answer.txt").write_text("2")
    monkeypatch.setenv("ADMIN_KEY", "host-secret-must-not-enter-container")
    name = "aria-loop-v-" + uuid4().hex
    try:
        await runtime.start(name, workspace, policy, readonly=True, assets=Path(policy.assets), seconds=60)
        script = """
import os, pathlib, socket
assert 'ADMIN_KEY' not in os.environ
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/Users/ben').exists()
for path in ['/workspace/answer.txt', '/checks/check.py', '/etc/loop-probe']:
    try:
        pathlib.Path(path).write_text('tamper')
    except OSError:
        pass
    else:
        raise AssertionError('Unexpected writable path: ' + path)
s = socket.socket()
s.settimeout(1)
assert s.connect_ex(('1.1.1.1', 443)) != 0
print('isolation checks passed')
"""
        result = await runtime.execute(name, ["/usr/bin/python3", "-c", script], 10)
        assert result["exit_code"] == 0, result
    finally:
        await runtime.stop(name)
    assert (workspace / "answer.txt").read_text() == "2"


@pytest.mark.parametrize("cancel", [False, True])
async def test_container_timeout_cancel_kills_detached_descendants(fixture, cancel):
    service, _, policy_path = fixture
    runtime, policy = configure(service, policy_path)
    workspace = service.root / "descendants"
    workspace.mkdir(parents=True)
    name = "aria-loop-w-" + uuid4().hex
    try:
        await runtime.start(name, workspace, policy, seconds=30)
        script = """
import os, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        fd = os.open('/dev/null', os.O_RDWR)
        for stream in (0, 1, 2):
            os.dup2(fd, stream)
        while True:
            open('/workspace/alive', 'w').write(str(time.time()))
            time.sleep(.05)
    os._exit(0)
time.sleep(.2)
"""
        started = await runtime.execute(name, ["/usr/bin/python3", "-c", script], 10)
        assert started["exit_code"] == 0, started
        alive = await runtime.execute(name, ["/bin/cat", "/workspace/alive"], 10)
        assert alive["exit_code"] == 0, alive
        job = asyncio.create_task(runtime.execute(name, ["/bin/sleep", "60"], .8 if not cancel else 20))
        if cancel:
            await asyncio.sleep(.8)
            job.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await job
    finally:
        await runtime.stop(name)
    before = (workspace / "alive").read_text()
    await asyncio.sleep(.2)
    assert (workspace / "alive").read_text() == before
    code, output = await process([runtime.binary, "inspect", name])
    assert code != 0 and b"no such" in output.lower()
    await runtime.stop(name)  # recovery is idempotent, including volume cleanup
