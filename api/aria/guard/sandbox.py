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
    allow-listing is Phase 4 work).
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
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
from typing import Iterable, Optional

from aria.config import settings
from aria.guard.policy import GuardPolicy, load_policy

logger = logging.getLogger(__name__)

# Masked over the real path, not /var/run/docker.sock: /var/run is a symlink to
# /run on this box and bwrap resolves the destination, so binding the symlinked
# name is at best redundant and at worst an error that refuses the spawn.
_DOCKER_SOCKET = "/run/docker.sock"

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


def _mask(path: pathlib.Path) -> list[str]:
    """tmpfs over a directory, /dev/null over a file (same shape as shell.py).

    A path that does not exist yet is still masked with a tmpfs: aria-api is
    long-lived, and "the file appeared after we built the profile" must not be a
    way to un-mask a credential store.
    """
    target = str(path)
    if path.is_file():
        return ["--ro-bind", "/dev/null", target]
    return ["--tmpfs", target]


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

    # 1. Masks first, so a later bind always wins over a mask.
    for raw in policy.sandbox_tmpfs_paths:
        argv += _mask(pathlib.Path(raw).expanduser())
    argv += sibling_repo_masks(workspace, source_repo, development_root)
    # No docker socket: `docker exec shared-mongod ... dropDatabase` is a
    # one-liner from inside a session otherwise (ben is in the docker group).
    argv += ["--ro-bind", "/dev/null", _DOCKER_SOCKET]

    # 2. Read-only exceptions the agent cannot run without.
    for raw in policy.sandbox_ro_paths:
        target = str(pathlib.Path(raw).expanduser())
        argv += ["--ro-bind-try", target, target]

    # 3. Writable surface: the worktree, the session scratch, and pi's transcript.
    argv += ["--bind", workspace, workspace]
    argv += ["--bind", tmp_dir, tmp_dir]
    for raw in list(policy.sandbox_rw_paths) + list(extra_rw_paths):
        target = str(pathlib.Path(raw).expanduser())
        if create_tmp:
            # --bind (unlike --bind-try) fails the whole spawn on a missing
            # source, and ~/.pi/agent/sessions does not exist until pi's first
            # run — create it rather than lose every first session on a machine.
            try:
                os.makedirs(target, exist_ok=True)
            except OSError:
                logger.warning("guard: could not create rw path %s", target, exc_info=True)
        argv += ["--bind-try", target, target]

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


def mem_available_gib() -> Optional[float]:
    """MemAvailable from /proc/meminfo, or None if it cannot be read."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        logger.warning("guard: could not read MemAvailable", exc_info=True)
    return None


def preflight() -> dict:
    """Can a guarded session be spawned right now?

    FAILS CLOSED. `guard_sandbox_enabled` with no bwrap means refuse — running
    the agent unsandboxed "just this once" is how a safety control becomes
    decorative. An unreadable MemAvailable also refuses: the 9 GiB floor exists
    because a spawn under it OOM-kills a resident model, and an unknown value is
    not evidence of headroom.
    """
    bwrap = shutil.which(settings.shell_sandbox_binary)
    systemd_run = shutil.which("systemd-run")
    mem = mem_available_gib()

    reasons: list[str] = []
    allowed = True

    if settings.guard_sandbox_enabled and not bwrap:
        allowed = False
        reasons.append(
            f"guard_sandbox_enabled is true but '{settings.shell_sandbox_binary}' "
            "is not on PATH; refusing to spawn unsandboxed"
        )
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
        "spawn_allowed": allowed,
        "reasons": reasons,
    }


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
