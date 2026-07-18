#!/usr/bin/env python3
"""
ARIA - S5 embedding migration: BSON Binary subtype 0 -> native vector subtype 9.

Re-encodes existing memory embeddings from the legacy struct-packed float32
representation (BSON Binary subtype 0) to MongoDB's native BSON vector type
(subtype 9), which is what `$vectorSearch` / mongot expect.

Idempotent: docs already stored as subtype 9 are skipped. This is a pure
re-encoding — the float values are unchanged, so NO re-embedding happens and
the 1024-dim / voyage-4-nano invariant is preserved.

Related: SHARED_SERVICES_DESIGN.md · S5

Usage (from the api/ dir, with the venv active):
    python -m aria.scripts.migrate_embeddings_vector_subtype9              # dry run
    python -m aria.scripts.migrate_embeddings_vector_subtype9 --apply      # write
    python -m aria.scripts.migrate_embeddings_vector_subtype9 --apply --db abp
"""
import argparse
import struct
import sys

from bson import Binary
from bson.binary import BinaryVectorDtype, VECTOR_SUBTYPE
from pymongo import MongoClient

from aria.config import settings


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate memory embeddings to BSON vector subtype 9.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--db", default=settings.mongodb_database, help="database name")
    ap.add_argument("--collection", default="memories", help="collection name")
    ap.add_argument("--batch", type=int, default=500, help="cursor batch size")
    args = ap.parse_args()

    client = MongoClient(settings.mongodb_uri)
    coll = client[args.db][args.collection]

    query = {"embedding": {"$type": "binData"}}
    total = coll.count_documents(query)
    print(f"[{args.db}.{args.collection}] docs with a binary embedding: {total}")
    if not args.apply:
        print("DRY RUN — no writes. Re-run with --apply to migrate.\n")

    migrated = skipped = errors = 0
    cursor = coll.find(query, {"embedding": 1}, no_cursor_timeout=True).batch_size(args.batch)
    try:
        for doc in cursor:
            emb = doc.get("embedding")
            if emb is None:
                continue
            if getattr(emb, "subtype", 0) == VECTOR_SUBTYPE:
                skipped += 1
                continue
            try:
                floats = list(struct.unpack(f"{len(emb) // 4}f", emb))
                native = Binary.from_vector([float(x) for x in floats], BinaryVectorDtype.FLOAT32)
                if args.apply:
                    coll.update_one({"_id": doc["_id"]}, {"$set": {"embedding": native}})
                migrated += 1
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"  ! {doc.get('_id')}: {e}", file=sys.stderr)
    finally:
        cursor.close()
        client.close()

    verb = "migrated" if args.apply else "would migrate"
    print(f"\n{verb}: {migrated} | already subtype-9: {skipped} | errors: {errors}")
    if not args.apply and migrated:
        print("Re-run with --apply to write these changes.")
    if args.apply and migrated:
        print("Done. Re-sync the memory_vector_index if recall doesn't pick up immediately.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
