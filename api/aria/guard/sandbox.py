"""
ARIA - Guard Sandbox

Purpose: Build the bwrap + systemd-run argv that a coding session runs inside,
decide whether a spawn is allowed at all, and scrub the environment it inherits.

This generalises `tools/builtin/shell.py:_build_sandbox_prefix` (ARIA's own shell
tool) from "one short command with no network" to "a long-lived coding agent that
must keep working". Four deliberate differences, each one a thing that breaks if
you copy the shell tool's profile verbatim:

  - **The network stays OPEN.** The agent talks to model endpoints (`:8108`,
    `:8080`), package registries and git remotes. `--unshare-net` would make
    every session fail at turn 1. Credentials are protected by masking the files
    instead of by removing the network (proposal §7.3; loopback/registry
    allow-listing is Phase 4 work). That trade only holds if the *file* masking
    is complete — an open network plus a readable `.env` is a one-line
    escalation to `POST /guard/sessions/{id}/merge`, so see `_credential_files`.
  - **`~/.pi/agent/sessions` is READ-WRITE.** pi streams its structured
    transcript there and the MetaSupervisor reads it back for loop/stall signals
    (§6.1). Masking it would blind the whole meta layer while looking like it
    worked.
  - **`~/.pi/agent/models.json` is READ-ONLY, not masked.** It is pi's provider
    table; without it pi cannot resolve a model and exits immediately.
  - **Sibling repos under ~/Development are masked.** That directory is the blast
    radius that made `rm -rf ~/Development` a total loss (§7.1). The session's own
    repo stays visible but read-only (only its worktree is bound read-write), so
    the agent can read the source tree it was pointed at and write only where the
    guard can roll it back.

bwrap sets PR_SET_NO_NEW_PRIVS itself on the unprivileged path — there is no flag
to pass, and inventing one (`--no-new-privs`) makes bwrap exit 1, i.e. refuses
every session. `--new-session` (no TIOCSTI injection into ARIA's terminal) and
`--die-with-parent` (nothing outlives the session) are passed explicitly.

**Order is a safety property, not a formatting choice.** bwrap applies operations
left to right and a later operation wins, so the argv is built in four phases:
masks → exceptions → writable surface → *re-applied* file masks. Two rules keep
that from being merely a convention:

  1. No `--ro-bind`/`--bind` may name a masked directory, a path inside one, or
     an ANCESTOR of one — the ancestor case is the subtle one, because
     `--bind /home/ben /home/ben` replaces the mount that `--tmpfs ~/.ssh` was
     hanging off and un-masks the keys without ever naming them. `_bind_conflict`
     refuses those; `policy.py` independently refuses to let the policy file put
     them in the list at all.
  2. A masked FILE inside a legitimately bound directory is re-applied after the
     bind (phase 4). `~/.claude` must be readable for `claude_code` to
     authenticate, and `~/.claude/.credentials.json` must not be readable at
     all — those are only compatible if the file mask outlives the directory
     bind. (Codex's reference bwrap policy re-applies `.git` read-only the same
     way, for the same reason.)
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Optional

from aria.config import settings
from aria.guard.policy import GuardPolicy, guard_state_path, load_policy

logger = logging.getLogger(__name__)

# Masked over the real path, not /var/run/docker.sock: /var/run is a symlink to
# /run on this box and bwrap resolves the destination, so binding the symlinked
# name is at best redundant and at worst an error that refuses the spawn.
_DOCKER_SOCKET = "/run/docker.sock"

# Credential FILES, masked by name wherever they are found in the trees a
# session can still see. Stripping the environment (`_STRIP_EXACT`) was never
# enough on its own: `.env` is gitignored, so it is not in the worktree, but it
# sits at the root of the repo the session is pointed at — and that repo is
# deliberately left readable (`sibling_repo_masks` skips it) under `--ro-bind /
# /`. Verified on 2026-08-15: a ProjectAria session could `cat ../../.env`, read
# ADMIN_KEY, and POST it to `/guard/sessions/{id}/merge` on localhost:8200 —
# the exact escalation `api/deps.py` says the key split exists to prevent, and
# `deps.py` even documents the split as existing "precisely because a session
# can read .env".
_CREDENTIAL_FILE_GLOBS = (".env", ".env.*", "*.pem", "credentials.json")
# Inside any `.claude` directory found in a visible tree, plus ~/.claude, which
# is READ-ONLY BOUND for claude_code to authenticate at all. `.credentials.json`
# there holds the Anthropic OAuth token — i.e. exactly the key the `_API_KEY`
# env-strip was meant to keep out of a session, handed back by the ro-bind.
_CLAUDE_CREDENTIAL_FILES = (".credentials.json", "settings.local.json")

# Bounded walk: a credential sweep that recurses into node_modules or a model
# directory would take longer than the session it is protecting.
_WALK_PRUNE = frozenset({
    ".git", ".worktrees", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "target",
    ".next", "models", "weights", "site-packages",
})
_WALK_MAX_DEPTH = 4

# Environment variables never handed to a session. Exact names first, then
# suffixes, because the interesting ones keep being invented (BRAVE_API_KEY,
# RESTIC_PASSWORD, GH_TOKEN...). ANTHROPIC_API_KEY is intentionally caught by the
# `_API_KEY` suffix: claude_code authenticates from ~/.claude (bound read-only),
# so dropping the variable costs nothing and removes a key an agent could
# exfiltrate over the open network.
_STRIP_EXACT = frozenset({
    "API_KEY", "ADMIN_KEY", "ARIA_API_KEY", "ARIA_ADMIN_KEY",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR",
    "RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE",
    "MONGODB_URI", "MONGO_URL",
    "SSH_AUTH_SOCK",          # an agent forwarding to Ben's key defeats the mask
})
_STRIP_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS", "_PRIVATE_KEY")
_STRIP_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_APPLICATION_")


def session_tmp_dir(session_id: str, tmp_root: str = "/tmp", create: bool = False) -> str:
    """The session's own writable scratch (`/tmp/aria-<sid>`)."""
    path = os.path.join(tmp_root, f"aria-{session_id}")
    if create:
        os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _stub_gitconfig(tmp_dir: str, create: bool) -> str:
    """A GIT_CONFIG_GLOBAL that has no credential helper.

    ~/.gitconfig on this box wires `gh` in as a credential helper with `repo`
    scope, so a session inheriting it can `git push --force` or delete branches
    on GitHub (§7.1). Pointing GIT_CONFIG_GLOBAL at an empty file removes that
    without touching Ben's real config.
    """
    path = os.path.join(tmp_dir, "gitconfig-stub")
    if create:
        try:
            os.makedirs(tmp_dir, mode=0o700, exist_ok=True)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(
                        "# ARIA guard: deliberately empty. No credential helper, "
                        "no push URL rewrites.\n[credential]\n\thelper =\n"
                    )
        except OSError:
            logger.warning("guard: could not write stub gitconfig at %s", path, exc_info=True)
    return path


