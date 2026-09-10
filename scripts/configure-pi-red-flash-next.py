#!/usr/bin/env python3
"""Add Red's Flash Next option to a managed Pi installation.

Additive: retires nothing and does not change the installation's default
model. The entry declares the 262144-token context configured in
red-r9700/flash-next/profile.env.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

BACKUP_SUFFIX = ".pre-red-flash-next-20260909"
GATEWAY = "http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified"


def write_config(path, document):
    serialized = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if json.loads(path.read_text()) == document:
        return False
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
        f.write(serialized)
        temporary = Path(f.name)
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-dir", type=Path, default=Path.home() / ".pi/agent")
    args = parser.parse_args()
    model = json.loads(Path(__file__).with_name("pi-red-flash-next-model.json").read_text())
    models_path = args.agent_dir / "models.json"
    settings_path = args.agent_dir / "settings.json"
    models = json.loads(models_path.read_text())
    settings = json.loads(settings_path.read_text())
    providers = models.get("providers", {})
    if set(providers) != {"aria"}:
        raise SystemExit("Expected the existing managed aria provider")
    provider = providers["aria"]
    if provider.get("baseUrl") != GATEWAY:
        raise SystemExit("Expected the identified Mac Aria gateway")
    entries = provider["models"]
    matches = [i for i, entry in enumerate(entries) if entry.get("id") == model["id"]]
    if len(matches) > 1:
        raise SystemExit("Duplicate Red Flash Next entries need review")
    if matches:
        entries[matches[0]] = model
    else:
        entries.append(model)
    # Pi resolves compaction against one global reserveTokens, so every enabled
    # model must keep contextWindow - reserveTokens above zero.
    reserve = settings.get("compaction", {}).get("reserveTokens", 0)
    if model["contextWindow"] - reserve <= 0:
        raise SystemExit(
            "reserveTokens %d leaves no usable window for a %d-token model"
            % (reserve, model["contextWindow"])
        )
    enabled = settings.get("enabledModels")
    if isinstance(enabled, list) and "aria/" + model["id"] not in enabled:
        enabled.append("aria/" + model["id"])
    changed = write_config(models_path, models)
    changed |= write_config(settings_path, settings)
    print(
        "Red Flash Next %s; enabled=%s default=%s"
        % ("configured" if changed else "already current",
           settings.get("enabledModels"), settings.get("defaultModel"))
    )


if __name__ == "__main__":
    main()
