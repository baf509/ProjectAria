"""Tests for aria.infrastructure.model_servers — the local LLM model-server
control plane. Docker calls and the GTT sysfs read are mocked; agent binding
uses a tiny in-memory fake Mongo collection."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace

from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

from aria.infrastructure import model_servers as ms
from aria.infrastructure.model_servers import (
    ModelServerBindingConflict,
    ModelServerError,
    ModelServerManager,
    ModelServerNotFound,
    ModelServerSafetyError,
)


# ─────────────────────────────────────────────────────────── fake mongo ──

def _match(doc: dict, query: dict) -> bool:
    for k, v in query.items():
        if isinstance(v, dict):
            if "$ne" in v and doc.get(k) == v["$ne"]:
                return False
            if "$exists" in v and (k in doc) != v["$exists"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query: dict) -> Optional[dict]:
        for d in self.docs:
            if _match(d, query):
                return d
        return None

    async def update_one(self, query: dict, update: dict):
        for d in self.docs:
            if _match(d, query):
                for k, v in update.get("$set", {}).items():
                    d[k] = v
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return

    def find(self, query: dict) -> FakeCursor:
        return FakeCursor([d for d in self.docs if _match(d, query)])


class FakeDB:
    def __init__(self):
        self.agents = FakeCollection()
        self.model_servers = FakeCollection()
        self.model_pulls = FakeCollection()


def _agent(slug: str, model_server: Optional[str] = None) -> dict:
    doc = {"_id": ObjectId(), "slug": slug}
    if model_server is not None:
        doc["model_server"] = model_server
    return doc


# ────────────────────────────────────────────────────────── docker fake ──

class FakeDocker:
    """Simulates `docker inspect/start/stop/compose` for _run().

    container_states maps name -> (state, compose_project) where
    compose_project is "" for hand-run containers.
    """

    def __init__(self, container_states: Optional[dict[str, tuple[str, str]]] = None):
        self.container_states = dict(container_states or {})
        self.calls: list[tuple] = []

    async def __call__(self, *args: str):
        if args[:2] == ("docker", "inspect"):
            name = args[-1]
            info = self.container_states.get(name)
            if info is None:
                return 1, "", "Error: No such object: " + name
            state, project = info
            return 0, f"{state}|{project}\n", ""
        if args[:2] == ("docker", "start"):
            self.calls.append(("start", args[2]))
            state, project = self.container_states[args[2]]
            self.container_states[args[2]] = ("running", project)
            return 0, "", ""
        if args[:2] == ("docker", "stop"):
            self.calls.append(("stop", args[2]))
            state, project = self.container_states[args[2]]
            self.container_states[args[2]] = ("exited", project)
            return 0, "", ""
        if args[:2] == ("docker", "compose"):
            self.calls.append(("compose", args))
            # Register the started service's container as running so a later
            # inspect (e.g. the second of two racing starts) observes it —
            # service name == container name for every registry entry.
            service = args[-1]
            self.container_states[service] = ("running", service)
            return 0, "created\n", ""
        return 1, "", f"unhandled: {args}"


class DaemonDownDocker:
    """Every docker invocation fails as if dockerd is unreachable."""

    async def __call__(self, *args: str):
        return 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"


@pytest.fixture(autouse=True)
def _never_touch_real_docker():
    """Hard stop against the suite shelling out to the real docker daemon.

    Not hypothetical: on 2026-07-30 `test_start_unstartable_without_force_raises`
    called manager.start() without mocking _run, relying on the unstartable
    gate to raise before any subprocess. The moment Chadrockv2 became startable
    that gate stopped firing, the test fell through, and it really ran
    `docker compose up -d chadrockv2` — starting a 27 GiB model server from a
    unit test. Tests that want docker behaviour patch _run themselves; this
    default makes the un-patched path fail loudly instead of escaping.
    """
    def _forbidden(*args, **kwargs):
        raise AssertionError(
            f"test attempted a real subprocess: {' '.join(args)!r}. "
            "Patch aria.infrastructure.model_servers._run in this test."
        )
    with patch.object(ms, "_run", _forbidden):
        yield


@pytest.fixture
def manager():
    return ModelServerManager()


def _startable_cpu_recovery_spec(manager: ModelServerManager):
    """Exercise generic Docker mechanics without making Mac-owned Gemma startable.

    Production intentionally marks Gemma unstartable in the Corsair registry so
    the actuator cannot boot a duplicate. These tests still need a CPU-only
    compose shape, so use a synthetic clone confined to the mocked manager.
    """
    return replace(
        manager.get_spec("gemma-4-e4b-Q4"),
        slug="synthetic-cpu-recovery",
        startable=True,
        not_startable_reason=None,
    )


# ───────────────────────────────────────────────────────────── registry ──

def test_registry_has_no_duplicate_slugs():
    slugs = [s.slug for s in ms.REGISTRY]
    assert len(slugs) == len(set(slugs))


def test_unknown_slug_raises_not_found(manager):
    with pytest.raises(ModelServerNotFound):
        manager.get_spec("does-not-exist")


def test_exclusivity_is_symmetric():
    """start() only consults the starting spec's own exclusive_with, so every
    pair must be present in both directions."""
    by_slug = {s.slug: s for s in ms.REGISTRY}
    for spec in ms.REGISTRY:
        for other in spec.exclusive_with:
            assert other in by_slug, f"{spec.slug} exclusive with unknown {other}"
            assert spec.slug in by_slug[other].exclusive_with, (
                f"{spec.slug} -> {other} not mirrored"
            )
def test_chadrock_qwen_coexistence_not_forbidden():
    """The deliberate two-server split (measured ~89.4 GiB combined)."""
    by_slug = {s.slug: s for s in ms.REGISTRY}
    assert "ROCmFP4-qwen3.6-35b-a3b" not in by_slug["Chadrock-Laguna-S-2.1"].exclusive_with


def test_laguna_rocmfp4_qwen_pair_is_forbidden():
    """87+29 SWAG exceeds the safety margin; the live GTT gate alone would
    currently allow it, so the static pair must catch it."""
    by_slug = {s.slug: s for s in ms.REGISTRY}
    assert "ROCmFP4-qwen3.6-35b-a3b" in by_slug["Laguna-S-2.1"].exclusive_with


# ─────────────────────────────────────────────────────────────── status ──

@pytest.mark.asyncio
async def test_status_reports_live_docker_state(manager):
    docker = FakeDocker({"chadrock": ("exited", ""), "gemma-aux": ("running", "gemma-aux")})
    # Remote state is stubbed so this stays a pure unit test: _remote_state
    # otherwise reaches the network (HTTP health, then ssh) for Ridge and RED.
    with patch.object(ms, "_run", docker), \
         patch.object(ms, "_read_gtt_gib", return_value=(25.0, 124.0)), \
         patch.object(ms, "_remote_state", AsyncMock(return_value="asleep")):
        results = await manager.status()

    by_slug = {r["slug"]: r for r in results}
    assert by_slug["Chadrock-Laguna-S-2.1"]["state"] == "exited"
    assert by_slug["gemma-4-e4b-Q4"]["state"] == "running"
    assert by_slug["Laguna-S-2.1"]["state"] == "not_created"  # not in container_states
    # Chadrockv2 is wired as of 2026-07-30 (compose service + container), so it
    # reports not_created like any other absent container. The synthetic-spec
    # tests below still cover the "unwired"/unstartable branches.
    assert by_slug["Chadrock-ROCmFP6-qwen3.6-27b"]["state"] == "not_created"
    # Ridge became remotely operable 2026-08-15, so it reports a real remote
    # state instead of the "external" placeholder. Seeded in the cache above so
    # this assertion does no network I/O.
    assert by_slug["Ridge-Qwen3.8-27B"]["state"] == "asleep"
    assert by_slug["gemma-4-e4b-Q4"]["gtt_used_gib"] == 25.0


@pytest.mark.asyncio
async def test_status_includes_bound_agents(manager):
    db = FakeDB()
    db.agents.docs.append(_agent("search-agent", model_server="context1-Q4"))
    with patch.object(ms, "_run", FakeDocker()), patch.object(ms, "_read_gtt_gib", return_value=None):
        results = await manager.status(db)
    by_slug = {r["slug"]: r for r in results}
    assert by_slug["context1-Q4"]["bound_agents"] == ["search-agent"]
    assert by_slug["Laguna-S-2.1"]["bound_agents"] == []


@pytest.mark.asyncio
async def test_status_raises_when_daemon_down(manager):
    """A daemon outage must NOT masquerade as an all-not_created fleet."""
    with patch.object(ms, "_run", DaemonDownDocker()), patch.object(ms, "_read_gtt_gib", return_value=None):
        with pytest.raises(ModelServerError, match="daemon"):
            await manager.status()


# ────────────────────────────────────────────────────────────── start() ──

# ─────────────────────────────────────────── synthetic exclusive pair ──
#
# ⚠️ THESE ARE FIXTURES, NOT REAL DEPLOYMENTS — deliberately, after this broke
# for the THIRD time. The exclusivity tests need "two startable, mutually
# exclusive, container-backed servers sharing a pool", and every attempt to
# borrow a real registry pair has rotted when that pair was retired:
#   - laguna + chadrock  -> unstartable 2026-08-14 (GGUFs removed from the box)
#   - the two R9700 Qwen3.8 variants -> unstartable 2026-08-17 (GGUFs deleted in
#     the radiance cutover; `start()` then refuses on `startable=False` BEFORE
#     reaching the exclusivity check, so six tests failed for a reason that had
#     nothing to do with what they test)
# Retiring a deployment is routine and must never break the safety tests. These
# specs are injected into the registry for the duration of a test, so the only
# thing under test is the exclusivity/RAM/compose logic itself.
_EXCL_A = "test-excl-a"
_EXCL_B = "test-excl-b"

_FIXTURE_SPECS = [
    ms.ModelServerSpec(
        slug=_EXCL_A,
        description="Synthetic fixture — exclusivity/RAM/compose tests only.",
        runtime_repo="n/a", runtime_ref="n/a", backend_device="test",
        container_name="qwen3.8-27b", compose_file="docker-compose.yml",
        service_name="qwen3.8-27b", port=18110,
        memory_pool=ms.POOL_R9700, resident_gib=20.0,
        exclusive_with=(_EXCL_B,),
    ),
    ms.ModelServerSpec(
        slug=_EXCL_B,
        description="Synthetic fixture — exclusivity/RAM/compose tests only.",
        runtime_repo="n/a", runtime_ref="n/a", backend_device="test",
        container_name="qwen3.8-27b-rocmfp4", compose_file="docker-compose.yml",
        service_name="qwen3.8-27b-rocmfp4", port=18110,
        # Profile-gated on purpose: test_start_uses_compose_up_when_container_missing
        # asserts `docker compose up` carries --profile for a gated service.
        profile="rocmfp4",
        memory_pool=ms.POOL_R9700, resident_gib=20.0,
        exclusive_with=(_EXCL_A,),
    ),
]


@pytest.fixture(autouse=True)
def _register_fixture_specs():
    """Make the synthetic pair resolvable for every test, then remove them.

    autouse so no test can forget it; the registry is restored afterwards so a
    fixture slug can never leak into a real `status()` listing.
    """
    # REGISTRY is a tuple (immutable by design), so rebind rather than mutate.
    orig_registry = ms.REGISTRY
    orig_by_slug = dict(ms._BY_SLUG)
    ms.REGISTRY = orig_registry + tuple(_FIXTURE_SPECS)
    for spec in _FIXTURE_SPECS:
        ms._BY_SLUG[spec.slug] = spec
    try:
        yield
    finally:
        ms.REGISTRY = orig_registry
        ms._BY_SLUG.clear()
        ms._BY_SLUG.update(orig_by_slug)


@pytest.mark.asyncio
async def test_start_refuses_on_exclusivity_conflict(manager):
    docker = FakeDocker({"qwen3.8-27b-rocmfp4": ("running", "")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(2.0, 32.0)):
        with pytest.raises(ModelServerSafetyError, match="mutually exclusive"):
            await manager.start(_EXCL_A)
    assert not docker.calls  # never got as far as issuing a start/compose command


@pytest.mark.asyncio
async def test_start_exclusivity_counts_paused_containers(manager):
    """A paused container's process is frozen with its allocations intact —
    it must conflict the same as a running one."""
    docker = FakeDocker({"qwen3.8-27b-rocmfp4": ("paused", "")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(2.0, 32.0)):
        with pytest.raises(ModelServerSafetyError, match="paused"):
            await manager.start(_EXCL_A)


@pytest.mark.asyncio
async def test_start_refuses_on_ram_swag_overflow(manager):
    # The ROCmFP4 variant is a 24 GiB SWAG in the R9700's 32 GiB pool:
    # 20 + 24 = 44 > 0.92 * 32 — exercises the memory gate alone, with no
    # exclusive peer running.
    docker = FakeDocker({})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(20.0, 32.0)):
        with pytest.raises(ModelServerSafetyError, match="safety margin"):
            await manager.start(_EXCL_B)
    assert not docker.calls


@pytest.mark.asyncio
async def test_start_skips_gtt_gate_for_cpu_only_server(manager):
    """A CPU-only recovery service never allocates from GTT, so even a
    nearly-full GTT pool must not refuse it."""
    docker = FakeDocker({})  # not created -> compose up
    spec = _startable_cpu_recovery_spec(manager)
    with patch.object(manager, "resolve_spec", AsyncMock(return_value=spec)), \
         patch.object(ms, "_run", docker), \
         patch.object(ms, "_read_gtt_gib", return_value=(120.0, 124.0)):
        result = await manager.start(spec.slug)
    assert result["action"] == "started"


@pytest.mark.asyncio
async def test_start_force_bypasses_safety_checks(manager):
    docker = FakeDocker({"chadrock": ("running", "")})  # would normally conflict
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(112.0, 124.0)):
        result = await manager.start("Laguna-S-2.1", force=True)
    assert result["action"] == "started"
    assert any(kind == "compose" for kind, *_ in docker.calls)


@pytest.mark.asyncio
async def test_start_handrun_container_uses_raw_docker_start_with_note(manager):
    """A hand-run container (no compose label) can't be adopted by compose up
    (name conflict) — raw docker start, with an explicit config-drift note."""
    docker = FakeDocker({"gemma-aux": ("exited", "")})
    spec = _startable_cpu_recovery_spec(manager)
    with patch.object(manager, "resolve_spec", AsyncMock(return_value=spec)), \
         patch.object(ms, "_run", docker), \
         patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        result = await manager.start(spec.slug)
    assert result["action"] == "started"
    assert docker.calls == [("start", "gemma-aux")]
    assert "compose-file changes are NOT applied" in result["note"]


@pytest.mark.asyncio
async def test_start_compose_managed_container_uses_compose_up(manager):
    """An existing compose-managed container goes through compose up -d so a
    compose-file edit is reconciled instead of resurrecting the old argv."""
    docker = FakeDocker({"gemma-aux": ("exited", "gemma-aux")})
    spec = _startable_cpu_recovery_spec(manager)
    with patch.object(manager, "resolve_spec", AsyncMock(return_value=spec)), \
         patch.object(ms, "_run", docker), \
         patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        result = await manager.start(spec.slug)
    assert result["action"] == "started"
    assert "note" not in result
    assert len(docker.calls) == 1
    kind, args = docker.calls[0]
    assert kind == "compose"
    assert "up" in args and "gemma-aux" in args


@pytest.mark.asyncio
async def test_start_uses_compose_up_when_container_missing(manager):
    docker = FakeDocker({})  # the rocmfp4 container doesn't exist yet
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(2.0, 32.0)):
        result = await manager.start(_EXCL_B)
    assert result["action"] == "started"
    assert len(docker.calls) == 1
    kind, args = docker.calls[0]
    assert kind == "compose"
    assert "--profile" in args and "rocmfp4" in args


@pytest.mark.asyncio
async def test_start_noop_when_already_running_even_if_gates_would_fail(manager):
    """The noop check must come BEFORE the safety gates: an already-running
    server's memory is already counted in GTT-used, and its exclusive peers
    being up is a pre-existing condition, not a new hazard."""
    docker = FakeDocker({
        "qwen3.8-27b": ("running", "qwen3.8-27b"),
        "qwen3.8-27b-rocmfp4": ("running", ""),
    })
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(31.0, 32.0)):
        result = await manager.start(_EXCL_A)
    assert result == {"slug": _EXCL_A, "state": "running", "action": "noop"}
    assert not docker.calls


@pytest.mark.asyncio
async def test_start_paused_container_raises_clear_error(manager):
    docker = FakeDocker({"gemma-aux": ("paused", "gemma-aux")})
    spec = _startable_cpu_recovery_spec(manager)
    with patch.object(manager, "resolve_spec", AsyncMock(return_value=spec)), \
         patch.object(ms, "_run", docker), \
         patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        with pytest.raises(ModelServerError, match="paused"):
            await manager.start(spec.slug)
    assert not docker.calls


# Every real onbox entry is startable as of 2026-07-30, so the unstartable
# branches are exercised with a synthetic spec rather than by pinning a test to
# whichever model happens to be un-wired that week.
_UNWIRED = ms.ModelServerSpec(
    slug="synthetic-unwired",
    description="test-only",
    runtime_repo="", runtime_ref="", backend_device="",
    startable=False,
    not_startable_reason="No compose service exists yet; needs a build.",
)


@pytest.fixture
def unwired_registry():
    # get_spec reads _BY_SLUG; status() walks REGISTRY — patch both.
    with patch.dict(ms._BY_SLUG, {"synthetic-unwired": _UNWIRED}), \
         patch.object(ms, "REGISTRY", ms.REGISTRY + (_UNWIRED,)):
        yield


@pytest.mark.asyncio
async def test_start_unstartable_without_force_raises(manager, unwired_registry):
    with pytest.raises(ModelServerSafetyError, match="No compose service"):
        await manager.start("synthetic-unwired")


@pytest.mark.asyncio
async def test_start_unstartable_with_force_still_fails_no_compose_service(manager, unwired_registry):
    # force bypasses the startable gate, but there's still no compose_file
    # configured, so it must fail loudly rather than silently no-op.
    docker = FakeDocker({})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        with pytest.raises(ModelServerSafetyError, match="no compose service configured"):
            await manager.start("synthetic-unwired", force=True)


@pytest.mark.asyncio
async def test_status_reports_unwired_for_container_less_spec(manager, unwired_registry):
    with patch.object(ms, "_run", FakeDocker()), patch.object(ms, "_read_gtt_gib", return_value=None):
        results = await manager.status()
    assert {r["slug"]: r["state"] for r in results}["synthetic-unwired"] == "unwired"


@pytest.mark.asyncio
async def test_start_offbox_server_without_remote_commands_refused(manager):
    """Off-box entries that declare no remote start/stop are still refused.

    Ridge and RED became remotely operable on 2026-08-15, so the refusal is now
    about MISSING REMOTE COMMANDS rather than about being off-box per se. A
    synthetic spec keeps that branch covered."""
    spec = ms.ModelServerSpec(
        slug="Synthetic-Offbox-Unwired",
        description="off-box, no remote control declared",
        runtime_repo="n/a", runtime_ref="remote", backend_device="remote",
        onbox=False, memory_pool=ms.POOL_REMOTE,
    )
    assert spec.remotely_operable is False
    with patch.object(manager, "resolve_spec", AsyncMock(return_value=spec)):
        with pytest.raises(ModelServerSafetyError, match="no remote start/stop"):
            await manager.start(spec.slug)
        with pytest.raises(ModelServerSafetyError, match="no remote start/stop"):
            await manager.stop(spec.slug)


@pytest.mark.asyncio
async def test_remote_start_wakes_then_starts_then_verifies(manager):
    """The operable-remote happy path: wake (if needed) -> start -> verify.

    Readiness is what makes it 'ready'; a start command that 'succeeds' without
    the service coming up must NOT be reported as started. That exact shape was
    real on RED, whose scheduled task returned SUCCESS and did nothing."""
    spec = manager.get_spec("Red-Qwen3.6-35B-A3B")
    assert spec.remotely_operable is True

    calls = []

    async def fake_run(*args):
        calls.append(args)
        return 0, "started 123", ""

    # unreachable -> reachable after wake; not serving -> serving after start
    health = iter([False, False, True])
    reach = iter([False, True, True, True])

    with patch.object(ms, "_run", fake_run), \
         patch.object(ms, "_remote_health_ok", AsyncMock(side_effect=lambda s, timeout=5.0: next(health))), \
         patch.object(ms, "_remote_box_reachable", AsyncMock(side_effect=lambda s, timeout=6.0: next(reach))), \
         patch.object(ms.asyncio, "sleep", AsyncMock(return_value=None)):
        res = await manager.start("Red-Qwen3.6-35B-A3B")

    assert res["state"] == "ready"
    assert res["action"] == "started"
    assert res["woken"] is True
    assert any("wake-red" in " ".join(c) for c in calls), calls


@pytest.mark.asyncio
async def test_remote_start_not_ready_reports_starting_not_started(manager):
    """A start whose service never answers health reports 'starting', never 'ready'."""
    with patch.object(ms, "_run", AsyncMock(return_value=(0, "", ""))), \
         patch.object(ms, "_remote_health_ok", AsyncMock(return_value=False)), \
         patch.object(ms, "_remote_box_reachable", AsyncMock(return_value=True)), \
         patch.object(ms.asyncio, "sleep", AsyncMock(return_value=None)), \
         patch.object(ms.time, "monotonic", side_effect=[0.0] + [10_000.0] * 40):
        res = await manager.start("Red-Qwen3.6-35B-A3B")
    assert res["state"] == "starting"
    assert res["action"] == "start_requested"


@pytest.mark.asyncio
async def test_concurrent_exclusive_starts_only_one_wins(manager):
    """Two concurrent mutually-exclusive starts must not both pass the gates:
    the lock serializes them, so the second observes the first's container
    (FakeDocker registers it on compose up) and refuses."""
    import asyncio

    docker = FakeDocker({})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(2.0, 32.0)):
        results = await asyncio.gather(
            manager.start(_EXCL_A),
            manager.start(_EXCL_B),
            return_exceptions=True,
        )
    started = [r for r in results if isinstance(r, dict) and r.get("action") == "started"]
    refused = [r for r in results if isinstance(r, ModelServerSafetyError)]
    assert len(started) == 1, f"expected exactly one winner, got {results}"
    assert len(refused) == 1, f"expected exactly one refusal, got {results}"
    assert len([c for c in docker.calls if c[0] == "compose"]) == 1


# ─────────────────────────────────────────────────────────────── stop() ──

@pytest.mark.asyncio
async def test_stop_noop_when_not_created(manager):
    docker = FakeDocker({})
    with patch.object(ms, "_run", docker):
        result = await manager.stop("Laguna-S-2.1")
    assert result["action"] == "noop"
    assert not docker.calls


@pytest.mark.asyncio
async def test_stop_calls_docker_stop_when_running(manager):
    docker = FakeDocker({"gemma-aux": ("running", "gemma-aux")})
    with patch.object(ms, "_run", docker):
        result = await manager.stop("gemma-4-e4b-Q4")
    assert result["action"] == "stopped"
    assert docker.calls == [("stop", "gemma-aux")]


@pytest.mark.asyncio
async def test_stop_raises_when_daemon_down_instead_of_false_success(manager):
    """dockerd unreachable must NOT be reported as 'not_created' -> noop
    'success' while the server is actually still running."""
    with patch.object(ms, "_run", DaemonDownDocker()):
        with pytest.raises(ModelServerError, match="daemon"):
            await manager.stop("gemma-4-e4b-Q4")


@pytest.mark.asyncio
async def test_remote_stop_does_not_suspend_the_box(manager):
    """stop() stops the model service only; suspending is sleep()'s job."""
    calls = []

    async def fake_run(*args):
        calls.append(args)
        return 0, "stopped", ""

    with patch.object(ms, "_run", fake_run), \
         patch.object(ms, "_remote_box_reachable", AsyncMock(return_value=True)), \
         patch.object(ms, "_remote_health_ok", AsyncMock(return_value=False)):
        res = await manager.stop("Red-Qwen3.6-35B-A3B")

    assert res["state"] == "stopped"
    joined = [" ".join(c) for c in calls]
    assert any("gateway-ctl.ps1" in c and "stop" in c for c in joined), joined
    assert not any("SetSuspendState" in c for c in joined), "stop() must not suspend"


