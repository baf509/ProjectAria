"""Tests for the Guard seam in the coding-session manager (proposal §7.2/§7.3).

What is asserted here is the SEAM, not the guard: `guard/` has its own tests for
worktrees, checkpoints and the bwrap profile. What could not be verified before
this file is that every ARIA-spawned session actually goes through it — that a
session runs in the worktree and not the live checkout, that a refusal is a
refusal, that the argv is systemd-run → bwrap → agent in that order, and that
the killswitch/e-stop now reach sessions that are ALREADY RUNNING.

No network, no aria-api, no Mongo (the db is the in-memory mock from conftest),
and no NotificationService — nothing here can reach `signal_rpc`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.agents.session import CodingSessionManager, _git_repo_root
from aria.config import settings
from aria.guard import sandbox as guard_sandbox
from tests.conftest import make_mock_db
from tests.test_coding_session import _make_manager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.name=Ben", "-c", "user.email=ben@example.com", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


PREFLIGHT_OK = {
    "spawn_allowed": True,
    "reasons": [],
    "systemd_run_present": True,
    "bwrap_present": True,
    "mem_available_gib": 42.0,
}
PREFLIGHT_REFUSED = {
    "spawn_allowed": False,
    "reasons": ["MemAvailable 3.2 GiB is below the 9.0 GiB spawn floor"],
    "systemd_run_present": True,
    "bwrap_present": False,
    "mem_available_gib": 3.2,
}


def _guard_session(worktree="/repo/.worktrees/demo-abcd1234", repo="/repo"):
    return {
        "repo": repo,
        "worktree": worktree,
        "branch": "aria/demo/abcd1234",
        "start_tag": "aria/ckpt/sid/start",
        "mirror": "/home/ben/git-safe/repo.git",
        "git_dir": f"{repo}/.git/worktrees/demo-abcd1234",
    }


def _manager(db=None, *, prepare=None, checkpoint=None):
    """A manager whose backend/registry/process manager are mocks and whose
    GitGuard is a fake — the git protocol itself is tested in test_guard.py."""
    if db is None:
        db = make_mock_db()
        db.coding_sessions.find_one = AsyncMock(
            return_value={"_id": "sid", "status": "running", "workspace": "/repo"}
        )
    mgr = _make_manager(db=db)
    mgr.registry.canonicalize.side_effect = lambda name: name
    mgr.shell_service = None  # force the subprocess substrate: deterministic argv
    guard = MagicMock()
    if isinstance(prepare, Exception):
        guard.prepare_session = AsyncMock(side_effect=prepare)
    else:
        guard.prepare_session = AsyncMock(return_value=prepare or _guard_session())
    guard.checkpoint = AsyncMock(
        return_value=checkpoint or {"ok": True, "committed": True, "sha": "cafebabe1234"}
    )
    mgr._fake_git_guard = guard
    return mgr


class _Ctx:
    """Everything a start_session call needs stubbed, in one `with`."""

    def __init__(self, mgr, *, preflight=None, sandbox_prefix=None, resource=None):
        self.mgr = mgr
        self.preflight = preflight or PREFLIGHT_OK
        self.sandbox_prefix = sandbox_prefix or ["bwrap", "--ro-bind", "/", "/"]
        self.resource = resource or ["systemd-run", "--user", "--scope"]
        self._patches = []

    def __enter__(self):
        estop = MagicMock()
        estop.is_active = AsyncMock(return_value=False)
        real_session_env = guard_sandbox.session_env
        self._patches = [
            patch("aria.api.deps.get_killswitch"),
            patch("aria.api.deps.resolve_estop_manager", AsyncMock(return_value=estop)),
            patch("aria.agents.session.preflight", MagicMock(return_value=self.preflight)),
            patch("aria.agents.session.get_git_guard",
                  MagicMock(return_value=self.mgr._fake_git_guard)),
            patch("aria.agents.session.resource_prefix",
                  MagicMock(return_value=list(self.resource))),
            patch("aria.agents.session.build_sandbox_prefix",
                  MagicMock(return_value=list(self.sandbox_prefix))),
            # create_tmp=False: the env scrub is real, the /tmp litter is not.
            patch("aria.agents.session.session_env",
                  lambda base, **kw: real_session_env(base, **{**kw, "create_tmp": False})),
            patch("aria.agents.session.record_event", AsyncMock(return_value={})),
            patch.object(settings, "guard_enabled", True),
            patch.object(settings, "guard_worktree_default", True),
            patch.object(settings, "guard_sandbox_enabled", False),
            patch.object(settings, "guard_checkpoint_enabled", False),
            patch.object(settings, "coding_routing_enabled", False),
        ]
        started = [p.start() for p in self._patches]
        started[0].return_value.check_or_raise = MagicMock()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _spawned_command(mgr):
    return mgr.process_manager.spawn.call_args.args[1]


# ---------------------------------------------------------------------------
# Repo detection — what decides whether a session is guarded at all
# ---------------------------------------------------------------------------

class TestRepoRoot:
    def test_finds_the_repo_root_from_a_subdirectory(self, tmp_path):
        repo = tmp_path / "proj"
        (repo / "pkg").mkdir(parents=True)
        _git(["init"], repo)
        assert _git_repo_root(str(repo / "pkg")) == str(repo)

    def test_a_plain_directory_is_not_a_repo(self, tmp_path):
        plain = tmp_path / "scratch"
        plain.mkdir()
        assert _git_repo_root(str(plain)) is None

    def test_a_missing_directory_is_not_a_repo(self, tmp_path):
        # The worktree default must never `git init` a path that isn't there.
        assert _git_repo_root(str(tmp_path / "nope")) is None

    def test_a_linked_worktree_resolves_to_its_repo(self, tmp_path):
        """Otherwise a session resumed inside a worktree nests .worktrees/ one
        level deeper on every run."""
        repo = tmp_path / "proj"
        repo.mkdir()
        _git(["init"], repo)
        (repo / "f.txt").write_text("x")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "init"], repo)
        wt = repo / ".worktrees" / "demo-abcd1234"
        _git(["worktree", "add", str(wt), "-b", "aria/demo/abcd1234"], repo)
        assert _git_repo_root(str(wt)) == str(repo)


# ---------------------------------------------------------------------------
# The seam: worktree by default
# ---------------------------------------------------------------------------

class TestWorktreeDefault:
    @pytest.mark.asyncio
    async def test_session_runs_in_the_worktree_not_the_live_checkout(self):
        mgr = _manager()
        with _Ctx(mgr), patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x")

        mgr._fake_git_guard.prepare_session.assert_awaited_once()
        doc = mgr.db.coding_sessions.insert_one.call_args.args[0]
        assert doc["workspace"] == "/repo/.worktrees/demo-abcd1234"
        assert doc["source_repo"] == "/repo"
        assert doc["branch"] == "aria/demo/abcd1234"
        assert doc["guard"]["active"] is True
        assert doc["guard"]["start_tag"] == "aria/ckpt/sid/start"

    @pytest.mark.asyncio
    async def test_explicit_false_keeps_the_live_checkout(self):
        """`create_worktree=False` is a different statement from saying nothing:
        the caller means the live checkout."""
        mgr = _manager()
        with _Ctx(mgr), patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x", create_worktree=False)

        mgr._fake_git_guard.prepare_session.assert_not_awaited()
        doc = mgr.db.coding_sessions.insert_one.call_args.args[0]
        assert doc["workspace"] == "/repo"

    @pytest.mark.asyncio
    async def test_a_non_repo_workspace_degrades_instead_of_initialising_one(self):
        """A default that silently `git init`s ~/Downloads is worse than no
        worktree: the new repo has no history to roll back to either."""
        mgr = _manager()
        with _Ctx(mgr), patch("aria.agents.session._git_repo_root", return_value=None):
            await mgr.start_session(workspace="/tmp/scratch", backend="claude_code",
                                    prompt="fix it", model="x")

        mgr._fake_git_guard.prepare_session.assert_not_awaited()
        doc = mgr.db.coding_sessions.insert_one.call_args.args[0]
        assert doc["workspace"] == "/tmp/scratch"
        assert doc["guard"]["active"] is False
        assert "not a git repository" in doc["guard"]["degraded"]

    @pytest.mark.asyncio
    async def test_remote_sessions_are_left_alone(self):
        """The repo, bwrap and the systemd user bus are on the other machine."""
        mgr = _manager()
        mgr._start_remote_shell_session = AsyncMock(
            return_value={"_id": "sid", "status": "running", "host": "mac"}
        )
        with _Ctx(mgr) as ctx, \
             patch("aria.nodes.is_remote_host", return_value=True), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x", host="mac")

        mgr._fake_git_guard.prepare_session.assert_not_awaited()
        doc = mgr.db.coding_sessions.insert_one.call_args.args[0]
        assert doc["guard"]["active"] is False
        assert doc["workspace"] == "/repo"

    @pytest.mark.asyncio
    async def test_guard_disabled_restores_the_pre_guard_behaviour(self):
        mgr = _manager()
        with _Ctx(mgr) as ctx, \
             patch.object(settings, "guard_enabled", False), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"), \
             patch("aria.agents.session.preflight",
                   MagicMock(side_effect=AssertionError("preflight must not run"))):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x")

        mgr._fake_git_guard.prepare_session.assert_not_awaited()
        doc = mgr.db.coding_sessions.insert_one.call_args.args[0]
        assert doc["workspace"] == "/repo"
        assert doc["guard"]["active"] is False


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

class TestFailsClosed:
    @pytest.mark.asyncio
    async def test_preflight_refusal_blocks_the_spawn_with_its_reasons(self):
        mgr = _manager()
        with _Ctx(mgr, preflight=PREFLIGHT_REFUSED), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"):
            with pytest.raises(RuntimeError, match="below the 9.0 GiB spawn floor"):
                await mgr.start_session(workspace="/repo", backend="claude_code",
                                        prompt="fix it", model="x")

        # Nothing was persisted and nothing was launched.
        mgr.db.coding_sessions.insert_one.assert_not_awaited()
        mgr.process_manager.spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sandbox_on_plus_failed_worktree_refuses_rather_than_degrading(self):
        """With the sandbox on, the worktree is the ONLY writable path in the
        profile. Falling back to the live checkout here would hand the agent a
        read-write bind on Ben's tree — the exact thing the guard exists for."""
        from aria.guard.gitguard import GuardGitError

        mgr = _manager(prepare=GuardGitError("no space left on device"))
        with _Ctx(mgr), \
             patch.object(settings, "guard_sandbox_enabled", True), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"):
            with pytest.raises(RuntimeError, match="sandbox is enabled"):
                await mgr.start_session(workspace="/repo", backend="claude_code",
                                        prompt="fix it", model="x")

        mgr.process_manager.spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sandbox_off_plus_failed_worktree_degrades_and_records_it(self):
        from aria.guard.gitguard import GuardGitError

        mgr = _manager(prepare=GuardGitError("no space left on device"))
        with _Ctx(mgr), patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x")

        doc = mgr.db.coding_sessions.insert_one.call_args.args[0]
        assert doc["workspace"] == "/repo"
        assert "no space left" in doc["guard"]["degraded"]

    @pytest.mark.asyncio
    async def test_an_explicit_worktree_request_still_raises_valueerror(self):
        """Unchanged contract: a caller that asked for a worktree and did not
        get one gets the 400, not a silent live-checkout session."""
        from aria.guard.gitguard import GuardGitError

        mgr = _manager(prepare=GuardGitError("not a repository"))
        with _Ctx(mgr), patch("aria.agents.session._git_repo_root", return_value=None):
            with pytest.raises(ValueError, match="Could not provision a worktree"):
                await mgr.start_session(workspace="/tmp/x", backend="claude_code",
                                        prompt="fix it", model="x", create_worktree=True)


