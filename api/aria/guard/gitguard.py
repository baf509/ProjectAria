"""
ARIA - Guard Git Protocol

Purpose: ARIA holds the git pen. Worktrees, checkpoint commits, tags, mirror
pushes, the merge gate and rollbacks all run in the aria-api process, outside the
agent's sandbox, so that (a) an agent cannot skip its own checkpoint, and (b) a
session that goes wrong is always ≤ one `git reset --hard` from recovery
(proposal §2 principle 11, §7.2).

Shape of a session's life:

    prepare_session()  worktree on aria/<project>/<sid8>, tag aria/ckpt/<sid>/start,
                       bare mirror ~/git-safe/<repo>.git wired up as remote `safe`
    checkpoint()       size-guarded `add` + commit as aria-guard, pushed to `safe`
    merge_gate()       check_command · diff size · protected paths · gitleaks ·
                       charter allowed_paths → a verdict, and NOTHING else
    merge()            tag aria-pre-merge/<ts> FIRST, then squash-merge; refuses
                       unless a gate verdict passed against this exact HEAD
    rollback()         reset --hard, worktree only
    discard()          drop the worktree, keep the branch under parked/

⚠️ **THE SIZE GUARD IS LOAD-BEARING.** On 2026-08-15 a naive `git add -A` in this
house hashed 18 GB of unignored model weights and put 6 GB of loose objects in
.git before it was killed. `.gitignore` is not protection: checkpoints run
unattended, every few minutes, across repos whose ignore rules drift. So each
candidate file is size-checked before staging, oversized files are skipped **and
reported**, and a checkpoint whose total exceeds the budget is abandoned rather
than half-written — the same semantics as
`infrastructure/scripts/git-safety-net.sh`, which is where these numbers were
learned.

Why the gate and the merge are separate calls: A ≤ 2 autonomy means a merge needs
Ben's `APPLY <id>`. A gate that merged on success would make that decision
unreachable, so `merge_gate()` is pure evaluation and `merge()` is a separate,
admin-keyed action.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import signal
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from aria.config import settings
from aria.guard import policy as guard_policy
from aria.guard.policy import GuardPolicy, load_policy, record_event
from aria.infrastructure.git_worktree import (
    WorktreeError,
    _ignore_worktrees_dir,
    _slugify,
    ensure_repo,
)

logger = logging.getLogger(__name__)

GUARD_SESSIONS_COLLECTION = "guard_sessions"
GUARD_CHECKPOINTS_COLLECTION = "guard_checkpoints"
GUARD_GATE_RUNS_COLLECTION = "guard_gate_runs"

# The identity every guard-authored commit carries. `git log --author=aria-guard`
# is then the complete list of what an agent wrote, separable from Ben's work.
GUARD_COMMITTER_NAME = "aria-guard"
GUARD_COMMITTER_EMAIL = "aria-guard@corsair-ai"

# Same detection the C1 verification gate uses: a global `make check` fallback
# landing in a repo with no such target is "no check configured", not a failure.
_GATE_NO_TARGET_RE = re.compile(
    r"No rule to make target|No targets specified|Makefile.*not found|"
    r"make(?:\[\d+\])?:\s*\*\*\*.*No such file|command not found",
    re.IGNORECASE,
)


class GuardGitError(Exception):
    """A git operation the guard needs failed."""


class GuardMergeConflict(GuardGitError):
    """The merge could not be completed — and the working tree is back as it was.

    Separate from `GuardGitError` because it is an ANSWER, not a fault: the
    caller turns it into a 409 ("this needs a human or a rebase"), whereas a
    bare GuardGitError from the merge path now means the far more serious "we
    could not put the tree back".
    """


def _now() -> datetime:
    return datetime.now(timezone.utc)


# How long a timed-out gitleaks scan is given before it is killed. A module
# constant so a test can shorten it without waiting three minutes.
GITLEAKS_TIMEOUT_SECONDS = 180


def _sid8(session_id: str) -> str:
    """A short, git-safe, COLLISION-RESISTANT label for a session id.

    It names branches (`aria/<project>/<sid8>`), worktree directories and
    `parked/` branches, so two sessions that share it cannot both exist: the
    second `git worktree add` fails and that session id can never be prepared.
    Truncating to 8 characters was fine for uuids and wrong for everything else
    — ARIA's own ids look like `sess-abcdef-0123456789`, where the first 8
    characters are `sess-abc` for every session ever created. The hash suffix is
    over the WHOLE id, so distinct ids stay distinct while the prefix keeps the
    name readable.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", session_id).strip("-.")
    safe = re.sub(r"\.{2,}", ".", safe) or "session"
    if len(safe) <= 8 and safe == session_id:
        return safe
    return f"{safe[:8]}-{hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:6]}"


