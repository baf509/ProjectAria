import copy
import importlib.util
import json
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[2] / "scripts/configure-pi-flashnext.py"
_spec = importlib.util.spec_from_file_location("pi_flashnext_cutover", _path)
cutover = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cutover)


def fixture():
    old = "Qwen3.8-Flash-Next-Hybrid-R9700-Halo"
    models = {"providers": {"aria": {"baseUrl": cutover.BASE_URL, "apiKey": "private-fixture",
        "models": [{"id": old, "compat": {"other": True}},
                   {"id": "Red-Qwen3.8-27B-MXFP4", "maxTokens": 16384}]}}}
    settings = {"defaultProvider": "aria", "defaultModel": old,
                "enabledModels": [old, "aria/" + old, "aria/Red-Qwen3.8-27B-MXFP4"],
                "compaction": {"enabled": True, "reserveTokens": 167144, "keepRecentTokens": 20000}}
    return models, settings


def test_scoped_migration_preserves_credentials_red_and_inputs():
    models, settings = fixture()
    original = copy.deepcopy((models, settings))
    new_models, new_settings = cutover.transform(models, settings)
    assert (models, settings) == original
    provider = new_models["providers"]["aria"]
    assert provider["apiKey"] == "private-fixture"
    assert provider["models"][0] == models["providers"]["aria"]["models"][1]
    selected = provider["models"][1]
    assert selected["id"] == cutover.MODEL
    assert (selected["contextWindow"], selected["maxTokens"]) == (262144, 32768)
    assert selected["compat"]["thinkingFormat"] == "chat-template"
    assert selected["compat"]["chatTemplateKwargs"]["enable_thinking"] == {"$var": "thinking.enabled"}
    assert new_settings["defaultModel"] == cutover.MODEL
    assert new_settings["enabledModels"] == ["aria/" + cutover.MODEL, "aria/Red-Qwen3.8-27B-MXFP4"]
    assert new_settings["compaction"] == settings["compaction"]
    assert cutover.transform(new_models, new_settings) == (new_models, new_settings)


def test_explicit_red_default_is_preserved():
    models, settings = fixture()
    settings["defaultModel"] = "Red-Qwen3.8-27B-MXFP4"
    assert cutover.transform(models, settings)[1]["defaultModel"] == settings["defaultModel"]


def test_remaining_corsair_radiance_reference_is_retired_but_red_is_preserved():
    models, settings = fixture()
    old = "Qwen3.8-27B-R9700-Radiance"
    models["providers"]["aria"]["models"].append({"id": old})
    settings["defaultModel"] = old
    settings["enabledModels"].append("aria/" + old)
    updated, preferences = cutover.transform(models, settings)
    assert {row["id"] for row in updated["providers"]["aria"]["models"]} == {
        cutover.MODEL, "Red-Qwen3.8-27B-MXFP4"}
    assert preferences["defaultModel"] == cutover.MODEL
    assert preferences["enabledModels"] == ["aria/" + cutover.MODEL, "aria/Red-Qwen3.8-27B-MXFP4"]


@pytest.mark.parametrize("change", ["endpoint", "duplicates", "compaction", "missing"])
def test_unknown_contract_refuses(change):
    models, settings = fixture()
    provider = models["providers"]["aria"]
    if change == "endpoint":
        provider["baseUrl"] = "http://corsair-ai:8131/v1"
    elif change == "duplicates":
        provider["models"].append(provider["models"][0])
    elif change == "compaction":
        settings["compaction"]["reserveTokens"] = 100
    else:
        provider["models"].pop(0)
    with pytest.raises(ValueError):
        cutover.transform(models, settings)


def test_private_verified_backup_and_drift_refusal(tmp_path):
    paths = tuple(tmp_path / name for name in ("models.json", "settings.json"))
    before = (b'{"original": 1}', b'{"original": 2}')
    after = (b'{"selected": 1}', b'{"selected": 2}')
    for p, data in zip(paths, before):
        p.write_bytes(data)
        p.chmod(0o600)
    backup = cutover.install_pair(paths, before, after)
    assert tuple(p.read_bytes() for p in paths) == after
    assert tuple((backup / p.name).read_bytes() for p in paths) == before
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in paths)
    with pytest.raises(ValueError, match="changed"):
        cutover.install_pair(paths, before, after)
    assert tuple(p.read_bytes() for p in paths) == after


def test_credential_installer_accepts_both_exact_managed_inventories():
    path = _path.with_name("configure-pi-aria-key.py")
    spec = importlib.util.spec_from_file_location("pi_key_cutover_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for ids in (module.APPROVED_MODELS, module.CURRENT_MODELS):
        provider = {"models": [{"id": name} for name in ids]}
        assert module.approved_inventory(provider)
        assert not module.approved_inventory({"models": provider["models"] + provider["models"][:1]})
    assert not module.approved_inventory({"models": [{"id": "unknown"}]})
    assert not module.approved_inventory({"models": [None]})


def test_removed_corsair_hardware_cannot_be_started_or_auto_routed():
    from aria.infrastructure.model_servers import ModelServerManager
    manager = ModelServerManager()
    for slug in ("Qwen3.8-Flash-Next-Hybrid-R9700-Halo", "Qwen3.8-27B-R9700-Radiance",
                 "Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K"):
        spec = manager.get_spec(slug)
        assert not spec.startable and not spec.allow_force_start and not spec.auto_route
        assert "Retired Corsair hardware" in spec.not_startable_reason


def test_independent_red_is_not_a_candidate_exclusivity_conflict():
    from aria.infrastructure.model_servers import ModelServerManager
    manager = ModelServerManager()
    candidate = manager.get_spec(cutover.MODEL)
    # Red's auto-routing deployment is PARO-INT5 since the 2026-09-15 cutover;
    # the original MXFP4 deployment is an explicit alternative.
    red = manager.get_spec("Red-Qwen3.8-27B-PARO-INT5")
    assert not red.onbox and red.startable and red.auto_route
    # Red is separate hardware: no Red deployment, default or alternative, may
    # be recorded as conflicting with the Corsair candidate.
    for slug in ("Red-Qwen3.8-27B-PARO-INT5", "Red-Qwen3.8-27B-MXFP4",
                 "Red-Qwen3.8-Flash-Next-MXFP4", "Red-Qwen3.8-27B-PARO-MXFP4"):
        spec = manager.get_spec(slug)
        assert not spec.onbox
        assert slug not in candidate.exclusive_with
        assert candidate.slug not in spec.exclusive_with
