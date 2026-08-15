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
import logging
import os
import re
import shutil
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sid8(session_id: str) -> str:
    return session_id.replace("/", "-")[:8]


class GitGuard:
    """The git protocol. `db` may be None — everything still works in-process,
    it just is not durable across a restart (degrade, never refuse)."""

    def __init__(
        self,
        db=None,
        *,
        mirror_root: Optional[str] = None,
        policy: Optional[GuardPolicy] = None,
    ):
        self.db = db
        self.mirror_root = os.path.expanduser(mirror_root or settings.guard_mirror_root)
        self._policy = policy
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
        """Worktree + branch + start tag + mirror. Idempotent."""
        repo = os.path.abspath(os.path.expanduser(repo))
        existing = await self.get_session(session_id)
        if existing and os.path.isdir(existing.get("worktree", "")):
            return {**existing, "session_id": session_id, "reused": True}

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
        return {**record, "reused": False}

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
            full = os.path.join(worktree, rel)
            try:
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                size = os.path.getsize(full)
            except OSError:
                continue
            if size > max_file:
                skipped.append({"path": rel, "bytes": size})
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
                "checkpoint:skipped_large",
                f"{len(skipped)} file(s) over {max_file // 1048576} MiB not checkpointed: "
                + ", ".join(f"{s['path']} ({s['bytes'] // 1048576} MiB)" for s in skipped[:5]),
                session_id=session_id, path=worktree, severity="warning",
            )

        if not to_add and not deleted:
            return {"ok": True, "committed": False, "reason": "clean", "skipped": skipped}

        for chunk in _chunks(to_add, 200):
            rc, _, err = await self._git(["add", "--ignore-errors", "--", *chunk], cwd=worktree)
            if rc != 0:
                logger.warning("guard: partial add failure in %s: %s", worktree, err)
        for rel in deleted:
            await self._git(["update-index", "--force-remove", "--", rel], cwd=worktree)

        rc, _, _ = await self._git(["diff", "--cached", "--quiet"], cwd=worktree)
        if rc == 0:
            return {"ok": True, "committed": False, "reason": "nothing staged", "skipped": skipped}

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
            "reason": reason, "at": _now(), **push,
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
        checks.append(await self._check_command(worktree, check_command))
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
        dirty = await self._zsplit(["status", "--porcelain", "-z"], worktree)
        return {
            "name": "worktree_clean",
            "passed": not dirty,
            "detail": "clean" if not dirty else (
                f"{len(dirty)} uncommitted change(s) — run a checkpoint first, "
                "a merge only carries committed work"
            ),
        }

    async def _check_command(self, worktree: str, check_command: Optional[str]) -> dict:
        command = check_command or settings.coding_gate_command
        if not command:
            return {"name": "check_command", "passed": True, "skipped": True,
                    "detail": "no check command configured"}
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=worktree,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=settings.coding_gate_timeout_seconds
            )
        except asyncio.TimeoutError:
            return {"name": "check_command", "passed": False,
                    "detail": f"`{command}` timed out after "
                              f"{settings.coding_gate_timeout_seconds}s"}
        except Exception as exc:  # noqa: BLE001
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
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180)
        except Exception as exc:  # noqa: BLE001
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

        # The pre-merge tag goes on FIRST and is the documented rollback point
        # (§7.6). Tagging after the merge would leave the window where the thing
        # you need to roll back to has no name.
        stamp = _now().strftime("%Y%m%dT%H%M%SZ")
        pre_tag = f"aria-pre-merge/{stamp}"
        source_tip = await self._git_ok(["rev-parse", f"refs/heads/{source}"], cwd=repo)
        rc, _, err = await self._git(["tag", "-f", pre_tag, source_tip], cwd=repo)
        if rc != 0:
            raise GuardGitError(f"could not create pre-merge tag {pre_tag}: {err}")
        await self._push_safe(repo, [f"refs/tags/{pre_tag}:refs/tags/{pre_tag}"])

        checked_out = await self._checkout_of(repo, source)
        message = f"aria-merge: {branch} ({_sid8(session_id)}) into {source}"
        if checked_out:
            dirty = await self._zsplit(["status", "--porcelain", "-z"], checked_out)
            if dirty:
                await self._event(
                    "merge:refused",
                    f"{source} is checked out at {checked_out} with {len(dirty)} local "
                    "modification(s)",
                    session_id=session_id, blocked=True, severity="warning",
                )
                return {"ok": False, "merged": False, "pre_merge_tag": pre_tag,
                        "reason": f"{source} is checked out at {checked_out} with local "
                                  "modifications; commit or stash them first"}
            merged_sha = await self._merge_in_checkout(checked_out, branch, message, squash)
        else:
            merged_sha = await self._merge_detached(repo, source, source_tip, branch, message, squash)

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
        self, checkout: str, branch: str, message: str, squash: bool
    ) -> str:
        identity = ["-c", f"user.name={GUARD_COMMITTER_NAME}",
                    "-c", f"user.email={GUARD_COMMITTER_EMAIL}"]
        args = (["merge", "--squash", branch] if squash
                else [*identity, "merge", "--no-ff", "--no-verify", "-m", message, branch])
        rc, out, err = await self._git(args, cwd=checkout)
        if rc != 0:
            await self._git(["merge", "--abort"], cwd=checkout)
            raise GuardGitError(f"merge of {branch} failed: {err or out}")
        if squash:
            rc, out, err = await self._git(
                ["-c", f"user.name={GUARD_COMMITTER_NAME}",
                 "-c", f"user.email={GUARD_COMMITTER_EMAIL}",
                 "commit", "--no-verify", "-m", message], cwd=checkout,
            )
            if rc != 0:
                raise GuardGitError(f"squash commit failed: {err or out}")
        return await self._git_ok(["rev-parse", "HEAD"], cwd=checkout)

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
            raise GuardGitError(f"merge of {branch} into {source} conflicts: {out or err}")
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