# ---------------------------------------------------------------------------
# The launch argv and environment
# ---------------------------------------------------------------------------

class TestGuardedLaunch:
    @pytest.mark.asyncio
    async def test_argv_is_systemd_run_then_bwrap_then_the_agent(self):
        mgr = _manager()
        with _Ctx(mgr), \
             patch.object(settings, "guard_sandbox_enabled", True), \
             patch.object(settings, "guard_sandbox_backends", ["claude_code"]), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x")

        argv = _spawned_command(mgr).argv
        assert argv[:3] == ["systemd-run", "--user", "--scope"]
        assert argv[3] == "bwrap"
        assert argv[-3:] == ["claude", "--prompt", "do stuff"]

    @pytest.mark.asyncio
    async def test_a_backend_outside_the_sandbox_list_still_gets_the_resource_scope(self):
        """Phase 2 ships with the sandbox OFF; the memory/CPU cap and the
        worktree must work anyway, or the default posture guards nothing."""
        mgr = _manager()
        with _Ctx(mgr), \
             patch.object(settings, "guard_sandbox_enabled", True), \
             patch.object(settings, "guard_sandbox_backends", ["pi-code"]), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x")

        argv = _spawned_command(mgr).argv
        assert argv[:3] == ["systemd-run", "--user", "--scope"]
        assert "bwrap" not in argv

    @pytest.mark.asyncio
    async def test_no_resource_prefix_when_systemd_run_is_absent(self):
        mgr = _manager()
        pre = {**PREFLIGHT_OK, "systemd_run_present": False}
        with _Ctx(mgr, preflight=pre), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x")

        assert _spawned_command(mgr).argv[0] == "claude"

    @pytest.mark.asyncio
    async def test_the_subprocess_environment_is_scrubbed(self):
        mgr = _manager()
        fake_env = {"PATH": "/usr/bin", "HOME": "/home/ben", "GH_TOKEN": "ghp_x",
                    "API_KEY": "secret"}
        with _Ctx(mgr), \
             patch.dict(os.environ, fake_env, clear=True), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x")

        env = _spawned_command(mgr).env
        assert "GH_TOKEN" not in env and "API_KEY" not in env
        assert env["PATH"] == "/usr/bin"          # a scrub, not a wipe
        assert env["GIT_TERMINAL_PROMPT"] == "0"  # no credential prompt to hang on

    def test_shell_substrate_env_prefix_unsets_secrets_after_the_login_shell(self):
        """`bash -lc` re-sources the profile after tmux gets the string, so the
        scrub has to be `env -u` INSIDE the command, not an assignment in front
        of it."""
        mgr = _manager()
        real_session_env = guard_sandbox.session_env
        with patch.dict(os.environ, {"PATH": "/usr/bin", "GH_TOKEN": "ghp_x"}, clear=True), \
             patch("aria.agents.session.session_env",
                   lambda base, **kw: real_session_env(base, **{**kw, "create_tmp": False})):
            argv = mgr._guard_env_argv("sid", {"ARIA_MANAGED": "1"})

        assert argv[0] == "env"
        assert argv[argv.index("-u") + 1] == "GH_TOKEN"
        assert "ARIA_MANAGED=1" in argv
        assert any(a.startswith("GIT_CONFIG_GLOBAL=") for a in argv)


