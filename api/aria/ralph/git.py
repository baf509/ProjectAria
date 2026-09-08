"""Private checkpoints and immutable candidates; no source branch mutation."""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from pathlib import Path

from aria.ralph.runtime import process


class OwnershipError(RuntimeError):
    pass


class TargetLock:
    """Local-host single writer, shared by all Ralph runs/controllers for a Git target.

    flock is intentionally not a time-expiring lease: a stalled live owner
    cannot overlap its replacement. Mongo CAS additionally fences stale writes.
    """

    def __init__(self, root: Path, target: str):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / (hashlib.sha256(target.encode()).hexdigest() + ".lock")
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise OwnershipError("Another controller owns this repository target")
        return self

    def release(self):
        if self.file:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None


GIT_ENV = {
    "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "aria-guard",
    "GIT_AUTHOR_EMAIL": "aria-guard@localhost", "GIT_COMMITTER_NAME": "aria-guard",
    "GIT_COMMITTER_EMAIL": "aria-guard@localhost", "LC_ALL": "C",
}


async def git(*args, stdin=None, env=None, max_output=4 * 1024 * 1024):
    code, output = await process(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *map(str, args)],
        env={**GIT_ENV, **(env or {})}, stdin=stdin, timeout=60, max_output=max_output,
    )
    if code:
        raise ValueError("Git operation failed: " + output.decode(errors="replace")[-2000:])
    return output


def validate_tree(path: Path, max_bytes=128 * 1024 * 1024):
    total, count = 0, 0
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            item = Path(base) / name
            info = item.lstat()
            if name in {".git", ".ralph-invalid-archive"} or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError("Git metadata, symlinks, and special files are not permitted")
            if stat.S_ISREG(info.st_mode):
                count += 1
                total += info.st_size
                if info.st_size > 16 * 1024 * 1024 or total > max_bytes or count > 30000:
                    raise ValueError("Candidate exceeds file count or size limits")


class GitWorkspace:
    def __init__(self, root: Path):
        self.root = root
        self.git_dir = root / "checkpoints.git"

    async def initialize(self, repository: str, revision: str):
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.git_dir.exists():
            # Local object copy retains source ancestry. No hooks or remote
            # transport is executed; source .git/config is never copied.
            await git("clone", "--bare", "--local", "--no-hardlinks", "--", repository, self.git_dir)
            await git("--git-dir", self.git_dir, "remote", "remove", "origin")
        await git("--git-dir", self.git_dir, "cat-file", "-e", revision + "^{commit}")

    async def checkout(self, revision: str, destination: Path):
        if destination.exists():
            raise ValueError("Refusing to overwrite a retained workspace")
        # git archive honors candidate-controlled export-ignore/export-subst.
        # Export raw blobs instead, so no accepted file can disappear from the
        # verified snapshot through .gitattributes.
        entries = await git("--git-dir", self.git_dir, "ls-tree", "-r", "-z", revision)
        files = []
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ")
            path = raw_path.decode()
            if (kind != b"blob" or mode not in {b"100644", b"100755"}
                    or not (destination / path).resolve().is_relative_to(destination.resolve())
                    or ".git" in path.lower().split("/")):
                raise ValueError("Unsupported repository entry (submodules/symlinks are not supported)")
            files.append((path, mode, oid))
        if len(files) > 30000:
            raise ValueError("Repository exceeds 30000 files")
        blobs = await git("--git-dir", self.git_dir, "cat-file", "--batch",
                          stdin=b"\n".join(f[2] for f in files) + b"\n" if files else b"",
                          max_output=160 * 1024 * 1024)
        destination.mkdir(parents=True)
        offset = 0
        for path, mode, oid in files:
            end = blobs.index(b"\n", offset)
            header = blobs[offset:end].split()
            if header[:2] != [oid, b"blob"]:
                raise ValueError("Candidate object identity mismatch")
            size = int(header[2])
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blobs[end + 1:end + 1 + size])
            target.chmod(0o755 if mode == b"100755" else 0o644)
            offset = end + size + 2
        validate_tree(destination)

    async def snapshot(self, workspace: Path, parent: str, attempt_id: str) -> str:
        validate_tree(workspace)
        env = {"GIT_INDEX_FILE": str(self.root / (attempt_id + ".index"))}
        args = ["-C", workspace, "--git-dir", self.git_dir, "--work-tree", workspace]
        await git(*args, "read-tree", parent, env=env)
        # Development outputs remain in the retained workspace. Acceptance is
        # against an export of this commit, never against those ignored files.
        await git(*args, "add", "--all", "--", ".", env=env)
        tree = (await git(*args, "write-tree", env=env)).decode().strip()
        commit = await git("--git-dir", self.git_dir, "commit-tree", tree, "-p", parent,
                           stdin=f"Ralph candidate {attempt_id}\n".encode())
        return commit.decode().strip()

    async def tree(self, revision: str) -> str:
        return (await git("--git-dir", self.git_dir, "rev-parse", revision + "^{tree}")).decode().strip()

    async def changes(self, base: str, candidate: str) -> list[str]:
        output = await git("--git-dir", self.git_dir, "diff", "--no-renames", "--name-only", "-z", base, candidate)
        return [p.decode() for p in output.split(b"\0") if p]

    async def diff(self, base: str, candidate: str) -> str:
        return (await git("--git-dir", self.git_dir, "diff", "--no-ext-diff", "--no-textconv", base, candidate)).decode(errors="replace")

    async def checkpoint_ref(self, run_id: str, revision: str):
        # Derived convenience ref. Mongo acceptance evidence is authoritative;
        # rebuilding this ref never means accepting an unverified commit.
        await git("--git-dir", self.git_dir, "update-ref", "refs/heads/ralph/" + run_id, revision)
