"""
ARIA - Obsidian LiveSync recovery reconciler tests

Phase: Obsidian LiveSync Corsair recovery (Phase 2)
Purpose: pin every row of the plan's resolution table to a behaviour

Related plan sections:
- Section 7, Phase 2: Resolution rules table and deletion policy
- Section 5: Safety invariants

These are the tests that stand between "the merge ran" and "no note was lost".
Each case builds three synthetic trees, runs the real reconciler, and asserts
on the merged bytes plus the ledger outcome - not merely on the exit code.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "obsidian-recovery"
sys.path.insert(0, str(TOOLS))

from obslib import EXCLUDES, hash_tree, is_excluded, is_forbidden  # noqa: E402


def build(root: Path, files: dict) -> Path:
    """Materialise {relpath: content|None} as a tree. None means ABSENT."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        if content is None:
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return root


def run_reconcile(tmp_path: Path, base: dict, mac: dict, corsair: dict,
                  *, extra_args=()) -> tuple[int, Path, dict]:
    build(tmp_path / "base", base)
    build(tmp_path / "mac", mac)
    build(tmp_path / "corsair", corsair)
    ws = tmp_path / "reconcile"
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "reconcile.py"),
         "--base", str(tmp_path / "base"),
         "--mac", str(tmp_path / "mac"),
         "--corsair", str(tmp_path / "corsair"),
         "--workspace", str(ws),
         "--run-id", "20260827-001500",
         *extra_args],
        capture_output=True, text=True,
    )
    manifest = {}
    mf = ws / "reconciliation-manifest.json"
    if mf.exists():
        manifest = {d["path"]: d for d in json.loads(mf.read_text())["decisions"]}
    return proc.returncode, ws, manifest


# ---------------------------------------------------------------------------
# The resolution table (plan Section 7, Phase 2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,base,mac,corsair,expect_content,expect_outcome",
    [
        # Base | Mac | Corsair | Resolution
        ("all identical",        "v0", "v0", "v0", "v0", "identical"),
        ("mac changed only",     "v0", "v1", "v0", "v1", "mac_changed"),
        ("corsair changed only", "v0", "v0", "v1", "v1", "corsair_changed"),
        ("same change on both",  "v0", "v1", "v1", "v1", "converged"),
        ("mac addition",         None, "v1", None, "v1", "mac_addition"),
        ("corsair addition",     None, None, "v1", "v1", "corsair_addition"),
        # Present | Absent | Changed -> preserve Corsair, flag the Mac absence
        ("mac absent corsair changed", "v0", None, "v1", "v1",
         "corsair_changed_mac_absent"),
        # Present | Changed | Absent -> preserve Mac, flag the Corsair absence
        ("corsair absent mac changed", "v0", "v1", None, "v1",
         "mac_changed_corsair_absent"),
        # Present | Absent | Base -> preserve base under deletion review
        ("mac deleted, corsair untouched", "v0", None, "v0", "v0",
         "mac_deletion_review"),
        ("corsair deleted, mac untouched", "v0", "v0", None, "v0",
         "corsair_deletion_review"),
    ],
)
def test_resolution_table(tmp_path, name, base, mac, corsair,
                          expect_content, expect_outcome):
    rc, ws, manifest = run_reconcile(
        tmp_path, {"n.md": base}, {"n.md": mac}, {"n.md": corsair})
    assert rc == 0, f"{name}: expected a clean run"
    merged = ws / "merged" / "n.md"
    assert merged.exists(), f"{name}: merged file missing"
    assert merged.read_text() == expect_content, f"{name}: wrong content kept"
    assert manifest["n.md"]["outcome"] == expect_outcome


def test_both_changed_text_merges_cleanly_on_disjoint_edits(tmp_path):
    """Any | Changed A | Changed B -> three-way merge."""
    base = "line1\nline2\nline3\nline4\nline5\n"
    mac = "MAC\nline2\nline3\nline4\nline5\n"
    corsair = "line1\nline2\nline3\nline4\nCORSAIR\n"
    rc, ws, manifest = run_reconcile(
        tmp_path, {"n.md": base}, {"n.md": mac}, {"n.md": corsair})
    assert rc == 0
    merged = (ws / "merged" / "n.md").read_text()
    assert "MAC" in merged and "CORSAIR" in merged
    assert manifest["n.md"]["outcome"] == "three_way_merged"
    assert manifest["n.md"]["needs_review"] is True
    # Invariant 6: both originals retained with their hashes.
    assert (ws / "originals" / "mac" / "n.md").read_text() == mac
    assert (ws / "originals" / "corsair" / "n.md").read_text() == corsair
    assert (ws / "originals" / "base" / "n.md").read_text() == base


def test_overlapping_edits_are_a_stop_gate_not_a_silent_pick(tmp_path):
    """A real conflict must block seeding rather than choose by mtime."""
    rc, ws, manifest = run_reconcile(
        tmp_path,
        {"n.md": "line1\nline2\n"},
        {"n.md": "MAC\nline2\n"},
        {"n.md": "CORSAIR\nline2\n"},
    )
    assert rc == 3, "unresolved conflict must trip the stop gate"
    assert manifest["n.md"]["outcome"] == "three_way_conflict"
    assert manifest["n.md"]["unresolved"] is True
    ledger = (ws / "RECONCILIATION_LEDGER.md").read_text()
    assert "conflict marker left in: n.md" in ledger