class GitGuard:
    """The git protocol. `db` may be None — everything still works in-process,
    it just is not durable across a restart (degrade, never refuse)."""

    def __init__(
        self,
        db=None,
        *,
        mirror_root: Optional[str] = None,
        policy: Optional[GuardPolicy] = None,
        notifier=None,
    ):
        self.db = db
        self.mirror_root = os.path.expanduser(mirror_root or settings.guard_mirror_root)
        self._policy = policy
        self._notifier = notifier
        self._sessions: dict[str, dict] = {}

    @property
    def policy(self) -> GuardPolicy:
        return self._policy or load_policy()

    # -- plumbing ---------------------------------------------------------

    async def _git(
        self, args: list[str], cwd: str, timeout: int = 120
    ) -> tuple[int, str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "/bin/true"
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise GuardGitError(f"`git {' '.join(args)}` timed out after {timeout}s")
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )

    async def _git_ok(self, args: list[str], cwd: str, timeout: int = 120) -> str:
        rc, out, err = await self._git(args, cwd, timeout)
        if rc != 0:
            raise GuardGitError(f"`git {' '.join(args)}` in {cwd} failed: {err or out}")
        return out

    async def _event(self, kind: str, detail: str, **kwargs) -> dict:
        return await record_event(self.db, kind, detail, **kwargs)

    async def _alert(self, event_type: str, detail: str, **kwargs) -> None:
        """Raise to Ben. Convenience path: FAILS OPEN.

        The durable half of a raise is the `guard_events` row and the merge
        gate's refusal, both of which happen whether or not this works; an alert
        backend that is down must not take the checkpoint down with it.
        """
        try:
            notifier = self._notifier
            if notifier is None:
                from aria.notifications.service import NotificationService

                notifier = self._notifier = NotificationService()
            await notifier.notify(
                source="guard", event_type=event_type, detail=detail, **kwargs
            )
        except Exception:  # noqa: BLE001
            logger.warning("guard: could not raise alert %s", event_type, exc_info=True)

    async def _raise_on_protected(
        self, record: dict, paths: Iterable[str], policy: GuardPolicy
    ) -> list[dict]:
        """A protected path was just staged — raise NOW, not at merge time.

        `guard/policy.yaml` promises "A touch is an immediate raise, not merely a
        merge rejection", and until this existed `is_protected()` had exactly one
        caller: the merge gate. So an agent could rewrite `api/aria/guard/**` or
        `.env`, have it committed by the guard itself every ten minutes, and the
        first anyone heard of it was whenever someone happened to run a gate.

        The checkpoint still stages the file. Refusing to would leave the edit
        uncommitted forever, which is both un-revertible (no commit to reset to)
        and a permanent gate deadlock — see `_classify_candidate`. Capture it,
        shout, and let the gate be the thing that refuses.
        """
        already = set(record.get("raised_protected") or [])
        hits: list[dict] = []
        for path in dict.fromkeys(paths):
            pattern = guard_policy.protecting_pattern(path, record["repo"], policy)
            if pattern:
                hits.append({"path": path, "pattern": pattern})
        if not hits:
            return []

        session_id = record["_id"]
        fresh = [h for h in hits if h["path"] not in already]
        detail = (
            f"session {session_id} modified {len(hits)} protected path(s): "
            + "; ".join(f"{h['path']} (rule {h['pattern']})" for h in hits[:10])
        )
        await self._event(
            "policy:protected_touch", detail,
            session_id=session_id, path=hits[0]["path"],
            blocked=True, severity="critical",
            extra={"hits": hits, "branch": record.get("branch")},
        )
        if fresh:
            # Only new paths page: a Ralph-looped session checkpoints every few
            # minutes, and re-raising the same file forever trains Ben to ignore
            # the one alert that must never be ignored.
            await self._alert(
                "protected_path_touched", detail,
                project_path=record.get("repo"), severity="critical",
                needs_human=True, dedup_key=f"guard:protected:{session_id}",
                cooldown_seconds=0,
            )
            record["raised_protected"] = sorted(already | {h["path"] for h in fresh})
            await self._save_session(record)
        return hits

    # -- session registry --------------------------------------------------

    async def get_session(self, session_id: str) -> Optional[dict]:
        record = self._sessions.get(session_id)
        if record:
            return record
        if self.db is None:
            return None
        try:
            doc = await self.db[GUARD_SESSIONS_COLLECTION].find_one({"_id": session_id})
        except Exception:  # noqa: BLE001 — a Mongo blip must not lose the worktree
            logger.warning("guard: could not read guard session %s", session_id, exc_info=True)
            return None
        if doc:
            self._sessions[session_id] = doc
        return doc

    async def _save_session(self, record: dict) -> None:
        self._sessions[record["_id"]] = record
        if self.db is None:
            return
        try:
            await self.db[GUARD_SESSIONS_COLLECTION].update_one(
                {"_id": record["_id"]}, {"$set": record}, upsert=True
            )
        except Exception:  # noqa: BLE001
            logger.warning("guard: could not persist guard session %s",
                           record["_id"], exc_info=True)

    # -- mirror ------------------------------------------------------------

    def mirror_path(self, repo: str) -> str:
        """~/git-safe/<repo>.git — the naming git-safety-net.sh already uses, so
        the hourly script and the guard converge on one mirror per repo rather
        than two half-populated ones. A leading dot is dropped (~/.hermes →
        hermes.git), same as the script."""
        name = os.path.basename(os.path.abspath(repo))
        if name.startswith("."):
            name = name[1:]
        return os.path.join(self.mirror_root, f"{name}.git")

    async def ensure_mirror(self, repo: str) -> dict:
        """Create the bare mirror if absent and point remote `safe` at it.

        Never `origin`: ProjectAria's origin is a PUBLIC GitHub repo, and pushing
        aria/* scratch there would publish it (§7.2, D6). denyNonFastForwards +
        denyDeletes mean a compromised session cannot erase history that already
        reached the mirror.
        """
        mirror = self.mirror_path(repo)
        created = False
        if not os.path.isdir(mirror):
            try:
                os.makedirs(self.mirror_root, exist_ok=True)
            except OSError as exc:
                # e.g. guard_mirror_root points at a file, or the disk is full.
                # Surfaced as GuardGitError so prepare_session degrades to
                # "no mirror" instead of failing the whole session.
                raise GuardGitError(f"mirror root {self.mirror_root} is unusable: {exc}") from exc
            rc, _, err = await self._git(["init", "--bare", "--quiet", mirror], cwd=self.mirror_root)
            if rc != 0:
                raise GuardGitError(f"could not create mirror {mirror}: {err}")
            created = True
        for key, value in (
            ("receive.denyNonFastForwards", "true"),
            ("receive.denyDeletes", "true"),
            ("receive.denyCurrentBranch", "refuse"),
            ("gc.reflogExpire", "never"),
            ("gc.reflogExpireUnreachable", "never"),
        ):
            await self._git(["config", key, value], cwd=mirror)

        rc, _, _ = await self._git(["remote", "get-url", "safe"], cwd=repo)
        verb = "set-url" if rc == 0 else "add"
        await self._git(["remote", verb, "safe", mirror], cwd=repo)
        return {"mirror": mirror, "created": created}

    async def _push_safe(self, cwd: str, refspecs: list[str]) -> dict:
        """Best-effort mirror push. A dead mirror must not stop a checkpoint —
        the commit is already durable in the worktree's own object store; the
        push is what shrinks RPO from hours to minutes."""
        rc, out, err = await self._git(["push", "safe", *refspecs], cwd=cwd, timeout=180)
        if rc != 0:
            logger.warning("guard: push to safe failed (%s): %s", refspecs, err or out)
        return {"pushed": rc == 0, "detail": (err or out)[-500:] if rc != 0 else ""}

    # -- prepare -----------------------------------------------------------

    async def prepare_session(
        self, repo: str, session_id: str, project_slug: Optional[str] = None
    ) -> dict:
        """Worktree + branch + start tag + mirror. Idempotent, and RECOVERABLE.

        Recoverable is the part that was missing: reuse required
        `isdir(worktree)`, and the fall-through re-ran `git worktree add` on a
        path git still had registered ("fatal: … is already registered"), so a
        session whose worktree directory had been deleted — by a `rm -rf`, a
        crash, a full disk — could never be prepared again under that id. Its
        checkpoints, branch and start tag all key off the id, so that is a
        permanently unrecoverable session. `discard()` has always pruned; this
        is the same one line, on the path that actually needs it.
        """
        repo = os.path.abspath(os.path.expanduser(repo))
        existing = await self.get_session(session_id)
        recovered = False
        if existing and os.path.isdir(existing.get("worktree", "")):
            return {**existing, "session_id": session_id, "reused": True}
        if existing and existing.get("worktree"):
            recovered = True
            await self._git(["worktree", "prune"], cwd=repo)
            await self._event(
                "session:worktree_recovered",
                f"worktree {existing['worktree']} was gone; pruning the stale "
                f"registration and re-adding it on {existing.get('branch')}",
                session_id=session_id, path=existing["worktree"], severity="warning",
            )

        try:
            initialized = await asyncio.to_thread(ensure_repo, repo)
            await asyncio.to_thread(_ignore_worktrees_dir, repo)
        except WorktreeError as exc:
            raise GuardGitError(str(exc)) from exc

        project = _slugify(project_slug or os.path.basename(repo))
        sid8 = _sid8(session_id)
        branch = f"{settings.guard_branch_prefix}/{project}/{sid8}"
        worktree = os.path.join(repo, ".worktrees", f"{project}-{sid8}")
        source_branch = await self._current_branch(repo)

        # git_worktree.create_worktree() is reused for the repo preparation
        # (ensure_repo/_ignore_worktrees_dir) but not for the add itself: it
        # names the branch after its own timestamped slug, and the guard needs
        # the deterministic aria/<project>/<sid8> name that the ladder, the gate
        # and `parked/` all key off.
        os.makedirs(os.path.dirname(worktree), exist_ok=True)
        rc, _, _ = await self._git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo)
        add_args = (["worktree", "add", worktree, branch] if rc == 0
                    else ["worktree", "add", worktree, "-b", branch])
        rc, out, err = await self._git(add_args, cwd=repo)
        if rc != 0 and "already registered" in (err or out):
            # The stale-registration case for a session this GitGuard has never
            # seen (a restart lost the in-memory map, Mongo lost the doc).
            await self._git(["worktree", "prune"], cwd=repo)
            recovered = True
            rc, out, err = await self._git(add_args, cwd=repo)
        if rc != 0:
            raise GuardGitError(f"could not create worktree {worktree}: {err or out}")

        start_tag = f"aria/ckpt/{session_id}/start"
        rc, _, err = await self._git(["tag", "-f", start_tag, "HEAD"], cwd=worktree)
        if rc != 0:
            logger.warning("guard: could not tag %s: %s", start_tag, err)
            start_tag = ""

        mirror_info: dict[str, Any]
        try:
            mirror_info = await self.ensure_mirror(repo)
        except GuardGitError as exc:
            # A missing mirror costs RPO, not correctness. Refusing the session
            # over it would make ~/git-safe a single point of failure for all
            # coding work.
            logger.warning("guard: mirror unavailable for %s: %s", repo, exc)
            mirror_info = {"mirror": None, "created": False, "error": str(exc)}

        # The worktree's private git dir lives under <repo>/.git/worktrees/<name>,
        # i.e. OUTSIDE the rw-bound worktree. Reported here so the launch path can
        # decide deliberately: leaving it read-only is what stops the agent
        # committing (the guard holds the pen) and also breaks `git status`'s
        # index refresh inside the sandbox — measure before choosing.
        rc, git_dir, _ = await self._git(["rev-parse", "--absolute-git-dir"], cwd=worktree)

        record = {
            "_id": session_id,
            "session_id": session_id,
            "repo": repo,
            "worktree": worktree,
            "git_dir": git_dir if rc == 0 else None,
            "branch": branch,
            "source_branch": source_branch,
            "project": project,
            "start_tag": start_tag,
            "mirror": mirror_info.get("mirror"),
            "repo_initialized": initialized,
            "status": "active",
            "created_at": _now(),
        }
        await self._save_session(record)

        if mirror_info.get("mirror"):
            refspecs = [f"refs/heads/{branch}:refs/heads/{branch}"]
            if start_tag:
                refspecs.append(f"refs/tags/{start_tag}:refs/tags/{start_tag}")
            await self._push_safe(worktree, refspecs)

        await self._event(
            "session:prepared",
            f"worktree {worktree} on {branch} (tag {start_tag or 'none'})",
            session_id=session_id, path=worktree,
        )
        return {**record, "reused": False, "recovered": recovered}

    async def _current_branch(self, cwd: str) -> str:
        rc, out, _ = await self._git(["symbolic-ref", "--short", "HEAD"], cwd=cwd)
        return out if rc == 0 and out else "HEAD"

    # -- checkpoint --------------------------------------------------------

    async def checkpoint(self, session_id: str, reason: str = "interval") -> dict:
        """Stage (size-guarded), commit as aria-guard, push to `safe`.

        No-ops on a clean tree so the interval timer can call it blindly.
        """
        record = await self.get_session(session_id)
        if not record:
            return {"ok": False, "reason": "no guard session for this id", "committed": False}
        worktree = record["worktree"]
        if not os.path.isdir(worktree):
            return {"ok": False, "reason": f"worktree {worktree} is gone", "committed": False}

        policy = self.policy
        max_file = policy.checkpoint_max_file_bytes
        max_total = policy.checkpoint_max_total_bytes

        deleted = await self._zsplit(["ls-files", "-d", "-z"], worktree)
        candidates = await self._zsplit(
            ["ls-files", "-m", "-o", "--exclude-standard", "-z"], worktree
        )

        to_add: list[str] = []
        skipped: list[dict] = []
        total = 0
        for rel in candidates:
            verdict, size = _classify_candidate(worktree, rel, max_file)
            if verdict is not None:
                skipped.append(verdict)
                continue
            total += size
            to_add.append(rel)

        if total > max_total:
            detail = (
                f"candidate set is {total // 1048576} MiB (> {max_total // 1048576} MiB "
                f"budget) — fix .gitignore rather than raising the cap"
            )
            await self._event(
                "checkpoint:aborted", detail, session_id=session_id,
                path=worktree, blocked=True, severity="critical",
            )
            return {
                "ok": False, "committed": False, "aborted": True, "reason": detail,
                "bytes": total, "files": len(to_add), "skipped": skipped,
            }

        if skipped:
            # Never silently: a real source file lost to the size guard is a
            # data-loss bug, and the only way anyone finds out is this event.
            await self._event(
                "checkpoint:skipped",
                f"{len(skipped)} file(s) not checkpointed: "
                + ", ".join(f"{s['path']} ({s['reason']})" for s in skipped[:5]),
                session_id=session_id, path=worktree, severity="warning",
            )

        if not to_add and not deleted:
            # NOT "clean" when something is still sitting in the worktree: the
            # gate measures dirt with `git status --porcelain`, which counts the
            # files this function just refused to stage, so reporting "clean"
            # here sent the operator to run a checkpoint that could never help.
            if skipped:
                return {
                    "ok": True, "committed": False, "reason": "blocked",
                    "skipped": skipped, "blocking": skipped,
                    "detail": "nothing could be staged; the worktree is NOT clean and the "
                              "merge gate will stay red until these are resolved: "
                              + "; ".join(f"{s['path']} ({s['reason']})" for s in skipped[:5]),
                }
            return {"ok": True, "committed": False, "reason": "clean", "skipped": []}

        for chunk in _chunks(to_add, 200):
            rc, _, err = await self._git(["add", "--ignore-errors", "--", *chunk], cwd=worktree)
            if rc != 0:
                logger.warning("guard: partial add failure in %s: %s", worktree, err)
        for rel in deleted:
            await self._git(["update-index", "--force-remove", "--", rel], cwd=worktree)

        rc, _, _ = await self._git(["diff", "--cached", "--quiet"], cwd=worktree)
        if rc == 0:
            return {"ok": True, "committed": False, "reason": "nothing staged", "skipped": skipped}

        protected = await self._raise_on_protected(record, to_add + deleted, policy)

        message = f"aria-ckpt: {_sid8(session_id)} {reason} {_now().strftime('%Y-%m-%dT%H:%M:%SZ')}"
        rc, out, err = await self._git(
            [
                "-c", f"user.name={GUARD_COMMITTER_NAME}",
                "-c", f"user.email={GUARD_COMMITTER_EMAIL}",
                # --no-verify: hooks live in the repo the agent is editing, so a
                # pre-commit hook is agent-controlled code that would run in
                # ARIA's process, outside the sandbox (the Amazon Q wiper class).
                "commit", "--no-verify", "-m", message,
            ],
            cwd=worktree,
        )
        if rc != 0:
            raise GuardGitError(f"checkpoint commit failed: {err or out}")

        sha = await self._git_ok(["rev-parse", "HEAD"], cwd=worktree)
        push = await self._push_safe(
            worktree, [f"HEAD:refs/heads/{record['branch']}"]
        ) if record.get("mirror") else {"pushed": False, "detail": "no mirror"}

        result = {
            "ok": True, "committed": True, "sha": sha, "session_id": session_id,
            "branch": record["branch"], "files": len(to_add), "deletions": len(deleted),
            "bytes": total, "skipped": skipped, "skipped_count": len(skipped),
            "protected": protected, "reason": reason, "at": _now(), **push,
        }
        if self.db is not None:
            try:
                await self.db[GUARD_CHECKPOINTS_COLLECTION].insert_one(dict(result))
            except Exception:  # noqa: BLE001
                logger.warning("guard: could not persist checkpoint %s", sha, exc_info=True)
        await self._event(
            "checkpoint:committed",
            f"{sha[:12]} on {record['branch']} ({len(to_add)} files, {total // 1024} KiB)",
            session_id=session_id, path=worktree,
        )
        return result

    async def _zsplit(self, args: list[str], cwd: str) -> list[str]:
        rc, out, _ = await self._git(args, cwd)
        if rc != 0 or not out:
            return []
        return [p for p in out.split("\0") if p]

    # -- rollback ----------------------------------------------------------

    async def rollback(self, session_id: str, to: str = "start") -> dict:
        """`git reset --hard` inside the worktree, and nowhere else.

        Untracked files are deliberately left alone (no `git clean`): the
        rollback exists to undo a bad edit, and taking new files with it would
        make rollback itself a data-loss event.
        """
        record = await self.get_session(session_id)
        if not record:
            return {"ok": False, "reason": "no guard session for this id"}
        worktree = record["worktree"]
        if not os.path.isdir(worktree):
            return {"ok": False, "reason": f"worktree {worktree} is gone"}

        target = record.get("start_tag") if to == "start" else to
        if not target:
            return {"ok": False, "reason": "no start tag recorded for this session"}

        rc, _, err = await self._git(["rev-parse", "--verify", f"{target}^{{commit}}"], cwd=worktree)
        if rc != 0:
            return {"ok": False, "reason": f"unknown rollback target {target}: {err}"}

        before = await self._git_ok(["rev-parse", "HEAD"], cwd=worktree)
        rc, out, err = await self._git(["reset", "--hard", target], cwd=worktree)
        if rc != 0:
            raise GuardGitError(f"rollback to {target} failed: {err or out}")
        after = await self._git_ok(["rev-parse", "HEAD"], cwd=worktree)

        await self._event(
            "session:rollback", f"{before[:12]} -> {after[:12]} (target {target})",
            session_id=session_id, path=worktree, severity="warning",
        )
        return {"ok": True, "session_id": session_id, "target": target,
                "from": before, "to": after}

    # -- merge gate --------------------------------------------------------

    async def merge_gate(
        self,
        session_id: str,
        *,
        check_command: Optional[str] = None,
        allowed_paths: Optional[Iterable[str]] = None,
        max_lines: Optional[int] = None,
        max_files: Optional[int] = None,
    ) -> dict:
        """Run every check and return a verdict. Never merges, never mutates."""
        record = await self.get_session(session_id)
        if not record:
            return {"passed": False, "checks": [
                {"name": "session", "passed": False, "detail": "no guard session for this id"}
            ]}
        worktree = record["worktree"]
        repo = record["repo"]
        policy = self.policy
        checks: list[dict] = []

        head = await self._git_ok(["rev-parse", "HEAD"], cwd=worktree)
        base = await self._merge_base(worktree, record)

        checks.append(await self._check_worktree_clean(worktree))
        checks.append(await self._check_command(worktree, check_command, session_id))
        changed = await self._changed_paths(worktree, base, head)
        checks.append(await self._check_diff_size(
            worktree, base, head,
            max_lines if max_lines is not None else policy.diff_max_lines,
            max_files if max_files is not None else policy.diff_max_files,
        ))
        checks.append(self._check_protected(changed, repo, policy))
        checks.append(await self._check_gitleaks(worktree, base, head, policy))
        checks.append(self._check_allowed_paths(changed, allowed_paths))

        failed = [c for c in checks if c["passed"] is False]
        skipped = [c["name"] for c in checks if c.get("skipped")]
        verdict = {
            "session_id": session_id,
            "passed": not failed,
            "checks": checks,
            "failed": [c["name"] for c in failed],
            "skipped": skipped,
            "branch": record["branch"],
            "base": base,
            "head_sha": head,
            "changed_files": len(changed),
            "at": _now(),
        }

        if self.db is not None:
            try:
                await self.db[GUARD_GATE_RUNS_COLLECTION].insert_one(dict(verdict))
            except Exception:  # noqa: BLE001
                logger.warning("guard: could not persist gate run", exc_info=True)
        record["last_gate"] = {
            "passed": verdict["passed"], "head_sha": head,
            "at": verdict["at"], "failed": verdict["failed"],
        }
        await self._save_session(record)

        if failed:
            await self._event(
                "gate:failed",
                f"{record['branch']} failed: {', '.join(verdict['failed'])}",
                session_id=session_id, path=worktree, severity="warning",
            )
        return verdict

    async def _merge_base(self, worktree: str, record: dict) -> str:
        source = record.get("source_branch") or "HEAD"
        rc, out, _ = await self._git(["merge-base", source, "HEAD"], cwd=worktree)
        if rc == 0 and out:
            return out
        # No common ancestor (or the source branch is gone): the pre-session tag
        # is the other honest baseline — everything since the session started.
        if record.get("start_tag"):
            rc, out, _ = await self._git(
                ["rev-parse", f"{record['start_tag']}^{{commit}}"], cwd=worktree
            )
            if rc == 0 and out:
                return out
        return "HEAD"

    async def _check_worktree_clean(self, worktree: str) -> dict:
        """Dirty tree = no merge. The DETAIL is the load-bearing part.

        "Run a checkpoint first" is useless advice for a file no checkpoint can
        ever stage (a symlink, or something over the file cap), and that was the
        deadlock: the gate said run a checkpoint, the checkpoint said "clean",
        and the session could never merge. Same classifier as `checkpoint()`, so
        the two cannot drift apart again, and the message names the file and the
        actual fix.
        """
        dirty = _porcelain_paths(await self._zsplit(["status", "--porcelain", "-z"], worktree))
        if not dirty:
            return {"name": "worktree_clean", "passed": True, "detail": "clean"}

        policy = self.policy
        blocking = [
            verdict for verdict in (
                _classify_candidate(worktree, rel, policy.checkpoint_max_file_bytes)[0]
                for rel in dirty
            ) if verdict is not None
        ]
        detail = (
            f"{len(dirty)} uncommitted change(s) — run a checkpoint first, "
            "a merge only carries committed work"
        )
        if blocking:
            detail = (
                f"{len(dirty)} uncommitted change(s), {len(blocking)} of which a checkpoint "
                "CANNOT stage — running one will not clear the gate. Resolve them by hand "
                "(delete, .gitignore, or commit deliberately): "
                + "; ".join(f"{b['path']} ({b['reason']})" for b in blocking[:10])
            )
        return {
            "name": "worktree_clean",
            "passed": False,
            "detail": detail,
            "dirty": len(dirty),
            "blocking": blocking,
        }

    async def _check_command(
        self, worktree: str, check_command: Optional[str], session_id: str = ""
    ) -> dict:
        command = check_command or settings.coding_gate_command
        if not command:
            return {"name": "check_command", "passed": True, "skipped": True,
                    "detail": "no check command configured"}
        try:
            # Explicit, scrubbed env. The gate runs the WORKTREE'S OWN
            # Makefile/test target as ben — that is the point of a check
            # command, and it means agent-authored code executes here. Passing
            # aria-api's environment through would hand that code ADMIN_KEY and
            # API_KEY (both live in the .env this unit loads), which is exactly
            # the escalation the key split exists to prevent.
            from aria.guard.sandbox import session_env

            proc = await asyncio.create_subprocess_shell(
                command, cwd=worktree,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=session_env(os.environ, session_id=session_id),
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"name": "check_command", "passed": False,
                    "detail": f"`{command}` could not be run: {exc}"}
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=settings.coding_gate_timeout_seconds
            )
        except asyncio.TimeoutError:
            # Returning without killing it leaks the child: a hung `make check`
            # is agent-authored code running as ben with a lock on the worktree,
            # and nothing would ever reap it (`_git` in this file has always
            # killed on timeout — this path simply did not).
            await _terminate(proc)
            return {"name": "check_command", "passed": False,
                    "detail": f"`{command}` timed out after "
                              f"{settings.coding_gate_timeout_seconds}s and was killed"}
        except Exception as exc:  # noqa: BLE001
            await _terminate(proc)
            return {"name": "check_command", "passed": False,
                    "detail": f"`{command}` could not be run: {exc}"}

        text = stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0 and _GATE_NO_TARGET_RE.search(text):
            # C1's rule: a missing check is "no check configured", not a red
            # gate — otherwise every repo without a Makefile is unmergeable.
            return {"name": "check_command", "passed": True, "skipped": True,
                    "detail": f"`{command}` has no target here; check skipped"}
        return {
            "name": "check_command",
            "passed": proc.returncode == 0,
            "detail": f"`{command}` exited {proc.returncode}\n{text[-1500:]}",
        }

    async def _changed_paths(self, worktree: str, base: str, head: str) -> list[str]:
        rc, out, _ = await self._git(
            ["diff", "--name-only", "--no-renames", "-z", base, head], cwd=worktree
        )
        if rc != 0 or not out:
            return []
        return [p for p in out.split("\0") if p]

    async def _check_diff_size(
        self, worktree: str, base: str, head: str, max_lines: int, max_files: int
    ) -> dict:
        rc, out, err = await self._git(["diff", "--numstat", base, head], cwd=worktree)
        if rc != 0:
            return {"name": "diff_size", "passed": False,
                    "detail": f"could not measure the diff: {err}"}
        files = 0
        lines = 0
        for row in out.splitlines():
            parts = row.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            for value in parts[:2]:
                if value.isdigit():          # "-" for binary files
                    lines += int(value)
        ok = files <= max_files and lines <= max_lines
        return {
            "name": "diff_size", "passed": ok,
            "detail": f"{files} file(s) / {lines} line(s) changed "
                      f"(caps: {max_files} files, {max_lines} lines)",
            "files": files, "lines": lines,
        }

    def _check_protected(self, changed: list[str], repo: str, policy: GuardPolicy) -> dict:
        hits = []
        for path in changed:
            pattern = guard_policy.protecting_pattern(path, repo, policy)
            if pattern:
                hits.append({"path": path, "pattern": pattern})
        return {
            "name": "protected_paths",
            "passed": not hits,
            "detail": "no protected path touched" if not hits else
                      "; ".join(f"{h['path']} (rule {h['pattern']})" for h in hits[:10]),
            "hits": hits,
        }

    async def _check_gitleaks(
        self, worktree: str, base: str, head: str, policy: GuardPolicy
    ) -> dict:
        if not policy.gitleaks_enabled:
            return {"name": "gitleaks", "passed": True, "skipped": True,
                    "detail": "gitleaks disabled by policy"}
        binary = shutil.which(settings.guard_gitleaks_binary)
        if not binary:
            # Explicitly SKIPPED, not passed: "we did not look" and "we looked
            # and found nothing" must not read the same in the verdict.
            return {"name": "gitleaks", "passed": True, "skipped": True,
                    "detail": f"{settings.guard_gitleaks_binary} is not installed — "
                              "the diff was NOT scanned for secrets"}
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "detect", "--no-banner", "--redact", "--exit-code", "1",
                "--source", worktree, "--log-opts", f"{base}..{head}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"name": "gitleaks", "passed": False,
                    "detail": f"gitleaks could not be run: {exc}"}
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=GITLEAKS_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            await _terminate(proc)
            return {"name": "gitleaks", "passed": False,
                    "detail": f"gitleaks did not finish within {GITLEAKS_TIMEOUT_SECONDS}s "
                              "and was killed; the diff was NOT scanned"}
        except Exception as exc:  # noqa: BLE001
            await _terminate(proc)
            return {"name": "gitleaks", "passed": False,
                    "detail": f"gitleaks could not be run: {exc}"}
        text = stdout.decode("utf-8", errors="replace")[-1500:]
        if proc.returncode == 0:
            return {"name": "gitleaks", "passed": True, "detail": "no leaks found"}
        # rc 1 = findings; anything else = the scanner itself failed, which is
        # not evidence of a clean diff — fail closed on a safety check.
        return {"name": "gitleaks", "passed": False,
                "detail": f"gitleaks exited {proc.returncode}\n{text}"}

    def _check_allowed_paths(
        self, changed: list[str], allowed_paths: Optional[Iterable[str]]
    ) -> dict:
        patterns = [p for p in (allowed_paths or []) if p]
        if not patterns:
            return {"name": "allowed_paths", "passed": True, "skipped": True,
                    "detail": "charter sets no allowed_paths"}
        outside = [p for p in changed if not guard_policy.match_any(p, patterns)]
        return {
            "name": "allowed_paths",
            "passed": not outside,
            "detail": "all changes inside the charter" if not outside else
                      f"outside allowed_paths: {', '.join(outside[:10])}",
        }

    # -- merge -------------------------------------------------------------

    async def merge(self, session_id: str, squash: bool = True, actor: str = "api") -> dict:
        """Merge the session branch into its source branch. Refuses without a
        passing gate against the CURRENT head.

        The head check is the point: gate-then-commit-more-then-merge would let a
        session pass review and merge something else entirely.
        """
        record = await self.get_session(session_id)
        if not record:
            return {"ok": False, "merged": False, "reason": "no guard session for this id"}

        worktree, repo = record["worktree"], record["repo"]
        branch, source = record["branch"], record.get("source_branch") or "HEAD"
        gate = record.get("last_gate") or {}
        head = await self._git_ok(["rev-parse", f"refs/heads/{branch}"], cwd=repo)

        if not gate.get("passed"):
            await self._event(
                "merge:refused", f"{branch}: merge gate has not passed",
                session_id=session_id, blocked=True, severity="warning",
            )
            return {"ok": False, "merged": False,
                    "reason": "merge gate has not passed for this session"}
        if gate.get("head_sha") != head:
            await self._event(
                "merge:refused",
                f"{branch}: gate passed at {str(gate.get('head_sha'))[:12]} but head is "
                f"{head[:12]} — re-run the gate",
                session_id=session_id, blocked=True, severity="warning",
            )
            return {"ok": False, "merged": False,
                    "reason": "the branch moved since the gate passed; re-run merge-gate"}
        if source in ("HEAD", branch):
            return {"ok": False, "merged": False,
                    "reason": f"refusing to merge into {source!r}"}

        # Every refusal is decided BEFORE the tag is written. The tag used to be
        # created and pushed first, so a merge refused for a dirty checkout —
        # the common case, since Ben works in this tree — littered an
        # `aria-pre-merge/<ts>` tag (and a mirror ref) on every attempt, for a
        # merge that never happened. Tag creation still precedes the merge
        # itself, which is the property §7.6 actually needs.
        checked_out = await self._checkout_of(repo, source)
        if checked_out:
            dirty = await self._zsplit(["status", "--porcelain", "-z"], checked_out)
            if dirty:
                await self._event(
                    "merge:refused",
                    f"{source} is checked out at {checked_out} with {len(dirty)} local "
                    "modification(s)",
                    session_id=session_id, blocked=True, severity="warning",
                )
                return {"ok": False, "merged": False, "pre_merge_tag": None,
                        "reason": f"{source} is checked out at {checked_out} with local "
                                  "modifications; commit or stash them first"}

        stamp = _now().strftime("%Y%m%dT%H%M%SZ")
        pre_tag = f"aria-pre-merge/{stamp}"
        source_tip = await self._git_ok(["rev-parse", f"refs/heads/{source}"], cwd=repo)
        rc, _, err = await self._git(["tag", "-f", pre_tag, source_tip], cwd=repo)
        if rc != 0:
            raise GuardGitError(f"could not create pre-merge tag {pre_tag}: {err}")
        await self._push_safe(repo, [f"refs/tags/{pre_tag}:refs/tags/{pre_tag}"])

        message = f"aria-merge: {branch} ({_sid8(session_id)}) into {source}"
        try:
            if checked_out:
                merged_sha = await self._merge_in_checkout(
                    checked_out, branch, message, squash, source_tip
                )
            else:
                merged_sha = await self._merge_detached(
                    repo, source, source_tip, branch, message, squash
                )
        except GuardMergeConflict as exc:
            # A conflict is an ordinary answer ("this needs a human"), not a
            # 500. The tree has been restored by this point — that is
            # `_merge_in_checkout`'s contract — so the session stays mergeable
            # after a rebase.
            await self._event(
                "merge:conflict", f"{branch} -> {source}: {exc}",
                session_id=session_id, path=repo, blocked=True, severity="warning",
            )
            return {"ok": False, "merged": False, "pre_merge_tag": pre_tag,
                    "conflict": True, "reason": str(exc)}

        await self._push_safe(repo, [
            f"refs/heads/{source}:refs/heads/{source}",
            f"refs/tags/{pre_tag}:refs/tags/{pre_tag}",
        ])
        record["status"] = "merged"
        record["merged_at"] = _now()
        record["merge_sha"] = merged_sha
        record["pre_merge_tag"] = pre_tag
        await self._save_session(record)
        await self._event(
            "merge:done",
            f"{branch} -> {source} as {merged_sha[:12]} (rollback: git reset --hard {pre_tag})",
            session_id=session_id, path=repo, actor=actor,
        )
        return {
            "ok": True, "merged": True, "session_id": session_id, "branch": branch,
            "into": source, "sha": merged_sha, "pre_merge_tag": pre_tag,
            "squash": squash, "rollback": f"git reset --hard {pre_tag}",
        }

    async def _checkout_of(self, repo: str, branch: str) -> Optional[str]:
        """Which working tree (if any) has `branch` checked out."""
        rc, out, _ = await self._git(["worktree", "list", "--porcelain"], cwd=repo)
        if rc != 0:
            return None
        path = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif line.startswith("branch ") and path:
                if line[len("branch "):].strip() == f"refs/heads/{branch}":
                    return path
        return None

    async def _merge_in_checkout(
        self, checkout: str, branch: str, message: str, squash: bool,
        head_before: Optional[str] = None,
    ) -> str:
        """Merge into a working tree a HUMAN may be sitting in.

        ⚠️ `git merge --abort` is not the recovery primitive for a squash merge.
        Verified with real git on 2026-08-15: `git merge --squash` records NO
        `MERGE_HEAD`, so on a conflict `--abort` fails with "There is no merge to
        abort" while the tree keeps `UU` conflict markers in tracked files. The
        old code ran it, discarded its return code, and raised — leaving Ben's
        checkout of `main` conflicted, the API returning 500, and nothing
        recorded. The tree is restored to `head_before` explicitly instead, and
        no recovery command's return code is ignored.
        """
        identity = ["-c", f"user.name={GUARD_COMMITTER_NAME}",
                    "-c", f"user.email={GUARD_COMMITTER_EMAIL}"]
        if head_before is None:
            head_before = await self._git_ok(["rev-parse", "HEAD"], cwd=checkout)
        args = (["merge", "--squash", branch] if squash
                else [*identity, "merge", "--no-ff", "--no-verify", "-m", message, branch])
        rc, out, err = await self._git(args, cwd=checkout)
        if rc != 0:
            await self._restore_checkout(checkout, head_before)
            raise GuardMergeConflict(
                f"merge of {branch} could not be completed and the checkout was "
                f"restored to {head_before[:12]}: {(err or out)[-500:]}"
            )
        if squash:
            rc, out, err = await self._git(
                ["-c", f"user.name={GUARD_COMMITTER_NAME}",
                 "-c", f"user.email={GUARD_COMMITTER_EMAIL}",
                 "commit", "--no-verify", "-m", message], cwd=checkout,
            )
            if rc != 0:
                await self._restore_checkout(checkout, head_before)
                raise GuardMergeConflict(
                    f"the squash of {branch} could not be committed and the checkout was "
                    f"restored to {head_before[:12]}: {(err or out)[-500:]}"
                )
        return await self._git_ok(["rev-parse", "HEAD"], cwd=checkout)

    async def _restore_checkout(self, checkout: str, head_before: str) -> None:
        """Put a working tree back exactly as it was, or say loudly that we could not.

        Called only after the dirty-check passed, so "as it was" means: HEAD at
        `head_before`, no staged changes, no untracked files. `git clean -fd` is
        safe for precisely that reason — `git status --porcelain` lists untracked
        files too, so anything untracked here was created by the failed merge.
        Ignored files are left alone (no `-x`).
        """
        problems: list[str] = []
        rc, _, _ = await self._git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=checkout)
        if rc == 0:
            rc, out, err = await self._git(["merge", "--abort"], cwd=checkout)
            if rc != 0:
                problems.append(f"merge --abort: {err or out}")
        for args in (["reset", "--hard", head_before], ["clean", "-fd"]):
            rc, out, err = await self._git(args, cwd=checkout)
            if rc != 0:
                problems.append(f"{' '.join(args)}: {err or out}")

        head_now = ""
        rc, head_now, _ = await self._git(["rev-parse", "HEAD"], cwd=checkout)
        leftover = await self._zsplit(["status", "--porcelain", "-z"], checkout)
        if problems or head_now != head_before or leftover:
            detail = (
                f"could not restore {checkout} to {head_before[:12]} after a failed merge "
                f"(HEAD is {head_now[:12] or 'unknown'}, {len(leftover)} path(s) still "
                f"modified){'; ' + '; '.join(problems) if problems else ''}"
            )
            await self._event(
                "merge:recovery_failed", detail, path=checkout,
                blocked=True, severity="critical",
            )
            # The one place the guard leaves a human's tree changed. It must be
            # impossible to miss, so it is an exception rather than a return
            # value a caller could ignore.
            raise GuardGitError(detail + " — this checkout needs manual repair")

    async def _merge_detached(
        self, repo: str, source: str, source_tip: str, branch: str, message: str, squash: bool
    ) -> str:
        """Merge a branch nobody has checked out, without touching any working
        tree: merge-tree writes the result to the object store and update-ref
        moves the branch with a compare-and-swap on its old value.

        `git merge --squash` would need a checkout, and checking the target out
        under Ben's dirty tree is how an agent's merge eats a human's work.
        """
        rc, out, err = await self._git(
            ["merge-tree", "--write-tree", source_tip, f"refs/heads/{branch}"], cwd=repo
        )
        if rc != 0:
            # Nothing to restore: merge-tree writes to the object store and
            # touches no working tree at all. That is why this is the safe half
            # of the merge and `_merge_in_checkout` is the dangerous one.
            raise GuardMergeConflict(
                f"merge of {branch} into {source} conflicts: {(out or err)[-500:]}"
            )
        tree = out.splitlines()[0].strip()
        parents = ["-p", source_tip] if squash else ["-p", source_tip, "-p", f"refs/heads/{branch}"]
        new_sha = await self._git_ok(
            ["-c", f"user.name={GUARD_COMMITTER_NAME}",
             "-c", f"user.email={GUARD_COMMITTER_EMAIL}",
             "commit-tree", tree, *parents, "-m", message], cwd=repo,
        )
        rc, out, err = await self._git(
            ["update-ref", f"refs/heads/{source}", new_sha, source_tip], cwd=repo
        )
        if rc != 0:
            raise GuardGitError(
                f"could not advance {source} (it moved under us): {err or out}"
            )
        return new_sha

    # -- discard -----------------------------------------------------------

    async def discard(self, session_id: str) -> dict:
        """Drop the worktree, keep the branch under parked/ (§6.2 rung L5).

        The branch survives on purpose: a parked session's work is the postmortem
        material, and deleting it would make "park" indistinguishable from "lose".
        """
        record = await self.get_session(session_id)
        if not record:
            return {"ok": False, "reason": "no guard session for this id"}
        repo, worktree, branch = record["repo"], record["worktree"], record["branch"]

        if os.path.isdir(worktree):
            rc, out, err = await self._git(["worktree", "remove", "--force", worktree], cwd=repo)
            if rc != 0:
                logger.warning("guard: worktree remove failed for %s: %s", worktree, err or out)
        await self._git(["worktree", "prune"], cwd=repo)

        prefix = f"{settings.guard_branch_prefix}/"
        parked = f"parked/{branch[len(prefix):]}" if branch.startswith(prefix) else f"parked/{branch}"
        rc, out, err = await self._git(["branch", "-m", branch, parked], cwd=repo)
        if rc != 0:
            logger.warning("guard: could not park %s: %s", branch, err or out)
            parked = branch
        else:
            await self._push_safe(repo, [f"refs/heads/{parked}:refs/heads/{parked}"])

        record["status"] = "discarded"
        record["parked_branch"] = parked
        record["discarded_at"] = _now()
        await self._save_session(record)
        await self._event(
            "session:discarded", f"worktree removed, branch parked as {parked}",
            session_id=session_id, path=worktree,
        )
        return {"ok": True, "session_id": session_id, "parked_branch": parked,
                "worktree_removed": not os.path.isdir(worktree)}


