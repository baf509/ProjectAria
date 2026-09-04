#!/usr/bin/env python3
"""Install ARIA's inference-only credential into an existing Pi config.

The credential is read from stdin or a dotenv file and is never printed.  The
script refuses to touch a Pi inventory that contains any provider/model outside
the three entries in the current managed deployment policy.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile


APPROVED_PROVIDER = "aria"
APPROVED_BASE_URL = "http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified"
APPROVED_MODELS = {
    "Qwen3.8-27B-R9700-Radiance",
    "Qwen3.8-Flash-Next-Q4_K_XL-Halo-2x256K",
    "Qwen3.8-Flash-Next-Hybrid-R9700-Halo",
}


def _dotenv_value(path: Path, name: str) -> str:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return ""


def _load_key(args: argparse.Namespace) -> str:
    if args.key_stdin:
        import sys

        value = sys.stdin.read().strip()
    else:
        value = _dotenv_value(args.key_env, args.key_name)
    if len(value) < 32 or any(ch.isspace() for ch in value):
        raise SystemExit("refusing invalid or missing inference gateway key")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        type=Path,
        default=Path.home() / ".pi" / "agent" / "models.json",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--key-stdin", action="store_true")
    source.add_argument("--key-env", type=Path)
    parser.add_argument("--key-name", default="LLM_GATEWAY_API_KEY")
    args = parser.parse_args()

    document = json.loads(args.models.read_text(encoding="utf-8"))
    providers = document.get("providers")
    if not isinstance(providers, dict) or set(providers) != {APPROVED_PROVIDER}:
        raise SystemExit("refusing Pi config: expected exactly one provider named aria")
    provider = providers[APPROVED_PROVIDER]
    if provider.get("baseUrl") != APPROVED_BASE_URL:
        raise SystemExit("refusing Pi config: ARIA gateway URL is not canonical")
    model_ids = {
        item.get("id") for item in provider.get("models", []) if isinstance(item, dict)
    }
    if model_ids != APPROVED_MODELS or len(provider.get("models", [])) != 3:
        raise SystemExit("refusing Pi config: model inventory is not the approved three-model set")

    provider["apiKey"] = _load_key(args)
    prior_mode = args.models.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=args.models.parent, delete=False
    ) as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.chmod(temporary, prior_mode or 0o600)
    os.replace(temporary, args.models)
    os.chmod(args.models, prior_mode or 0o600)
    print("Pi ARIA inference credential installed; provider=aria models=3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
