"""
ARIA - Non-LLM service registry tests

Purpose: lock the invariants that make this registry safe to keep *separate*
from the model-server registry, and the `expected_state` semantics that decide
whether a stopped service pages a human.

The disjointness test is the load-bearing one. Merging the two registries is a
tempting simplification that breaks three ways (see services.py module
docstring); the most damaging is that health.py's port-keyed
`stopped_on_purpose` map would make "mongod is down" read as "stopped on
purpose" and silence the alert. If someone ever adds a service on a model
server's port — or vice versa — that failure comes back, so it is asserted.
"""

import pytest
from unittest.mock import patch

from aria.infrastructure.model_servers import REGISTRY as MODEL_SERVERS
from aria.infrastructure.services import (
    REGISTRY,
    ServiceManager,
    ServiceNotFound,
    ServiceNotManageable,
    ServiceSpec,
    get_spec,
    is_healthy,
    review_needed,
)


# --------------------------------------------------------------------------
# Separation from the model-server registry
# --------------------------------------------------------------------------


def test_registries_are_disjoint_by_slug():
    """A shared slug would let `model: "<slug>"` route LLM traffic to a
    non-LLM service via llm_route.match_requested()."""
    overlap = {s.slug for s in MODEL_SERVERS} & {s.slug for s in REGISTRY}
    assert not overlap, f"slug collision between registries: {overlap}"


def test_registries_are_disjoint_by_port():
    """A shared port would let health.py's port-keyed `stopped_on_purpose`
    map mark a down always_up service as 'stopped on purpose' — silencing the
    exact alert this registry exists to raise."""
    ms_ports = {s.port for s in MODEL_SERVERS if s.port}
    svc_ports = {s.port for s in REGISTRY if s.port}
    overlap = ms_ports & svc_ports
    assert not overlap, f"port collision between registries: {overlap}"


def test_service_specs_carry_no_llm_routing_fields():
    """The fields that make llm_route treat a row as a servable model must not
    exist on a ServiceSpec, even optionally."""
    forbidden = {
        "resident_gib",
        "gtt_resident",
        "exclusive_with",
        "backend_device",
        "model_file",
        "endpoint_override",
    }
    present = forbidden & set(ServiceSpec.__dataclass_fields__)
    assert not present, f"ServiceSpec must not carry LLM-routing fields: {present}"


def test_no_model_server_leaked_into_service_registry():
    """The local LLM servers are managed by the other registry. Duplicating one
    here would give it two control planes with different safety gates — the
    service one has no RAM-exclusivity check at all."""
    llm_markers = ("deepseek", "gemma-aux", "qwen", "laguna", "chadrock", "ling", "context1")
    for spec in REGISTRY:
        slug = spec.slug.lower()
        assert not any(m in slug for m in llm_markers), (
            f"{spec.slug} looks like a model server; it belongs in "
            f"model_servers.REGISTRY, which has the exclusivity + GTT gates."
        )


# --------------------------------------------------------------------------
# expected_state semantics — the reason the registry exists
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["running", "active"])
def test_live_service_is_healthy_either_way(state):
    assert is_healthy(state, "always_up")
    assert is_healthy(state, "on_demand")


def test_always_up_service_down_is_unhealthy():
    """The core requirement: a stopped mongod must never read as fine."""
    for state in ("stopped", "exited", "not_created", "failed", "unknown"):
        assert not is_healthy(state, "always_up"), f"{state} must be unhealthy"


def test_on_demand_service_stopped_is_healthy():
    """aria-stt sat EXITED for 7 days while health.py counted it unhealthy
    every tick. on_demand exists so that is reportable as normal."""
    for state in ("stopped", "exited", "not_created"):
        assert is_healthy(state, "on_demand"), f"{state} should be fine on_demand"


def test_on_demand_service_that_failed_is_still_unhealthy():
    """'Stopped' is normal for on_demand; 'failed' is a crash and never is."""
    assert not is_healthy("failed", "on_demand")


def test_not_applicable_service_is_not_an_incident():
    assert is_healthy("not_applicable", "always_up")
    assert is_healthy("not_applicable", "on_demand")


def test_every_spec_has_a_valid_expected_state():
    for spec in REGISTRY:
        assert spec.expected_state in ("always_up", "on_demand"), spec.slug


def test_core_data_plane_is_always_up():
    """These are the ones whose absence breaks ARIA outright. If a future edit
    downgrades one to on_demand, its outage stops paging."""
    for slug in ("shared-mongod", "shared-mongot", "shared-embeddings", "aria-api"):
        assert get_spec(slug).expected_state == "always_up"


