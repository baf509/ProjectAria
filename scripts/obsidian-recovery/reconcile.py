#!/usr/bin/env python3
"""
ARIA - Obsidian LiveSync recovery: three-way reconciliation

Phase: Obsidian LiveSync Corsair recovery (Phase 2)
Purpose: merge the Mac and Corsair post-cutover vaults offline, never in place

Related plan sections:
- Section 7, Phase 2: Construct a three-way reconciliation workspace
- Section 5: Safety invariants (3: absence is not deletion; 6: keep both originals)

Inputs are three FROZEN trees - the August 23 base commit checkout, the Mac
snapshot, and the Corsair snapshot. Nothing here writes to a live vault: the
only output is a staging workspace that a human reviews before Phase 4 seeds
from it. Exit 3 means a stop gate tripped and seeding must not begin.

Usage:
    reconcile.py --base BASE --mac MAC --corsair CORSAIR --workspace WS \
                 [--run-id ID] [--honor-concordant-deletions]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obslib import ABSENT, hash_tree, is_forbidden  # noqa: E402

CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


class Decision:
    """One row of the reconciliation ledger for one relative path."""

    def __init__(self, rel: str, base: str, mac: str, corsair: str):
        self.rel = rel
        self.hashes = {"base": base, "mac": mac, "corsair": corsair}
        self.outcome = ""          # what happened
        self.source = ""           # which tree the merged bytes came from
        self.rationale = ""
        self.needs_review = False
        self.deletion_review = False
        self.unresolved = False

    def as_dict(self) -> dict:
        return {
            "path": self.rel,
            "hashes": self.hashes,
            "outcome": self.outcome,
            "source": self.source,
            "rationale": self.rationale,
            "needs_review": self.needs_review,
            "deletion_review": self.deletion_review,
            "unresolved": self.unresolved,
        }


def is_text(path: Path) -> bool:
    try:
        path.read_text(encoding="utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def copy_into(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def three_way_merge(base: Optional[Path], mac: Path, corsair: Path,
                    out: Path) -> bool:
    """git merge-file into `out`. Returns True when no conflict remains.

    A missing base is merged against an empty file, which is what git itself
    does for an add/add conflict - it produces markers rather than silently
    picking a side, and the markers are what the validator catches.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    empty = out.parent / ".empty-base"
    if base is None:
        empty.write_bytes(b"")
        base_path = empty
    else:
        base_path = base
    try:
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--diff3",
             "-L", "mac", "-L", "base(2026-08-23)", "-L", "corsair",
             str(mac), str(base_path), str(corsair)],
            capture_output=True,
        )
    finally:
        if empty.exists():
            empty.unlink()
    out.write_bytes(proc.stdout)
    # git merge-file exits 0 on a clean merge and >0 with the conflict count.
    return proc.returncode == 0


