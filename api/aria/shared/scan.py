"""
ARIA - Shared Services · S2: Scan / Reconcile Worker substrate

ONE periodic worker observes live machine state and feeds pluggable emitters, so
we never build two scanners. The Ontology graph registers an emitter that upserts
entity `attributes`; the Coherence layer (C2) registers one that writes
machine-change memories. Read-only, timeout-bounded, idempotent.

Emitter interface:
    class ScanEmitter:
        async def emit(self, db, snapshot: dict, diff: dict) -> None: ...

Related: SHARED_SERVICES_DESIGN.md · S2 (substrate), S3 (review), ownership.py
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Protocol

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.memory.long_term import LongTermMemory
from aria.shared.review import add_review_item

logger = logging.getLogger(__name__)

STATE_COLLECTION = "scan_state"


async def _run_cmd(cmd: list[str], timeout: float = 10.0) -> str:
    """Run a read-only command; return stdout or "" on any failure/timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except (FileNotFoundError, OSError) as e:
        logger.debug("scan: cannot exec %s: %s", cmd[0], e)
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning("scan: %s timed out", cmd[0])
        return ""


async def collect_snapshot() -> dict:
    """Build a normalized observed-state snapshot of the local machine."""
    containers_raw, services_raw, ports_raw = await asyncio.gather(
        _run_cmd(["docker", "ps", "--format", "{{.Names}}"]),
        _run_cmd(["systemctl", "--user", "list-units", "--type=service",
                  "--state=running", "--no-legend", "--plain"]),
        _run_cmd(["ss", "-tlnH"]),
    )
    containers = sorted({l.strip() for l in containers_raw.splitlines() if l.strip()})
    services = sorted({
        l.split()[0] for l in services_raw.splitlines()
        if l.strip() and l.split()[0].endswith(".service")
    })
    ports = sorted({
        part.rsplit(":", 1)[-1]
        for l in ports_raw.splitlines() if l.strip()
        for part in [l.split()[3]] if ":" in part and part.rsplit(":", 1)[-1].isdigit()
    })
    return {
        "containers": containers,
        "services": services,
        "ports": ports,
        "at": datetime.now(timezone.utc),
    }


def _diff(prev: Optional[dict], cur: dict) -> dict:
    """added/removed per category between two snapshots."""
    out: dict = {}
    for key in ("containers", "services", "ports"):
        old = set((prev or {}).get(key, []) or [])
        new = set(cur.get(key, []) or [])
        added, removed = sorted(new - old), sorted(old - new)
        if added or removed:
            out[key] = {"added": added, "removed": removed}
    return out


class ScanEmitter(Protocol):
    async def emit(self, db: AsyncIOMotorDatabase, snapshot: dict, diff: dict) -> None: ...


class MachineScanMemoryEmitter:
    """Coherence C2: write a memory when a container/service appears or disappears."""

    def __init__(self, node_id: str):
        self.node_id = node_id

    async def emit(self, db: AsyncIOMotorDatabase, snapshot: dict, diff: dict) -> None:
        if not diff:
            return
        ltm = LongTermMemory(db)
        for key in ("containers", "services"):
            change = diff.get(key)
            if not change:
                continue
            for name in change.get("added", []):
                await ltm.create_memory(
                    content=f"{key[:-1]} '{name}' started on {self.node_id}.",
                    content_type="event",
                    categories=["machine_scan", key],
                    importance=0.3,
                    confidence=0.9,
                    source={"type": "machine_scan", "node": self.node_id, "change": "added"},
                    private=True,
                )
            for name in change.get("removed", []):
                await ltm.create_memory(
                    content=f"{key[:-1]} '{name}' stopped on {self.node_id}.",
                    content_type="event",
                    categories=["machine_scan", key],
                    importance=0.4,
                    confidence=0.9,
                    source={"type": "machine_scan", "node": self.node_id, "change": "removed"},
                    private=True,
                )