# ---------------------------------------------------------------------------
# Checkpoints — ARIA makes the commit, the agent cannot skip it
# ---------------------------------------------------------------------------

class TestCheckpoints:
    @pytest.mark.asyncio
    async def test_checkpoint_session_commits_and_makes_the_metadata_real(self):
        mgr = _manager()
        with patch("aria.agents.session.get_git_guard",
                   MagicMock(return_value=mgr._fake_git_guard)), \
             patch("aria.agents.session.write_checkpoint", new_callable=AsyncMock) as meta, \
             patch.object(settings, "guard_enabled", True), \
             patch.object(settings, "guard_checkpoint_enabled", True):
            result = await mgr.checkpoint_session("sid", reason="nudge")

        assert result["committed"] is True
        mgr._fake_git_guard.checkpoint.assert_awaited_once_with("sid", reason="nudge")
        # session_checkpoints has never been written in production; a real
        # commit behind it is the whole point of the change.
        meta.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_checkpoint_never_raises_into_its_caller(self):
        """The watchdog calls this on every nudge and the kill paths call it on
        the way out; neither may die because git did."""
        mgr = _manager()
        mgr._fake_git_guard.checkpoint = AsyncMock(side_effect=RuntimeError("git exploded"))
        with patch("aria.agents.session.get_git_guard",
                   MagicMock(return_value=mgr._fake_git_guard)), \
             patch.object(settings, "guard_enabled", True), \
             patch.object(settings, "guard_checkpoint_enabled", True):
            result = await mgr.checkpoint_session("sid")

        assert result["committed"] is False
        assert "git exploded" in result["reason"]

    @pytest.mark.asyncio
    async def test_disabled_checkpoints_are_a_no_op(self):
        mgr = _manager()
        with patch("aria.agents.session.get_git_guard",
                   MagicMock(return_value=mgr._fake_git_guard)), \
             patch.object(settings, "guard_checkpoint_enabled", False):
            result = await mgr.checkpoint_session("sid")

        assert result["committed"] is False
        mgr._fake_git_guard.checkpoint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_guarded_session_gets_a_periodic_checkpoint_task(self):
        mgr = _manager()
        with _Ctx(mgr), \
             patch.object(settings, "guard_checkpoint_enabled", True), \
             patch("aria.agents.session._git_repo_root", return_value="/repo"):
            await mgr.start_session(workspace="/repo", backend="claude_code",
                                    prompt="fix it", model="x")
            assert "sid" not in mgr._checkpoint_tasks  # keyed by the real session id
            task_ids = list(mgr._checkpoint_tasks)
            assert len(task_ids) == 1
            mgr._stop_checkpoint_loop(task_ids[0])
            assert not mgr._checkpoint_tasks

    @pytest.mark.asyncio
    async def test_an_unguarded_session_gets_no_checkpoint_task(self):
        mgr = _manager()
        with _Ctx(mgr), \
             patch.object(settings, "guard_checkpoint_enabled", True), \
             patch("aria.agents.session._git_repo_root", return_value=None):
            await mgr.start_session(workspace="/tmp/scratch", backend="claude_code",
                                    prompt="fix it", model="x")

        assert not mgr._checkpoint_tasks

    @pytest.mark.asyncio
    async def test_stop_session_checkpoints_before_it_kills(self):
        db = make_mock_db()
        db.coding_sessions.find_one = AsyncMock(return_value={
            "_id": "s1", "status": "running", "workspace": "/repo/.worktrees/demo",
            "tmux_pane_id": None,
            "guard": {"active": True, "worktree": "/repo/.worktrees/demo"},
        })
        mgr = _manager(db=db)
        with patch("aria.agents.session.get_git_guard",
                   MagicMock(return_value=mgr._fake_git_guard)), \
             patch("aria.agents.session.write_checkpoint", new_callable=AsyncMock), \
             patch.object(settings, "guard_enabled", True), \
             patch.object(settings, "guard_checkpoint_enabled", True):
            assert await mgr.stop_session("s1") is True

        mgr._fake_git_guard.checkpoint.assert_awaited_once_with("s1", reason="stop")