def reconcile(base_root: Optional[Path], mac_root: Path, corsair_root: Path,
              ws: Path, honor_concordant_deletions: bool) -> List[Decision]:
    base_h = hash_tree(base_root) if base_root else {}
    mac_h = hash_tree(mac_root)
    cor_h = hash_tree(corsair_root)

    merged = ws / "merged"
    originals = ws / "originals"
    merged.mkdir(parents=True, exist_ok=True)
    originals.mkdir(parents=True, exist_ok=True)

    decisions: List[Decision] = []
    for rel in sorted(set(base_h) | set(mac_h) | set(cor_h)):
        b = base_h.get(rel, ABSENT)
        m = mac_h.get(rel, ABSENT)
        c = cor_h.get(rel, ABSENT)
        d = Decision(rel, b, m, c)

        bp = (base_root / rel) if b != ABSENT else None
        mp = (mac_root / rel) if m != ABSENT else None
        cp = (corsair_root / rel) if c != ABSENT else None

        def keep(src: Path, which: str, outcome: str, why: str) -> None:
            copy_into(src, merged / rel)
            d.source, d.outcome, d.rationale = which, outcome, why

        # --- both peers present ------------------------------------------
        if m != ABSENT and c != ABSENT:
            if m == c:
                keep(mp, "mac", "identical" if m == b else "converged",
                     "Mac and Corsair agree; one copy kept")
            elif c == b:
                keep(mp, "mac", "mac_changed", "Only the Mac changed since base")
            elif m == b:
                keep(cp, "corsair", "corsair_changed",
                     "Only Corsair changed since base")
            else:
                # Both changed. Semantics, not mtime (plan Phase 2).
                for which, src in (("mac", mp), ("corsair", cp)):
                    copy_into(src, originals / which / rel)
                if bp is not None:
                    copy_into(bp, originals / "base" / rel)
                if is_text(mp) and is_text(cp):
                    clean = three_way_merge(bp, mp, cp, merged / rel)
                    d.source = "merged"
                    d.outcome = "three_way_merged" if clean else "three_way_conflict"
                    d.needs_review = True
                    d.unresolved = not clean
                    d.rationale = ("Both peers changed; git three-way merge "
                                   + ("applied cleanly" if clean
                                      else "left conflict markers - resolve by hand"))
                else:
                    # Binary divergence cannot be merged mechanically.
                    keep(mp, "mac", "binary_conflict",
                         "Both peers changed a non-text file; Mac kept "
                         "provisionally, both originals retained")
                    d.needs_review = True
                    d.unresolved = True

        # --- present on the Mac only -------------------------------------
        elif m != ABSENT:
            if b == ABSENT:
                keep(mp, "mac", "mac_addition", "New on the Mac since base")
            elif m != b:
                keep(mp, "mac", "mac_changed_corsair_absent",
                     "Mac changed it, Corsair lost it; Mac wins and the "
                     "Corsair absence is recorded as ambiguous")
                d.needs_review = True
                d.deletion_review = True
            else:
                copy_into(bp, merged / rel)
                d.source, d.outcome = "base", "corsair_deletion_review"
                d.rationale = ("Absent on Corsair and unchanged on the Mac; "
                               "preserved pending deletion review")
                d.deletion_review = True

        # --- present on Corsair only -------------------------------------
        elif c != ABSENT:
            if b == ABSENT:
                keep(cp, "corsair", "corsair_addition",
                     "New on Corsair since base")
            elif c != b:
                keep(cp, "corsair", "corsair_changed_mac_absent",
                     "Corsair changed it, the Mac lost it; Corsair wins and "
                     "the Mac absence is recorded as ambiguous")
                d.needs_review = True
                d.deletion_review = True
            else:
                copy_into(bp, merged / rel)
                d.source, d.outcome = "base", "mac_deletion_review"
                d.rationale = ("Absent on the Mac and unchanged on Corsair; "
                               "preserved pending deletion review")
                d.deletion_review = True

        # --- absent on both peers ----------------------------------------
        else:
            d.deletion_review = True
            if honor_concordant_deletions:
                d.source, d.outcome = "none", "concordant_deletion_applied"
                d.rationale = "Deleted on both peers; deletion honoured by flag"
            else:
                copy_into(bp, merged / rel)
                d.source, d.outcome = "base", "concordant_deletion_review"
                d.rationale = ("Deleted on both peers, but preservation wins "
                               "during recovery; re-delete after Phase 9")

        decisions.append(d)

    return decisions


def validate(ws: Path, decisions: List[Decision]) -> List[str]:
    """Plan Phase 2 validation. Every failure is a stop gate."""
    merged = ws / "merged"
    problems: List[str] = []

    seen_ci: Dict[str, str] = {}
    for rel in sorted(hash_tree(merged)):
        low = rel.lower()
        if low in seen_ci and seen_ci[low] != rel:
            problems.append(f"case-insensitive duplicate: {rel} vs {seen_ci[low]}")
        seen_ci[low] = rel

        if is_forbidden(rel):
            problems.append(f"forbidden path in merged tree: {rel}")

        path = merged / rel
        if rel.endswith(".md"):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                problems.append(f"invalid UTF-8 in Markdown: {rel}")
                continue
            for line in text.splitlines():
                if any(line.startswith(mk) for mk in CONFLICT_MARKERS):
                    problems.append(f"conflict marker left in: {rel}")
                    break

    for d in decisions:
        if not d.outcome:
            problems.append(f"path has no manifest outcome: {d.rel}")
        if d.unresolved:
            problems.append(f"unresolved semantic conflict: {d.rel}")

    return problems