# ──────────────────────────────────────────────────── bind() / unbind() ──

@pytest.mark.asyncio
async def test_bind_sets_model_server_on_agent(manager):
    db = FakeDB()
    agent = _agent("search-agent")
    db.agents.docs.append(agent)

    result = await manager.bind(db, "context1-Q4", "search-agent")
    assert result["model_server"] == "context1-Q4"
    assert agent["model_server"] == "context1-Q4"


@pytest.mark.asyncio
async def test_bind_conflict_without_force_raises(manager):
    db = FakeDB()
    db.agents.docs.append(_agent("agent-a", model_server="context1-Q4"))
    db.agents.docs.append(_agent("agent-b"))

    with pytest.raises(ModelServerBindingConflict, match="agent-a"):
        await manager.bind(db, "context1-Q4", "agent-b")


@pytest.mark.asyncio
async def test_bind_conflict_with_force_adds_extra_slot(manager):
    db = FakeDB()
    a = _agent("agent-a", model_server="context1-Q4")
    b = _agent("agent-b")
    db.agents.docs.extend([a, b])

    result = await manager.bind(db, "context1-Q4", "agent-b", force=True)
    assert result["extra_slot"] is True
    assert a["model_server"] == "context1-Q4"
    assert b["model_server"] == "context1-Q4"


