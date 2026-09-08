#!/usr/bin/env python3
"""Replace other Qwen3.8-27B Pi entries with Red Radiance; retain Flash Next."""

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile


def write_config(path, document):
    serialized = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if json.loads(path.read_text()) == document:
        return
    backup = path.with_name(path.name + ".pre-red-radiance-20260907")
    if not backup.exists():
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        f.write(serialized)
        temporary = Path(f.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-dir", type=Path, default=Path.home() / ".pi/agent")
    args = parser.parse_args()
    model = json.loads(Path(__file__).with_name("pi-red-radiance-model.json").read_text())
    models_path = args.agent_dir / "models.json"
    settings_path = args.agent_dir / "settings.json"
    models = json.loads(models_path.read_text())
    settings = json.loads(settings_path.read_text())
    providers = models.get("providers", {})
    if set(providers) != {"aria"}:
        raise SystemExit("Expected the existing managed aria provider")
    provider = providers["aria"]
    if provider.get("baseUrl") != "http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified":
        raise SystemExit("Expected the identified Mac Aria gateway")
    retired_ids = {
        entry["id"] for entry in provider["models"]
        if "qwen3.8" in entry.get("id", "").lower()
        and "27b" in entry.get("id", "").lower()
        and entry["id"] != model["id"]
    }
    entries = provider["models"] = [
        entry for entry in provider["models"] if entry.get("id") not in retired_ids
    ]
    matches = [i for i, entry in enumerate(entries) if entry.get("id") == model["id"]]
    if len(matches) > 1:
        raise SystemExit("Duplicate Red model entries need review")
    if matches:
        entries[matches[0]] = model
    else:
        entries.append(model)
    enabled = settings.get("enabledModels")
    if isinstance(enabled, list):
        settings["enabledModels"] = [
            entry for entry in enabled
            if entry not in retired_ids and entry not in {"aria/" + item for item in retired_ids}
        ]
        if "aria/" + model["id"] not in settings["enabledModels"]:
            settings["enabledModels"].append("aria/" + model["id"])
    if settings.get("defaultProvider") == "aria" and settings.get("defaultModel") in retired_ids:
        settings["defaultModel"] = model["id"]
    write_config(models_path, models)
    write_config(settings_path, settings)
    print("Red Radiance option configured; default=" + str(settings.get("defaultModel")))


if __name__ == "__main__":
    main()