def test_binary_divergence_is_unresolved(tmp_path):
    rc, ws, manifest = run_reconcile(
        tmp_path,
        {"i.png": b"\x89PNG\x00base"},
        {"i.png": b"\x89PNG\x00mac"},
        {"i.png": b"\x89PNG\x00corsair"},
    )
    assert rc == 3
    assert manifest["i.png"]["outcome"] == "binary_conflict"
    assert (ws / "originals" / "corsair" / "i.png").exists()


# ---------------------------------------------------------------------------
# Deletion policy (plan Section 7, Phase 2 - "preservation wins")
# ---------------------------------------------------------------------------

def test_concordant_deletion_is_preserved_by_default(tmp_path):
    rc, ws, manifest = run_reconcile(
        tmp_path, {"gone.md": "v0"}, {"gone.md": None}, {"gone.md": None})
    assert rc == 0
    assert (ws / "merged" / "gone.md").read_text() == "v0"
    assert manifest["gone.md"]["outcome"] == "concordant_deletion_review"


def test_concordant_deletion_honoured_only_behind_the_flag(tmp_path):
    rc, ws, manifest = run_reconcile(
        tmp_path, {"gone.md": "v0"}, {"gone.md": None}, {"gone.md": None},
        extra_args=("--honor-concordant-deletions",))
    assert rc == 0
    assert not (ws / "merged" / "gone.md").exists()
    assert manifest["gone.md"]["outcome"] == "concordant_deletion_applied"


def test_deletion_candidates_become_an_inventory_note(tmp_path):
    rc, ws, _ = run_reconcile(
        tmp_path, {"a.md": "v0", "b.md": "v0"},
        {"a.md": None, "b.md": "v0"}, {"a.md": "v0", "b.md": "v0"})
    assert rc == 0
    inv = ws / "merged" / "Recovery" / "DeletionReview" / "20260827" / "INVENTORY.md"
    assert inv.exists(), "deletion candidates must be inventoried, not deleted"
    assert "`a.md`" in inv.read_text()
    assert "`b.md`" not in inv.read_text()


# ---------------------------------------------------------------------------
# Content-set boundaries (plan Sections 3.6, 5.4)
# ---------------------------------------------------------------------------

def test_metadata_never_enters_the_merged_tree(tmp_path):
    rc, ws, manifest = run_reconcile(
        tmp_path,
        {"n.md": "v0"},
        {"n.md": "v0",
         ".git/config": "[core]",
         ".obsidian/workspace.json": "{}",
         "bridge/dat/config.json": '{"password": "hunter2"}',
         ".DS_Store": "junk"},
        {"n.md": "v0"},
    )
    assert rc == 0
    merged_paths = set(hash_tree(ws / "merged"))
    assert merged_paths == {"n.md"}
    assert set(manifest) == {"n.md"}


def test_excludes_agree_between_python_and_shell():
    """OBS_EXCLUDES in lib.sh and EXCLUDES here must describe one set."""
    lib = (TOOLS / "lib.sh").read_text()
    block = re.search(r"OBS_EXCLUDES=\((.*?)\n\)", lib, re.S)
    assert block, "OBS_EXCLUDES not found in lib.sh"
    shell = re.findall(r"'([^']+)'", block.group(1))
    assert shell == EXCLUDES, (
        "rsync and the reconciler disagree about the synchronized content set")


def test_forbidden_and_excluded_helpers():
    assert is_excluded(".obsidian/plugins/x/data.json")
    assert is_excluded("ProjectAria/.DS_Store")
    assert not is_excluded("ProjectAria/Planning/PLAN.md")
    assert is_forbidden("bridge/dat/config.json")
    assert not is_forbidden("ProjectAria/Planning/PLAN.md")


def test_every_input_path_gets_an_outcome(tmp_path):
    base = {f"d{i}/n{i}.md": "v0" for i in range(5)}
    mac = dict(base, **{"mac-only.md": "m"})
    corsair = dict(base, **{"corsair-only.md": "c"})
    rc, ws, manifest = run_reconcile(tmp_path, base, mac, corsair)
    assert rc == 0
    assert set(manifest) == set(base) | {"mac-only.md", "corsair-only.md"}
    assert all(d["outcome"] for d in manifest.values())


def test_workspace_must_be_empty(tmp_path):
    ws = tmp_path / "reconcile"
    ws.mkdir()
    (ws / "stale").write_text("x")
    build(tmp_path / "base", {"n.md": "v0"})
    build(tmp_path / "mac", {"n.md": "v0"})
    build(tmp_path / "corsair", {"n.md": "v0"})
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "reconcile.py"),
         "--base", str(tmp_path / "base"), "--mac", str(tmp_path / "mac"),
         "--corsair", str(tmp_path / "corsair"), "--workspace", str(ws)],
        capture_output=True, text=True)
    assert proc.returncode == 2
    assert "not empty" in proc.stderr