@pytest.mark.asyncio
async def test_bind_unknown_agent_raises(manager):
    db = FakeDB()
    with pytest.raises(ModelServerNotFound, match="Unknown agent"):
        await manager.bind(db, "context1-Q4", "no-such-agent")


@pytest.mark.asyncio
async def test_bind_unknown_slug_raises(manager):
    db = FakeDB()
    db.agents.docs.append(_agent("agent-a"))
    with pytest.raises(ModelServerNotFound, match="Unknown model server"):
        await manager.bind(db, "no-such-slug", "agent-a")


@pytest.mark.asyncio
async def test_unbind_clears_field(manager):
    db = FakeDB()
    agent = _agent("search-agent", model_server="context1-Q4")
    db.agents.docs.append(agent)

    result = await manager.unbind(db, "search-agent")
    assert result["model_server"] is None
    assert "model_server" not in agent


# ─────────────────────────────── dynamic (pulled) entries + pull service ──

from aria.infrastructure.model_pull import (  # noqa: E402
    RUNTIME_TEMPLATES,
    ModelPullService,
    _compose_yaml,
    _PORT_RANGE,
)
from aria.infrastructure.model_servers import REGISTRY  # noqa: E402


# model_servers/model_pulls now live on the base FakeDB; alias kept for the
# dynamic-entry tests' readability.
FakeDBWithServers = FakeDB


