#!/usr/bin/env python3
"""
ARIA - Obsidian LiveSync recovery: CouchDB curl credential file

Phase: Obsidian LiveSync Corsair recovery (Phases 0, 1, 5)
Purpose: hand CouchDB credentials to curl without ever putting them in argv

Related plan sections:
- Section 3.6: Security boundaries
- Section 7, Phase 1: "Do not print the request authorization header"

Reads a bridge config.json (or the Obsidian plugin's data.json), extracts the
CouchDB endpoint and basic-auth pair for the `obsidian` database, and writes a
mode-0600 curl --config file. Credentials therefore never appear in the process
table, in shell history, or in any captured log. The endpoint is printed on
stdout as OBS_COUCH_URL=... for the caller to eval; the credentials are not.

Usage:
    couch-config.py --source /path/to/config.json --out /path/to/curlrc
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path


def from_bridge_config(doc: dict) -> tuple[str, str, str, str]:
    for peer in doc.get("peers", []):
        if peer.get("type") == "couchdb":
            return (peer["url"], peer.get("database", "obsidian"),
                    peer["username"], peer["password"])
    raise KeyError("no couchdb peer in bridge config")


def from_plugin_data(doc: dict) -> tuple[str, str, str, str]:
    return (doc["couchDB_URI"], doc.get("couchDB_DBNAME", "obsidian"),
            doc["couchDB_USER"], doc["couchDB_PASSWORD"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True, type=Path,
                    help="bridge dat/config.json or plugin data.json")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    try:
        doc = json.loads(args.source.read_text(encoding="utf-8"))
    except OSError as exc:
        # Deliberately terse: the path may itself be sensitive.
        print(f"couch-config: cannot read source ({exc.strerror})", file=sys.stderr)
        return 2

    try:
        url, db, user, password = (
            from_bridge_config(doc) if "peers" in doc else from_plugin_data(doc))
    except KeyError as exc:
        print(f"couch-config: source is missing {exc}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Create restricted, then write - never widen an existing file's mode.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f'user = "{user}:{password}"\n')
        fh.write("silent\n")
        fh.write("show-error\n")

    # Only non-secret facts reach stdout.
    print(f"OBS_COUCH_URL={url.rstrip('/')}")
    print(f"OBS_COUCH_DB={db}")
    print(f"OBS_COUCH_CURL_CONFIG={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