def test_assumed_states_are_flagged_for_review():
    """Anything whose expected_state was inferred rather than confirmed must
    be flagged, so it gets a human pass instead of quietly becoming ground
    truth — the failure mode that rotted the hand-written ontology seed list."""
    flagged = {s.slug for s in review_needed()}
    assert "aria-stt" in flagged
    assert all(get_spec(s).needs_review for s in flagged)


# --------------------------------------------------------------------------
# Addressing + manageability
# --------------------------------------------------------------------------


def test_every_spec_has_exactly_one_addressing_mode():
    for spec in REGISTRY:
        modes = [
            bool(spec.user_unit),
            bool(spec.system_unit),
            bool(spec.container_name),
        ]
        assert sum(modes) == 1, (
            f"{spec.slug} must have exactly one of user_unit/system_unit/"
            f"container_name, got {sum(modes)}"
        )


@pytest.mark.asyncio
async def test_darwin_uses_launchd_instead_of_linux_service_manager():
    from aria.infrastructure import services

    spec = get_spec("aria-api")
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return 0, "state = running\n", ""

    with patch.object(services.sys, "platform", "darwin"), patch.object(
        services, "_run", fake_run
    ):
        state = await services._state_of(spec)

    assert state == "running"
    assert calls == [("launchctl", "print", "system/com.ben.devbox.aria-api")]


@pytest.mark.asyncio
async def test_darwin_linux_only_service_is_not_applicable():
    from aria.infrastructure import services

    spec = get_spec("samba")
    with patch.object(services.sys, "platform", "darwin"):
        assert await services._state_of(spec) == "not_applicable"


def test_darwin_rows_report_native_handle_and_no_linux_mutation():
    from aria.infrastructure import services

    spec = get_spec("aria-ui")
    with patch.object(services.sys, "platform", "darwin"):
        row = services._row_for(spec, "running")

    assert row["unit"] == "com.ben.devbox.aria-ui"
    assert row["container"] is None
    assert row["compose_file"] is None
    assert row["manageable"] is False


def test_slugs_are_unique():
    slugs = [s.slug for s in REGISTRY]
    assert len(slugs) == len(set(slugs))


def test_unknown_slug_raises():
    with pytest.raises(ServiceNotFound):
        get_spec("no-such-service")


@pytest.mark.parametrize("slug", ["aria-api", "aria-tmux", "samba"])
async def test_unmanageable_services_refuse_start_and_stop(slug):
    """aria-api would be restarting itself from inside its own request handler;
    aria-tmux owns the tmux server whose death takes every watched session with
    it; samba is a system unit ARIA has no root for."""
    manager = ServiceManager()
    with pytest.raises(ServiceNotManageable):
        await manager.start(slug)
    with pytest.raises(ServiceNotManageable):
        await manager.stop(slug)


def test_aria_tmux_is_not_manageable():
    """Explicit: restarting this from ARIA can orphan the tmux server into
    aria-api's cgroup, and the next aria-api restart then kills every watched
    claude-* session. Documented in CLAUDE.md as a critical gotcha."""
    assert get_spec("aria-tmux").manageable is False


# ---------------------------------------------------------------------------
# Single-entity reads must not pay the full-fleet cost
# ---------------------------------------------------------------------------

def _counting_state_of(states: dict):
    """Patch _state_of with a counter; `states` maps slug -> state."""
    from unittest.mock import patch

    calls: list[str] = []

    async def fake(spec):
        calls.append(spec.slug)
        return states.get(spec.slug, "stopped")

    return patch("aria.infrastructure.services._state_of", fake), calls


@pytest.mark.asyncio
async def test_get_probes_only_the_requested_service():
    """get() used to run the full status() sweep — every service probed,
    every row upserted — to answer a question about one."""
    target = REGISTRY[0].slug
    states = {spec.slug: "running" for spec in REGISTRY}
    p, calls = _counting_state_of(states)
    with p:
        manager = ServiceManager()
        entry = await manager.get(target)
        assert calls == [target], f"get() probed {calls}, expected only {target}"
        # Same row shape as the full sweep (which probes everything).
        calls.clear()
        full = await manager.status()
        assert sorted(calls) == sorted(states)
    assert entry["slug"] == target
    assert entry["state"] == "running"
    full_row = next(r for r in full if r["slug"] == target)
    assert entry.keys() == full_row.keys()


@pytest.mark.asyncio
async def test_get_persists_only_its_own_row():
    """A single-entity read upserts only its own service_state row — the
    old code upserted the whole roster."""
    from unittest.mock import AsyncMock, MagicMock

    target = REGISTRY[0].slug
    p, calls = _counting_state_of({target: "running"})
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=MagicMock(update_one=AsyncMock()))
    with p:
        manager = ServiceManager()
        await manager.get(target, db)
    coll = db.__getitem__.return_value
    assert coll.update_one.call_count == 1
    query = coll.update_one.call_args.args[0]
    assert query["_id"] == target