def _dynamic_doc(slug="pulled-qwen", port=8105):
    return {
        "slug": slug,
        "description": "Pulled from hf.co/org/repo",
        "runtime_repo": "ghcr.io/ggml-org/llama.cpp:server-vulkan",
        "runtime_ref": "template mainline-vulkan",
        "backend_device": "Vulkan0",
        "model_file": f"models/llm/{slug}/model.gguf",
        "port": port,
        "compose_file": f"generated/{slug}/docker-compose.yml",
        "service_name": slug,
        "container_name": slug,
        "resident_gib": 20.0,
        "gtt_resident": True,
    }


@pytest.mark.asyncio
async def test_resolve_spec_finds_dynamic_entry(manager):
    db = FakeDBWithServers()
    db.model_servers.docs.append(_dynamic_doc())
    spec = await manager.resolve_spec("pulled-qwen", db)
    assert spec.port == 8105
    assert spec.compose_file == "generated/pulled-qwen/docker-compose.yml"
    assert spec.exclusive_with == ()


@pytest.mark.asyncio
async def test_resolve_spec_unknown_still_raises_with_db(manager):
    db = FakeDBWithServers()
    with pytest.raises(ModelServerNotFound):
        await manager.resolve_spec("nope", db)


@pytest.mark.asyncio
async def test_status_merges_dynamic_entries(manager):
    db = FakeDBWithServers()
    db.model_servers.docs.append(_dynamic_doc())
    docker = FakeDocker({"pulled-qwen": ("running", "pulled-qwen")})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=None):
        results = await manager.status(db)
    by_slug = {r["slug"]: r for r in results}
    assert by_slug["pulled-qwen"]["state"] == "running"
    assert len(results) == len(ms.REGISTRY) + 1


@pytest.mark.asyncio
async def test_start_dynamic_entry_uses_its_compose_file(manager):
    db = FakeDBWithServers()
    db.model_servers.docs.append(_dynamic_doc())
    docker = FakeDocker({})
    with patch.object(ms, "_run", docker), patch.object(ms, "_read_gtt_gib", return_value=(10.0, 124.0)):
        result = await manager.start("pulled-qwen", db=db)
    assert result["action"] == "started"
    kind, args = docker.calls[0]
    assert kind == "compose"
    assert any("generated/pulled-qwen/docker-compose.yml" in a for a in args)


@pytest.mark.asyncio
async def test_bind_accepts_dynamic_slug(manager):
    db = FakeDBWithServers()
    db.model_servers.docs.append(_dynamic_doc())
    db.agents.docs.append(_agent("some-agent"))
    result = await manager.bind(db, "pulled-qwen", "some-agent")
    assert result["model_server"] == "pulled-qwen"


class TestPullValidation:
    @pytest.mark.asyncio
    async def test_rejects_bad_repo(self):
        svc = ModelPullService()
        with pytest.raises(ModelServerError, match="repo id"):
            await svc._validate(FakeDBWithServers(), "not a repo", "m.gguf", "ok-name", "mainline-cpu", None)

    @pytest.mark.asyncio
    async def test_rejects_bad_filename(self):
        svc = ModelPullService()
        with pytest.raises(ModelServerError, match="filename"):
            await svc._validate(FakeDBWithServers(), "org/repo", "../evil.gguf", "ok-name", "mainline-cpu", None)
        with pytest.raises(ModelServerError, match="filename"):
            await svc._validate(FakeDBWithServers(), "org/repo", "model.bin", "ok-name", "mainline-cpu", None)

    @pytest.mark.asyncio
    async def test_rejects_bad_name_and_unknown_runtime(self):
        svc = ModelPullService()
        with pytest.raises(ModelServerError, match="Invalid name"):
            await svc._validate(FakeDBWithServers(), "org/repo", "m.gguf", "has spaces", "mainline-cpu", None)
        with pytest.raises(ModelServerError, match="Unknown runtime"):
            await svc._validate(FakeDBWithServers(), "org/repo", "m.gguf", "ok-name", "cuda", None)

    @pytest.mark.asyncio
    async def test_rejects_duplicate_slug_static_and_dynamic(self):
        svc = ModelPullService()
        with pytest.raises(ModelServerError, match="already exists"):
            await svc._validate(FakeDBWithServers(), "org/repo", "m.gguf", "gemma-4-e4b-Q4", "mainline-cpu", None)
        db = FakeDBWithServers()
        db.model_servers.docs.append(_dynamic_doc(slug="taken"))
        with pytest.raises(ModelServerError, match="already exists"):
            await svc._validate(db, "org/repo", "m.gguf", "taken", "mainline-cpu", None)

    @pytest.mark.asyncio
    async def test_port_allocation_skips_used_ports(self):
        svc = ModelPullService()
        db = FakeDBWithServers()
        db.model_servers.docs.append(_dynamic_doc(slug="taken", port=8107))
        port = await svc._validate(db, "org/repo", "m.gguf", "ok-name", "mainline-cpu", None)
        # Assert the *property* (lowest free port in the range), not a literal.
        # This used to assert 8108 and broke the moment a new static server was
        # registered on that port — the assertion has to be derived from the
        # registry, or every registration is a spurious test failure.
        taken = {s.port for s in REGISTRY if s.port} | {8107}
        assert port not in taken
        assert all(p in taken for p in _PORT_RANGE if p < port), (
            f"allocated {port} while a lower port in the range was free"
        )
        with pytest.raises(ModelServerError, match="already assigned"):
            await svc._validate(db, "org/repo", "m.gguf", "ok-name", "mainline-cpu", 8103)  # static qwen port


def test_compose_yaml_generation_per_runtime():
    for runtime, t in RUNTIME_TEMPLATES.items():
        yml = _compose_yaml("my-model", runtime, "my-model.gguf", 8110, 32768)
        assert f"image: {t['image']}" in yml
        assert "container_name: my-model" in yml
        assert '"127.0.0.1:8110:8080"' in yml
        assert "/models/my-model.gguf" in yml
        assert f'- "{t["ngl"]}"' in yml
        if t["entrypoint"]:
            assert t["entrypoint"] in yml
        if t["gpu"] == "vulkan":
            assert "/dev/dri" in yml
        else:
            assert "/dev/dri" not in yml


