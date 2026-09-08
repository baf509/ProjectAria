#!/usr/bin/env python3
"""Preview a scoped RTX3090/Halo Pi cutover; apply only with reviewed input hashes.

Does not start a model, alter ARIA routing, rotate credentials, or establish
qualification. Apply after qualification or explicit operator acceptance of
the runtime's known risks; preserve outstanding technical test results.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

MODEL = "Qwen3.8-Flash-Next-CUDA-Halo-Candidate"
RETIRED = {
    "Qwen3.8-Flash-Next-Hybrid-R9700-Halo",
    "Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K",
    "Qwen3.8-27B-R9700-Radiance",
}
BASE_URL = "http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def transform(models, settings):
    models, settings = copy.deepcopy(models), copy.deepcopy(settings)
    providers = models.get("providers", {})
    if set(providers) != {"aria"} or providers["aria"].get("baseUrl") != BASE_URL:
        raise ValueError("Expected the managed, identified ARIA provider")
    provider = providers["aria"]
    entries = provider.get("models", [])
    ids = [entry.get("id") for entry in entries]
    if not ids or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError("Missing or duplicate model inventory")
    template = next((entry for entry in entries if entry["id"] == MODEL), None)
    if template is None:
        template = next((entry for entry in entries if entry["id"] == "Qwen3.8-Flash-Next-Hybrid-R9700-Halo"), None)
    if template is None:
        raise ValueError("Expected an existing Flash Next contract to migrate")
    selected = copy.deepcopy(template)
    selected.update(id=MODEL, name="Qwen3.8 Flash Next — RTX 3090 + Strix Halo",
                    reasoning=True, contextWindow=262144, maxTokens=32768)
    selected.setdefault("compat", {}).update(
        thinkingFormat="chat-template", supportsDeveloperRole=False,
        supportsReasoningEffort=False,
        chatTemplateKwargs={"enable_thinking": {"$var": "thinking.enabled"},
                            "reasoning_effort": {"$var": "thinking.effort"}})
    selected["thinkingLevelMap"] = {
        "minimal": None, "low": "low", "medium": "medium", "high": "high",
        "xhigh": None, "max": None,
    }
    selected.setdefault("samplingParams", {})["temperature"] = 0.0
    # Preserve every unrelated model, including its output budget and metadata.
    provider["models"] = [entry for entry in entries if entry["id"] not in RETIRED | {MODEL}] + [selected]
    if settings.get("defaultProvider") == "aria" and settings.get("defaultModel") in RETIRED:
        settings["defaultModel"] = MODEL
    enabled = settings.get("enabledModels")
    if enabled is not None:
        if not isinstance(enabled, list) or any(not isinstance(x, str) for x in enabled):
            raise ValueError("Expected a string enabledModels list")
        renamed = ["aria/" + MODEL if x in RETIRED | {"aria/" + old for old in RETIRED} else x for x in enabled]
        settings["enabledModels"] = list(dict.fromkeys(renamed))
    # A global compaction setting affects Red as well. Preserve the already
    # managed 95K policy; refuse an unknown policy instead of replacing it.
    compaction = settings.get("compaction", {})
    if (compaction.get("enabled") is not True or compaction.get("reserveTokens") != 167144
            or compaction.get("keepRecentTokens") != 20000):
        raise ValueError("Compaction policy differs from the reviewed 95K/20K contract")
    return models, settings


def read_regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Configuration must be a regular non-symlink file")
    return path.read_bytes()


def install_pair(paths, before, after):
    """Verified private backups and per-file drift checks; not a cross-writer lock."""
    if tuple(read_regular(p) for p in paths) != before:
        raise ValueError("Configuration changed; nothing installed")
    backup = Path(tempfile.mkdtemp(prefix="flashnext-cutover-", dir=paths[0].parent))
    for path, data in zip(paths, before):
        saved = backup / path.name
        with saved.open("xb") as stream:
            stream.write(data)
        saved.chmod(0o600)
        if saved.read_bytes() != data:
            raise ValueError("Backup verification failed")
    for path, original, replacement in zip(paths, before, after):
        if original == replacement:
            continue
        if read_regular(path) != original:
            raise ValueError("Concurrent edit: stop and inspect the retained cutover backup")
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".flashnext-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            temporary.chmod(mode & 0o600)
            if read_regular(path) != original:
                raise ValueError("Concurrent edit before replacement; original preserved")
            os.replace(temporary, path)
            if read_regular(path) != replacement:
                raise ValueError("Configuration changed after replacement; inspect backup")
        finally:
            temporary.unlink(missing_ok=True)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-dir", type=Path, default=Path.home() / ".pi/agent")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-models-sha256")
    parser.add_argument("--expected-settings-sha256")
    args = parser.parse_args()
    paths = tuple(args.agent_dir / name for name in ("models.json", "settings.json"))
    before = tuple(read_regular(path) for path in paths)
    documents = transform(*(json.loads(data) for data in before))
    after = tuple((json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode() for doc in documents)
    summary = {"model": MODEL, "default_model": documents[1].get("defaultModel"),
               "context": 262144, "max_output": 32768, "compact_at": 95000,
               "before_sha256": {p.name: sha(data) for p, data in zip(paths, before)},
               "changed": [p.name for p, a, b in zip(paths, before, after) if json.loads(a) != json.loads(b)],
               "applied": False}
    if args.apply:
        if tuple(map(sha, before)) != (args.expected_models_sha256, args.expected_settings_sha256):
            raise ValueError("Apply requires both current reviewed input hashes")
        if summary["changed"]:
            summary["backup"] = str(install_pair(paths, before, after))
        summary["applied"] = True
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
