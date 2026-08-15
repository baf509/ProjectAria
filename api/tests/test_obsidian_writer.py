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