# ─────────────────────────────────────────────────────────────── sleep() ──

class SleepFakeSSH:
    """Simulates the two ssh invocations sleep() makes: the reachability
    probe (`... ridge exit`) and the suspend command."""

    def __init__(self, reachable: bool):
        self.reachable = reachable
        self.calls: list[tuple] = []

    async def __call__(self, *args: str):
        self.calls.append(args)
        if not self.reachable:
            return 255, "", "ssh: connect to host ridge port 22: Connection timed out"
        if args[-1] == "exit":
            return 0, "", ""
        # Suspend drops the connection — nonzero exit is the success shape.
        return 255, "", "Connection to ridge closed by remote host."


@pytest.mark.asyncio
async def test_sleep_ridge_sends_suspend_when_reachable(manager):
    # SleepFakeSSH stays reachable, so the off-box verification added
    # 2026-08-15 correctly reports a FAILED sleep rather than a requested one.
    # That is the point: `ssh exit 0` is not evidence the machine suspended.
    ssh = SleepFakeSSH(reachable=True)
    times = iter([0.0] + [10_000.0] * 50)
    with patch.object(ms, "_run", ssh), \
         patch.object(ms.asyncio, "sleep", AsyncMock(return_value=None)), \
         patch.object(ms.time, "monotonic", side_effect=lambda: next(times)):
        result = await manager.sleep("Ridge-Qwen3.8-27B")
    assert result["action"] == "sleep_failed"
    assert result["verified"] is False
    # the suspend itself is issued via the reliable script, not rundll32
    assert "sleep-now.ps1" in ssh.calls[1][-1]
    assert "SetSuspendState" not in ssh.calls[1][-1]


@pytest.mark.asyncio
async def test_sleep_noop_when_already_asleep(manager):
    ssh = SleepFakeSSH(reachable=False)
    with patch.object(ms, "_run", ssh):
        result = await manager.sleep("Ridge-Qwen3.8-27B")
    assert result == {
        "slug": "Ridge-Qwen3.8-27B", "state": "asleep", "action": "noop",
        "detail": "unreachable over ssh — already asleep",
    }
    assert len(ssh.calls) == 1  # probe only, no suspend attempt


@pytest.mark.asyncio
async def test_sleep_refused_for_onbox_server(manager):
    with pytest.raises(ModelServerSafetyError, match="no sleep command"):
        await manager.sleep("gemma-4-e4b-Q4")


# ────────────────────────── binding drives real routing (not just a label) ──

@pytest.mark.asyncio
async def test_resolve_endpoint_static_slug():
    from aria.infrastructure.model_servers import resolve_endpoint
    assert await resolve_endpoint("Chadrock-ROCmFP6-qwen3.6-27b") == "http://localhost:8105/v1"


@pytest.mark.asyncio
async def test_resolve_endpoint_prefers_override_for_offbox():
    """Ridge is reachable ONLY via its tailnet-bound proxy — localhost is
    refused there, so the override must win over any port-derived guess."""
    from aria.infrastructure.model_servers import resolve_endpoint
    assert await resolve_endpoint("Ridge-Qwen3.8-27B") == "http://127.0.0.1:8092/v1"


@pytest.mark.asyncio
async def test_resolve_endpoint_dynamic_and_unknown():
    from aria.infrastructure.model_servers import resolve_endpoint
    db = FakeDB()
    db.model_servers.docs.append(_dynamic_doc(slug="pulled-x", port=8120))
    assert await resolve_endpoint("pulled-x", db) == "http://localhost:8120/v1"
    assert await resolve_endpoint("nope", db) is None


def test_get_adapter_caches_per_base_url():
    """Two agents on the same backend+model but different bound servers must
    NOT share an adapter — otherwise the second silently talks to the first's
    server. Adapter construction is stubbed so this doesn't need the openai
    SDK (present in the API venv, not necessarily in the test interpreter)."""
    from types import SimpleNamespace
    from aria.llm.manager import LLMManager

    mgr = LLMManager()
    with patch("aria.llm.llamacpp.LlamaCppAdapter", lambda **kw: SimpleNamespace(**kw)):
        a = mgr.get_adapter("agentic", "m", base_url="http://localhost:8105/v1")
        b = mgr.get_adapter("agentic", "m", base_url="http://localhost:8106/v1")
        again = mgr.get_adapter("agentic", "m", base_url="http://localhost:8105/v1")
        default = mgr.get_adapter("agentic", "m")

    assert a.base_url == "http://localhost:8105/v1"
    assert b.base_url == "http://localhost:8106/v1"
    assert a is not b            # distinct servers -> distinct adapters
    assert again is a            # same server -> cached
    assert default is not a      # unbound falls back to the static AGENTIC_URL


# ──────────────────────────────────────────────────── launch geometry ──
# Served context is read from the launch file rather than declared, so these
# guard the parse: a wrong number here silently under-counts the GTT gate,
# which is the exact failure this mechanism exists to prevent.

def test_argv_geometry_reads_ctx_and_slots():
    geo = ms._argv_geometry(["llama-server", "-c", "230400", "-np", "6"], "t")
    assert (geo.n_ctx, geo.slots) == (230400, 6)
    # -c is PER SEQUENCE: llama.cpp reports n_ctx_seq == -c and gives every slot
    # its own full-size cache, so ctx_per_slot is -c itself and the KV total
    # MULTIPLIES by slots. Confirmed on the live server 2026-08-09:
    # "n_slots = 6" with "new slot, n_ctx = 230400" six times.
    assert geo.ctx_per_slot == 230400
    assert geo.total_kv_tokens == 1382400


def test_argv_geometry_accepts_long_and_equals_forms():
    geo = ms._argv_geometry(["--ctx-size=65536", "--parallel=2"], "t")
    assert (geo.n_ctx, geo.slots) == (65536, 2)
    assert geo.ctx_per_slot == 65536
    assert geo.total_kv_tokens == 131072


def test_cache_ram_is_not_mistaken_for_ctx():
    """`-cram` and `--cache-ram` both start with the `-c` prefix. Matching on
    prefix rather than whole tokens would read the cache size as the context."""
    geo = ms._argv_geometry(["--cache-ram", "1024", "-cram", "512", "-c", "4096"], "t")
    assert geo.n_ctx == 4096


def test_ctx_per_slot_defaults_to_single_slot():
    geo = ms._argv_geometry(["-c", "8192"], "t")
    assert geo.ctx_per_slot == 8192
    assert geo.total_kv_tokens == 8192


def test_geometry_is_unknown_not_wrong_when_unparseable():
    geo = ms._argv_geometry(["llama-server", "--webui"], "t")
    assert geo.n_ctx is None and geo.ctx_per_slot is None
    assert geo.total_kv_tokens is None


def test_systemd_geometry_ignores_execstartpre(tmp_path):
    """ExecStartPre runs `sha256sum -c manifest/bundle.sha256`. Reading any
    ExecStart* line would parse that `-c` as a context size."""
    unit = tmp_path / "fake.service"
    unit.write_text(
        "[Service]\n"
        "ExecStartPre=/usr/bin/bash -lc 'sha256sum -c manifest/bundle.sha256'\n"
        "ExecStart=/opt/bin/llama-server -m x.gguf -c 1382400 -np 6\n"
    )
    with patch.object(ms, "_SYSTEMD_USER_DIR", str(tmp_path)):
        geo = ms._systemd_geometry("fake.service")
    assert (geo.n_ctx, geo.slots) == (1382400, 6)


def test_effective_resident_gib_computed_from_served_ctx():
    """weights + KV(-c) + buffers, so raising context in the unit moves the
    projection without anyone editing the registry."""
    spec = ms.ModelServerSpec(
        slug="t", description="", runtime_repo="", runtime_ref="", backend_device="",
        weights_gib=85.26, kv_kib_per_token=6.71875, overhead_gib=2.1,
    )
    # DS4 as actually deployed: 6 slots x 230400 = 1382400 tokens of KV.
    # Projected 96.2 GiB; the live server measured 94.56 -> conservative, which
    # is the correct direction for a gate that refuses overcommit.
    geo = ms.LaunchGeometry(n_ctx=230400, slots=6)
    assert ms.effective_resident_gib(spec, geo) == 96.2
    # SLOTS multiply the KV cost. Getting this backwards under-counts by the
    # slot count -- the 2026-08-09 OOM that took the server down on startup.
    assert ms.effective_resident_gib(spec, ms.LaunchGeometry(n_ctx=230400, slots=1)) < 89
    bigger = ms.effective_resident_gib(spec, ms.LaunchGeometry(n_ctx=460800, slots=6))
    assert bigger > 103


