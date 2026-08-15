"""Tests for choosing HOW a model loads — launch parameters, the systemd
drop-in that carries them, ARIA-materialised units, and the per-device memory
pools that decide whether two models can be resident at once.

The sysfs tree and systemd's unit directory are both faked into tmp_path; no
test here touches the real ~/.config/systemd/user or /sys.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from aria.infrastructure import gpu_devices as gd
from aria.infrastructure import model_servers as ms
from aria.infrastructure.model_servers import (
    LaunchParam,
    ModelServerManager,
    ModelServerSafetyError,
)


# ───────────────────────────────────────────────────── parameter validation ──

def test_int_param_rejects_non_numeric():
    param = LaunchParam(name="ctx", env="CTX", label="Context", kind="int")
    assert param.validate("131072") == "131072"
    with pytest.raises(ModelServerSafetyError, match="positive integer"):
        param.validate("131072; rm -rf /")


def test_enum_param_rejects_undeclared_value():
    param = LaunchParam(
        name="placement", env="PLACEMENT", label="Placement", kind="enum",
        choices=(("split", ""), ("hybrid", "")),
    )
    assert param.validate("hybrid") == "hybrid"
    with pytest.raises(ModelServerSafetyError, match="must be one of"):
        param.validate("both")


def test_path_param_requires_an_existing_absolute_path(tmp_path):
    param = LaunchParam(name="draft", env="DRAFT", label="Drafter", kind="path")
    gguf = tmp_path / "draft.gguf"
    gguf.write_text("x")
    assert param.validate(str(gguf)) == str(gguf)
    # 'none' is the documented sentinel for "no speculation" and must survive.
    assert param.validate("none") == "none"
    with pytest.raises(ModelServerSafetyError, match="no such file"):
        param.validate(str(tmp_path / "missing.gguf"))
    with pytest.raises(ModelServerSafetyError, match="absolute path"):
        param.validate("relative/path.gguf")


def test_free_form_param_is_an_allowlist_not_an_escape():
    """These values land in a systemd Environment= line read by a shell script,
    so anything that could break out of one is rejected rather than quoted."""
    param = LaunchParam(name="alias", env="ALIAS", label="Alias")
    assert param.validate("ds4-halo") == "ds4-halo"
    for hostile in ("a b", "a$(id)", "a;b", "a\nEnvironment=X=y", "a`id`"):
        with pytest.raises(ModelServerSafetyError):
            param.validate(hostile)


def test_overrides_refused_for_a_server_with_no_parameters():
    """A compose-frozen server must say so, not silently ignore the request."""
    spec = ms._BY_SLUG["Qwen3.8-27B-Q6_K-R9700-Vulkan-MTP"]
    assert spec.parameters == ()
    with pytest.raises(ModelServerSafetyError, match="no selectable launch parameters"):
        ms.validate_overrides(spec, {"ctx": "65536"})


def test_unknown_override_names_are_rejected_with_the_available_set():
    spec = ms._BY_SLUG["DS4-0731-IQ3_S-Hybrid-ROCm-Dual"]
    with pytest.raises(ModelServerSafetyError, match="unknown parameter"):
        ms.validate_overrides(spec, {"kv": "q8_0"})  # halo-only knob, not this one


def test_validate_overrides_maps_names_to_env_vars():
    spec = ms._BY_SLUG["DS4-0731-IQ3_S-Hybrid-ROCm-Dual"]
    assert ms.validate_overrides(spec, {"placement": "hybrid", "ctx": "32768"}) == {
        "PLACEMENT": "hybrid",
        "CTX": "32768",
    }


def test_empty_overrides_is_not_an_error():
    spec = ms._BY_SLUG["Qwen3.8-27B-Q6_K-R9700-Vulkan-MTP"]
    assert ms.validate_overrides(spec, None) == {}
    assert ms.validate_overrides(spec, {}) == {}


# ──────────────────────────────────────────── drop-ins and generated units ──

@pytest.fixture
def fake_systemd(tmp_path, monkeypatch):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    monkeypatch.setattr(ms, "_SYSTEMD_USER_DIR", str(unit_dir))
    return unit_dir


def _spec_with_script(tmp_path, **kw):
    script = tmp_path / "serve.sh"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'PORT="${PORT:-8108}"; CTX="${CTX:-65536}"\n'
        'KV="${KV:-f16}"\n'
        'exec llama-server -m model.gguf -c "$CTX" -np 1 --port "$PORT"\n'
    )
    defaults = dict(
        slug="test-server",
        description="",
        runtime_repo="",
        runtime_ref="",
        backend_device="",
        launch_script=str(script),
        parameters=(
            LaunchParam(name="ctx", env="CTX", label="Context", kind="int"),
            LaunchParam(
                name="kv", env="KV", label="KV", kind="enum",
                choices=(("f16", ""), ("q8_0", "")),
            ),
        ),
        ctx_param="ctx",
    )
    defaults.update(kw)
    return ms.ModelServerSpec(**defaults)


def test_unit_name_is_generated_only_for_script_deployments(tmp_path):
    scripted = _spec_with_script(tmp_path)
    assert ms.unit_name(scripted) == "aria-model-test-server.service"
    # A deployment with its own hand-written unit keeps it.
    hand_written = _spec_with_script(tmp_path, systemd_unit="ds4-halo-xxs.service")
    assert ms.unit_name(hand_written) == "ds4-halo-xxs.service"
    # Compose-only entries have no unit at all.
    assert ms.unit_name(ms._BY_SLUG["gemma-4-e4b-Q4"]) is None


def test_script_defaults_are_read_from_the_script_not_declared(tmp_path):
    spec = _spec_with_script(tmp_path)
    assert ms._script_defaults(spec) == {"PORT": "8108", "CTX": "65536", "KV": "f16"}


def test_script_literals_supply_geometry_the_execstart_cannot(tmp_path, fake_systemd):
    """serve.sh pins `-np 1` as a literal; `-c "$CTX"` is not resolvable there
    and must come from the parameter layer instead."""
    spec = _spec_with_script(tmp_path)
    geometry = ms.read_launch_geometry(spec)
    assert geometry.slots == 1          # literal in the script
    assert geometry.n_ctx == 65536      # from the script's CTX default


def test_parameter_precedence_aria_override_beats_dropin_beats_script(tmp_path, fake_systemd):
    spec = _spec_with_script(tmp_path)
    unit = ms.unit_name(spec)
    dropin_dir = fake_systemd / f"{unit}.d"
    dropin_dir.mkdir()
    (dropin_dir / "10-hand.conf").write_text("[Service]\nEnvironment=CTX=99999\n")

    by_name = {p["name"]: p for p in ms.resolve_parameters(spec)}
    assert (by_name["ctx"]["value"], by_name["ctx"]["source"]) == ("99999", "unit_dropin")
    assert (by_name["kv"]["value"], by_name["kv"]["source"]) == ("f16", "script_default")

    (dropin_dir / ms._ARIA_DROPIN_NAME).write_text(
        "[Service]\nEnvironment=CTX=131072\n"
    )
    by_name = {p["name"]: p for p in ms.resolve_parameters(spec)}
    assert (by_name["ctx"]["value"], by_name["ctx"]["source"]) == ("131072", "aria_override")
    # A knob ARIA did not set stays attributed to where it really came from.
    assert by_name["kv"]["source"] == "script_default"


def test_aria_dropin_sorts_after_hand_written_ones():
    """systemd applies drop-ins in lexical order, later winning. ARIA's file
    must sort last or a hand-written 10-*.conf would override the choice the
    operator just made."""
    assert ms._ARIA_DROPIN_NAME > "99-anything.conf"
    assert ms._ARIA_DROPIN_NAME.endswith(".conf")


@pytest.mark.asyncio
async def test_start_writes_overrides_then_clears_them_on_a_plain_start(
    tmp_path, fake_systemd
):
    """A previous session's context size must not silently outlive it."""
    spec = _spec_with_script(tmp_path)
    manager = ModelServerManager()
    unit = ms.unit_name(spec)
    dropin = fake_systemd / f"{unit}.d" / ms._ARIA_DROPIN_NAME

    calls: list[tuple] = []

    async def fake_run(*args):
        calls.append(args)
        if args[:3] == ("systemctl", "--user", "list-unit-files"):
            return (0, "", "")  # unit not registered yet -> "not_created"/"ready"
        return (0, "", "")

    with patch.dict(ms._BY_SLUG, {spec.slug: spec}), \
         patch.object(ms, "_run", fake_run), \
         patch.object(ms, "_read_gtt_gib", return_value=(1.0, 124.0)):
        result = await manager.start(spec.slug, overrides={"kv": "q8_0", "ctx": "131072"})
        assert result["action"] == "started"
        assert dropin.exists()
        assert "Environment=KV=q8_0" in dropin.read_text()
        assert result["launch_config"]["ctx"]["source"] == "aria_override"

        await manager.start(spec.slug)

    assert not dropin.exists(), "a plain start must clear ARIA's own overrides"
    assert any(a[:3] == ("systemctl", "--user", "daemon-reload") for a in calls)