def _mask_kind(path: pathlib.Path) -> Optional[str]:
    """"dir" | "file" | None — what (if anything) it makes sense to mask here.

    A path that does not exist is deliberately NOT masked. Measured against real
    bwrap on 2026-08-15: under `--ro-bind / /` the mount point has to be created
    inside a read-only tree, so

        bwrap --ro-bind / / --tmpfs /home/ben/.missing-dir /bin/echo
        bwrap: Can't mkdir /home/ben/.missing-dir: Read-only file system  (rc 1)

    Masking an absent path therefore refuses EVERY session rather than
    protecting anything — and `~/.kube`, `~/.aws` and `~/.config/gcloud` are all
    in the policy while being absent on this box. The residual risk (a
    credential store created after the profile was built stays visible to that
    one long-running session) is real but bounded: profiles are built per spawn,
    and every tree a session can see is bound read-only, so the session itself
    cannot be what creates the file.
    """
    try:
        if path.is_file():
            return "file"
        if path.is_dir():
            return "dir"
    except OSError:  # e.g. a dangling symlink or a permission error on the parent
        return None
    return None


def _mask(path: pathlib.Path) -> list[str]:
    """tmpfs over a directory, /dev/null over a file (same shape as shell.py)."""
    target = str(path)
    kind = _mask_kind(path)
    if kind == "file":
        return ["--ro-bind", "/dev/null", target]
    if kind == "dir":
        return ["--tmpfs", target]
    logger.debug("guard: nothing to mask at %s (absent)", target)
    return []


