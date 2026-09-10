"""The experimental engine is observable without becoming a production loadout."""

from unittest.mock import AsyncMock

import pytest

from aria.infrastructure import model_servers as ms
from aria.infrastructure.llm_route import is_servable, select


ENGINE = "Qwen3.8-Flash-Next-Engine-R9700-Halo"
PRODUCTION = "Qwen3.8-Flash-Next-Hybrid-R9700-Halo"


def test_experimental_engine_claims_both_pools_and_all_corsair_gpu_conflicts():
    specs = {spec.slug: spec for spec in ms.REGISTRY}
    engine = specs[ENGINE]
    assert engine.memory_pool == ms.POOL_HALO
    assert engine.also_uses == (ms.POOL_R9700,)
    for spec in ms.REGISTRY:
        if spec.slug == ENGINE:
            continue
        if spec.onbox and spec.memory_pool in (ms.POOL_HALO, ms.POOL_R9700):
            assert spec.slug in engine.exclusive_with
            assert ENGINE in spec.exclusive_with
        else:
            assert spec.slug not in engine.exclusive_with
    assert len(engine.exclusive_with) == len(set(engine.exclusive_with))
    assert engine.model_file == specs[PRODUCTION].model_file
    assert engine.resident_gib == specs[PRODUCTION].resident_gib
    assert engine.bench_decode_tok_s is None
    assert engine.bench_prefill_tok_s is None
    assert engine.bench_at is None
    assert engine.auto_route is False
    # Other engineering/retired deployments are now ineligible too. In
    # particular, the historical production control needs the removed R9700.
    assert specs[PRODUCTION].auto_route is False
    assert specs["Red-Qwen3.8-27B-MXFP4"].auto_route is True


def test_experimental_launcher_identity_and_fixed_geometry_are_explicit():
    engine = ms.ModelServerManager().get_spec(ENGINE)
    assert engine.deployment == "flashnext-engine"
    assert engine.launch_script == "flashnext-engine/serve.sh"
    assert engine.systemd_unit == "flashnext-engine.service"
    assert engine.port == 8122
    assert engine.runtime_family == "llamacpp"
    assert engine.ctx_is_total is True
    params = {param.name: param for param in engine.parameters}
    assert params['placement'].default == 'hybrid'
    for placement in ('hybrid', 'halo-only'):
        assert ms.validate_overrides(engine, {'placement': placement})['FLASHNEXT_PLACEMENT'] == placement
    for placement in ('ROCm0', 'cpu', 'halo'):
        with pytest.raises(ms.ModelServerSafetyError):
            ms.validate_overrides(engine, {'placement': placement})
    expected = {"ctx": "262144", "slots": "1", "layout": "0", "batch": "4096",
                "ubatch": "2048", "cache_ram_mib": "16384", "kv_type_k": "q8_0",
                "kv_type_v": "q8_0", "spec_draft_n_max": "3", "port": "8122"}
    for name, value in expected.items():
        assert params[name].default == value
        assert params[name].validate(value) == value
    # These are fixed by the launcher, so an apparently accepted override
    # must not promise a geometry that will not actually run.
    for override in ({"ctx": "131072"}, {"slots": "2"}, {"port": "8121"}, {"layout": "10"}):
        with pytest.raises(ms.ModelServerSafetyError):
            ms.validate_overrides(engine, override)


