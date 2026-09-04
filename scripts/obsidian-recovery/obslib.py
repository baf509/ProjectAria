"""
ARIA - Obsidian LiveSync recovery: shared Python helpers

Phase: Obsidian LiveSync Corsair recovery (Phases 1, 2, 4)
Purpose: one definition of "the synchronized content set", plus hashing

Related plan sections:
- Section 5: Safety invariants (item 4: metadata never enters the content set)
- Section 7, Phase 2: exclusions from note reconciliation

The exclude list here and OBS_EXCLUDES in lib.sh describe the same set. They
are duplicated because one is consumed by rsync and one by the reconciler, but
tests/test_obsidian_recovery.py asserts they stay in agreement - a divergence
would mean the tree we merge is not the tree we seed.
"""

from __future__ import annotations

import hashlib
import os
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, Iterator

ABSENT = "ABSENT"

# Kept byte-identical to OBS_EXCLUDES in lib.sh (see module docstring).
EXCLUDES = [
    ".git",
    ".git/**",
    ".gitignore",
    ".obsidian",
    ".obsidian/**",
    ".trash",
    ".trash/**",
    ".DS_Store",
    "**/.DS_Store",
    "bridge",
    "bridge/**",
    ".obsidian-livesync-*",
    "*.tmp",
    "*.swp",
    "*~",
]

# Anything matching these must never appear in a merged tree. Distinct from
# EXCLUDES: those are skipped silently as machine-local metadata, these are a
# hard validation failure because their presence means secret material or Git
# internals reached the content set (plan Sections 3.6 and 5.4).
FORBIDDEN = [
    "**/.git/**",
    "**/.git",
    "**/config.json",
    "**/*.pem",
    "**/*.key",
    "**/id_rsa*",
    "**/id_ed25519*",
]


def _matches(rel: str, patterns) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        if fnmatch(rel, pat):
            return True
        # A bare name like ".obsidian" should exclude the directory wherever it
        # appears, not only at the vault root.
        if "/" not in pat and any(fnmatch(p, pat) for p in parts):
            return True
    return False


def is_excluded(rel: str) -> bool:
    return _matches(rel, EXCLUDES)


def is_forbidden(rel: str) -> bool:
    return _matches(rel, FORBIDDEN)


def walk_content(root: Path) -> Iterator[str]:
    """Yield relative POSIX paths of the synchronized content set under root."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        # Prune excluded directories in place so we never descend into .git or
        # a nested recovery snapshot.
        dirnames[:] = [
            d for d in dirnames
            if not is_excluded(f"{rel_dir}/{d}".lstrip("/"))
        ]
        for name in filenames:
            rel = f"{rel_dir}/{name}".lstrip("/")
            if is_excluded(rel):
                continue
            full = root / rel
            # Symlinks are not part of the note set; following them could take
            # the merge outside the vault entirely.
            if full.is_symlink() or not full.is_file():
                continue
            yield rel


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_tree(root: Path) -> Dict[str, str]:
    """Map relative path -> sha256 for every file in the content set."""
    root = Path(root)
    return {rel: sha256_file(root / rel) for rel in sorted(walk_content(root))}
