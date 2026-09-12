"""Operator-owned policy, deliberately outside every worker mount."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aria.ralph.models import StrictModel, safe_relative


class RalphSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RALPH_", extra="ignore")
    enabled: bool = False
    policy_file: str = "~/.aria/ralph-policy.json"
    state_dir: str = "~/.aria/ralph"
    docker_binary: str = "docker"


class Check(StrictModel):
    argv: list[str] = Field(min_length=1, max_length=50)
    timeout_seconds: int = Field(300, ge=1, le=1800)
    version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def bounded_command(self):
        if sum(len(a) for a in self.argv) > 2048 or any("\x00" in a for a in self.argv):
            raise ValueError("Trusted command exceeds 2048 characters or contains NUL")
        return self


class ProjectPolicy(StrictModel):
    repository: str
    image: str = Field(pattern=r"^(sha256:[a-f0-9]{64}|[^\s]+@sha256:[a-f0-9]{64})$")
    assets: str
    checks: dict[str, Check] = Field(min_length=1)
    regression_check_ids: list[str] = Field(min_length=1, max_length=10)
    final_check_ids: list[str] = Field(min_length=1, max_length=20)
    protected_paths: list[str] = Field(default_factory=lambda: ["AGENTS.md", "CLAUDE.md", ".github/"])
    allowed_backends: list[str] = Field(default_factory=lambda: ["llamacpp"])
    memory_mb: int = Field(2048, ge=128, le=32768)
    cpus: int = Field(2, ge=1, le=16)

    @model_validator(mode="after")
    def valid(self):
        for check in self.regression_check_ids + self.final_check_ids:
            if check not in self.checks:
                raise ValueError(f"Unknown trusted check: {check}")
        for path in self.protected_paths:
            safe_relative(path)
        return self


class Policy(StrictModel):
    projects: dict[str, ProjectPolicy]


def assets_digest(root: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("Trusted assets must contain regular files and directories only")
        if path.is_file():
            total += path.stat().st_size
            if total > 32 * 1024 * 1024:
                raise ValueError("Trusted assets exceed 32 MiB")
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def load_project(settings: RalphSettings, slug: str) -> tuple[ProjectPolicy, str]:
    config_path = Path(settings.policy_file).expanduser().resolve(strict=True)
    policy = Policy.model_validate_json(config_path.read_text())
    if slug not in policy.projects:
        raise ValueError("Project is not enabled in the operator's Ralph policy")
    project = policy.projects[slug]
    repo = Path(project.repository).expanduser().resolve(strict=True)
    assets = Path(project.assets).expanduser().resolve(strict=True)
    state = Path(settings.state_dir).expanduser().resolve()
    if not repo.is_dir() or not assets.is_dir():
        raise ValueError("Repository and trusted assets must be directories")
    if any(p.is_relative_to(repo) for p in (assets, state, config_path)) or repo.is_relative_to(state):
        raise ValueError("Policy, state, and trusted assets must be outside the repository")
    project.repository, project.assets = str(repo), str(assets)
    data = project.model_dump()
    data["assets_digest"] = assets_digest(assets)
    return project, hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
