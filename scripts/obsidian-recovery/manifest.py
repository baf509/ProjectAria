#!/usr/bin/env python3
"""
ARIA - Obsidian LiveSync recovery: SHA-256 tree manifest

Phase: Obsidian LiveSync Corsair recovery (Phase 1 validation, Phase 4 proof)
Purpose: emit a stable "<sha256>  <relpath>" manifest of a vault tree

Related plan sections:
- Section 6: Required artifacts (SHA-256 file manifests)
- Section 7, Phase 4: prove the synchronized content set is identical

Usage:
    manifest.py --root /path/to/tree [--out manifest.txt]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obslib import hash_tree  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    if not args.root.is_dir():
        print(f"manifest: not a directory: {args.root}", file=sys.stderr)
        return 2

    lines = [f"{digest}  {rel}" for rel, digest in hash_tree(args.root).items()]
    body = "\n".join(lines) + ("\n" if lines else "")
    if args.out:
        args.out.write_text(body, encoding="utf-8")
        print(f"{len(lines)} files -> {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
