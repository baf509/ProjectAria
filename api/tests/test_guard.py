"""Tests for the Guard package — policy, sandbox profile, and the git protocol.

The git tests run against a REAL git repository created in tmp_path. Mocking git
here would test that we can write argv, not that the protocol works; the whole
value of the guard is that `git reset --hard aria/ckpt/<sid>/start` actually
brings the tree back, so that is what these assert.

Nothing here needs MongoDB (GitGuard takes db=None and keeps its session registry
in-process), a live aria-api, or the network. bwrap is never executed — the
sandbox tests assert on the argv the guard would run.
"""

import dataclasses
import os
import subprocess
from datetime import datetime, timezone

import pytest

from aria.config import settings
from aria.guard import policy as guard_policy
from aria.guard import sandbox as guard_sandbox
from aria.guard.gitguard import GitGuard
from aria.guard.policy import (
    GuardPolicy,
    PolicyError,
    is_protected,
    load_policy,
    parse_simple_yaml,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def git(args, cwd):
    return subprocess.run(
        ["git", "-c", "user.name=Ben", "-c", "user.email=ben@example.com", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def flag_targets(argv: list[str], flag: str) -> list[str]:
    """Destinations bound/masked with `flag` (last argument of each occurrence)."""
    out = []
    for i, token in enumerate(argv):
        if token != flag:
            continue
        if flag in ("--tmpfs",):
            out.append(argv[i + 1])
        else:
            out.append(argv[i + 2])
    return out


def has_triple(argv: list[str], flag: str, src: str, dest: str) -> bool:
    for i, token in enumerate(argv):
        if token == flag and argv[i + 1] == src and argv[i + 2] == dest:
            return True
    return False


class FakeCollection:
    """Just enough motor surface for the policy/tamper paths, with real storage
    so the assertions are about behaviour rather than about call counts."""

    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return None

    async def insert_one(self, doc):
        self.docs.append(doc)
        return type("R", (), {"inserted_id": len(self.docs)})()

    async def update_one(self, query, update, upsert=False):
        existing = await self.find_one(query)
        if existing is None:
            if not upsert:
                return None
            existing = dict(query)
            self.docs.append(existing)
        existing.update(update.get("$set", {}))
        return type("R", (), {"modified_count": 1})()


class FakeDB:
    def __init__(self):
        self._collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._collections.setdefault(name, FakeCollection())

    def __getattr__(self, name):
        return self[name]


@pytest.fixture
def repo(tmp_path):
    """A real git repo with one commit on `main`."""
    path = tmp_path / "demo-project"
    path.mkdir()
    git(["init", "-b", "main", "--quiet"], path)
    (path / "README.md").write_text("hello\n")
    (path / ".gitignore").write_text("*.log\n")
    git(["add", "."], path)
    git(["commit", "-q", "-m", "init"], path)
    return path


@pytest.fixture
def guard(tmp_path):
    return GitGuard(db=None, mirror_root=str(tmp_path / "git-safe"))


SID = "sess-abcdef-0123456789"


async def prepared(guard: GitGuard, repo) -> dict:
    return await guard.prepare_session(str(repo), SID, "demo")


# ---------------------------------------------------------------------------
# Policy: parser
# ---------------------------------------------------------------------------

class TestPolicyParser:
    def test_parses_the_supported_subset(self):
        data = parse_simple_yaml(
            "# leading comment\n"
            "protected_paths:\n"
            '  - "guard/**"\n'
            "  - CLAUDE.md   # inline comment\n"
            "sandbox:\n"
            "  tmpfs_paths:\n"
            "    - ~/.ssh\n"
            "merge_gate:\n"
            "  diff_max_lines: 400\n"
            "  gitleaks_enabled: true\n"
        )
        assert data["protected_paths"] == ["guard/**", "CLAUDE.md"]
        assert data["sandbox"]["tmpfs_paths"] == ["~/.ssh"]
        assert data["merge_gate"] == {"diff_max_lines": 400, "gitleaks_enabled": True}

    def test_rejects_unsupported_yaml_rather_than_guessing(self):
        with pytest.raises(PolicyError):
            parse_simple_yaml("protected_paths: [a, b]\n")
        with pytest.raises(PolicyError):
            parse_simple_yaml("base: &anchor\n  a: 1\n")
        with pytest.raises(PolicyError):
            parse_simple_yaml("sandbox:\n\ttmpfs_paths:\n")

    def test_repo_policy_file_is_parseable(self):
        """The file that actually ships must load — an unparseable policy
        silently degrades to settings, which is exactly what we must not do."""
        policy = load_policy(force=True)
        assert policy.error is None
        assert policy.source == "file"
        assert policy.path.endswith("guard/policy.yaml")


# ---------------------------------------------------------------------------
# Policy: path protection
# ---------------------------------------------------------------------------

class TestProtectedPaths:
    ROOT = "/home/ben/Development/ProjectAria"

    @pytest.mark.parametrize("path", [
        "api/aria/guard/sandbox.py",
        "guard/policy.yaml",
        "api/aria/agents/watchdog.py",
        "api/aria/config.py",
        ".env",
        "api/.env",                      # bare pattern matches at any depth
        "CLAUDE.md",
        "docs/CLAUDE.md",
        ".claude/settings.json",
        "sub/.claude/settings.local.json",
        "hooks/pre_tool_call.py",
        "a/b/hooks/x.py",
        "api/tests/test_guard.py",
        ".git/hooks/pre-commit",
    ])
    def test_protected(self, path):
        assert is_protected(path, self.ROOT) is True

    @pytest.mark.parametrize("path", [
        "api/aria/steward/service.py",
        "api/aria/api/routes/planning.py",
        "README.md",
        "ui/app/page.tsx",
    ])
    def test_not_protected(self, path):
        assert is_protected(path, self.ROOT) is False

    def test_star_does_not_cross_directories(self):
        policy = dataclasses.replace(load_policy(), protected_paths=["api/*"])
        assert is_protected("api/thing.py", self.ROOT, policy) is True
        assert is_protected("api/aria/thing.py", self.ROOT, policy) is False

    def test_paths_outside_the_repo_fail_closed(self):
        assert is_protected("/etc/passwd", self.ROOT) is True
        assert is_protected("../../.ssh/id_ed25519", self.ROOT) is True
        # No repo root to judge against is also "don't know" -> protected.
        assert is_protected("/home/ben/.ssh/id_ed25519", None) is True


# ---------------------------------------------------------------------------
# Policy: file loading, tighten-only, tamper hash
# ---------------------------------------------------------------------------

class TestPolicyFile:
    def _write(self, tmp_path, text):
        path = tmp_path / "policy.yaml"
        path.write_text(text)
        return str(path)

    def test_file_can_tighten_but_not_loosen(self, tmp_path):
        path = self._write(tmp_path, (
            "protected_paths:\n  - docs/**\n"
            "merge_gate:\n"
            f"  diff_max_lines: {settings.guard_diff_max_lines + 5000}\n"
            "  diff_max_files: 3\n"
        ))
        policy = load_policy(path, force=True)
        # Added, and the compiled-in defaults are still in force.
        assert "docs/**" in policy.protected_paths
        assert "api/aria/guard/**" in policy.protected_paths
        # A larger cap in the file is ignored; a smaller one wins.
        assert policy.diff_max_lines == settings.guard_diff_max_lines
        assert policy.diff_max_files == 3

    def test_malformed_file_falls_back_without_shrinking_the_deny_list(self, tmp_path):
        path = self._write(tmp_path, "protected_paths: [oops]\n")
        policy = load_policy(path, force=True)
        assert policy.source == "settings"
        assert policy.error and "PolicyError" in policy.error
        assert is_protected("api/aria/guard/x.py", "/repo", policy) is True

    def test_hash_tracks_the_file_bytes(self, tmp_path):
        path = self._write(tmp_path, "protected_paths:\n  - docs/**\n")
        first = load_policy(path, force=True).hash
        assert load_policy(path, force=True).hash == first
        (tmp_path / "policy.yaml").write_text("protected_paths:\n  - docs/**\n  - x/**\n")
        assert load_policy(path, force=True).hash != first


class TestTamperDetection:
    async def test_trust_on_first_use_then_detects_a_change(self, tmp_path, monkeypatch):
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        db = FakeDB()

        first = await guard_policy.verify_policy(db)
        assert first["status"] == "trusted_on_first_use"
        assert first["ok"] is True

        assert (await guard_policy.verify_policy(db))["status"] == "ok"

        path.write_text("protected_paths:\n  - docs/**\n  - evil/**\n")
        tampered = await guard_policy.verify_policy(db)
        assert tampered["ok"] is False
        assert tampered["status"] == "tamper"

        events = db[guard_policy.GUARD_EVENTS_COLLECTION].docs
        assert any(e["kind"] == "policy:tamper" and e["blocked"] for e in events)

    async def test_accept_policy_blesses_the_new_hash(self, tmp_path, monkeypatch):
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        db = FakeDB()
        await guard_policy.verify_policy(db)

        path.write_text("protected_paths:\n  - docs/**\n  - new/**\n")
        assert (await guard_policy.verify_policy(db))["status"] == "tamper"

        await guard_policy.accept_policy(db, guard_policy.policy_hash(), actor="ben")
        assert (await guard_policy.verify_policy(db))["status"] == "ok"

    async def test_record_event_survives_a_dead_db(self):
        event = await guard_policy.record_event(
            None, "test:event", "no db here", blocked=True, severity="critical"
        )
        assert event["kind"] == "test:event" and event["blocked"] is True


# ---------------------------------------------------------------------------
# Sandbox profile
# ---------------------------------------------------------------------------

class TestSandboxPrefix:
    @pytest.fixture
    def layout(self, tmp_path):
        dev = tmp_path / "Development"
        (dev / "ProjectAria" / ".worktrees" / "demo-sess").mkdir(parents=True)
        (dev / "infrastructure").mkdir()
        (dev / "war-audio-game").mkdir()
        return dev

    def build(self, layout, tmp_path):
        return guard_sandbox.build_sandbox_prefix(
            str(layout / "ProjectAria" / ".worktrees" / "demo-sess"),
            "sess-1234",
            source_repo=str(layout / "ProjectAria"),
            development_root=str(layout),
            tmp_root=str(tmp_path / "tmp"),
            create_tmp=False,
        )

    def test_network_stays_open(self, layout, tmp_path):
        # A coding agent must reach :8108/:8080 and package registries; the
        # shell tool's --unshare-net would fail every session at turn 1.
        assert "--unshare-net" not in self.build(layout, tmp_path)

    def test_root_is_read_only_and_namespaces_are_unshared(self, layout, tmp_path):
        argv = self.build(layout, tmp_path)
        assert has_triple(argv, "--ro-bind", "/", "/")
        for flag in ("--unshare-pid", "--unshare-ipc", "--unshare-uts",
                     "--die-with-parent", "--new-session"):
            assert flag in argv

    def test_ssh_key_is_masked(self, layout, tmp_path):
        # The explicit red-team assertion: reading ~/.ssh must fail at the
        # kernel layer, not at a string check.
        argv = self.build(layout, tmp_path)
        assert os.path.expanduser("~/.ssh") in flag_targets(argv, "--tmpfs")

    def test_vault_and_credential_stores_are_masked(self, layout, tmp_path):
        masked = flag_targets(self.build(layout, tmp_path), "--tmpfs")
        for path in ("~/Obsidian", "~/.hermes", "~/.aria", "~/git-safe", "~/.config/gh"):
            assert os.path.expanduser(path) in masked

    def test_docker_socket_is_masked(self, layout, tmp_path):
        assert has_triple(self.build(layout, tmp_path),
                          "--ro-bind", "/dev/null", "/run/docker.sock")

    def test_pi_models_json_is_readable_not_masked(self, layout, tmp_path):
        argv = self.build(layout, tmp_path)
        models = os.path.expanduser("~/.pi/agent/models.json")
        assert has_triple(argv, "--ro-bind-try", models, models)
        assert models not in flag_targets(argv, "--tmpfs")

    def test_pi_session_transcripts_stay_writable(self, layout, tmp_path):
        argv = self.build(layout, tmp_path)
        sessions = os.path.expanduser("~/.pi/agent/sessions")
        assert has_triple(argv, "--bind-try", sessions, sessions)
        assert sessions not in flag_targets(argv, "--tmpfs")

    def test_sibling_repos_are_masked_but_own_repo_is_not(self, layout, tmp_path):
        argv = self.build(layout, tmp_path)
        masked = flag_targets(argv, "--tmpfs")
        assert str(layout / "infrastructure") in masked
        assert str(layout / "war-audio-game") in masked
        assert str(layout / "ProjectAria") not in masked

    def test_worktree_and_session_tmp_are_the_writable_surface(self, layout, tmp_path):
        worktree = str(layout / "ProjectAria" / ".worktrees" / "demo-sess")
        session_tmp = str(tmp_path / "tmp" / "aria-sess-1234")
        argv = self.build(layout, tmp_path)
        assert has_triple(argv, "--bind", worktree, worktree)
        assert has_triple(argv, "--bind", session_tmp, session_tmp)
        assert argv[-2:] == ["--chdir", worktree]

    def test_masks_precede_binds(self, layout, tmp_path):
        # bwrap applies operations in order, so a mask emitted after a bind
        # would silently blank the writable path it was supposed to protect.
        argv = self.build(layout, tmp_path)
        worktree = str(layout / "ProjectAria" / ".worktrees" / "demo-sess")
        last_mask = max(i for i, t in enumerate(argv) if t == "--tmpfs")
        bind_index = next(i for i, t in enumerate(argv)
                          if t == "--bind" and argv[i + 1] == worktree)
        assert last_mask < bind_index


class TestPreflight:
    def test_fails_closed_without_bwrap(self, monkeypatch):
        monkeypatch.setattr(settings, "guard_sandbox_enabled", True)
        monkeypatch.setattr(guard_sandbox.shutil, "which", lambda name: None)
        result = guard_sandbox.preflight()
        assert result["spawn_allowed"] is False
        assert any("not on PATH" in r for r in result["reasons"])

    def test_refuses_below_the_memory_floor(self, monkeypatch):
        monkeypatch.setattr(guard_sandbox, "mem_available_gib", lambda: 2.0)
        result = guard_sandbox.preflight()
        assert result["spawn_allowed"] is False
        assert any("spawn floor" in r for r in result["reasons"])

    def test_refuses_when_memory_is_unreadable(self, monkeypatch):
        monkeypatch.setattr(guard_sandbox, "mem_available_gib", lambda: None)
        assert guard_sandbox.preflight()["spawn_allowed"] is False

    def test_allows_with_headroom_and_sandbox_off(self, monkeypatch):
        monkeypatch.setattr(settings, "guard_sandbox_enabled", False)
        monkeypatch.setattr(guard_sandbox, "mem_available_gib", lambda: 64.0)
        assert guard_sandbox.preflight()["spawn_allowed"] is True


class TestSessionEnvAndResources:
    def test_strips_credentials_and_neuters_git_config(self, tmp_path):
        env = guard_sandbox.session_env(
            {
                "PATH": "/usr/bin", "HOME": "/home/ben", "LANG": "C",
                "API_KEY": "secret", "ADMIN_KEY": "admin", "GH_TOKEN": "ghp_x",
                "ANTHROPIC_API_KEY": "sk-ant", "AWS_SECRET_ACCESS_KEY": "aws",
                "RESTIC_PASSWORD": "restic", "SSH_AUTH_SOCK": "/run/agent",
            },
            session_id="sess-1", tmp_root=str(tmp_path), create_tmp=False,
        )
        for stripped in ("API_KEY", "ADMIN_KEY", "GH_TOKEN", "ANTHROPIC_API_KEY",
                         "AWS_SECRET_ACCESS_KEY", "RESTIC_PASSWORD", "SSH_AUTH_SOCK"):
            assert stripped not in env
        assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/ben"
        # No gh credential helper -> no `git push --force` to GitHub.
        assert env["GIT_CONFIG_GLOBAL"].endswith("gitconfig-stub")
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_session_token_is_the_only_credential_added(self, tmp_path):
        env = guard_sandbox.session_env(
            {"API_KEY": "secret"}, session_id="s", session_token="scoped-token",
            tmp_root=str(tmp_path), create_tmp=False,
        )
        assert env["ARIA_SESSION_TOKEN"] == "scoped-token"
        assert "API_KEY" not in env

    def test_resource_prefix_caps_memory_and_cpu(self):
        argv = guard_sandbox.resource_prefix("sess-abcdefgh")
        assert argv[:4] == ["systemd-run", "--user", "--scope", "--quiet"]
        assert f"MemoryMax={settings.guard_session_memory_max}" in argv
        assert f"CPUQuota={settings.guard_session_cpu_quota}" in argv


# ---------------------------------------------------------------------------
# Git protocol — real repositories
# ---------------------------------------------------------------------------

class TestPrepareSession:
    async def test_creates_worktree_branch_tag_and_mirror(self, guard, repo, tmp_path):
        result = await prepared(guard, repo)

        assert os.path.isdir(result["worktree"])
        assert result["branch"] == "aria/demo/sess-abc"
        assert result["source_branch"] == "main"
        assert git(["rev-parse", "--abbrev-ref", "HEAD"], result["worktree"]) == result["branch"]

        tags = git(["tag", "-l"], repo).splitlines()
        assert f"aria/ckpt/{SID}/start" in tags

        # The launcher needs this to decide whether the worktree's git metadata
        # (which lives outside the rw-bound worktree) is writable in the sandbox.
        assert result["git_dir"] == os.path.join(
            str(repo), ".git", "worktrees", os.path.basename(result["worktree"])
        )

        mirror = tmp_path / "git-safe" / "demo-project.git"
        assert mirror.is_dir()
        assert git(["config", "receive.denyNonFastForwards"], mirror) == "true"
        assert git(["config", "receive.denyDeletes"], mirror) == "true"
        assert git(["remote", "get-url", "safe"], repo) == str(mirror)
        # The branch reached the mirror, so the session already has an off-tree
        # copy before the agent has written a line.
        assert result["branch"] in git(["branch", "--list"], mirror).replace("*", "")

    async def test_is_idempotent(self, guard, repo):
        first = await prepared(guard, repo)
        second = await prepared(guard, repo)
        assert second["reused"] is True
        assert second["worktree"] == first["worktree"]


class TestCheckpoint:
    async def test_commits_as_aria_guard(self, guard, repo):
        session = await prepared(guard, repo)
        with open(os.path.join(session["worktree"], "feature.py"), "w") as handle:
            handle.write("print('work')\n")

        result = await guard.checkpoint(SID, reason="test")
        assert result["committed"] is True
        assert result["files"] == 1

        wt = session["worktree"]
        assert git(["log", "-1", "--format=%an"], wt) == "aria-guard"
        assert git(["log", "-1", "--format=%s"], wt).startswith("aria-ckpt: sess-abc test")
        assert "feature.py" in git(["ls-files"], wt).splitlines()
        assert git(["rev-parse", "HEAD"], wt) == result["sha"]

    async def test_clean_tree_is_a_no_op(self, guard, repo):
        await prepared(guard, repo)
        result = await guard.checkpoint(SID)
        assert result == {"ok": True, "committed": False, "reason": "clean", "skipped": []}

    async def test_oversized_files_are_skipped_and_reported(self, guard, repo, tmp_path):
        guard._policy = dataclasses.replace(
            load_policy(), checkpoint_max_file_bytes=1024
        )
        session = await prepared(guard, repo)
        wt = session["worktree"]
        with open(os.path.join(wt, "weights.gguf"), "wb") as handle:
            handle.write(b"\0" * 4096)
        with open(os.path.join(wt, "small.py"), "w") as handle:
            handle.write("x = 1\n")

        result = await guard.checkpoint(SID)
        assert result["committed"] is True
        assert [s["path"] for s in result["skipped"]] == ["weights.gguf"]
        assert result["skipped"][0]["bytes"] == 4096
        tracked = git(["ls-files"], wt).splitlines()
        assert "small.py" in tracked and "weights.gguf" not in tracked

    async def test_aborts_rather_than_hashing_a_huge_tree(self, guard, repo):
        """The 2026-08-15 failure: `add -A` over 18 GB of model weights."""
        guard._policy = dataclasses.replace(
            load_policy(), checkpoint_max_file_bytes=1 << 20, checkpoint_max_total_bytes=1024
        )
        session = await prepared(guard, repo)
        wt = session["worktree"]
        head_before = git(["rev-parse", "HEAD"], wt)
        with open(os.path.join(wt, "big.bin"), "wb") as handle:
            handle.write(b"\0" * 8192)

        result = await guard.checkpoint(SID)
        assert result["aborted"] is True and result["committed"] is False
        assert "MiB budget" in result["reason"]
        assert git(["rev-parse", "HEAD"], wt) == head_before

    async def test_records_events_when_a_db_is_present(self, repo, tmp_path):
        db = FakeDB()
        guard = GitGuard(db=db, mirror_root=str(tmp_path / "git-safe"))
        session = await guard.prepare_session(str(repo), SID, "demo")
        with open(os.path.join(session["worktree"], "a.py"), "w") as handle:
            handle.write("a = 1\n")
        await guard.checkpoint(SID)

        kinds = [e["kind"] for e in db[guard_policy.GUARD_EVENTS_COLLECTION].docs]
        assert "session:prepared" in kinds and "checkpoint:committed" in kinds
        assert len(db["guard_checkpoints"].docs) == 1


class TestRollback:
    async def test_restores_the_pre_session_state(self, guard, repo):
        session = await prepared(guard, repo)
        wt = session["worktree"]
        with open(os.path.join(wt, "README.md"), "w") as handle:
            handle.write("the agent rewrote this\n")
        await guard.checkpoint(SID)
        assert open(os.path.join(wt, "README.md")).read() == "the agent rewrote this\n"

        result = await guard.rollback(SID, to="start")
        assert result["ok"] is True
        assert open(os.path.join(wt, "README.md")).read() == "hello\n"

    async def test_rejects_an_unknown_target(self, guard, repo):
        await prepared(guard, repo)
        result = await guard.rollback(SID, to="deadbeef")
        assert result["ok"] is False and "unknown rollback target" in result["reason"]


async def _work(guard: GitGuard, repo, files: dict) -> dict:
    session = await prepared(guard, repo)
    for rel, content in files.items():
        full = os.path.join(session["worktree"], rel)
        if os.path.dirname(rel):
            os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(content)
    await guard.checkpoint(guard._sessions[SID]["session_id"])
    return session


class TestMergeGate:
    async def test_passes_on_a_small_clean_diff(self, guard, repo):
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        verdict = await guard.merge_gate(SID, check_command="true")
        assert verdict["passed"] is True, verdict["checks"]
        assert {c["name"] for c in verdict["checks"]} == {
            "worktree_clean", "check_command", "diff_size",
            "protected_paths", "gitleaks", "allowed_paths",
        }

    async def test_rejects_a_protected_path(self, guard, repo):
        await _work(guard, repo, {"api/aria/guard/sandbox.py": "# owned\n"})
        verdict = await guard.merge_gate(SID, check_command="true")
        assert verdict["passed"] is False
        assert "protected_paths" in verdict["failed"]
        check = next(c for c in verdict["checks"] if c["name"] == "protected_paths")
        assert check["hits"][0]["path"] == "api/aria/guard/sandbox.py"

    async def test_rejects_an_oversized_diff(self, guard, repo):
        await _work(guard, repo, {"big.py": "\n".join(f"line {i}" for i in range(200)) + "\n"})
        verdict = await guard.merge_gate(SID, check_command="true", max_lines=10, max_files=20)
        assert verdict["passed"] is False and "diff_size" in verdict["failed"]

    async def test_rejects_a_failing_check_command(self, guard, repo):
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        verdict = await guard.merge_gate(SID, check_command="exit 3")
        assert verdict["passed"] is False and "check_command" in verdict["failed"]

    async def test_missing_make_target_skips_rather_than_traps(self, guard, repo):
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        verdict = await guard.merge_gate(SID, check_command="make check")
        check = next(c for c in verdict["checks"] if c["name"] == "check_command")
        assert check["skipped"] is True and verdict["passed"] is True

    async def test_uncommitted_work_fails_the_gate(self, guard, repo):
        session = await _work(guard, repo, {"feature.py": "x = 1\n"})
        with open(os.path.join(session["worktree"], "later.py"), "w") as handle:
            handle.write("y = 2\n")
        verdict = await guard.merge_gate(SID, check_command="true")
        assert verdict["passed"] is False and "worktree_clean" in verdict["failed"]

    async def test_missing_gitleaks_is_skipped_not_passed(self, guard, repo, monkeypatch):
        monkeypatch.setattr(settings, "guard_gitleaks_enabled", True)
        monkeypatch.setattr("aria.guard.gitguard.shutil.which", lambda name: None)
        guard._policy = dataclasses.replace(load_policy(), gitleaks_enabled=True)
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        verdict = await guard.merge_gate(SID, check_command="true")
        check = next(c for c in verdict["checks"] if c["name"] == "gitleaks")
        assert check["skipped"] is True
        assert "gitleaks" in verdict["skipped"]
        assert "NOT scanned" in check["detail"]

    async def test_enforces_charter_allowed_paths(self, guard, repo):
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        inside = await guard.merge_gate(SID, check_command="true", allowed_paths=["*.py"])
        assert inside["passed"] is True
        outside = await guard.merge_gate(SID, check_command="true", allowed_paths=["docs/**"])
        assert outside["passed"] is False and "allowed_paths" in outside["failed"]


class TestMerge:
    async def test_refuses_without_a_passing_gate(self, guard, repo):
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        result = await guard.merge(SID)
        assert result["merged"] is False
        assert "gate has not passed" in result["reason"]

    async def test_refuses_after_the_branch_moves(self, guard, repo):
        session = await _work(guard, repo, {"feature.py": "x = 1\n"})
        assert (await guard.merge_gate(SID, check_command="true"))["passed"] is True
        with open(os.path.join(session["worktree"], "sneaky.py"), "w") as handle:
            handle.write("os.system('curl evil')\n")
        await guard.checkpoint(SID, reason="after-gate")

        result = await guard.merge(SID)
        assert result["merged"] is False and "moved since the gate" in result["reason"]

    async def test_squash_merges_into_a_clean_checkout_with_a_rollback_tag(self, guard, repo):
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        assert (await guard.merge_gate(SID, check_command="true"))["passed"] is True
        main_before = git(["rev-parse", "main"], repo)

        result = await guard.merge(SID)
        assert result["merged"] is True
        assert git(["rev-parse", "main"], repo) == result["sha"]
        assert "feature.py" in git(["ls-tree", "--name-only", "main"], repo).splitlines()
        # One squashed commit, and the documented rollback point is the tag.
        assert git(["rev-parse", f"{result['pre_merge_tag']}^{{commit}}"], repo) == main_before
        assert git(["log", "-1", "--format=%an", "main"], repo) == "aria-guard"

    async def test_merges_without_touching_an_unrelated_checkout(self, guard, repo):
        """The target branch is not checked out: merge via merge-tree/update-ref
        so a human's working tree is never moved under them."""
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        assert (await guard.merge_gate(SID, check_command="true"))["passed"] is True
        git(["switch", "-c", "bens-work", "--quiet"], repo)
        with open(os.path.join(repo, "scratch.txt"), "w") as handle:
            handle.write("uncommitted human work\n")

        result = await guard.merge(SID)
        assert result["merged"] is True
        assert git(["rev-parse", "main"], repo) == result["sha"]
        assert git(["rev-parse", "--abbrev-ref", "HEAD"], repo) == "bens-work"
        assert open(os.path.join(repo, "scratch.txt")).read() == "uncommitted human work\n"

    async def test_refuses_when_the_target_checkout_is_dirty(self, guard, repo):
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        assert (await guard.merge_gate(SID, check_command="true"))["passed"] is True
        with open(os.path.join(repo, "README.md"), "w") as handle:
            handle.write("ben was editing this\n")

        result = await guard.merge(SID)
        assert result["merged"] is False
        assert "local modifications" in result["reason"]
        assert open(os.path.join(repo, "README.md")).read() == "ben was editing this\n"


class TestDiscard:
    async def test_removes_the_worktree_and_parks_the_branch(self, guard, repo):
        session = await _work(guard, repo, {"feature.py": "x = 1\n"})
        result = await guard.discard(SID)

        assert result["ok"] is True
        assert not os.path.isdir(session["worktree"])
        assert result["parked_branch"] == "parked/demo/sess-abc"
        branches = [b.strip("* ") for b in git(["branch", "--list"], repo).splitlines()]
        assert "parked/demo/sess-abc" in branches
        # Parked, not lost: the work is still reachable for the postmortem.
        assert "feature.py" in git(
            ["ls-tree", "--name-only", "parked/demo/sess-abc"], repo
        ).splitlines()


class TestDegradation:
    async def test_unknown_session_never_raises(self, guard):
        assert (await guard.checkpoint("nope"))["ok"] is False
        assert (await guard.rollback("nope"))["ok"] is False
        assert (await guard.merge("nope"))["merged"] is False
        assert (await guard.merge_gate("nope"))["passed"] is False

    async def test_checkpoint_survives_a_missing_mirror(self, repo, tmp_path):
        """RPO is a nice-to-have; refusing to checkpoint because ~/git-safe is
        unwritable would trade a small loss for a total one."""
        blocked = tmp_path / "no-mirrors"
        blocked.write_text("not a directory")
        guard = GitGuard(db=None, mirror_root=str(blocked))
        session = await guard.prepare_session(str(repo), SID, "demo")
        assert session["mirror"] is None

        with open(os.path.join(session["worktree"], "a.py"), "w") as handle:
            handle.write("a = 1\n")
        result = await guard.checkpoint(SID)
        assert result["committed"] is True and result["pushed"] is False


class TestGuardEventShape:
    async def test_events_carry_what_the_cockpit_filters_on(self):
        db = FakeDB()
        await guard_policy.record_event(
            db, "sandbox:blocked", "attempted read of ~/.ssh/id_ed25519",
            session_id="s1", path="/home/ben/.ssh/id_ed25519", blocked=True,
            severity="critical",
        )
        event = db[guard_policy.GUARD_EVENTS_COLLECTION].docs[0]
        assert event["kind"] == "sandbox:blocked"
        assert event["session_id"] == "s1"
        assert event["blocked"] is True
        assert isinstance(event["at"], datetime)
        assert event["at"].tzinfo == timezone.utc