@pytest.mark.asyncio
async def test_start_materialises_a_unit_for_a_script_only_deployment(
    tmp_path, fake_systemd
):
    spec = _spec_with_script(
        tmp_path,
        unit_environment=(("DS4_MIN_RUN_KIB", "12582912"),),
        unit_exec_start_pre=("/usr/bin/bash -c 'test 1 = 1'",),
    )
    manager = ModelServerManager()

    async def fake_run(*args):
        return (0, "", "")

    with patch.dict(ms._BY_SLUG, {spec.slug: spec}), \
         patch.object(ms, "_run", fake_run), \
         patch.object(ms, "_read_gtt_gib", return_value=(1.0, 124.0)):
        await manager.start(spec.slug)

    unit_file = fake_systemd / ms.unit_name(spec)
    body = unit_file.read_text()
    # The guards are the point: a hand-rolled command line would drop all of them.
    assert "Environment=DS4_MIN_RUN_KIB=12582912" in body
    assert "ExecStartPre=/usr/bin/bash -c 'test 1 = 1'" in body
    assert "OOMScoreAdjust=900" in body
    assert "Restart=no" in body
    assert body.startswith(ms._ARIA_UNIT_MARKER)


def test_invalid_override_leaves_no_partial_state(tmp_path, fake_systemd):
    """Validation happens before any file is touched."""
    spec = _spec_with_script(tmp_path)
    with pytest.raises(ModelServerSafetyError):
        ms.validate_overrides(spec, {"kv": "q3_k"})
    assert not (fake_systemd / f"{ms.unit_name(spec)}.d").exists()


