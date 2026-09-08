"""One Mongo aggregate makes task acceptance + run revision atomic on standalone Mongo."""
from __future__ import annotations

from datetime import datetime, timezone

from aria.core.logging import scrub_secrets
from aria.guard.policy import record_event
from aria.ralph.git import OwnershipError
from pymongo.errors import DuplicateKeyError


def now():
    return datetime.now(timezone.utc)


class RunStore:
    def __init__(self, db):
        self.db = db
        self.runs = db.ralph_runs

    async def initialize(self):
        await self.runs.create_index([("state", 1), ("updated_at", -1)])
        await self.runs.create_index([("target", 1), ("state", 1)])
        await self.db.ralph_logs.create_index([("run_id", 1), ("attempt_id", 1), ("at", 1)])

    async def get(self, run_id):
        run = await self.runs.find_one({"_id": run_id})
        if not run:
            raise KeyError("Ralph run not found")
        return run

    async def reserve_target(self, run, state_root, owner):
        key = {"_id": run["target"]}
        try:
            await self.db.ralph_targets.update_one(key, {"$setOnInsert": {
                "host": run["host"], "state_root": str(state_root), "owner": None, "run_id": None,
            }}, upsert=True)
        except DuplicateKeyError:
            pass
        result = await self.db.ralph_targets.update_one(
            {**key, "host": run["host"], "state_root": str(state_root), "owner": None},
            {"$set": {"owner": owner, "run_id": run["_id"]}},
        )
        if result.matched_count != 1:
            raise OwnershipError("Repository reserved by another run, or controller host/state root differs; recover its run first")

    async def release_target(self, run):
        await self.db.ralph_targets.update_one(
            {"_id": run["target"], "run_id": run["_id"], "owner": run["owner"]},
            {"$set": {"owner": None, "run_id": None}},
        )

    async def save(self, run, *, acceptance=False):
        query = {"_id": run["_id"], "version": run["version"], "owner": run["owner"]}
        if acceptance:
            query["requested"] = {"$in": ["run", "pause"]}
        fields = {k: v for k, v in run.items() if k not in {"_id", "requested", "version"}}
        fields["updated_at"] = now()
        result = await self.runs.update_one(query, {"$set": fields, "$inc": {"version": 1}})
        if result.matched_count != 1:
            raise OwnershipError("Controller ownership, version, or acceptance authorization changed")
        run["version"] += 1
        run["updated_at"] = fields["updated_at"]

    async def event(self, run, kind, detail, *, acceptance=False):
        event = {"kind": kind, "detail": scrub_secrets(detail)[:2000], "at": now(),
                 "attempt_id": run.get("active_attempt"), "revision": run.get("accepted_revision")}
        run["events"].append(event)
        # This journal is the durable outbox/evidence; the existing guard event
        # stream is its best-effort cockpit projection, never acceptance authority.
        await self.save(run, acceptance=acceptance)
        await record_event(self.db, "ralph." + kind, event["detail"], actor="ralph",
                           session_id=run.get("active_attempt"),
                           extra={"run_id": run["_id"], "revision": event["revision"]})

    async def log(self, run_id, attempt_id, kind, payload):
        import json
        content = scrub_secrets(json.dumps(payload, default=str))
        truncated = len(content) > 1100000
        await self.db.ralph_logs.insert_one({
            "run_id": run_id, "attempt_id": attempt_id, "kind": kind, "at": now(),
            "content": content[:1100000] + ("\n[Record truncated at 1100000 characters]" if truncated else ""),
            "truncated": truncated,
        })