def sibling_repo_masks(workspace: str, source_repo: Optional[str] = None,
                       development_root: Optional[str] = None) -> list[str]:
    """tmpfs over every project directory under ~/Development except this
    session's own repo (whose worktree is the only writable thing it gets)."""
    root = pathlib.Path(development_root or os.path.expanduser("~/Development"))
    if not root.is_dir():
        return []
    ws = pathlib.Path(os.path.abspath(workspace))
    src = pathlib.Path(os.path.abspath(source_repo)) if source_repo else None

    args: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        # Keep the repo that CONTAINS the worktree: masking it would take the
        # worktree with it, and the agent needs to read the source tree.
        if src is not None and entry == src:
            continue
        if ws == entry or _is_within(ws, entry):
            continue
        args += ["--tmpfs", str(entry)]
    return args


def _is_within(child: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _credential_files(root: str, max_depth: int = _WALK_MAX_DEPTH) -> list[str]:
    """Credential files under `root`, by name, depth-bounded.

    Enumerated at spawn time rather than pattern-matched at read time because
    bwrap masks paths, not globs. That is sound for the trees this covers: they
    are bound READ-ONLY, so a file that appears after the profile is built
    cannot have been put there by the session.
    """
    found: list[str] = []
    base = pathlib.Path(root)
    if not base.is_dir():
        return found
    base_depth = len(base.parts)
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        here = pathlib.Path(dirpath)
        if len(here.parts) - base_depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in _WALK_PRUNE]
        for name in filenames:
            if name in _CLAUDE_CREDENTIAL_FILES and here.name == ".claude":
                found.append(str(here / name))
                continue
            if any(fnmatch.fnmatch(name, pattern) for pattern in _CREDENTIAL_FILE_GLOBS):
                found.append(str(here / name))
    return sorted(found)


def credential_masks(
    visible_roots: Iterable[str], home: Optional[str] = None
) -> list[str]:
    """Every credential file a session could still read, in mask order.

    Only files that EXIST are returned — see `_mask_kind`: bwrap cannot create a
    mount point under `--ro-bind / /`, so naming an absent `.env` would refuse
    the session instead of protecting anything.
    """
    home_path = pathlib.Path(home or pathlib.Path.home())
    targets: list[str] = []
    for root in visible_roots:
        if not root:
            continue
        targets += _credential_files(str(pathlib.Path(os.path.abspath(root))))
    targets += [str(home_path / ".claude" / name) for name in _CLAUDE_CREDENTIAL_FILES]
    return [
        target for target in dict.fromkeys(targets)
        if _mask_kind(pathlib.Path(target)) == "file"
    ]


def _bind_conflict(
    target: str, masked_dirs: Iterable[str], masked_files: Iterable[str]
) -> Optional[str]:
    """Why `target` may NOT be bound, or None if it is safe to bind.

    The ancestor case is the one that produced the vulnerability: a policy file
    with `rw_paths: ["/home/ben"]` emitted `--bind /home/ben /home/ben` AFTER
    `--tmpfs /home/ben/.ssh`, which replaces the mount the mask hung off and
    hands back the keys — plus the whole home, writable — without naming
    `.ssh` anywhere.
    """
    path = pathlib.Path(os.path.abspath(os.path.expanduser(target)))
    for raw in masked_files:
        masked = pathlib.Path(raw)
        if path == masked:
            return f"{target} is masked as a credential file"
    for raw in masked_dirs:
        masked = pathlib.Path(raw)
        if path == masked:
            return f"{target} is masked"
        if _is_within(path, masked):
            return f"{target} is inside the masked directory {raw}"
        if _is_within(masked, path):
            return f"binding {target} would remount over the mask on {raw}"
    return None