# ─────────────────────────────────────────────── devices and memory pools ──

def _fake_drm(tmp_path, cards: dict[str, dict]) -> str:
    root = tmp_path / "drm"
    root.mkdir()
    for name, values in cards.items():
        device = root / name / "device"
        device.mkdir(parents=True)
        for key, value in values.items():
            (device / key).write_text(str(value))
    return str(root)


_GIB = 1024**3


def test_discrete_and_integrated_cards_are_classified_by_vram_not_by_order(
    tmp_path, monkeypatch
):
    """card0 is the DISCRETE R9700 on this box and card1 the Strix Halo iGPU.
    Classifying by enumeration order — as the old hardcoded card0 read did —
    reports the dGPU's near-empty pool while the Halo holds ~100 GiB."""
    monkeypatch.setattr(gd, "_DRM_ROOT", _fake_drm(tmp_path, {
        "card0": {  # R9700
            "mem_info_vram_total": 32 * _GIB, "mem_info_vram_used": 21 * _GIB,
            "mem_info_gtt_total": 124 * _GIB, "mem_info_gtt_used": 0,
        },
        "card1": {  # Strix Halo
            "mem_info_vram_total": 1 * _GIB, "mem_info_vram_used": 0,
            "mem_info_gtt_total": 124 * _GIB, "mem_info_gtt_used": 97 * _GIB,
        },
    }))
    by_card = {d.card: d for d in gd.discover_devices()}
    assert by_card["card0"].discrete is True
    assert by_card["card0"].pool == gd.POOL_R9700
    assert by_card["card1"].discrete is False
    assert by_card["card1"].pool == gd.POOL_HALO

    halo = gd.read_pool(gd.POOL_HALO)
    assert halo.used_gib == 97.0 and halo.free_gib == 27.0
    r9700 = gd.read_pool(gd.POOL_R9700)
    assert r9700.used_gib == 21.0 and r9700.total_gib == 31.86 or r9700.total_gib == 32.0