def test_effective_resident_gib_falls_back_to_declared():
    """Entries not characterised with the two constants, or whose launch file
    is unparseable, keep the hand-declared SWAG rather than reporting None."""
    spec = ms.ModelServerSpec(
        slug="t", description="", runtime_repo="", runtime_ref="", backend_device="",
        resident_gib=42.0,
    )
    assert ms.effective_resident_gib(spec, ms.LaunchGeometry(n_ctx=65536)) == 42.0
    characterised = ms.ModelServerSpec(
        slug="t", description="", runtime_repo="", runtime_ref="", backend_device="",
        resident_gib=42.0, weights_gib=40.0, kv_kib_per_token=11.0,
    )
    assert ms.effective_resident_gib(characterised, ms.LaunchGeometry()) == 42.0


def test_ds4_projection_tracks_its_live_unit():
    """End-to-end against the real unit file: whatever -c it carries is what
    the registry projects from."""
    spec = ms._BY_SLUG["DS4-0731-ROCMFPX-affine-256k"]
    geo = ms.read_launch_geometry(spec)
    if geo.n_ctx is None:
        pytest.skip("DS4 unit not installed on this host")
    assert geo.ctx_per_slot == geo.n_ctx
    assert geo.total_kv_tokens == geo.n_ctx * geo.slots
    assert ms.effective_resident_gib(spec, geo) > spec.weights_gib


# ────────────────────────────────────────────────── runtime utilisation ──

def test_parse_prometheus_picks_known_gauges_only():
    text = (
        "# HELP llamacpp:requests_deferred Number of requests deferred.\n"
        "llamacpp:requests_deferred 3\n"
        "llamacpp:predicted_tokens_seconds 15.9067\n"
        "llamacpp:something_we_do_not_track 99\n"
        "malformed line without value\n"
        "llamacpp:prompt_tokens_total notanumber\n"
    )
    parsed = ms._parse_prometheus(text)
    assert parsed == {"requests_deferred": 3.0, "predicted_tokens_per_second": 15.9067}


def test_runtime_stats_derived_fields():
    stats = ms.RuntimeStats(total_slots=6, busy_slots=2, requests_deferred=0.0)
    assert stats.free_slots == 4
    assert stats.slot_utilisation == round(2 / 6, 3)
    assert stats.saturated is False


def test_saturated_means_requests_are_queuing():
    """Every slot busy is fine; queued requests are not. A deferred request
    lands in whichever slot frees first, not the one holding its prefix."""
    full_but_calm = ms.RuntimeStats(total_slots=6, busy_slots=6, requests_deferred=0.0)
    assert full_but_calm.slot_utilisation == 1.0
    assert full_but_calm.saturated is False
    queuing = ms.RuntimeStats(total_slots=6, busy_slots=6, requests_deferred=2.0)
    assert queuing.saturated is True


def test_saturated_is_unknown_without_metrics():
    """`--metrics` absent must read as unknown, never as a confident False."""
    stats = ms.RuntimeStats(total_slots=6, busy_slots=1, metrics_available=False)
    assert stats.saturated is None
    assert stats.slot_utilisation is not None  # /slots alone still gives this


def test_base_url_for_spec_prefers_endpoint_override():
    """DS4 binds the tailnet IP only; a port-derived localhost URL is refused."""
    override = ms.ModelServerSpec(
        slug="t", description="", runtime_repo="", runtime_ref="", backend_device="",
        port=8107, endpoint_override="http://100.123.245.84:8107/v1",
    )
    assert ms.base_url_for_spec(override) == "http://100.123.245.84:8107/v1"
    plain = ms.ModelServerSpec(
        slug="t", description="", runtime_repo="", runtime_ref="", backend_device="",
        port=8104,
    )
    assert ms.base_url_for_spec(plain) == "http://localhost:8104/v1"
    assert ms.base_url_for_spec(ms.ModelServerSpec(
        slug="t", description="", runtime_repo="", runtime_ref="", backend_device="",
    )) is None


@pytest.mark.asyncio
async def test_sleep_is_verified_from_off_box(manager):
    """sleep() must confirm the box actually went down, not trust the exit code.

    The old implementation returned success on `ssh exit 0`, which is how a
    sleep verb that never suspended anything survived undetected: rundll32
    SetSuspendState silently no-ops over ssh without SeShutdownPrivilege.
    """
    ms._remote_state_cache.clear()
    # reachable for the pre-flight probe, then gone once suspended
    reach = iter([True, False])
    with patch.object(ms, "_run", AsyncMock(return_value=(0, "suspend-issued", ""))), \
         patch.object(ms, "_remote_box_reachable",
                      AsyncMock(side_effect=lambda s, timeout=6.0: next(reach))), \
         patch.object(ms.asyncio, "sleep", AsyncMock(return_value=None)):
        res = await manager.sleep("Ridge-Qwen3.8-27B")
    assert res["state"] == "asleep"
    assert res["action"] == "slept"
    assert res["verified"] is True


@pytest.mark.asyncio
async def test_sleep_that_does_not_suspend_reports_failure(manager):
    """A box still reachable after the deadline is a FAILED sleep, not a success."""
    ms._remote_state_cache.clear()
    times = iter([0.0] + [10_000.0] * 50)
    with patch.object(ms, "_run", AsyncMock(return_value=(0, "suspend-issued", ""))), \
         patch.object(ms, "_remote_box_reachable", AsyncMock(return_value=True)), \
         patch.object(ms.asyncio, "sleep", AsyncMock(return_value=None)), \
         patch.object(ms.time, "monotonic", side_effect=lambda: next(times)):
        res = await manager.sleep("Ridge-Qwen3.8-27B")
    assert res["action"] == "sleep_failed"
    assert res["verified"] is False
    assert "WakeOnPattern" in res["detail"]


# ---------------------------------------------------------------------------
# Remote state: stale-while-revalidate
#
# Why these exist: the web UI measured GET /infrastructure/model-servers at
# 8.8s once every 20 seconds — a 3s health timeout plus a 4s reachability
# timeout per asleep remote, paid on the read's own critical path each time the
# TTL lapsed, while the page polled every 10s. The read now serves the
# remembered state and refreshes behind it; only an unknown remote blocks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_remote_state_is_served_stale_and_refreshed(manager):
    """An expired entry must NOT put a probe on the read's critical path."""
    spec = ms._BY_SLUG["Ridge-Qwen3.8-27B"]
    ms._remote_state_cache.clear()
    ms._remote_state_inflight.clear()
    # Remembered 60s ago: past the 20s TTL, well inside the 300s max age.
    ms._remote_state_cache[spec.slug] = (ms.time.monotonic() - 60.0, "running")

    probes = 0

    async def slow_probe(s, timeout=3.0):
        nonlocal probes
        probes += 1
        await asyncio.sleep(0.05)
        return False

    with patch.object(ms, "_remote_health_ok", AsyncMock(side_effect=slow_probe)), \
         patch.object(ms, "_remote_box_reachable", AsyncMock(return_value=False)):
        state = await ms._remote_state(spec)
        assert state == "running", "stale value should be served immediately"
        # ...and the refresh should be running behind it.
        task = ms._remote_state_inflight[spec.slug]
        await task

    assert probes == 1
    assert ms._remote_state_cache[spec.slug][1] == "asleep"


@pytest.mark.asyncio
async def test_concurrent_cold_reads_share_one_probe(manager):
    """Single-flight: N rows for the same remote must not each pay the timeout."""
    spec = ms._BY_SLUG["Ridge-Qwen3.8-27B"]
    ms._remote_state_cache.clear()
    ms._remote_state_inflight.clear()

    probes = 0

    async def counted(s, timeout=3.0):
        nonlocal probes
        probes += 1
        await asyncio.sleep(0.05)
        return True

    with patch.object(ms, "_remote_health_ok", AsyncMock(side_effect=counted)):
        states = await asyncio.gather(*(ms._remote_state(spec) for _ in range(5)))

    assert states == ["running"] * 5
    assert probes == 1, "five concurrent reads should share one probe"