def _chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


async def _terminate(proc) -> None:
    """Kill a subprocess we have stopped waiting for, and REAP it.

    The whole process GROUP, because `make check` is a shell that spawns
    children: killing only the shell leaves the pytest run holding the worktree,
    which is the thing the timeout was trying to release. Both call sites pass
    `start_new_session=True` so the group is the command's own, never ours.

    Without the `wait()` the child becomes a zombie held by the event loop's
    child watcher, so "we killed it" would be as untrue as not killing it.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except Exception:  # noqa: BLE001
            logger.warning("guard: could not kill a timed-out child", exc_info=True)
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        logger.warning("guard: timed-out child did not reap")


def _classify_candidate(
    worktree: str, rel: str, max_file_bytes: int
) -> tuple[Optional[dict], int]:
    """(why this file cannot be checkpointed, size) — None means "stage it".

    THE ONE definition of un-checkpointable, used by `checkpoint()` when it
    decides what to stage and by `_check_worktree_clean` when it explains why
    the gate is red. They disagreed before: the checkpoint skipped symlinks
    silently and oversized files with a report, then returned `reason: "clean"`,
    while the gate's `git status --porcelain` still counted both. A session in
    that state could never merge and the operator was told to run a checkpoint,
    which could not help — a permanent deadlock built out of two functions that
    each believed they were right.
    """
    full = os.path.join(worktree, rel)
    try:
        if os.path.islink(full):
            # Never silently (the module contract). A symlink in a checkpoint is
            # not obviously wrong, but `git add` on one stores the link target,
            # which is a path on this box — and the guard's job is to say what
            # it did not capture.
            return {"path": rel, "bytes": 0, "reason": "symlink"}, 0
        if not os.path.exists(full):
            # Raced with a delete; `ls-files -d` covers the deletion separately.
            return None, 0
        if not os.path.isfile(full):
            return {"path": rel, "bytes": 0, "reason": "not a regular file"}, 0
        size = os.path.getsize(full)
    except OSError as exc:
        return {"path": rel, "bytes": 0, "reason": f"unreadable ({exc.strerror or exc})"}, 0
    if size > max_file_bytes:
        return {
            "path": rel, "bytes": size,
            "reason": f"over the {max_file_bytes // 1048576} MiB file cap",
        }, size
    return None, size


def _porcelain_paths(entries: list[str]) -> list[str]:
    """Repo-relative paths out of `git status --porcelain -z` records.

    With -z a rename is TWO records: `R  <new>` then a bare `<old>`. Splitting
    on NUL and treating every record as `XY <path>` would turn the old path into
    the nonsense path `me.py` (three characters eaten).
    """
    out: list[str] = []
    expect_source = False
    for entry in entries:
        if expect_source:
            expect_source = False
            continue
        if len(entry) > 3 and entry[2] == " ":
            status, path = entry[:2], entry[3:]
            if "R" in status or "C" in status:
                expect_source = True
            out.append(path.strip('"'))
        else:
            out.append(entry)
    return out


_git_guard: Optional[GitGuard] = None


def get_git_guard(db=None) -> GitGuard:
    """Process-wide GitGuard. The in-memory session map is what makes a
    checkpoint work when Mongo is briefly unreachable, so it must be one
    instance, not one per request."""
    global _git_guard
    if _git_guard is None:
        _git_guard = GitGuard(db=db)
    elif db is not None and _git_guard.db is None:
        _git_guard.db = db
    return _git_guard