def test_discrete_card_holding_gtt_is_reported_as_spilling(tmp_path, monkeypatch):
    """A dGPU serving out of system RAM has stopped being an independent pool —
    it is now competing with the Halo, which is why `-fit off` is mandatory."""
    monkeypatch.setattr(gd, "_DRM_ROOT", _fake_drm(tmp_path, {
        "card0": {
            "mem_info_vram_total": 32 * _GIB, "mem_info_vram_used": 31 * _GIB,
            "mem_info_gtt_total": 124 * _GIB, "mem_info_gtt_used": 40 * _GIB,
        },
    }))
    assert gd.read_pool(gd.POOL_R9700).spilling is True


def test_unreadable_pool_is_unknown_not_empty(tmp_path, monkeypatch):
    """None must never be read as free space by the gate."""
    monkeypatch.setattr(gd, "_DRM_ROOT", str(tmp_path / "does-not-exist"))
    assert gd.read_pool(gd.POOL_R9700) is None
    assert gd.read_pool(gd.POOL_HALO) is None
    assert ms._read_gtt_gib(gd.POOL_R9700) is None


def test_single_gpu_box_still_gets_a_halo_pool(tmp_path, monkeypatch):
    """Degrade to the biggest GTT aperture rather than disabling the gate."""
    monkeypatch.setattr(gd, "_DRM_ROOT", _fake_drm(tmp_path, {
        "card0": {
            "mem_info_vram_total": 32 * _GIB, "mem_info_vram_used": 0,
            "mem_info_gtt_total": 124 * _GIB, "mem_info_gtt_used": 8 * _GIB,
        },
    }))
    pool = gd.read_pool(gd.POOL_HALO)
    assert pool is not None and pool.used_gib == 8.0


def test_every_onbox_spec_names_a_real_pool():
    valid = {gd.POOL_HALO, gd.POOL_R9700, gd.POOL_HOST, gd.POOL_REMOTE}
    for spec in ms.REGISTRY:
        assert spec.memory_pool in valid, f"{spec.slug} has pool {spec.memory_pool}"
        for pool in spec.also_uses:
            assert pool in valid


# ────────────────────────────────────────── cross-pool residency and ports ──

def test_models_on_different_cards_are_not_mutually_exclusive():
    """The whole point of the two-GPU topology: DS4 on the Halo and Qwen3.8 on
    the R9700 were verified resident together on 2026-08-14. Forbidding that
    would break the deployment Hermes and pi are currently wired to."""
    halo = ms._BY_SLUG["DS4-0731-IQ3_XXS-Halo-Vulkan"]
    dgpu = ms._BY_SLUG["Qwen3.8-27B-R9700-HIP"]
    assert dgpu.slug not in halo.exclusive_with
    assert halo.slug not in dgpu.exclusive_with
    assert halo.memory_pool != dgpu.memory_pool