@pytest.mark.asyncio
async def test_operations_always_probe_fresh(manager):
    """A start/stop decision must never be made against a remembered state."""
    spec = ms._BY_SLUG["Ridge-Qwen3.8-27B"]
    ms._remote_state_cache.clear()
    ms._remote_state_inflight.clear()
    ms._remote_state_cache[spec.slug] = (ms.time.monotonic(), "asleep")

    with patch.object(ms, "_remote_health_ok", AsyncMock(return_value=True)):
        assert await ms._remote_state(spec) == "asleep"          # cached read
        assert await ms._remote_state(spec, fresh=True) == "running"  # operation


@pytest.mark.asyncio
async def test_state_older_than_max_age_blocks(manager):
    """Past the max age the read waits again, so the UI cannot show minutes-old state."""
    spec = ms._BY_SLUG["Ridge-Qwen3.8-27B"]
    ms._remote_state_cache.clear()
    ms._remote_state_inflight.clear()
    ms._remote_state_cache[spec.slug] = (ms.time.monotonic() - (ms._REMOTE_STATE_MAX_AGE + 1), "running")

    with patch.object(ms, "_remote_health_ok", AsyncMock(return_value=False)), \
         patch.object(ms, "_remote_box_reachable", AsyncMock(return_value=False)):
        assert await ms._remote_state(spec) == "asleep"


# ---------------------------------------------------------------------------
# Memory pools: halo-gtt and host-ram are the SAME physical memory
#
# Raised by Ben 2026-08-17 while reviewing the rebuilt UI: the memory panel
# drew one bar per pool, so a 124 GiB machine appeared to have ~248 GiB and the
# ~102 GiB the iGPU holds was counted twice — once as halo-gtt, once inside
# host-ram. The discrete card's 32 GiB, meanwhile, was not drawn at all.
# ---------------------------------------------------------------------------


def _fake_devices():
    from aria.infrastructure.gpu_devices import GpuDevice

    return [
        # card0: discrete R9700 — its own VRAM, genuinely separate.
        GpuDevice(card="card0", pci_address="0000:c6:00.0", discrete=True,
                  vram_total_gib=31.85, vram_used_gib=29.75,
                  gtt_total_gib=124.0, gtt_used_gib=0.03),
        # card1: Strix Halo — no memory of its own, GTT out of system RAM.
        GpuDevice(card="card1", pci_address="0000:c8:00.0", discrete=False,
                  vram_total_gib=1.0, vram_used_gib=0.14,
                  gtt_total_gib=124.0, gtt_used_gib=102.11),
    ]


def test_pool_snapshot_marks_shared_backing():
    """A consumer must be able to tell which pools are the same DIMMs."""
    from aria.infrastructure import gpu_devices as gd

    # Keep this topology test host-independent. macOS has no /proc/meminfo,
    # so relying on the real host probe would silently omit host-ram here.
    host = gd.MemoryPool(
        pool=gd.POOL_HOST, label="host RAM",
        used_gib=118.4, total_gib=124.4, source="/proc/meminfo",
    )
    with patch.object(gd, "discover_devices", return_value=_fake_devices()), \
         patch.object(gd, "_host_pool", return_value=host):
        pools = {p["pool"]: p for p in gd.pool_snapshot()}

    assert pools["halo-gtt"]["backing"] == "system"
    assert pools["host-ram"]["backing"] == "system"
    assert pools["r9700-vram"]["backing"] == "device"
    # Each system pool names the other, so a UI can group them into one bar.
    assert pools["halo-gtt"]["overlaps"] == ["host-ram"]
    assert pools["host-ram"]["overlaps"] == ["halo-gtt"]
    assert pools["r9700-vram"]["overlaps"] == []


def test_system_memory_snapshot_does_not_double_count_the_igpu():
    """total == igpu + other + available, with the iGPU counted exactly once."""
    from aria.infrastructure import gpu_devices as gd

    host = gd.MemoryPool(
        pool=gd.POOL_HOST, label="host RAM",
        used_gib=118.4, total_gib=124.4, source="/proc/meminfo",
    )
    with patch.object(gd, "discover_devices", return_value=_fake_devices()), \
         patch.object(gd, "_host_pool", return_value=host):
        sysmem = gd.system_memory_snapshot()

    assert sysmem["total_gib"] == 124.4
    assert sysmem["igpu_gib"] == 102.1          # what the Halo holds via GTT
    assert sysmem["other_gib"] == 16.3          # 118.4 - 102.1, NOT 118.4
    assert sysmem["available_gib"] == 6.0
    parts = sysmem["igpu_gib"] + sysmem["other_gib"] + sysmem["available_gib"]
    assert abs(parts - sysmem["total_gib"]) < 0.2, (
        f"segments {parts} must add up to the machine's memory {sysmem['total_gib']}"
    )


def test_system_memory_snapshot_survives_disagreeing_samples():
    """GTT and MemAvailable are sampled separately and can disagree slightly."""
    from aria.infrastructure import gpu_devices as gd

    # GTT reports MORE than total host usage — a sampling skew, not negative RAM.
    host = gd.MemoryPool(
        pool=gd.POOL_HOST, label="host RAM",
        used_gib=100.0, total_gib=124.4, source="/proc/meminfo",
    )
    with patch.object(gd, "discover_devices", return_value=_fake_devices()), \
         patch.object(gd, "_host_pool", return_value=host):
        sysmem = gd.system_memory_snapshot()

    assert sysmem["other_gib"] == 0.0


# ───────────────────────────────────────────────────────── status cache ──
# The full status() is ~70-80 subprocess spawns (8.57 s cold / 0.6 s warm
# measured) and used to sit on the critical path of every /llm/v1 request.
# These tests pin the caching contract: compute once, serve from cache within
# the TTL, recompute on force or after any state mutation (start/stop/sleep/
# bind/unbind). The seam is _status_uncached — the cache wraps it.

