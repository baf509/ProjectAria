#!/usr/bin/env python3
"""
ARIA - Obsidian LiveSync recovery: bridge peer identity

Phase: Obsidian LiveSync Corsair recovery (Phase 3)
Purpose: inspect or safely adjust host-local bridge storage settings

Related plan sections:
- Section 3.4: Unique peer identities
- Section 7, Phase 3: Prepare bridge identities and clean metadata boundaries

Peer names are host-local Deno checkpoint keys. Reusing `vault-remote` or
`corsair-files` on another host does not create a cross-host identity collision.
This tool therefore preserves peer names and credentials byte for byte; it can
adjust only the storage root and bootstrap scan flag.

Two modes:
    --show     read-only: print the peer identity table, secrets redacted
    --apply    rewrite the config (a mode-preserved .bak is kept first)

Usage:
    bridge-config.py --config dat/config.json --host mac --show
    bridge-config.py --config dat/config.json --host corsair \
                     --base-dir /app/vault/ --scan-offline false --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

HOSTS = ("mac", "corsair")

SECRET_KEYS = {"password", "passphrase", "obfuscatePassphrase", "username", "url"}


def redact(peer: dict) -> dict:
    return {k: ("<redacted>" if k in SECRET_KEYS else v) for k, v in peer.items()}


def check(config: dict, host: str) -> list[str]:
    problems: list[str] = []
    peers = config.get("peers", [])
    names = [p.get("name") for p in peers]
    if len(names) != len(set(names)):
        problems.append(f"duplicate peer names within the config: {names}")

    for kind in ("couchdb", "storage"):
        matches = [p for p in peers if p.get("type") == kind]
        if len(matches) != 1:
            problems.append(f"expected exactly one {kind} peer, found {len(matches)}")
            continue
        peer = matches[0]
        if not peer.get("name"):
            problems.append(f"{kind} peer has no host-local name")
        if peer.get("group") != "main":
            problems.append(f"{kind} peer group is {peer.get('group')!r}, expected 'main'")

    couch = next((p for p in peers if p.get("type") == "couchdb"), {})
    for key in ("database", "username", "password", "url", "passphrase"):
        if not couch.get(key):
            problems.append(f"couchdb peer is missing {key}")

    storage = next((p for p in peers if p.get("type") == "storage"), {})
    if not storage.get("baseDir"):
        problems.append("storage peer has no baseDir")

    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--host", required=True, choices=HOSTS)
    ap.add_argument("--base-dir", help="storage peer baseDir")
    ap.add_argument("--scan-offline", choices=("true", "false"),
                    help="storage peer scanOfflineChanges (Phase 3 wants false "
                         "for the first post-reconciliation start)")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))

    if args.apply:
        for peer in config.get("peers", []):
            kind = peer.get("type")
            if kind == "storage":
                if args.base_dir:
                    peer["baseDir"] = args.base_dir
                if args.scan_offline:
                    peer["scanOfflineChanges"] = args.scan_offline == "true"

        problems = check(config, args.host)
        if problems:
            print("bridge-config: refusing to write an invalid config:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 3

        backup = args.config.with_suffix(args.config.suffix + ".bak-prerecovery")
        if not backup.exists():
            shutil.copy2(args.config, backup)
            os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)

        # Write restricted from creation; a config that is briefly world-
        # readable is a credential leak, and this file holds four.
        tmp = args.config.with_suffix(args.config.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=4)
            fh.write("\n")
        os.replace(tmp, args.config)
        os.chmod(args.config, stat.S_IRUSR | stat.S_IWUSR)
        print(f"bridge-config: wrote {args.config} (mode 0600, backup at {backup.name})",
              file=sys.stderr)

    problems = check(config, args.host)
    if args.show or not args.apply:
        print(json.dumps({"host": args.host,
                          "peers": [redact(p) for p in config.get("peers", [])]},
                         indent=2))
    if problems:
        print(f"bridge-config: {len(problems)} problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 3
    print("bridge-config: peer identity valid", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