class GitChangeEmitter:
    """Coherence C2: mint a memory when a repo gets new commits since we last
    looked — "what changed in my code," the half of C2 the machine-scan
    snapshot/diff above doesn't cover (that's containers/services, not git).

    Independent of this worker's node-wide snapshot/diff: ignores both params
    and runs its own per-repo cursor (last-seen HEAD sha), stored in the same
    `scan_state` collection under a `git:<repo>` key so it doesn't collide
    with the node-keyed snapshot doc. Read-only (rev-parse / diff --shortstat
    / log), timeout-bounded via `_run_cmd`.

    Deliberately commits-only, not uncommitted/dirty-tree changes — there's
    no stable cursor for "still dirty," so tracking it would re-fire the same
    memory every tick for as long as something sits uncommitted.
    """

    _SHORTSTAT_RE = re.compile(
        r"(?:(\d+) files? changed)?,?\s*"
        r"(?:(\d+) insertions?\(\+\))?,?\s*"
        r"(?:(\d+) deletions?\(-\))?"
    )

    def __init__(self, roots: list[str], min_change_lines: int = 10):
        self.roots = roots
        self.min_change_lines = min_change_lines

    async def emit(self, db: AsyncIOMotorDatabase, snapshot: dict, diff: dict) -> None:
        from aria.shells.harvest import _find_git_repos

        ltm = LongTermMemory(db)
        for repo in _find_git_repos(self.roots):
            try:
                await self._emit_for_repo(db, ltm, repo)
            except Exception as exc:  # noqa: BLE001 — one bad repo must not kill the tick
                logger.debug("git-change scan failed for %s: %s", repo, exc)

    async def _emit_for_repo(
        self, db: AsyncIOMotorDatabase, ltm: LongTermMemory, repo: str
    ) -> None:
        cursor_id = f"git:{repo}"
        state = await db[STATE_COLLECTION].find_one({"_id": cursor_id})
        last_sha = (state or {}).get("last_sha")

        head = (await _run_cmd(["git", "-C", repo, "rev-parse", "HEAD"])).strip()
        if not head:
            return  # not a real repo yet (no commits), or rev-parse failed

        # First time seeing this repo: establish the baseline only, same
        # "don't diff against nothing" rule the node-wide snapshot follows.
        if last_sha and last_sha != head:
            shortstat = await _run_cmd(
                ["git", "-C", repo, "diff", "--shortstat", f"{last_sha}..{head}"]
            )
            match = self._SHORTSTAT_RE.search(shortstat)
            changed_lines = sum(int(g) for g in (match.groups() if match else ()) if g)
            if changed_lines >= self.min_change_lines:
                subjects_raw = await _run_cmd(
                    ["git", "-C", repo, "log", f"{last_sha}..{head}", "--pretty=format:%s", "-n", "20"]
                )
                subjects = [l for l in subjects_raw.splitlines() if l.strip()]
                repo_name = repo.rstrip("/").rsplit("/", 1)[-1]
                await ltm.create_memory(
                    content=(
                        f"{repo_name}: {len(subjects)} new commit(s), "
                        f"{shortstat.strip() or 'changes'}. "
                        f"{'; '.join(subjects[:5])}"
                    ),
                    content_type="event",
                    categories=["machine_scan", "git", repo_name],
                    importance=0.4,
                    confidence=0.9,
                    source={"type": "machine_scan", "repo": repo, "revision": head},
                    private=True,
                )

        if last_sha != head:
            await db[STATE_COLLECTION].update_one(
                {"_id": cursor_id},
                {"$set": {"last_sha": head, "repo": repo, "updated_at": datetime.now(timezone.utc)}},
                upsert=True,
            )


class ScanReconcileWorker:
    """Periodic scan → diff → emitters. Start/stop/_run pattern (see shells/adopt.py)."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        emitters: Optional[list[ScanEmitter]] = None,
        interval_seconds: int = 300,
        node_id: str = "corsair-ai",
    ):
        self.db = db
        self.emitters = emitters or []
        self.interval = max(30, interval_seconds)
        self.node_id = node_id
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="shared.scan_reconcile")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _tick(self) -> None:
        snapshot = await collect_snapshot()
        state = await self.db[STATE_COLLECTION].find_one({"_id": self.node_id})
        prev = state.get("snapshot") if state else None
        diff = _diff(prev, snapshot)

        # First run establishes the baseline only — everything would otherwise
        # look "added" and spam a memory per container/service. Emit changes only
        # once we have a prior snapshot to diff against.
        if prev is not None and diff:
            for em in self.emitters:
                try:
                    await em.emit(self.db, snapshot, diff)
                except Exception as e:  # noqa: BLE001 — one emitter must not kill the tick
                    logger.error("scan emitter %s failed: %s", type(em).__name__, e)

            # S3: a removed service is worth a glance
            for key in ("containers", "services"):
                for name in diff.get(key, {}).get("removed", []):
                    await add_review_item(
                        self.db, kind="removed", subject=name,
                        detail=f"{key[:-1]} '{name}' disappeared on {self.node_id}",
                    )

        await self.db[STATE_COLLECTION].update_one(
            {"_id": self.node_id},
            {"$set": {"snapshot": snapshot, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as e:  # noqa: BLE001
                logger.warning("scan tick failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