def build_sandbox_prefix(
    workspace: str,
    session_id: str,
    *,
    source_repo: Optional[str] = None,
    binary: Optional[str] = None,
    tmp_root: str = "/tmp",
    create_tmp: bool = True,
    development_root: Optional[str] = None,
    extra_rw_paths: Iterable[str] = (),
    policy: Optional[GuardPolicy] = None,
) -> list[str]:
    """bwrap argv prefix for a coding session. Prepend to the real launch argv.

    Callers MUST consult `preflight()` first: this function still returns a
    usable argv when bwrap is absent (so the profile stays inspectable and
    testable), and refusing the spawn is preflight's job, not argv building's.
    """
    policy = policy or load_policy()
    home = str(pathlib.Path.home())
    workspace = os.path.abspath(workspace)
    tmp_dir = session_tmp_dir(session_id, tmp_root=tmp_root, create=create_tmp)
    bwrap = binary or shutil.which(settings.shell_sandbox_binary) or settings.shell_sandbox_binary

    argv: list[str] = [
        bwrap,
        # Everything readable, nothing writable, unless named below.
        "--ro-bind", "/", "/",
        "--proc", "/proc",
        "--dev", "/dev",
        # A private /tmp: other sessions' scratch, ARIA's own temp files and any
        # stray socket are invisible. The session's own dir is bound back below.
        "--tmpfs", "/tmp",
        # NO --unshare-net: the agent needs model endpoints and registries.
        "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--die-with-parent", "--new-session",
    ]

    # 1. Masks first. Directory masks are also the veto list for phases 2 and 3.
    masked_dirs: list[str] = []
    masked_files: list[str] = []

    def _add_mask(raw_path) -> None:
        target = pathlib.Path(raw_path).expanduser()
        kind = _mask_kind(target)
        if kind is None:
            return
        argv.extend(_mask(target))
        (masked_files if kind == "file" else masked_dirs).append(str(target))

    for raw in policy.sandbox_tmpfs_paths:
        _add_mask(raw)
    # The accepted-policy record. ~/.aria already covers the default location,
    # but the path is relocatable and a session that can edit it can re-arm
    # trust-on-first-use (policy.py, `verify_policy`).
    state_dir = pathlib.Path(guard_state_path()).parent
    if not any(_is_within(state_dir, pathlib.Path(d)) for d in masked_dirs):
        _add_mask(state_dir)
    siblings = sibling_repo_masks(workspace, source_repo, development_root)
    argv += siblings
    masked_dirs += [siblings[i + 1] for i, token in enumerate(siblings) if token == "--tmpfs"]

    # Credential files in the trees a session can still read — including the
    # session's OWN repo, which stays visible on purpose.
    cred_files = credential_masks([source_repo] if source_repo else [], home=home)
    # ...but never inside the session's OWN worktree. That tree is the agent's
    # writable space; the repo's real `.env` is gitignored and therefore not in
    # it, so anything matching there is a file the session itself created, and
    # masking it would break the agent's work to protect it from itself.
    cred_files = [
        target for target in cred_files
        if not _is_within(pathlib.Path(target), pathlib.Path(workspace))
    ]
    for target in cred_files:
        argv += ["--ro-bind", "/dev/null", target]
    masked_files += cred_files
    # No docker socket: `docker exec shared-mongod ... dropDatabase` is a
    # one-liner from inside a session otherwise (ben is in the docker group).
    # os.path.exists, not _mask_kind: a unix socket is neither a file nor a
    # directory, and on a box with no docker the mount point does not exist to
    # bind over (bwrap would exit 1 rather than protect anything).
    if os.path.exists(_DOCKER_SOCKET):
        argv += ["--ro-bind", "/dev/null", _DOCKER_SOCKET]
        masked_files.append(_DOCKER_SOCKET)

    # 2. Read-only exceptions the agent cannot run without.
    bound: list[str] = []
    for raw in policy.sandbox_ro_paths:
        target = str(pathlib.Path(raw).expanduser())
        conflict = _bind_conflict(target, masked_dirs, masked_files)
        if conflict:
            logger.error("guard: refusing ro-bind — %s", conflict)
            continue
        argv += ["--ro-bind-try", target, target]
        bound.append(target)

    # 3. Writable surface: the worktree, the session scratch, and pi's transcript.
    argv += ["--bind", workspace, workspace]
    argv += ["--bind", tmp_dir, tmp_dir]
    bound += [workspace, tmp_dir]
    for raw in list(policy.sandbox_rw_paths) + list(extra_rw_paths):
        target = str(pathlib.Path(raw).expanduser())
        conflict = _bind_conflict(target, masked_dirs, masked_files)
        if conflict:
            logger.error("guard: refusing rw-bind — %s", conflict)
            continue
        if create_tmp:
            # --bind (unlike --bind-try) fails the whole spawn on a missing
            # source, and ~/.pi/agent/sessions does not exist until pi's first
            # run — create it rather than lose every first session on a machine.
            try:
                os.makedirs(target, exist_ok=True)
            except OSError:
                logger.warning("guard: could not create rw path %s", target, exc_info=True)
        argv += ["--bind-try", target, target]
        bound.append(target)

    # 4. Re-apply the file masks a bind just mounted over. ~/.claude is
    # ro-bound so claude_code can authenticate; without this line that bind
    # hands back ~/.claude/.credentials.json, which is the Anthropic key the
    # env-strip removed.
    for masked in masked_files:
        if any(_is_within(pathlib.Path(masked), pathlib.Path(b)) for b in bound):
            argv += ["--ro-bind", "/dev/null", masked]

    argv += ["--chdir", workspace]
    _stub_gitconfig(tmp_dir, create_tmp)
    return argv


