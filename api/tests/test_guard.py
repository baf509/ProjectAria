"""Tests for the Guard package — policy, sandbox profile, and the git protocol.

The git tests run against a REAL git repository created in tmp_path. Mocking git
here would test that we can write argv, not that the protocol works; the whole
value of the guard is that `git reset --hard aria/ckpt/<sid>/start` actually
brings the tree back, so that is what these assert.

Nothing here needs MongoDB (GitGuard takes db=None and keeps its session registry
in-process), a live aria-api, or the network. bwrap is never executed — the
sandbox tests assert on the argv the guard would run.

⚠️ Nothing here may touch `~/.aria/guard/accepted_policy.json` either. An earlier
version of these tests did, and the hash of a tmp_path policy file ended up in
the production acceptance record — which `main.py` turns into an e-stop with
`auto_thaw=False` on the next aria-api restart. The autouse `guard_state_isolation`
fixture below is what makes that impossible; do not remove it, and do not add a
test that unsets it.
"""

import dataclasses
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from aria.config import settings
from aria.guard import gitguard as guard_gitguard
from aria.guard import policy as guard_policy
from aria.guard import sandbox as guard_sandbox
from aria.guard.gitguard import GitGuard, GuardGitError, GuardMergeConflict
from aria.guard.policy import (
    GuardPolicy,
    PolicyError,
    is_protected,
    load_policy,
    parse_simple_yaml,
)


def _production_state_fingerprint():
    path = os.path.expanduser(guard_policy.DEFAULT_GUARD_STATE_PATH)
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