def test_experimental_feature_flags_are_off_and_validate_allowed_cells():
    engine = ms.ModelServerManager().get_spec(ENGINE)
    params = {param.name: param for param in engine.parameters}
    assert params["qsa_indexed_prefill"].default == "0"
    assert params["gdn_chunked_prefill"].default == "0"
    assert params["moe_prefill"].default == "0"
    assert params["moe_compact"].default == "0"
    for value in ("0", "1", "16", "32", "48", "64", "128"):
        assert ms.validate_overrides(engine, {"moe_compact": value})["FLASHNEXT_MOE_COMPACT"] == value
    for value in ("2", "032", "-1", "yes"):
        with pytest.raises(ms.ModelServerSafetyError):
            ms.validate_overrides(engine, {"moe_compact": value})
    assert params["qsa_min_kv"].default == "32768"
    for value in ("0", "8192", "16384", "32768", "65536"):
        assert ms.validate_overrides(engine, {"qsa_min_kv": value})["FLASHNEXT_QSA_MIN_KV"] == value
    for value in ("032768", "-1", "1", "1000000000"):
        with pytest.raises(ms.ModelServerSafetyError):
            ms.validate_overrides(engine, {"qsa_min_kv": value})
    for value in ("0", "1", "2", "3", "4", "5", "6"):
        assert ms.validate_overrides(engine, {"moe_prefill": value})["FLASHNEXT_MOE_PREFILL"] == value
    for value in ("7", "01", "-1", "yes"):
        with pytest.raises(ms.ModelServerSafetyError):
            ms.validate_overrides(engine, {"moe_prefill": value})
    for name, key in (("qsa_head_fast", "FLASHNEXT_QSA_HEAD_FAST"),
                      ("moe_prefetch", "FLASHNEXT_MOE_PREFETCH"),
                      ("gdn_direct_reduce", "FLASHNEXT_GDN_DIRECT_REDUCE"),
                      ("qsa_wmma_prefill", "FLASHNEXT_QSA_WMMA_PREFILL"),
                      ("qsa_wmma_parallel_softmax", "FLASHNEXT_QSA_WMMA_PARALLEL_SOFTMAX")):
        assert params[name].default == "0"
        for value in ("0", "1"):
            assert ms.validate_overrides(engine, {name: value})[key] == value
        with pytest.raises(ms.ModelServerSafetyError):
            ms.validate_overrides(engine, {name: "2"})
    for qsa in ("0", "1"):
        for gdn in ("0", "128", "512"):
            env = ms.validate_overrides(engine, {"qsa_indexed_prefill": qsa, "gdn_chunked_prefill": gdn})
            assert env["FLASHNEXT_QSA_INDEXED_PREFILL"] == qsa
            assert env["FLASHNEXT_GDN_CHUNKED_PREFILL"] == gdn
    for override in ({"qsa_indexed_prefill": "2"}, {"gdn_chunked_prefill": "256"}):
        with pytest.raises(ms.ModelServerSafetyError):
            ms.validate_overrides(engine, override)


@pytest.mark.asyncio
async def test_experimental_start_is_blocked_before_any_process_mutation(monkeypatch):
    monkeypatch.setattr(ms, "_corsair_forward_mode", lambda: False)
    run = AsyncMock(side_effect=AssertionError("unqualified engine must not launch"))
    monkeypatch.setattr(ms, "_run", run)
    engine = ms.ModelServerManager().get_spec(ENGINE)
    assert engine.startable is False
    # Two gates stack here, and both must stay visible: the Corsair-loadout
    # retirement prefix (every onbox spec outside CURRENT_MODEL_CHOICES gets it)
    # and this engine's own qualification gate. Asserting the whole string meant
    # the retirement prefix silently broke this test when it was added.
    assert engine.not_startable_reason.endswith(
        "Experimental runtime awaiting hardware and model qualification")
    assert "Not a current Corsair option" in engine.not_startable_reason
    with pytest.raises(ms.ModelServerSafetyError, match="awaiting hardware and model qualification"):
        await ms.ModelServerManager().start(ENGINE)
    run.assert_not_called()


def test_explicit_experimental_routing_does_not_change_production_pin():
    # A test process may be observed as running while normal starts are gated.
    # Existing pinned consumers must retain the qualified production choice.
    servers = [{"slug": slug, "state": "running", "onbox": True, "port": port,
                "endpoints": {"local": f"http://127.0.0.1:{port}/v1"}, "resident_gib_estimate": 82,
                "auto_route": slug != ENGINE}
               for slug, port in ((PRODUCTION, 8121), (ENGINE, 8122))]
    selected, _, unavailable = select(servers, requested="aria-resident", pin=PRODUCTION)
    assert selected["slug"] == PRODUCTION
    assert unavailable is False
    selected, _, _ = select(servers, requested=ENGINE, pin=PRODUCTION)
    assert selected["slug"] == ENGINE