def test_the_dual_device_split_conflicts_with_both_pools():
    """ds4-hybrid is the one deployment spanning both cards, so it is the one
    Halo entry that must also conflict with every dGPU resident."""
    hybrid = ms._BY_SLUG["DS4-0731-IQ3_S-Hybrid-ROCm-Dual"]
    assert "Qwen3.8-27B-R9700-HIP" in hybrid.exclusive_with
    assert "DS4-0731-IQ3_XXS-Halo-Vulkan" in hybrid.exclusive_with
    assert hybrid.memory_pool == gd.POOL_HALO
    assert gd.POOL_R9700 in hybrid.also_uses


def test_halo_resident_models_are_mutually_exclusive():
    halo_entries = [
        "DS4-0731-IQ3_XXS-Halo-Vulkan",
        "DS4-0731-ROCmFPX-Affine-Quality",
        "DS4-0731-IQ3_S-Hybrid-ROCm-Dual",
    ]
    for slug in halo_entries:
        others = set(ms._BY_SLUG[slug].exclusive_with)
        assert others.issuperset(set(halo_entries) - {slug})


@pytest.mark.asyncio
async def test_start_refuses_when_the_port_is_already_held():
    """Two servers can be in different memory pools and still collide on a
    port — the three :8110 Qwen variants and the DS4s on :8107 do exactly that."""
    manager = ModelServerManager()

    async def fake_run(*args):
        if args[:2] == ("docker", "inspect"):
            name = args[-1]
            if name == "qwen3.8-27b":
                return (0, "running|qwen3.8-27b\n", "")
            return (1, "", "Error: No such object: " + name)
        return (0, "", "")

    spec = ms._BY_SLUG["Qwen3.8-27B-ROCmFP4-R9700-Vulkan"]
    other = ms._BY_SLUG["Qwen3.8-27B-Q6_K-R9700-Vulkan-MTP"]
    assert spec.port == other.port  # the precondition this test exists for

    # Force past the (real) exclusivity pair so the PORT check is what refuses.
    trimmed = ms.ModelServerSpec(**{
        **{f.name: getattr(spec, f.name) for f in spec.__dataclass_fields__.values()},
        "exclusive_with": (),
    })
    with patch.dict(ms._BY_SLUG, {spec.slug: trimmed}), \
         patch.object(ms, "_run", fake_run), \
         patch.object(ms, "_read_gtt_gib", return_value=(1.0, 32.0)):
        with pytest.raises(ModelServerSafetyError, match="Port 8110"):
            await manager.start(spec.slug)


# ──────────────────────────────────────────────── registry health invariants ──

def test_unstartable_entries_always_explain_themselves():
    """`startable=False` is a record of what happened to a deployment, so it is
    useless without the reason — and several entries acquired the flag when the
    2026-08-11..14 consolidation moved their runtimes."""
    for spec in ms.REGISTRY:
        if not spec.startable:
            assert spec.not_startable_reason, f"{spec.slug} is unstartable with no reason"


def test_live_deployments_point_at_files_that_exist():
    """The failure this catches is exactly the one that made most of this
    registry stale: a path that moved without the entry following it."""
    live = [s for s in ms.REGISTRY if s.startable and s.onbox and s.launch_script]
    assert live, "expected at least one script-launched deployment"
    for spec in live:
        script = ms._abs_infra(spec.launch_script)
        assert os.path.exists(script), f"{spec.slug}: missing launch script {script}"
        if spec.model_file:
            model = ms._abs_infra(spec.model_file)
            assert os.path.exists(model), f"{spec.slug}: missing model {model}"


def test_declared_parameter_defaults_are_valid_values():
    for spec in ms.REGISTRY:
        for param in spec.parameters:
            if param.default is None or param.kind == "path":
                continue  # path defaults are documented as repo-relative
            assert param.validate(param.default) == param.default, (
                f"{spec.slug}.{param.name}: default {param.default!r} is not valid"
            )
