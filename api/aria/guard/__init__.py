"""
ARIA - Guard

Purpose: The safety substrate under every ARIA-spawned coding session — an OS
sandbox around the agent's process and a git protocol ARIA (not the agent)
executes, so that any run can be rolled back and no run can reach credentials,
sibling repos, or the guard's own code.

Principle 11 (proposal §2): **the guard holds the git pen, not the agent.**
Checkpoint commits, tags, pushes, merges and rollbacks are performed by the ARIA
process. An agent that can skip its own checkpoint has no checkpoint.

Principle 12: the evaluator and the kill switch are unwritable by the thing they
evaluate — hence `policy.is_protected()` and the tamper hash.

Three modules, deliberately independent so a failure in one does not disable the
others:
  - policy.py   — what is protected, tamper detection, the guard_events log
  - sandbox.py  — bwrap/systemd-run argv construction, preflight, env scrubbing
  - gitguard.py — worktrees, checkpoint commits, the bare mirror, gate, merge
"""

from aria.guard.policy import (
    ACCEPT_REMEDY,
    GuardPolicy,
    PolicyError,
    accept_policy,
    guard_state_path,
    is_protected,
    load_policy,
    policy_hash,
    record_event,
    repo_root,
    verify_policy,
)
from aria.guard.sandbox import (
    build_sandbox_prefix,
    credential_masks,
    mem_available_gib,
    preflight,
    preflight_async,
    resource_prefix,
    sandbox_canary,
    session_env,
    session_tmp_dir,
)
from aria.guard.gitguard import (
    GitGuard,
    GuardGitError,
    GuardMergeConflict,
    get_git_guard,
)

__all__ = [
    "ACCEPT_REMEDY",
    "GuardPolicy",
    "PolicyError",
    "accept_policy",
    "guard_state_path",
    "is_protected",
    "load_policy",
    "policy_hash",
    "record_event",
    "repo_root",
    "verify_policy",
    "build_sandbox_prefix",
    "credential_masks",
    "mem_available_gib",
    "preflight",
    "preflight_async",
    "resource_prefix",
    "sandbox_canary",
    "session_env",
    "session_tmp_dir",
    "GitGuard",
    "GuardGitError",
    "GuardMergeConflict",
    "get_git_guard",
]