class _CountingStatus:
    """Replaces _status_uncached: counts calls, returns a fixed row."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, db=None):
        self.calls += 1
        return [{"slug": "fake", "state": "running"}]


@pytest.mark.asyncio
async def test_status_is_cached_within_ttl(manager):
    inner = _CountingStatus()
    with patch.object(ModelServerManager, "_status_uncached", inner):
        first = await manager.status()
        second = await manager.status()
    assert first is second  # same object — served from the cache
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_status_force_bypasses_cache(manager):
    inner = _CountingStatus()
    with patch.object(ModelServerManager, "_status_uncached", inner):
        await manager.status()
        await manager.status(force=True)
    assert inner.calls == 2


@pytest.mark.asyncio
async def test_status_cache_expires_after_ttl(manager):
    inner = _CountingStatus()
    with patch.object(ModelServerManager, "_status_uncached", inner), \
         patch.object(ModelServerManager, "STATUS_CACHE_TTL", 0.0):
        await manager.status()
        await asyncio.sleep(0.01)
        await manager.status()
    assert inner.calls == 2


@pytest.mark.asyncio
async def test_start_invalidates_status_cache(manager, tmp_path):
    inner = _CountingStatus()
    docker = FakeDocker({})
    with patch.object(ModelServerManager, "_status_uncached", inner), \
         patch.object(ms, "_run", docker), \
         patch.object(ms, "_read_gtt_gib", return_value=None), \
         patch.object(ms, "_SYSTEMD_USER_DIR", str(tmp_path)):
        await manager.status()
        assert inner.calls == 1
        await manager.start(_EXCL_A)
        assert manager._status_cache is None  # start dropped the cache
        await manager.status()
        assert inner.calls == 2


@pytest.mark.asyncio
async def test_stop_invalidates_status_cache(manager, tmp_path):
    inner = _CountingStatus()
    docker = FakeDocker({_EXCL_A: ("running", "")})
    with patch.object(ModelServerManager, "_status_uncached", inner), \
         patch.object(ms, "_run", docker), \
         patch.object(ms, "_SYSTEMD_USER_DIR", str(tmp_path)):
        await manager.status()
        await manager.stop(_EXCL_A)
        assert manager._status_cache is None


@pytest.mark.asyncio
async def test_bind_and_unbind_invalidate_status_cache(manager):
    inner = _CountingStatus()
    db = FakeDB()
    db.agents.docs.append(_agent("test-agent"))
    with patch.object(ModelServerManager, "_status_uncached", inner):
        await manager.status(db)
        assert inner.calls == 1
        await manager.bind(db, _EXCL_A, "test-agent")
        assert manager._status_cache is None
        await manager.unbind(db, "test-agent")
        assert manager._status_cache is None


# ───────────────────────────────────────────────────── running_summary ──
# The light routing answer: at most two subprocesses (one systemctl, one
# docker ps) no matter how many specs there are.

class _FleetProbe:
    """Counts subprocess calls; answers the two fleet-wide queries."""

    def __init__(self, active_units: set[str], running_containers: set[str]):
        self.active_units = active_units
        self.running_containers = running_containers
        self.calls: list[tuple] = []

    async def __call__(self, *args: str):
        self.calls.append(args)
        if args[:2] == ("systemctl", "--user") and "list-units" in args:
            return 0, "\n".join(sorted(self.active_units)) + "\n", ""
        if args[:2] == ("docker", "ps"):
            return 0, "\n".join(sorted(self.running_containers)) + "\n", ""
        return 1, "", f"unhandled: {args}"


@pytest.fixture
def _seeded_remote_state():
    """Pre-seed the module-level remote-state cache so running_summary never
    fires a real ssh/httpx probe at Ridge/RED from a unit test."""
    import time as _time
    orig = dict(ms._remote_state_cache)
    for spec in ms.REGISTRY:
        if not spec.onbox and spec.remotely_operable:
            ms._remote_state_cache[spec.slug] = (_time.monotonic(), "asleep")
    yield
    ms._remote_state_cache.clear()
    ms._remote_state_cache.update(orig)


@pytest.mark.asyncio
async def test_running_summary_uses_at_most_two_subprocesses(manager, tmp_path, _seeded_remote_state):
    probe = _FleetProbe(active_units=set(), running_containers={"qwen3.8-27b"})
    with patch.object(ms, "_run", probe), \
         patch.object(ms, "_SYSTEMD_USER_DIR", str(tmp_path)):
        rows = await manager.running_summary()
    fleet_calls = [c for c in probe.calls if c[:2] in (("systemctl", "--user"), ("docker", "ps"))]
    assert len(fleet_calls) <= 2, probe.calls
    by_slug = {r["slug"]: r for r in rows}
    # EXCL_A's container is in the running set; EXCL_B's is not. (The real
    # registry shares those container names — that is fine: the state answer
    # is per-container, and every row reports what its container is doing.)
    assert by_slug[_EXCL_A]["state"] == "running"
    assert by_slug[_EXCL_B]["state"] == "exited"
    # routing fields are present and shaped for llm_route.select()
    for row in rows:
        assert row["slug"] and row["state"] and "endpoints" in row
        assert "resident_gib_estimate" in row and "startable" in row


@pytest.mark.asyncio
async def test_running_summary_routes_like_full_status(manager, tmp_path, _seeded_remote_state):
    """A server that is running in the summary is servable by llm_route."""
    from aria.infrastructure.llm_route import is_servable, select

    probe = _FleetProbe(active_units=set(), running_containers={"qwen3.8-27b"})
    with patch.object(ms, "_run", probe), \
         patch.object(ms, "_SYSTEMD_USER_DIR", str(tmp_path)):
        rows = await manager.running_summary()
    chosen, reason, unavailable = select(rows, requested=None, pin=None)
    assert chosen is not None and not unavailable
    assert is_servable(chosen)
    # The running container is shared by the fixture and a real (retired)
    # registry entry — either may win the largest-resident pick.
    assert chosen["slug"] in {_EXCL_A, "Qwen3.8-27B-Q6_K-R9700-Vulkan-MTP"}
    # naming a stopped server by slug is a known-but-stopped error, not auto
    chosen2, reason2, unavailable2 = select(rows, requested=_EXCL_B, pin=None)
    assert chosen2 is None and unavailable2


class _ForwardReader:
    async def readline(self):
        return b"HTTP/1.1 200 OK\r\n"


class _ForwardWriter:
    def write(self, _data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_mac_forward_mode_discovers_models_without_linux_tools(
    manager, _seeded_remote_state
):
    selected = ms._BY_SLUG[_EXCL_A]

    async def fake_open(host, port):
        assert host == "127.0.0.1"
        if port == selected.port:
            return _ForwardReader(), _ForwardWriter()
        raise OSError("closed")

    linux_tools = AsyncMock(side_effect=AssertionError("Linux tool invoked on Mac"))
    with patch.object(ms.sys, "platform", "darwin"), \
         patch.dict(ms.os.environ, {"ARIA_CORSAIR_MODEL_FORWARDS": "1"}), \
         patch.object(ms.asyncio, "open_connection", side_effect=fake_open), \
         patch.object(ms, "_run", linux_tools):
        rows = await manager.running_summary()

    by_slug = {row["slug"]: row for row in rows}
    assert by_slug[_EXCL_A]["state"] == "running"
    assert linux_tools.await_count == 0


@pytest.mark.asyncio
async def test_mac_forward_mode_uses_restricted_corsair_actuator(manager):
    actuator = AsyncMock(side_effect=[
        {"slug": _EXCL_A, "state": "running", "action": "noop"},
        {"slug": _EXCL_A, "state": "stopped", "action": "stopped"},
    ])
    with patch.object(ms.sys, "platform", "darwin"), \
         patch.dict(ms.os.environ, {"ARIA_CORSAIR_MODEL_FORWARDS": "1"}), \
         patch.object(ms, "_corsair_actuate", actuator):
        started = await manager.start(_EXCL_A)
        stopped = await manager.stop(_EXCL_A)

    assert started["action"] == "noop"
    assert stopped["action"] == "stopped"
    assert actuator.await_args_list[0].args == ("start", _EXCL_A)
    assert actuator.await_args_list[0].kwargs == {"force": False}
    assert actuator.await_args_list[1].args == ("stop", _EXCL_A)


@pytest.mark.asyncio
async def test_mac_forward_mode_refuses_remote_launch_overrides(manager):
    with patch.object(ms.sys, "platform", "darwin"), \
         patch.dict(ms.os.environ, {"ARIA_CORSAIR_MODEL_FORWARDS": "1"}):
        with pytest.raises(ModelServerSafetyError, match="overrides are not accepted"):
            await manager.start(_EXCL_A, overrides={"CTX": "65536"})


@pytest.mark.asyncio
async def test_corsair_actuator_ssh_is_forced_key_only():
    response = '{"ok":true,"result":{"slug":"%s","action":"noop"}}\n' % _EXCL_A
    runner = AsyncMock(return_value=(0, response, ""))
    with patch.object(ms, "_run", runner), patch.dict(ms.os.environ, {}, clear=True):
        result = await ms._corsair_actuate("start", _EXCL_A)

    assert result["action"] == "noop"
    argv = runner.await_args.args
    assert argv[0] == "/usr/bin/ssh"
    assert "ClearAllForwardings=yes" in argv
    assert "RequestTTY=no" in argv
    assert "HostKeyAlias=corsair-ai.local" in argv
    assert argv[-3:] == ("aria-model-actuator", "start", _EXCL_A)


# ─────────────────────────────────────────────────────── one() (D8) ───────
# A single-entity read must not pay the full-fleet cost: the old
# GET /model-servers/{slug} ran the whole status() sweep (~70-80
# subprocesses) to return one row. one() probes only the requested spec
# and shares the row builder with status() so the two views cannot drift.

@pytest.mark.asyncio
async def test_one_probes_only_the_requested_spec(manager, unwired_registry):
    inspected: list[str] = []

    async def fake_inspect(_self, spec):
        inspected.append(spec.slug)
        return "stopped", False

    def _patches():
        return (
            patch.object(ms, "_run", FakeDocker()),
            patch.object(ms, "_read_gtt_gib", return_value=None),
            patch.object(ms, "read_pool", return_value=None),
            patch.object(ms, "measure_resident_gib", AsyncMock(return_value=None)),
            patch.object(ModelServerManager, "_inspect", fake_inspect),
        )

    with contextlib.ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        row = await manager.one("synthetic-unwired")
    assert inspected == ["synthetic-unwired"], \
        f"one() inspected {inspected}, expected only the requested spec"
    assert row["slug"] == "synthetic-unwired"
    assert row["state"] == "stopped"

    with contextlib.ExitStack() as stack:
        for p in _patches():
            stack.enter_context(p)
        full = await manager.status()
    full_row = next(r for r in full if r["slug"] == "synthetic-unwired")
    assert row.keys() == full_row.keys(), "one() and status() rows drifted"


@pytest.mark.asyncio
async def test_one_unknown_slug_raises_not_found(manager):
    with pytest.raises(ModelServerNotFound):
        await manager.one("does-not-exist")
