#!/usr/bin/env python3
"""Clean up Hermes's model providers on the Mac gateway.

HISTORICAL (2026-08-26): originally written to run as the `devboxsvc` service
account. That account no longer exists on the Mac; run as `ben`. Annotated
2026-08-31.

    python3 hermes-providers-cleanup-20260826.py            # dry run: prints the diff
    python3 hermes-providers-cleanup-20260826.py --apply    # backs up config.yaml, writes it
    launchctl kickstart -k system/com.ben.devbox.hermes-gateway

Only the top-level `model:` and `providers:` blocks are rewritten; every other line of
config.yaml is passed through byte-for-byte.

What it does (2026-08-26, after the Flash-Next deployment on Corsair):
  * default model -> Qwen3.8-Flash-Next (custom:qwen38-flash, :8120 via the Corsair forward)
  * providers reduced to what ARIA can actually start on Corsair today:
      qwen38-flash   Qwen3.8-Flash-Next UD-IQ4_XS, Halo, :8120   (NEW, default)
      qwen38-r9700   Qwen3.8-27B Radiance, R9700, :8080         (vision-capable fallback)
      ds4-halo       DS4 DwarfStar, Halo, :8112                 (stopped while Flash-Next holds the Halo)
      gemma-aux      Gemma 4 E4B, CPU, :8104                    (auxiliary/cron model)
      aria           ARIA-resident route, pruned to the four registered slugs above
  * fixes ds4-halo, whose model list advertised a slug (DS4-0731-UD-IQ3-XXS-Halo-DSpark) that
    the server on :8112 never served; the served name is deepseek-v4-flash
  * drops the 10 retired ARIA-resident entries (Ling*, DS4 affine/IQ2M/dual-Vulkan, Chadrock*,
    qwen3.6*, Laguna*, Qwythos, context1) — none is a current deployment
  * any provider key not in the list above is kept untouched (printed as a note)
"""
import argparse, difflib, os, re, shutil, sys, time
import yaml

CONFIG = os.path.expanduser(os.environ.get("HERMES_CONFIG", "~/.hermes/config.yaml"))

PROVIDERS = {
    "qwen38-flash": {
        "name": "Qwen3.8-Flash-Next UD-IQ4_XS (Strix Halo, llama.cpp qwen4exp, 1 slot / 262K) — DEFAULT",
        "base_url": "http://127.0.0.1:8120/v1",
        "api_key": "local-no-key",
        "model": "qwen3.8-flash-next",
        "models": {"qwen3.8-flash-next": {"context_length": 250000}},
        "default_model": "qwen3.8-flash-next",
    },
    "qwen38-r9700": {
        "name": "Qwen3.8-27B int4 AutoRound (Radeon AI PRO R9700, vllm-radiance, 1 slot / 262K) — vision-capable fallback",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "local-no-key",
        "model": "qwen3.8-27b-r9700",
        "models": {"qwen3.8-27b-r9700": {"context_length": 245760}},
        "default_model": "qwen3.8-27b-r9700",
    },
    "ds4-halo": {
        "name": "DeepSeek V4 Flash 0731 (Strix Halo via DwarfStar) — mutually exclusive with qwen38-flash; start through ARIA",
        "base_url": "http://127.0.0.1:8112/v1",
        "api_key": "local-no-key",
        "model": "deepseek-v4-flash",
        "models": {"deepseek-v4-flash": {"context_length": 131072}},
        "default_model": "deepseek-v4-flash",
    },
    "gemma-aux": {
        "name": "Gemma 4 E4B-it Q4_0 (CPU-only auxiliary/cron model)",
        "base_url": "http://127.0.0.1:8104/v1",
        "api_key": "local-no-key",
        "model": "gemma-4-e4b-it",
        "models": {"gemma-4-e4b-it": {"context_length": 65536}},
        "default_model": "gemma-4-e4b-it",
    },
    "aria": {
        "name": "ARIA-resident local model (follows ARIA's model-server control plane)",
        "base_url": "http://localhost:8200/llm/v1-identified",
        "api_key": "${env:ARIA_API_KEY}",
        "model": "Qwen3.8-Flash-Next-IQ4_XS-Halo",
        "models": {
            "Qwen3.8-Flash-Next-IQ4_XS-Halo": {"context_length": 250000},
            "Qwen3.8-27B-R9700-Radiance": {"context_length": 245760},
            "DS4-0731-Q8Protected-Halo-DwarfStar": {"context_length": 131072},
            "gemma-4-e4b-Q4": {"context_length": 65536},
            "aria-resident": {"context_length": 100000},
        },
        "default_model": "Qwen3.8-Flash-Next-IQ4_XS-Halo",
    },
}
MODEL = {"default": "qwen3.8-flash-next", "provider": "custom:qwen38-flash"}

_TOP = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:")  # a top-level mapping key at column 0


def _block_span(lines, key):
    """(start, end) line indices of the top-level `key:` block, end exclusive."""
    start = next((i for i, l in enumerate(lines) if l.startswith(f"{key}:")), None)
    if start is None:
        raise SystemExit(f"top-level key {key!r} not found in {CONFIG}")
    end = start + 1
    while end < len(lines) and not _TOP.match(lines[end]):
        end += 1
    # leave trailing blank/comment lines with the next block
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1
    return start, end


def _dump(key, value):
    return yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True, width=120).rstrip("\n").split("\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with open(CONFIG) as f:
        before = f.read()
    lines = before.split("\n")
    cfg = yaml.safe_load(before)
    kept_unknown = {k: v for k, v in (cfg.get("providers") or {}).items() if k not in PROVIDERS}
    if kept_unknown:
        print(f"note: providers not in the cleanup list are kept untouched: {sorted(kept_unknown)}")
    providers = {**PROVIDERS, **kept_unknown}

    # replace the later block first so the earlier span stays valid
    spans = sorted([("providers", providers), ("model", MODEL)],
                   key=lambda kv: _block_span(lines, kv[0])[0], reverse=True)
    for key, value in spans:
        s, e = _block_span(lines, key)
        lines[s:e] = _dump(key, value)
    after = "\n".join(lines)

    check = yaml.safe_load(after)
    assert check["model"] == MODEL and set(check["providers"]) == set(providers), "post-check failed"
    for k in cfg:
        if k not in ("model", "providers"):
            assert check.get(k) == cfg.get(k), f"section {k!r} changed unexpectedly"

    print("\n".join(difflib.unified_diff(before.split("\n"), after.split("\n"),
                                         "config.yaml (before)", "config.yaml (after)", lineterm="")))
    if not args.apply:
        print("\n(dry run — re-run with --apply)")
        return 0
    backup = f"{CONFIG}.bak-pre-flash-next-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CONFIG, backup)
    with open(CONFIG, "w") as f:
        f.write(after)
    print(f"\nwritten; backup at {backup}. Restart: launchctl kickstart -k system/com.ben.devbox.hermes-gateway")
    return 0


if __name__ == "__main__":
    sys.exit(main())