def resource_prefix(session_id: str) -> list[str]:
    """systemd-run scope that caps a session's memory and CPU.

    Outermost in the argv (systemd-run → bwrap → agent): the transient scope must
    own the whole process tree, and running systemd-run from *inside* the sandbox
    would need the user bus, which the read-only /run does not allow.
    """
    return [
        "systemd-run", "--user", "--scope", "--quiet",
        "--description", f"ARIA guarded coding session {session_id[:8]}",
        "-p", f"MemoryMax={settings.guard_session_memory_max}",
        "-p", f"CPUQuota={settings.guard_session_cpu_quota}",
    ]


def _darwin_mem_available_gib() -> Optional[float]:
    """Estimate available memory from macOS's native pressure report."""
    try:
        result = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        total_bytes: Optional[int] = None
        free_percent: Optional[float] = None
        for raw in result.stdout.splitlines():
            line = raw.strip()
            if line.startswith("The system has "):
                total_bytes = int(line.split()[3])
            elif line.startswith("System-wide memory free percentage:"):
                free_percent = float(line.rsplit(" ", 1)[1].rstrip("%"))
        if result.returncode == 0 and total_bytes is not None and free_percent is not None:
            return total_bytes * (free_percent / 100.0) / (1024 ** 3)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        logger.warning("guard: could not read macOS memory pressure", exc_info=True)
    return None


def mem_available_gib() -> Optional[float]:
    """Host-native available memory estimate, or None if unreadable."""
    if sys.platform == "darwin":
        return _darwin_mem_available_gib()

    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        logger.warning("guard: could not read MemAvailable", exc_info=True)
    return None


# The canary result, cached: (key, checked_at_monotonic, result).
_canary_cache: dict[str, Any] = {"key": None, "at": 0.0, "result": None}
_CANARY_TTL_SECONDS = 300
_CANARY_TOKEN = "aria-guard-canary-ok"


