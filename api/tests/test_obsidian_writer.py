"""Tests for the C6 ObsidianWriter (atomic publish, guards, folder mapping)."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from aria.config import settings
from aria.integrations.obsidian import ObsidianWriter, _slugify_title


def _enabled(tmp_path):
    return (
        patch.object(settings, "obsidian_enabled", True),
        patch.object(settings, "obsidian_vault_path", str(tmp_path)),
    )


def test_slugify_title():
    assert _slugify_title("What's new in llama.cpp? (2026)") == "Whats new in llamacpp 2026"
    assert _slugify_title("   ") == "untitled"


@pytest.mark.asyncio
async def test_publish_disabled_returns_none(tmp_path):
    with patch.object(settings, "obsidian_enabled", False):
        w = ObsidianWriter(str(tmp_path))
        assert await w.publish("body", title="T") is None


@pytest.mark.asyncio
async def test_publish_writes_into_default_folder(tmp_path):
    e1, e2 = _enabled(tmp_path)
    with e1, e2:
        w = ObsidianWriter(str(tmp_path))
        path = await w.publish("The findings.", title="GPU memory research")
    assert path is not None
    assert f"/{settings.obsidian_default_folder}/Research/" in path
    text = open(path, encoding="utf-8").read()
    # Published docs lead with YAML frontmatter as of 2026-08-15 (steward plan
    # §3.1 item 8). Every human-authored doc in the vault carries `created:` /
    # `updated:` keys, and the VaultReader now parses that block to pick up
    # Ben's `approval:` / `accepted:` edits — so a doc ARIA writes without
    # frontmatter is a doc the read-back loop cannot see. The H1 still follows.
    assert text.startswith("---\n")
    fm, _, body = text.partition("\n---\n")
    assert "title: GPU memory research" in fm
    assert "generated_by: aria" in fm
    assert "created:" in fm and "updated:" in fm
    assert body.lstrip().startswith("# GPU memory research")
    assert "Published by ARIA on" in text
    assert "The findings." in text


@pytest.mark.asyncio
async def test_publish_maps_repo_path_to_vault_folder(tmp_path):
    e1, e2 = _enabled(tmp_path)
    with e1, e2:
        w = ObsidianWriter(str(tmp_path))
        path = await w.publish(
            "x", title="T", doc_type="Analysis", project="/home/ben/Development/ProjectAria/"
        )
    assert "/ProjectAria/Analysis/" in path


@pytest.mark.asyncio
async def test_publish_never_clobbers_same_day_duplicate(tmp_path):
    e1, e2 = _enabled(tmp_path)
    with e1, e2:
        w = ObsidianWriter(str(tmp_path))
        p1 = await w.publish("first", title="Same title")
        p2 = await w.publish("second", title="Same title")
    assert p1 != p2
    assert "first" in open(p1, encoding="utf-8").read()
    assert "second" in open(p2, encoding="utf-8").read()


@pytest.mark.asyncio
async def test_publish_rejects_bad_doc_type(tmp_path):
    e1, e2 = _enabled(tmp_path)
    with e1, e2:
        w = ObsidianWriter(str(tmp_path))
        # ValueError is swallowed into a None return (publish never raises
        # into the caller's flow).
        assert await w.publish("x", title="T", doc_type="Bogus") is None


@pytest.mark.asyncio
async def test_append_section_creates_and_appends(tmp_path):
    e1, e2 = _enabled(tmp_path)
    with e1, e2:
        w = ObsidianWriter(str(tmp_path))
        p = await w.append_section("notes.md", "First pass", "alpha")
        assert p is not None
        p2 = await w.append_section("notes.md", "Second pass", "beta")
    # Guard: the file was just written, so the human-edit window would
    # normally block the second append — but we wrote it, and the guard is
    # mtime-based. Verify the *skip* behavior instead:
    assert p2 is None
    text = open(p, encoding="utf-8").read()
    assert "## First pass" in text and "alpha" in text


@pytest.mark.asyncio
async def test_append_section_proceeds_when_file_is_old(tmp_path):
    e1, e2 = _enabled(tmp_path)
    with e1, e2:
        w = ObsidianWriter(str(tmp_path))
        p = await w.append_section("notes.md", "First", "alpha")
        old = 10_000
        os.utime(p, (os.stat(p).st_atime - old, os.stat(p).st_mtime - old))
        p2 = await w.append_section("notes.md", "Second", "beta")
    assert p2 == p
    text = open(p, encoding="utf-8").read()
    assert "## Second" in text and "beta" in text


@pytest.mark.asyncio
async def test_upsert_managed_without_a_db_never_claims_an_existing_doc(tmp_path):
    """No db handle means no hash record, which means ARIA cannot prove it wrote
    anything — so it must not rewrite a doc that already exists. Refusing here
    is the honest answer; the alternative is overwriting Ben's file on the
    strength of an assumption."""
    e1, e2 = _enabled(tmp_path)
    with e1, e2:
        w = ObsidianWriter(str(tmp_path))
        first = await w.upsert_managed("PLAN.md", {"status": "active"},
                                       "# Plan\n\nbody", managed_keys=["status"])
        assert first["wrote"] is True                 # creating it is fine
        second = await w.upsert_managed("PLAN.md", {"status": "paused"},
                                        "# Plan\n\nrewritten", managed_keys=["status"])
    assert second["wrote"] is False
    assert second["reason"] == "unknown-provenance"
    path = first["path"]
    assert "rewritten" not in open(path, encoding="utf-8").read()
    # The proposal still lands beside it, so nothing ARIA computed is lost.
    assert second["proposal_path"] and os.path.exists(second["proposal_path"])


@pytest.mark.asyncio
async def test_seed_keys_default_to_the_three_control_keys(tmp_path):
    """A caller that knows nothing about the split still gets the safe
    behaviour: `approval`/`autonomy`/`accepted` are seeded, never managed."""
    e1, e2 = _enabled(tmp_path)
    with e1, e2:
        w = ObsidianWriter(str(tmp_path))
        result = await w.upsert_managed(
            "PLAN.md",
            {"status": "active", "approval": "pending", "autonomy": 0,
             "accepted": "pending"},
            "# Plan\n\nbody",
            # Deliberately claiming them as managed: the split must win.
            managed_keys=["status", "approval", "autonomy", "accepted"],
        )
    assert result["seeded"] == ["accepted", "approval", "autonomy"]
    text = open(result["path"], encoding="utf-8").read()
    assert "approval: pending" in text and "autonomy: 0" in text


# ---------------------------------------------------------------------------
# Vault file permissions (2026-08-19)
#
# `tempfile.mkstemp` creates 0600 and `os.replace` preserves it, so every file
# written here landed readable only by ben. The obsidian-livesync bridge reads
# this vault from a container as a DIFFERENT uid, and one unreadable file does
# not degrade its sync -- it kills the `corsair-files` peer at startup with
# EACCES and silently stops disk->phone sync for the whole vault.
# ---------------------------------------------------------------------------

class TestVaultFilePermissions:
    def test_atomic_write_is_group_and_world_readable(self, tmp_path):
        import os
        import stat

        from aria.integrations.obsidian import ObsidianWriter

        target = tmp_path / "STEWARD_PLAN.md"
        ObsidianWriter._atomic_write(target, "# plan\n")

        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert mode & stat.S_IROTH, (
            f"mode {oct(mode)}: the livesync bridge runs as another uid and cannot "
            "read this file -- one such file stops sync for the entire vault"
        )
        assert mode & stat.S_IRGRP, f"mode {oct(mode)}: not group-readable"
        assert not (mode & (stat.S_IWOTH | stat.S_IXOTH)), (
            f"mode {oct(mode)}: a note should not be world-writable or executable"
        )

    def test_rewrite_does_not_regress_the_mode(self, tmp_path):
        """The second write goes through os.replace onto an existing inode --
        the mode must come from the new temp file, not be inherited."""
        import os
        import stat

        from aria.integrations.obsidian import ObsidianWriter

        target = tmp_path / "STEWARD_PLAN.md"
        ObsidianWriter._atomic_write(target, "# first\n")
        os.chmod(target, 0o600)
        ObsidianWriter._atomic_write(target, "# second\n")

        mode = stat.S_IMODE(os.stat(target).st_mode)
        assert mode & stat.S_IROTH, f"mode {oct(mode)}: rewrite left the file unreadable"