@pytest.mark.parametrize("requested", [None, "", "aria", "auto", "aria-resident", "unrecognized-client-model"])
@pytest.mark.parametrize("pin", [None, "removed-production-pin", PRODUCTION])
def test_auto_fallback_never_selects_experimental_resident(requested, pin):
    candidate = {"slug": ENGINE, "state": "running", "onbox": True, "port": 8122,
                 "endpoints": {"local": "http://127.0.0.1:8122/v1"},
                 "resident_gib_estimate": 120, "auto_route": False, "startable": False}
    stopped_control = {"slug": PRODUCTION, "state": "exited", "onbox": True, "port": 8121,
                       "endpoints": {"local": "http://127.0.0.1:8121/v1"}, "auto_route": True}
    assert is_servable(candidate)
    chosen, _, unavailable = select([candidate, stopped_control], requested=requested, pin=pin)
    assert chosen is None
    assert unavailable is False
    # An available ordinary auxiliary remains the fallback despite being much smaller.
    auxiliary = {"slug": "auxiliary", "state": "running", "onbox": True, "port": 8104,
                 "endpoints": {"local": "http://127.0.0.1:8104/v1"}, "resident_gib_estimate": 4}
    chosen, _, _ = select([candidate, stopped_control, auxiliary], requested=requested, pin=pin)
    assert chosen["slug"] == "auxiliary"


def test_disabled_auto_route_preserves_explicit_request_and_deliberate_pin():
    candidate = {"slug": ENGINE, "state": "running", "onbox": True, "port": 8122,
                 "endpoints": {"local": "http://127.0.0.1:8122/v1"},
                 "auto_route": False, "startable": False}
    chosen, reason, unavailable = select([candidate], requested=ENGINE)
    assert chosen == candidate and not unavailable
    assert "requested by caller" in reason
    chosen, reason, unavailable = select([candidate], requested="aria-resident", pin=ENGINE)
    assert chosen == candidate and not unavailable
    assert "pinned in ARIA" in reason


def test_running_deployment_with_disabled_launcher_retains_default_auto_behavior():
    retained = {"slug": "retained-runtime", "state": "running", "onbox": True, "port": 8123,
                "endpoints": {"local": "http://127.0.0.1:8123/v1"}, "startable": False}
    selected, _, unavailable = select([retained], requested="aria-resident")
    assert selected == retained and not unavailable


@pytest.mark.asyncio
async def test_full_status_and_light_summary_preserve_auto_route_policy(monkeypatch):
    manager = ms.ModelServerManager()
    specs = (manager.get_spec(PRODUCTION), manager.get_spec(ENGINE))
    monkeypatch.setattr(ms, "REGISTRY", specs)
    monkeypatch.setattr(ms, "_corsair_forward_mode", lambda: False)
    monkeypatch.setattr(ms, "_run", AsyncMock(return_value=(0,
        "qwen3.8-flash-next-hybrid.service\nflashnext-engine.service\n", "")))
    geometry = ms.LaunchGeometry(n_ctx=262144, slots=1, source="test")
    monkeypatch.setattr(ms, "read_launch_geometry", lambda _spec: geometry)
    monkeypatch.setattr(ms, "resolve_parameters", lambda _spec: [])
    monkeypatch.setattr(ms, "read_aria_overrides", lambda _spec: {})
    summaries = {row["slug"]: row for row in await manager.running_summary()}
    for spec in specs:
        full = ms._server_row(spec, "running", geometry, {}, None, {}, {}, {})
        assert full["auto_route"] is summaries[spec.slug]["auto_route"] is spec.auto_route
        assert summaries[spec.slug]["state"] == "running"


def test_dynamic_models_default_to_eligible_but_can_explicitly_opt_out():
    manager = ms.ModelServerManager()
    assert manager._spec_from_doc({"slug": "dynamic-default"}).auto_route is True
    assert manager._spec_from_doc({"slug": "dynamic-experiment", "auto_route": False}).auto_route is False