# ---------------------------------------------------------------------------
# The stop button actually stops things
# ---------------------------------------------------------------------------

class TestStopAllRunning:
    @pytest.mark.asyncio
    async def test_stops_running_and_queued_sessions(self):
        db = make_mock_db()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[
            {"_id": "a", "status": "running"}, {"_id": "b", "status": "queued"},
        ])
        db.coding_sessions.find = MagicMock(return_value=cursor)
        mgr = _manager(db=db)
        mgr.stop_session = AsyncMock(return_value=True)

        with patch("aria.agents.session.record_event", new_callable=AsyncMock):
            result = await mgr.stop_all_running(reason="drill")

        assert result["stopped"] == 2
        assert db.coding_sessions.find.call_args.args[0] == {
            "status": {"$in": ["running", "queued"]}
        }

    @pytest.mark.asyncio
    async def test_one_wedged_session_does_not_block_the_rest(self):
        db = make_mock_db()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[
            {"_id": "a", "status": "running"}, {"_id": "b", "status": "running"},
        ])
        db.coding_sessions.find = MagicMock(return_value=cursor)
        mgr = _manager(db=db)
        mgr.stop_session = AsyncMock(side_effect=[RuntimeError("tmux hung"), True])

        with patch("aria.agents.session.record_event", new_callable=AsyncMock):
            result = await mgr.stop_all_running(reason="drill")

        assert result["stopped"] == 1
        assert result["failed"][0]["session_id"] == "a"

    @pytest.mark.asyncio
    async def test_killswitch_activation_stops_running_sessions(self):
        from aria.core.killswitch import Killswitch

        manager = MagicMock()
        manager.stop_all_running = AsyncMock(return_value={"stopped": 3, "sessions": []})
        ks = Killswitch()
        ks.set_coding_manager(manager)

        result = await ks.activate(reason="red team drill")

        assert result["stopped_sessions"] == 3
        assert "killswitch" in manager.stop_all_running.call_args.kwargs["reason"]

    @pytest.mark.asyncio
    async def test_killswitch_still_activates_when_stopping_fails(self):
        """Refusing to freeze because a session would not die is the wrong
        failure: the freeze is what stops the next spawn."""
        from aria.core.killswitch import Killswitch

        manager = MagicMock()
        manager.stop_all_running = AsyncMock(side_effect=RuntimeError("mongo down"))
        ks = Killswitch()
        ks.set_coding_manager(manager)

        result = await ks.activate(reason="drill")

        assert result["active"] is True
        assert result["stopped_sessions"] == 0
        assert ks.is_active is True

    @pytest.mark.asyncio
    async def test_estop_activation_stops_running_sessions(self):
        from aria.agents.estop import EstopManager

        db = make_mock_db()
        manager = MagicMock()
        manager.stop_all_running = AsyncMock(return_value={"stopped": 1, "sessions": ["a"]})
        estop = EstopManager(db, session_manager=manager)

        state = await estop.activate(reason="ESTOP from Signal", triggered_by="relay")

        assert state.active is True
        manager.stop_all_running.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_estop_without_a_wired_manager_is_still_an_estop(self):
        from aria.agents.estop import EstopManager

        estop = EstopManager(make_mock_db())
        with patch("aria.agents.session.resolve_active_session_manager", return_value=None):
            state = await estop.activate(reason="no manager here")

        assert state.active is True


