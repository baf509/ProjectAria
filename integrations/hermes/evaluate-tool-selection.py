#!/usr/bin/env python3
"""Score Hermes tool-selection transcripts against the Signal regression set.

Input is JSONL, one record per scenario/model:
  {"id":"inspect-corsair-shell","model":"...","first_tool":"fleet_status",
   "arguments":{},"transcript":"..."}
The runner that talks to a particular Hermes model remains outside this script;
this scorer is deterministic and safe to use for primary and fallback captures.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent


def _contains(haystack: object, needle: object) -> bool:
    if isinstance(needle, dict):
        return isinstance(haystack, dict) and all(
            key in haystack and _contains(haystack[key], value)
            for key, value in needle.items()
        )
    return haystack == needle


def score(records: list[dict], cases: list[dict]) -> dict:
    expected = {case["id"]: case for case in cases}
    results = []
    for record in records:
        case = expected.get(record.get("id"))
        if not case:
            results.append({"id": record.get("id"), "ok": False, "errors": ["unknown case"]})
            continue
        errors: list[str] = []
        if record.get("first_tool") != case.get("first_tool"):
            errors.append(
                f"first_tool={record.get('first_tool')!r}, expected={case.get('first_tool')!r}"
            )
        wanted_args = case.get("arguments") or {}
        if wanted_args and not _contains(record.get("arguments") or {}, wanted_args):
            errors.append(f"arguments missing expected subset {wanted_args!r}")
        transcript = str(record.get("transcript") or "").lower()
        for forbidden in case.get("forbidden") or []:
            if str(forbidden).lower() in transcript:
                errors.append(f"forbidden fallback appeared: {forbidden}")
        results.append(
            {"id": case["id"], "model": record.get("model"), "ok": not errors, "errors": errors}
        )
    missing = sorted(set(expected) - {str(record.get("id")) for record in records})
    return {
        "ok": all(item["ok"] for item in results) and not missing,
        "passed": sum(item["ok"] for item in results),
        "total": len(results),
        "missing": missing,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path, help="JSONL model/tool transcripts")
    parser.add_argument(
        "--cases", type=Path, default=HERE / "tool-selection-evals.json"
    )
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in args.results.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = score(records, cases)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
