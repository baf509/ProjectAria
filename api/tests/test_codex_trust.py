"""Tests for codex_trust.ensure_codex_trusted (pre-seeding ~/.codex/config.toml)."""

import tomllib

import pytest

from aria.config import settings
from aria.shells.codex_trust import ensure_codex_trusted


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(settings, "shells_codex_config_path", str(path), raising=False)
    return path


def _trust_level(path, workdir):
    data = tomllib.loads(path.read_text())
    return data.get("projects", {}).get(str(workdir), {}).get("trust_level")


def test_creates_missing_config(config_path, tmp_path):
    workdir = tmp_path / "repo"
    assert ensure_codex_trusted(str(workdir)) is True
    assert _trust_level(config_path, workdir) == "trusted"


def test_appends_preserving_existing_content(config_path, tmp_path):
    config_path.write_text(
        '# hand-written comment\nmodel = "gpt-5.6-sol"\n\n'
        '[projects."/somewhere/else"]\ntrust_level = "trusted"\n'
    )
    workdir = tmp_path / "repo"
    assert ensure_codex_trusted(str(workdir)) is True
    text = config_path.read_text()
    assert "# hand-written comment" in text
    data = tomllib.loads(text)
    assert data["model"] == "gpt-5.6-sol"
    assert data["projects"]["/somewhere/else"]["trust_level"] == "trusted"
    assert _trust_level(config_path, workdir) == "trusted"


def test_already_trusted_is_noop(config_path, tmp_path):
    workdir = tmp_path / "repo"
    config_path.write_text(
        f'[projects."{workdir}"]\ntrust_level = "trusted"\n'
    )
    before = config_path.read_text()
    assert ensure_codex_trusted(str(workdir)) is True
    assert config_path.read_text() == before


def test_never_overrides_explicit_non_trusted_entry(config_path, tmp_path):
    workdir = tmp_path / "repo"
    config_path.write_text(
        f'[projects."{workdir}"]\ntrust_level = "denied"\n'
    )
    before = config_path.read_text()
    assert ensure_codex_trusted(str(workdir)) is False
    assert config_path.read_text() == before


def test_refuses_corrupt_config(config_path, tmp_path):
    config_path.write_text("[projects.\nnot toml")
    before = config_path.read_text()
    assert ensure_codex_trusted(str(tmp_path / "repo")) is False
    assert config_path.read_text() == before