def sandbox_canary(*, force: bool = False, timeout: int = 20) -> dict:
    """Can bwrap, with THIS profile, actually start a process?

    `shutil.which("bwrap")` answers a different question than the one that
    matters, and the gap between them is not theoretical: on 2026-08-15 the
    profile named `~/.git-credentials`, which does not exist on this box, so
    every spawn died with `Can't mkdir …: Read-only file system` before exec.
    The red-team drill scored 5/9 "contained" against a sandbox that had never
    run a single process — every probe was phrased as "the secret did not appear
    in the output", and nothing had produced any output. An actuator needs an
    oracle that is independent of its own exit code (plan principle 8), so the
    canary asserts on a token bwrap has to *print*, not on rc alone.

    Cached for `_CANARY_TTL_SECONDS`, keyed on the binary and the enforced
    policy hash, so it is not a per-spawn cost — but a policy edit re-runs it.
    """
    policy = load_policy()
    binary = shutil.which(settings.shell_sandbox_binary)
    key = f"{binary}|{policy.hash}|{settings.guard_sandbox_enabled}"
    now = time.monotonic()
    cached = _canary_cache.get("result")
    if (
        not force
        and cached is not None
        and _canary_cache.get("key") == key
        and now - float(_canary_cache.get("at") or 0) < _CANARY_TTL_SECONDS
    ):
        return {**cached, "cached": True}

    if not binary:
        result = {"ok": False, "detail": f"{settings.shell_sandbox_binary} is not on PATH"}
    else:
        workspace = tempfile.mkdtemp(prefix="aria-guard-canary-")
        session_id = f"canary-{os.getpid()}"
        try:
            argv = build_sandbox_prefix(
                workspace, session_id, binary=binary, create_tmp=True
            )
            proc = subprocess.run(  # noqa: S603 — argv list, no shell
                [*argv, "/bin/echo", _CANARY_TOKEN],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            output = (proc.stdout + proc.stderr).strip()
            ok = _CANARY_TOKEN in proc.stdout
            result = {
                "ok": ok,
                "detail": "a process started inside the sandbox" if ok else
                          f"bwrap exited {proc.returncode}: {output[:300] or '(no output)'}",
            }
        except (OSError, subprocess.SubprocessError) as exc:
            result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(session_tmp_dir(session_id), ignore_errors=True)

    _canary_cache.update({"key": key, "at": now, "result": result})
    return {**result, "cached": False}


def preflight() -> dict:
    """Can a guarded session be spawned right now?

    FAILS CLOSED. `guard_sandbox_enabled` with no bwrap means refuse — running
    the agent unsandboxed "just this once" is how a safety control becomes
    decorative. An unreadable MemAvailable also refuses: the 9 GiB floor exists
    because a spawn under it OOM-kills a resident model, and an unknown value is
    not evidence of headroom. And a bwrap that is present but cannot start a
    process refuses too — see `sandbox_canary`.

    Stays synchronous (callers include `scripts/guard_redteam.py`); the API
    surface reaches it through `preflight_async`, which runs it in a thread so
    the canary's subprocess never blocks the event loop.
    """
    bwrap = shutil.which(settings.shell_sandbox_binary)
    systemd_run = shutil.which("systemd-run")
    mem = mem_available_gib()

    reasons: list[str] = []
    allowed = True
    canary: Optional[dict] = None

    if settings.guard_sandbox_enabled and not bwrap:
        allowed = False
        reasons.append(
            f"guard_sandbox_enabled is true but '{settings.shell_sandbox_binary}' "
            "is not on PATH; refusing to spawn unsandboxed"
        )
    elif settings.guard_sandbox_enabled:
        # Only when the sandbox is actually in use: with it disabled this would
        # spawn bwrap on every /guard/status poll to answer a question nobody
        # is asking.
        canary = sandbox_canary()
        if not canary["ok"]:
            allowed = False
            reasons.append(f"the sandbox cannot start a process: {canary['detail']}")
    if mem is None:
        allowed = False
        reasons.append("MemAvailable could not be read; refusing rather than guessing")
    elif mem < settings.guard_min_mem_available_gib:
        allowed = False
        reasons.append(
            f"MemAvailable {mem:.1f} GiB is below the "
            f"{settings.guard_min_mem_available_gib} GiB spawn floor"
        )
    if not systemd_run:
        # Advisory: the resource cap is a nice-to-have, the sandbox is not.
        reasons.append("systemd-run not found; sessions will run without MemoryMax/CPUQuota")

    return {
        "guard_enabled": settings.guard_enabled,
        "sandbox_enabled": settings.guard_sandbox_enabled,
        "sandbox_binary": settings.shell_sandbox_binary,
        "bwrap_present": bool(bwrap),
        "bwrap_path": bwrap,
        "systemd_run_present": bool(systemd_run),
        "systemd_run_path": systemd_run,
        "mem_available_gib": round(mem, 2) if mem is not None else None,
        "mem_floor_gib": settings.guard_min_mem_available_gib,
        "memory_max": settings.guard_session_memory_max,
        "cpu_quota": settings.guard_session_cpu_quota,
        "canary": canary,
        "spawn_allowed": allowed,
        "reasons": reasons,
    }


async def preflight_async() -> dict:
    """`preflight()` off the event loop — it does blocking I/O and may spawn."""
    return await asyncio.to_thread(preflight)


def session_env(
    base_env: Optional[dict] = None,
    *,
    session_id: Optional[str] = None,
    session_token: Optional[str] = None,
    tmp_root: str = "/tmp",
    create_tmp: bool = False,
) -> dict:
    """The environment a session gets: no credentials, no git credential helper.

    Keeps PATH/HOME/LANG/TERM and anything else unremarkable — stripping to an
    allowlist breaks toolchains in ways that surface as "the agent is broken",
    not "the guard did that". The denylist is the auditable half.
    """
    env = dict(base_env if base_env is not None else os.environ)

    for name in list(env):
        upper = name.upper()
        if (
            upper in _STRIP_EXACT
            or upper.endswith(_STRIP_SUFFIXES)
            or upper.startswith(_STRIP_PREFIXES)
        ):
            env.pop(name, None)

    if session_id:
        tmp_dir = session_tmp_dir(session_id, tmp_root=tmp_root, create=create_tmp)
        env["TMPDIR"] = tmp_dir
        env["GIT_CONFIG_GLOBAL"] = _stub_gitconfig(tmp_dir, create_tmp)
        env["ARIA_SESSION_ID"] = session_id
    else:
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"

    # Fail fast instead of hanging on a credential prompt no one can answer.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/true"

    if session_token:
        env["ARIA_SESSION_TOKEN"] = session_token
    return env