@pytest.fixture(autouse=True)
def guard_state_isolation(tmp_path, monkeypatch):
    """Every test gets its own accepted-hash file, and proves it left the real
    one alone. See the module docstring."""
    before = _production_state_fingerprint()
    monkeypatch.setenv(
        guard_policy.GUARD_STATE_ENV, str(tmp_path / "guard-state" / "accepted.json")
    )
    yield
    assert _production_state_fingerprint() == before, (
        f"a guard test modified {guard_policy.DEFAULT_GUARD_STATE_PATH} — that file is "
        "the production tamper baseline and a wrong value e-stops aria-api at boot"
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
SID8 = "sess-abc-cba999"   # 8 readable chars + 6 of sha256(SID) — see _sid8


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

    def test_file_cannot_open_a_hole_in_the_sandbox(self, tmp_path):
        """The reviewer's policy: `ro_paths: ["~/.ssh"]`, `rw_paths: ["/home/ben"]`.

        Unioning those with the floor put `--ro-bind-try ~/.ssh` and
        `--bind /home/ben /home/ben` AFTER the masks, which un-masked the SSH
        keys and made the whole home writable. Exception lists intersect.
        """
        path = self._write(tmp_path, (
            "sandbox:\n"
            "  tmpfs_paths:\n    - ~/.ssh\n"
            "  ro_paths:\n    - ~/.ssh\n    - ~/.pi/agent/models.json\n"
            "  rw_paths:\n    - /home/ben\n    - ~/.pi/agent/sessions\n"
        ))
        policy = load_policy(path, force=True)

        assert os.path.expanduser("~/.ssh") not in [
            os.path.expanduser(p) for p in policy.sandbox_ro_paths
        ]
        assert "/home/ben" not in [os.path.expanduser(p) for p in policy.sandbox_rw_paths]
        # Masks still union (deny side), and the floor's own exceptions survive.
        assert "~/.ssh" in policy.sandbox_tmpfs_paths
        assert "~/.pi/agent/models.json" in policy.sandbox_ro_paths
        assert "~/.pi/agent/sessions" in policy.sandbox_rw_paths
        # ...and the attempt is reported rather than silently dropped.
        rejected = {(r["list"], r["path"]) for r in policy.rejected}
        assert ("ro_paths", "~/.ssh") in rejected
        assert ("rw_paths", "/home/ben") in rejected

    def test_file_may_still_drop_an_exception(self, tmp_path):
        """Removing an ro/rw entry is a tightening, so the file keeps that power."""
        path = self._write(tmp_path, "sandbox:\n  ro_paths:\n    - ~/.claude\n")
        policy = load_policy(path, force=True)
        assert policy.sandbox_ro_paths == ["~/.claude"]
        assert "~/.pi/agent/models.json" not in policy.sandbox_ro_paths

    def test_omitting_a_sandbox_key_keeps_the_floor(self, tmp_path):
        """Absent means "no opinion" — otherwise deleting a key would break every
        session instead of tightening anything."""
        policy = load_policy(self._write(tmp_path, "protected_paths:\n  - docs/**\n"), force=True)
        assert policy.sandbox_ro_paths == list(settings.guard_sandbox_ro_paths)
        assert policy.sandbox_rw_paths == list(settings.guard_sandbox_rw_paths)

    def test_regex_cache_is_bounded(self):
        """Keyed by caller-supplied strings (charter allowed_paths), so unbounded
        is a slow leak in a process that never restarts."""
        for i in range(guard_policy._REGEX_CACHE_MAX + 200):
            guard_policy.match_any("a/b.py", [f"pattern-{i}/**"])
        assert len(guard_policy._regex_cache) <= guard_policy._REGEX_CACHE_MAX

    def test_absurd_pattern_is_refused_not_compiled(self):
        assert guard_policy.match_any("a.py", ["x" * 5000]) is None

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

    async def test_deleting_the_accepted_hash_is_tamper_not_first_use(
        self, tmp_path, monkeypatch
    ):
        """The one-delete bypass: Mongo is bound 0.0.0.0 with no auth and the
        sandbox leaves the network open, so a session can drop
        `app_state/_id=guard_policy` itself. That used to re-arm
        trust-on-first-use, which blessed an edited policy.yaml on the next boot
        with no tamper event and no e-stop.
        """
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        db = FakeDB()
        assert (await guard_policy.verify_policy(db))["status"] == "trusted_on_first_use"

        # The attack: edit the policy, then delete the single document.
        path.write_text("protected_paths:\n  - docs/**\nsandbox:\n  ro_paths:\n    - ~/.claude\n")
        db.app_state.docs.clear()

        verdict = await guard_policy.verify_policy(db)
        assert verdict["ok"] is False
        assert verdict["status"] == "tamper"
        assert "deletion, not a first run" in verdict["detail"]
        assert "guard/policy/accept" in verdict["remedy"]
        kinds = [e["kind"] for e in db[guard_policy.GUARD_EVENTS_COLLECTION].docs]
        assert "policy:state_reset" in kinds
        # Single-use: the document is restored from disk, so the next boot
        # compares against a real baseline instead of re-arming again.
        assert db.app_state.docs and db.app_state.docs[0]["hash"]

    async def test_tofu_leaves_evidence_outside_mongo(self, tmp_path, monkeypatch):
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        await guard_policy.verify_policy(FakeDB())

        state = json.loads(open(guard_policy.guard_state_path()).read())
        assert state["hash"] == guard_policy.policy_hash()
        assert state["history"][0]["actor"] == "trust-on-first-use"
        assert oct(os.stat(guard_policy.guard_state_path()).st_mode)[-3:] == "600"

    async def test_unreadable_accepted_hash_fails_closed(self, tmp_path, monkeypatch):
        """`main.py` e-stops on `not ok`; returning ok=True with
        status="unknown" meant an unreadable baseline produced no e-stop at
        all — the failure mode a tamper check must never have."""
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))

        class BrokenDB(FakeDB):
            @property
            def app_state(self):
                raise RuntimeError("mongo is unreachable")

        verdict = await guard_policy.verify_policy(BrokenDB())
        assert verdict["ok"] is False
        assert verdict["status"] == "unknown"

    async def test_corrupt_state_file_fails_closed(self, tmp_path, monkeypatch):
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        state = guard_policy.guard_state_path()
        os.makedirs(os.path.dirname(state), exist_ok=True)
        with open(state, "w") as handle:
            handle.write("{ not json")

        verdict = await guard_policy.verify_policy(FakeDB())
        assert verdict["ok"] is False and verdict["status"] == "unknown"
        # Absent and unreadable are DIFFERENT answers; only absent is first use.
        assert "unreadable" in verdict["detail"]

    async def test_mongo_outage_with_a_matching_disk_record_does_not_estop(
        self, tmp_path, monkeypatch
    ):
        """The one non-fail-closed branch, and why: the on-disk record is the
        harder of the two stores to reach from a session, so a match there is
        positive evidence. E-stopping on a Mongo hiccup would make the guard the
        outage."""
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        db = FakeDB()
        await guard_policy.verify_policy(db)

        class BrokenDB(FakeDB):
            @property
            def app_state(self):
                raise RuntimeError("mongo is unreachable")

        verdict = await guard_policy.verify_policy(BrokenDB())
        assert verdict["ok"] is True and verdict["status"] == "ok_disk_only"

    async def test_a_hashless_accepted_record_fails_closed(self, tmp_path, monkeypatch):
        """A stored document with no hash used to skip the comparison entirely
        and return ok — "we don't know" reading as "fine"."""
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths:\n  - docs/**\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        db = FakeDB()
        await guard_policy.verify_policy(db)
        db.app_state.docs[0].pop("hash")

        verdict = await guard_policy.verify_policy(db)
        assert verdict["ok"] is False and verdict["status"] == "unknown"

    async def test_an_unparseable_policy_is_never_blessed(self, tmp_path, monkeypatch):
        """`current` is the settings digest when the file will not parse, so
        accepting it would record a baseline that says nothing about the file —
        and then read as tamper the moment the file parses again."""
        path = tmp_path / "policy.yaml"
        path.write_text("protected_paths: [oops]\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        db = FakeDB()

        verdict = await guard_policy.verify_policy(db)
        assert verdict["ok"] is False and verdict["status"] == "unparseable"
        assert db.app_state.docs == []
        assert not os.path.exists(guard_policy.guard_state_path())

    async def test_a_widening_policy_file_is_reported_as_critical(self, tmp_path, monkeypatch):
        path = tmp_path / "policy.yaml"
        path.write_text("sandbox:\n  rw_paths:\n    - /home/ben\n")
        monkeypatch.setattr(guard_policy, "policy_file_path", lambda: str(path))
        db = FakeDB()
        await guard_policy.verify_policy(db)
        events = db[guard_policy.GUARD_EVENTS_COLLECTION].docs
        assert any(
            e["kind"] == "policy:rejected_widening" and e["severity"] == "critical"
            for e in events
        )


# ---------------------------------------------------------------------------
# Sandbox profile
# ---------------------------------------------------------------------------

class TestSandboxPrefix:
    @pytest.fixture
    def layout(self, tmp_path):
        dev = tmp_path / "Development"
        (dev / "ProjectAria" / ".worktrees" / "demo-sess").mkdir(parents=True)
        (dev / "ProjectAria" / ".claude").mkdir()
        (dev / "ProjectAria" / ".env").write_text("ADMIN_KEY=super-secret\n")
        (dev / "ProjectAria" / ".env.bak-20260721").write_text("API_KEY=old\n")
        (dev / "ProjectAria" / ".claude" / "settings.local.json").write_text("{}\n")
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

    def test_the_visible_repos_credential_files_are_masked(self, layout, tmp_path):
        """The session's own repo stays readable so the agent can read the
        source tree — which left `<repo>/.env` (ADMIN_KEY, API_KEY) readable
        under `--ro-bind / /`. `.env` is gitignored, so it is not in the
        worktree, but it is at the repo root; with the network open that key
        authorises POST /guard/sessions/{id}/merge on localhost:8200. Stripping
        the ENVIRONMENT was never enough — the FILE has to go too.
        """
        argv = self.build(layout, tmp_path)
        repo = layout / "ProjectAria"
        for secret in (".env", ".env.bak-20260721", ".claude/settings.local.json"):
            assert has_triple(argv, "--ro-bind", "/dev/null", str(repo / secret)), secret

    def test_the_agents_own_worktree_is_not_credential_masked(self, layout, tmp_path):
        """The worktree is the agent's writable space and the repo's real .env
        is gitignored (so it is not in the worktree at all) — masking a .env the
        session created there would break its work to protect it from itself."""
        worktree = layout / "ProjectAria" / ".worktrees" / "demo-sess"
        (worktree / ".env").write_text("PORT=8080\n")
        argv = self.build(layout, tmp_path)
        assert not has_triple(argv, "--ro-bind", "/dev/null", str(worktree / ".env"))

    def test_claude_credentials_are_masked_after_the_claude_ro_bind(self, layout, tmp_path):
        """`~/.claude` is ro-bound so claude_code can authenticate, and that bind
        mounts over any earlier mask — so the credential file mask has to be
        re-applied AFTER it, or the ro-bind hands back the Anthropic token that
        the `_API_KEY` env-strip was meant to remove."""
        argv = self.build(layout, tmp_path)
        creds = os.path.expanduser("~/.claude/.credentials.json")
        claude = os.path.expanduser("~/.claude")
        bind_at = max(i for i, t in enumerate(argv)
                      if t == "--ro-bind-try" and argv[i + 1] == claude)
        mask_at = max(i for i, t in enumerate(argv)
                      if t == "--ro-bind" and argv[i + 2] == creds)
        assert mask_at > bind_at

    def test_a_policy_bind_can_never_unmask_or_widen(self, tmp_path, layout, monkeypatch):
        """Second line of defence, at the argv the guard actually emits: even if
        `ro_paths`/`rw_paths` somehow contained these, the profile must not.

        The three shapes that matter, all from the reviewer's run:
          - ro-bind of a masked directory        (~/.ssh un-masked)
          - rw-bind of an ANCESTOR of a mask     (/home/ben remounts over ~/.ssh)
          - bind of a path inside a masked dir   (content resurfaces in a tmpfs)
        """
        home = os.path.expanduser("~")
        hostile = dataclasses.replace(
            load_policy(),
            sandbox_ro_paths=["~/.ssh", f"{home}/.hermes/config.yaml"],
            sandbox_rw_paths=[home, "~/.pi/agent/sessions"],
        )
        argv = guard_sandbox.build_sandbox_prefix(
            str(layout / "ProjectAria" / ".worktrees" / "demo-sess"),
            "sess-1234",
            source_repo=str(layout / "ProjectAria"),
            development_root=str(layout),
            tmp_root=str(tmp_path / "tmp"),
            create_tmp=False,
            policy=hostile,
        )
        ro_targets = flag_targets(argv, "--ro-bind-try")
        rw_targets = flag_targets(argv, "--bind-try") + flag_targets(argv, "--bind")
        assert os.path.expanduser("~/.ssh") not in ro_targets
        assert f"{home}/.hermes/config.yaml" not in ro_targets
        assert home not in rw_targets
        # The legitimate exception still survives the filter.
        assert os.path.expanduser("~/.pi/agent/sessions") in rw_targets

    def test_absent_paths_are_not_masked(self, layout, tmp_path, monkeypatch):
        """bwrap cannot create a mount point under `--ro-bind / /`:
        `--tmpfs ~/.missing` exits 1 with "Read-only file system" and refuses the
        WHOLE session. Masking an absent path protects nothing and breaks
        everything — which is what `~/.git-credentials` (absent on this box) did
        to every spawn until 2026-08-15.
        """
        policy = dataclasses.replace(
            load_policy(), sandbox_tmpfs_paths=[str(tmp_path / "nope"), str(layout)]
        )
        argv = guard_sandbox.build_sandbox_prefix(
            str(layout / "ProjectAria" / ".worktrees" / "demo-sess"), "sess-1234",
            source_repo=str(layout / "ProjectAria"), development_root=str(layout),
            tmp_root=str(tmp_path / "tmp"), create_tmp=False, policy=policy,
        )
        assert str(tmp_path / "nope") not in flag_targets(argv, "--tmpfs")
        assert str(layout) in flag_targets(argv, "--tmpfs")

    def test_the_accepted_policy_record_is_masked(self, layout, tmp_path):
        """A session that can edit the acceptance file can re-arm
        trust-on-first-use — the other half of the G2 attack."""
        argv = self.build(layout, tmp_path)
        state_dir = os.path.dirname(guard_policy.guard_state_path())
        masked = flag_targets(argv, "--tmpfs")
        assert any(
            state_dir == m or state_dir.startswith(m.rstrip("/") + "/") for m in masked
        ), (state_dir, masked)

    def test_worktree_and_session_tmp_are_the_writable_surface(self, layout, tmp_path):
        worktree = str(layout / "ProjectAria" / ".worktrees" / "demo-sess")
        session_tmp = str(tmp_path / "tmp" / "aria-sess-1234")
        argv = self.build(layout, tmp_path)
        assert has_triple(argv, "--bind", worktree, worktree)
        assert has_triple(argv, "--bind", session_tmp, session_tmp)
        assert argv[-2:] == ["--chdir", worktree]

    def test_directory_masks_precede_binds(self, layout, tmp_path):
        # bwrap applies operations in order, so a DIRECTORY mask emitted after a
        # bind would silently blank the writable path it was supposed to
        # protect. (File masks are deliberately re-applied afterwards — see
        # test_claude_credentials_are_masked_after_the_claude_ro_bind.)
        argv = self.build(layout, tmp_path)
        worktree = str(layout / "ProjectAria" / ".worktrees" / "demo-sess")
        last_mask = max(i for i, t in enumerate(argv) if t == "--tmpfs")
        bind_index = next(i for i, t in enumerate(argv)
                          if t == "--bind" and argv[i + 1] == worktree)
        assert last_mask < bind_index


class TestPreflight:
    @pytest.fixture(autouse=True)
    def no_canary_cache(self):
        """The canary is cached process-wide; a stale entry would make these
        tests depend on each other's order."""
        guard_sandbox._canary_cache.update({"key": None, "at": 0.0, "result": None})

    def test_fails_closed_without_bwrap(self, monkeypatch):
        monkeypatch.setattr(settings, "guard_sandbox_enabled", True)
        monkeypatch.setattr(guard_sandbox.shutil, "which", lambda name: None)
        result = guard_sandbox.preflight()
        assert result["spawn_allowed"] is False
        assert any("not on PATH" in r for r in result["reasons"])

    def test_a_sandbox_that_cannot_start_a_process_refuses_the_spawn(self, monkeypatch):
        """`which("bwrap")` answers a different question than the one that
        matters. On 2026-08-15 the profile named `~/.git-credentials`, absent on
        this box, so every spawn died with "Can't mkdir …: Read-only file
        system" before exec — and the red-team drill scored 5/9 "contained"
        against a sandbox that had never run a process, because every probe was
        "the secret did not appear in the output". An actuator needs an oracle
        that is not its own exit code.
        """
        monkeypatch.setattr(settings, "guard_sandbox_enabled", True)
        monkeypatch.setattr(
            guard_sandbox, "sandbox_canary",
            lambda **_: {"ok": False, "detail": "Can't mkdir /home/ben/.git-credentials"},
        )
        result = guard_sandbox.preflight()
        assert result["spawn_allowed"] is False
        assert any("cannot start a process" in r for r in result["reasons"])
        assert result["canary"]["ok"] is False

    def test_the_canary_is_cached(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=guard_sandbox._CANARY_TOKEN + "\n", stderr=""
            )

        monkeypatch.setattr(guard_sandbox.subprocess, "run", fake_run)
        monkeypatch.setattr(guard_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
        assert guard_sandbox.sandbox_canary()["ok"] is True
        second = guard_sandbox.sandbox_canary()
        assert second["ok"] is True and second["cached"] is True
        assert len(calls) == 1

    def test_the_canary_asserts_on_output_not_on_rc(self, monkeypatch):
        """rc=0 with no output is exactly the silent success this exists to
        catch, so the token has to be *printed*."""
        monkeypatch.setattr(guard_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
        monkeypatch.setattr(
            guard_sandbox.subprocess, "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
        )
        assert guard_sandbox.sandbox_canary()["ok"] is False

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
        assert result["branch"] == f"aria/demo/{SID8}"
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

    async def test_recovers_a_worktree_that_was_deleted(self, guard, repo):
        """Reuse required isdir(worktree); otherwise it fell through to
        `git worktree add` on a path git still had registered, which fails
        forever. `discard()` prunes — this path did not, so a session whose
        directory was removed could never be prepared under its id again, and
        the id is what its branch, tag and checkpoints all key off.
        """
        first = await prepared(guard, repo)
        shutil.rmtree(first["worktree"])

        second = await prepared(guard, repo)
        assert second["recovered"] is True
        assert os.path.isdir(second["worktree"])
        assert second["branch"] == first["branch"]
        assert git(["rev-parse", "--abbrev-ref", "HEAD"], second["worktree"]) == first["branch"]

    async def test_two_ids_sharing_a_prefix_get_different_worktrees(self, guard, repo):
        """ARIA's session ids are `sess-<uuid-ish>`, so the first 8 characters
        are `sess-abc` for every session ever created; truncating to 8 made the
        second `worktree add` fail and that id permanently unpreparable."""
        other = SID[:-1] + "X"
        first = await guard.prepare_session(str(repo), SID, "demo")
        second = await guard.prepare_session(str(repo), other, "demo")
        assert first["branch"] != second["branch"]
        assert first["worktree"] != second["worktree"]
        assert os.path.isdir(second["worktree"])


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
        assert git(["log", "-1", "--format=%s"], wt).startswith(f"aria-ckpt: {SID8} test")
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

    async def test_symlinks_are_reported_not_skipped_silently(self, guard, repo):
        """"Never silently" is the module's stated contract, and a silent skip
        plus `reason: "clean"` is what deadlocked the gate: `git status
        --porcelain` still counts the symlink, so the gate stays red and the
        advice ("run a checkpoint first") can never clear it."""
        session = await prepared(guard, repo)
        wt = session["worktree"]
        os.symlink("/etc/passwd", os.path.join(wt, "sneaky-link"))

        result = await guard.checkpoint(SID)
        assert result["committed"] is False
        assert result["reason"] == "blocked"          # NOT "clean"
        assert [s["path"] for s in result["skipped"]] == ["sneaky-link"]
        assert result["skipped"][0]["reason"] == "symlink"
        assert "sneaky-link" in result["detail"]

    async def test_the_gate_explains_what_a_checkpoint_cannot_clear(self, guard, repo):
        """The two notions of clean must agree, or the gate must say precisely
        what is blocking it and what to do about it."""
        session = await prepared(guard, repo)
        wt = session["worktree"]
        os.symlink("/etc/passwd", os.path.join(wt, "sneaky-link"))
        await guard.checkpoint(SID)

        verdict = await guard.merge_gate(SID, check_command="true")
        check = next(c for c in verdict["checks"] if c["name"] == "worktree_clean")
        assert check["passed"] is False
        assert "CANNOT stage" in check["detail"]
        assert "sneaky-link" in check["detail"]
        assert [b["path"] for b in check["blocking"]] == ["sneaky-link"]

    async def test_a_protected_path_raises_at_write_time(self, repo, tmp_path):
        """policy.yaml: "A touch is an immediate raise, not merely a merge
        rejection". Until this existed, `is_protected` had no caller outside the
        merge gate — an agent could rewrite api/aria/guard/** and have the guard
        itself commit it every ten minutes, silently, until someone ran a gate.
        """
        db = FakeDB()
        alerts: list[dict] = []

        class FakeNotifier:
            async def notify(self, **kwargs):
                alerts.append(kwargs)
                return {"queued": True}

        guard = GitGuard(db=db, mirror_root=str(tmp_path / "git-safe"),
                         notifier=FakeNotifier())
        session = await guard.prepare_session(str(repo), SID, "demo")
        target = os.path.join(session["worktree"], "api", "aria", "guard", "policy.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as handle:
            handle.write("# the agent rewrote its own evaluator\n")

        result = await guard.checkpoint(SID)
        assert result["committed"] is True            # captured, so it is revertible
        assert [h["path"] for h in result["protected"]] == ["api/aria/guard/policy.py"]

        event = next(e for e in db[guard_policy.GUARD_EVENTS_COLLECTION].docs
                     if e["kind"] == "policy:protected_touch")
        assert event["severity"] == "critical" and event["blocked"] is True
        assert alerts and alerts[0]["needs_human"] is True
        assert alerts[0]["source"] == "guard"

        # A Ralph loop checkpoints every few minutes; re-raising the same path
        # forever is how the one alert that must not be ignored gets ignored.
        with open(target, "a") as handle:
            handle.write("# again\n")
        await guard.checkpoint(SID)
        assert len(alerts) == 1

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

    async def test_a_timed_out_check_command_is_killed(self, guard, repo, monkeypatch):
        """Returning on timeout without killing leaves agent-authored code
        running as ben, holding the worktree, forever. `_git` in the same file
        has always killed on timeout; these two paths simply did not."""
        await _work(guard, repo, {"feature.py": "x = 1\n"})
        monkeypatch.setattr(settings, "coding_gate_timeout_seconds", 1)

        started = []
        real = guard_gitguard.asyncio.create_subprocess_shell

        async def spy(*args, **kwargs):
            proc = await real(*args, **kwargs)
            started.append(proc)
            return proc

        monkeypatch.setattr(guard_gitguard.asyncio, "create_subprocess_shell", spy)
        verdict = await guard.merge_gate(SID, check_command="sleep 60")

        check = next(c for c in verdict["checks"] if c["name"] == "check_command")
        assert check["passed"] is False and "timed out" in check["detail"]
        assert started and started[0].returncode is not None, "the child was leaked"

    async def test_a_timed_out_gitleaks_is_killed(self, guard, repo, tmp_path, monkeypatch):
        fake = tmp_path / "gitleaks"
        fake.write_text("#!/bin/sh\nsleep 60\n")
        fake.chmod(0o755)
        monkeypatch.setattr("aria.guard.gitguard.shutil.which", lambda name: str(fake))
        monkeypatch.setattr(guard_gitguard, "GITLEAKS_TIMEOUT_SECONDS", 1)
        guard._policy = dataclasses.replace(load_policy(), gitleaks_enabled=True)
        await _work(guard, repo, {"feature.py": "x = 1\n"})

        started = []
        real = guard_gitguard.asyncio.create_subprocess_exec

        async def spy(program, *args, **kwargs):
            proc = await real(program, *args, **kwargs)
            if program == str(fake):
                started.append(proc)
            return proc

        monkeypatch.setattr(guard_gitguard.asyncio, "create_subprocess_exec", spy)
        verdict = await guard.merge_gate(SID, check_command="true")

        check = next(c for c in verdict["checks"] if c["name"] == "gitleaks")
        assert check["passed"] is False and "killed" in check["detail"]
        assert started and started[0].returncode is not None, "the scanner was leaked"

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
        # And no rollback tag for a merge that never happened: the tag used to
        # be created and pushed BEFORE this refusal, littering one per attempt.
        assert result["pre_merge_tag"] is None
        assert [t for t in git(["tag", "-l"], repo).splitlines()
                if t.startswith("aria-pre-merge/")] == []

    async def test_a_conflicting_merge_leaves_bens_tree_exactly_as_it_was(self, repo, tmp_path):
        """The one that corrupts a human's checkout.

        `git merge --squash` records NO MERGE_HEAD, so `git merge --abort` fails
        with "There is no merge to abort" — and the old code ran it, discarded
        the return code, and raised. Ben has `main` checked out and clean, the
        dirty-check passes, and the guard leaves his working tree full of `UU`
        conflict markers while the API returns 500 and records nothing.
        """
        db = FakeDB()
        guard = GitGuard(db=db, mirror_root=str(tmp_path / "git-safe"))
        await _work(guard, repo, {"README.md": "the agent's version\n"})
        assert (await guard.merge_gate(SID, check_command="true"))["passed"] is True

        # Ben commits a conflicting change on main and leaves the tree clean.
        with open(os.path.join(repo, "README.md"), "w") as handle:
            handle.write("ben's version\n")
        git(["commit", "-q", "-am", "ben edits the same lines"], repo)
        main_before = git(["rev-parse", "main"], repo)

        result = await guard.merge(SID)

        assert result["merged"] is False and result["conflict"] is True
        assert git(["rev-parse", "main"], repo) == main_before
        assert git(["status", "--porcelain"], repo) == ""
        assert open(os.path.join(repo, "README.md")).read() == "ben's version\n"
        kinds = [e["kind"] for e in db[guard_policy.GUARD_EVENTS_COLLECTION].docs]
        assert "merge:conflict" in kinds

    async def test_a_conflicting_merge_into_an_unchecked_out_branch_is_also_clean(
        self, guard, repo
    ):
        await _work(guard, repo, {"README.md": "the agent's version\n"})
        assert (await guard.merge_gate(SID, check_command="true"))["passed"] is True
        with open(os.path.join(repo, "README.md"), "w") as handle:
            handle.write("ben's version\n")
        git(["commit", "-q", "-am", "ben edits the same lines"], repo)
        main_before = git(["rev-parse", "main"], repo)
        git(["switch", "-c", "bens-work", "--quiet"], repo)

        result = await guard.merge(SID)
        assert result["merged"] is False and result["conflict"] is True
        assert git(["rev-parse", "main"], repo) == main_before

    async def test_a_failed_restore_is_loud(self, guard, repo, monkeypatch):
        """The one case that must NOT be swallowed: we could not put the tree
        back. That is a GuardGitError (a 500 and a critical event), not a 409."""
        session = await _work(guard, repo, {"README.md": "the agent's version\n"})
        assert (await guard.merge_gate(SID, check_command="true"))["passed"] is True
        with open(os.path.join(repo, "README.md"), "w") as handle:
            handle.write("ben's version\n")
        git(["commit", "-q", "-am", "conflict"], repo)

        real_git = guard._git

        async def sabotaged(args, cwd, timeout=120):
            if args[:2] == ["reset", "--hard"]:
                return 1, "", "simulated: could not reset"
            return await real_git(args, cwd, timeout)

        monkeypatch.setattr(guard, "_git", sabotaged)
        with pytest.raises(GuardGitError, match="needs manual repair"):
            await guard.merge(SID)


class TestDiscard:
    async def test_removes_the_worktree_and_parks_the_branch(self, guard, repo):
        session = await _work(guard, repo, {"feature.py": "x = 1\n"})
        result = await guard.discard(SID)

        assert result["ok"] is True
        assert not os.path.isdir(session["worktree"])
        assert result["parked_branch"] == f"parked/demo/{SID8}"
        branches = [b.strip("* ") for b in git(["branch", "--list"], repo).splitlines()]
        assert f"parked/demo/{SID8}" in branches
        # Parked, not lost: the work is still reachable for the postmortem.
        assert "feature.py" in git(
            ["ls-tree", "--name-only", f"parked/demo/{SID8}"], repo
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
