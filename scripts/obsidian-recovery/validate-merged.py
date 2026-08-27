#!/usr/bin/env python3
"""
ARIA - Obsidian LiveSync recovery: re-validate a hand-edited merged tree

Phase: Obsidian LiveSync Corsair recovery (Phase 2 stop gate)
Purpose: re-run Phase 2's validation after a human resolves conflicts

Related plan sections:
- Section 7, Phase 2: Validation and stop gate

reconcile.py refuses to touch a non-empty workspace, so after resolving
conflict markers by hand the gate is re-checked with this instead of by
re-running the merge. Exit 3 means the tree still may not be seeded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reconcile import Decision, validate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True, type=Path)
    args = ap.parse_args()

    manifest = args.workspace / "reconciliation-manifest.json"
    if not manifest.exists():
        print(f"validate: no manifest at {manifest}", file=sys.stderr)
        return 2

    doc = json.loads(manifest.read_text(encoding="utf-8"))
    decisions = []
    for row in doc["decisions"]:
        d = Decision(row["path"], **{k: row["hashes"][k]
                                     for k in ("base", "mac", "corsair")})
        d.outcome = row["outcome"]
        d.needs_review = row["needs_review"]
        d.deletion_review = row["deletion_review"]
        # A hand-resolved file no longer carries conflict markers, so the
        # marker scan below is the real test - trust the tree, not the old flag.
        d.unresolved = False
        decisions.append(d)

    problems = validate(args.workspace, decisions)
    if problems:
        print(f"validate: STOP GATE - {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 3
    print("validate: merged tree passes all Phase 2 checks", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