def write_deletion_inventory(ws: Path, decisions: List[Decision], run_id: str) -> None:
    """Deletion candidates become a note, not a deletion (plan Phase 2)."""
    rows = [d for d in decisions if d.deletion_review]
    if not rows:
        return
    day = run_id.split("-")[0]
    out = ws / "merged" / "Recovery" / "DeletionReview" / day / "INVENTORY.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"title: Deletion review inventory {day}",
        f"created: {datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
        "document_type: recovery-artifact",
        "tags: [obsidian, livesync, recovery, deletion-review]",
        "---",
        "",
        f"# Deletion review inventory ({run_id})",
        "",
        "Every path below was absent on at least one peer during the "
        "2026-08-23 divergence. Recovery preserves rather than deletes, so "
        "these files are present in the reconciled vault. Confirm each one "
        "and delete deliberately after the soak in Phase 9.",
        "",
        "| Path | Outcome | Base | Mac | Corsair |",
        "|---|---|---|---|---|",
    ]
    for d in rows:
        h = {k: (v[:12] if v != ABSENT else ABSENT) for k, v in d.hashes.items()}
        lines.append(
            f"| `{d.rel}` | {d.outcome} | {h['base']} | {h['mac']} | {h['corsair']} |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ledger(ws: Path, decisions: List[Decision], run_id: str,
                 problems: List[str], base_commit: str) -> None:
    counts: Dict[str, int] = {}
    for d in decisions:
        counts[d.outcome] = counts.get(d.outcome, 0) + 1

    (ws / "reconciliation-manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "base_commit": base_commit,
                "counts": counts,
                "problems": problems,
                "decisions": [d.as_dict() for d in decisions],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    lines = [
        f"# Reconciliation ledger {run_id}",
        "",
        f"Base commit: `{base_commit}`",
        f"Paths considered: {len(decisions)}",
        "",
        "## Outcomes",
        "",
        "| Outcome | Count |",
        "|---|---|",
    ]
    for k in sorted(counts):
        lines.append(f"| {k} | {counts[k]} |")

    for title, pred in (
        ("Needs human review", lambda d: d.needs_review),
        ("Preserved deletion candidates", lambda d: d.deletion_review),
    ):
        rows = [d for d in decisions if pred(d)]
        lines += ["", f"## {title} ({len(rows)})", ""]
        lines += [f"- `{d.rel}` - {d.outcome}: {d.rationale}" for d in rows] or ["- none"]

    lines += ["", "## Validation", ""]
    lines += [f"- FAIL {p}" for p in problems] or ["- all checks passed"]
    (ws / "RECONCILIATION_LEDGER.md").write_text("\n".join(lines) + "\n",
                                                 encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path,
                    help="checkout of the August 23 base commit")
    ap.add_argument("--mac", required=True, type=Path)
    ap.add_argument("--corsair", required=True, type=Path)
    ap.add_argument("--workspace", required=True, type=Path)
    ap.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--base-commit", default="e7534f61f7bea5ef730aea2198eda4f33f0018c8")
    ap.add_argument("--honor-concordant-deletions", action="store_true",
                    help="delete files both peers deleted instead of preserving "
                         "them for review (default: preserve)")
    ap.add_argument("--no-base", action="store_true",
                    help="run without a base tree; every both-changed file "
                         "then becomes an add/add conflict needing review")
    args = ap.parse_args()

    for label, p in (("mac", args.mac), ("corsair", args.corsair)):
        if not p.is_dir():
            print(f"reconcile: {label} tree is not a directory: {p}", file=sys.stderr)
            return 2

    if args.no_base:
        base_root = None
        print("reconcile: WARNING running without a base tree - both-changed "
              "files cannot be merged automatically", file=sys.stderr)
    else:
        if not args.base or not args.base.is_dir():
            print("reconcile: --base is required (or pass --no-base)", file=sys.stderr)
            return 2
        base_root = args.base

    ws = args.workspace
    if ws.exists() and any(ws.iterdir()):
        print(f"reconcile: workspace is not empty: {ws}", file=sys.stderr)
        return 2
    ws.mkdir(parents=True, exist_ok=True)

    decisions = reconcile(base_root, args.mac, args.corsair, ws,
                          args.honor_concordant_deletions)
    write_deletion_inventory(ws, decisions, args.run_id)
    problems = validate(ws, decisions)
    write_ledger(ws, decisions, args.run_id, problems, args.base_commit)

    print(f"reconcile: {len(decisions)} paths -> {ws}/merged", file=sys.stderr)
    print(f"reconcile: ledger {ws}/RECONCILIATION_LEDGER.md", file=sys.stderr)
    if problems:
        print(f"reconcile: STOP GATE - {len(problems)} validation problem(s):",
              file=sys.stderr)
        for p in problems[:20]:
            print(f"  - {p}", file=sys.stderr)
        return 3
    print("reconcile: all validations passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