# ---------------------------------------------------------------------------
# get_diff has to survive the fact that work is now committed as it happens
# ---------------------------------------------------------------------------

class TestGuardedDiff:
    @pytest.mark.asyncio
    async def test_diff_of_a_guarded_session_is_taken_from_the_start_tag(self, tmp_path):
        """Checkpoint commits empty `git diff`. Without this, review and the
        stuck-detector would see 'no changes' for a session that rewrote 40
        files."""
        repo = tmp_path / "proj"
        repo.mkdir()
        _git(["init"], repo)
        (repo / "a.txt").write_text("one\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "base"], repo)
        _git(["tag", "start"], repo)
        (repo / "a.txt").write_text("two\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "aria-ckpt"], repo)   # the guard's own commit

        db = make_mock_db()
        db.coding_sessions.find_one = AsyncMock(return_value={
            "_id": "s1", "workspace": str(repo),
            "guard": {"active": True, "worktree": str(repo), "start_tag": "start"},
        })
        mgr = _manager(db=db)
        diff = await mgr.get_diff("s1")
        assert "-one" in diff and "+two" in diff

    @pytest.mark.asyncio
    async def test_diff_of_an_unguarded_session_is_unchanged(self, tmp_path):
        repo = tmp_path / "proj"
        repo.mkdir()
        _git(["init"], repo)
        (repo / "a.txt").write_text("one\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "base"], repo)
        (repo / "a.txt").write_text("two\n")

        db = make_mock_db()
        db.coding_sessions.find_one = AsyncMock(
            return_value={"_id": "s1", "workspace": str(repo)}
        )
        mgr = _manager(db=db)
        diff = await mgr.get_diff("s1")
        assert "+two" in diff


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

class TestToolExposure:
    def test_start_coding_session_tool_exposes_create_worktree(self):
        from aria.tools.builtin.coding import StartCodingSessionTool

        tool = StartCodingSessionTool(MagicMock())
        names = {p.name for p in tool.parameters}
        assert {"create_worktree", "worktree_name"} <= names

    @pytest.mark.asyncio
    async def test_omitted_create_worktree_is_not_passed_as_false(self):
        """None means 'follow guard_worktree_default'; False means 'live
        checkout'. A tool call that said nothing must not mean the second."""
        from aria.tools.builtin.coding import StartCodingSessionTool

        manager = MagicMock()
        manager.start_session = AsyncMock(return_value={"_id": "s1"})
        tool = StartCodingSessionTool(manager)
        await tool.execute({"workspace": "/repo", "prompt": "go"})

        assert "create_worktree" not in manager.start_session.call_args.kwargs

    @pytest.mark.asyncio
    async def test_pi_coding_tool_forwards_the_worktree_flag(self):
        from aria.tools.builtin.pi_coding import PiCodingAgentTool

        manager = MagicMock()
        manager.start_session = AsyncMock(return_value={"_id": "s1", "status": "running"})
        tool = PiCodingAgentTool(manager)
        await tool.execute({"task": "go", "workspace": "/repo", "create_worktree": False})

        assert manager.start_session.call_args.kwargs["create_worktree"] is False
