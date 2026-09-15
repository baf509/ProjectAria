"""Operator-owned scope. Candidate processes never receive this file or its paths."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aria.loop.config import Check, assets_digest
from aria.loop.models import StrictModel, safe_relative


class WeeklySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEEKLY_IMPROVEMENT_", extra="ignore", env_file=(".env", "../.env"))
    enabled: bool = False
    policy_file: str = "~/.aria/weekly-improvement-policy.json"
    state_dir: str = "~/.aria/weekly-improvement"


class Command(StrictModel):
    # Controller-installed executable, never a script from the candidate.
    executable: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timeout_seconds: int = Field(300, ge=1, le=1800)
    configuration: str | None = None
    configuration_sha256: str | None = None

    def validate_executable(self) -> Path:
        path = Path(self.executable).expanduser().resolve(strict=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.sha256:
            raise ValueError("Deployment adapter hash changed")
        if self.configuration:
            data = Path(self.configuration).expanduser().resolve(strict=True).read_bytes()
            if hashlib.sha256(data).hexdigest() != self.configuration_sha256:
                raise ValueError("Deployment configuration hash changed")
        return path


class Deployment(StrictModel):
    # A fixed adapter protocol: preflight/apply/health/rollback request.json.
    command: Command
    watch_hours: int = Field(72, ge=1, le=168)
    min_samples: int = Field(10, ge=1)


class Target(StrictModel):
    repository: str
    branch: str = "main"
    allowed_paths: list[str] = Field(min_length=1)
    protected_paths: list[str] = Field(default_factory=list)
    image: str = Field(pattern=r"^(sha256:[a-f0-9]{64}|[^\s]+@sha256:[a-f0-9]{64})$")
    assets: str
    checks: dict[str, Check] = Field(default_factory=dict)
    regression_check_ids: list[str] = Field(default_factory=list)
    acceptance_check_ids: list[str] = Field(default_factory=list)
    check_descriptions: dict[str, str] = Field(default_factory=dict)
    deployment: Deployment | None = None
    memory_mb: int = Field(2048, ge=128, le=32768)
    cpus: int = Field(2, ge=1, le=16)

    @model_validator(mode="after")
    def scope(self):
        if not self.branch or self.branch.startswith("-") or any(c.isspace() for c in self.branch):
            raise ValueError("Invalid destination branch")
        for path in self.allowed_paths + self.protected_paths:
            safe_relative(path)
            if any(x.lower() == ".git" for x in path.split("/")):
                raise ValueError("Git metadata is not a change target")
        for name in self.regression_check_ids + self.acceptance_check_ids:
            if name not in self.checks:
                raise ValueError("Unknown trusted check: " + name)
        return self


class Policy(StrictModel):
    id: str = "platform"
    model: str = "gpt-6-astra"
    reasoning_effort: str = "high"
    codex_version: Literal["0.154.0"] = "0.154.0"
    docker_binary: str = "docker"
    run_seconds: int = Field(7200, ge=600, le=7200)
    max_changes: int = Field(3, ge=1, le=3)
    max_turns: int = Field(60, ge=2, le=100)
    context_chars: int = Field(160000, ge=4000, le=1000000)
    evidence_bytes: int = Field(10 * 1024 * 1024, ge=1024, le=10 * 1024 * 1024)
    records_per_source: int = Field(2000, ge=1, le=10000)
    hermes_database: str
    log_files: list[str] = Field(default_factory=list, max_length=20)
    targets: dict[str, Target] = Field(min_length=1, max_length=10)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def load_policy(config: WeeklySettings) -> tuple[Policy, str]:
    path = Path(config.policy_file).expanduser().resolve(strict=True)
    policy = Policy.model_validate_json(path.read_text())
    state = Path(config.state_dir).expanduser().resolve()
    fingerprints = {}
    repositories = [Path(t.repository).expanduser().resolve(strict=True) for t in policy.targets.values()]
    for key, target in policy.targets.items():
        if not key.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid target ID")
        repo = Path(target.repository).expanduser().resolve(strict=True)
        assets = Path(target.assets).expanduser().resolve(strict=True)
        private_paths = [path, state, assets]
        if target.deployment:
            private_paths.append(Path(target.deployment.command.executable).expanduser().resolve())
            if target.deployment.command.configuration:
                private_paths.append(Path(target.deployment.command.configuration).expanduser().resolve())
        for private in private_paths:
            for candidate_repo in repositories:
                if private.is_relative_to(candidate_repo) or candidate_repo.is_relative_to(private):
                    raise ValueError("Policy, state, adapters and checks must be outside all target repositories")
        if target.deployment:
            adapter = target.deployment.command.validate_executable()
            if adapter.is_relative_to(repo):
                raise ValueError("Deployment adapter must be outside the candidate repository")
        target.repository, target.assets = str(repo), str(assets)
        fingerprints[key] = assets_digest(assets)
    return policy, digest({"policy": policy.model_dump(), "assets": fingerprints})


class Finding(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    target: str = Field(max_length=100)
    problem: str = Field(min_length=1, max_length=3000)
    expected_behavior: str = Field(max_length=2000)
    proposed_fix: str = Field(max_length=3000)
    confidence: Literal["high", "medium", "low"]
    rationale: str = Field(min_length=1, max_length=2000)
    contrary_evidence: str = Field(max_length=2000)
    evidence_ids: list[str] = Field(max_length=30)
    check_id: str | None
    next_step: str = Field(max_length=2000)

    def fingerprint(self):
        return digest({"target": self.target, "title": self.title.lower().strip(),
                       "expected_behavior": self.expected_behavior.lower().strip()})


class Step(StrictModel):
    action: Literal["command", "finish"]
    target: str
    command: str = Field(max_length=12000)
    findings: list[Finding] = Field(max_length=30)
    summary: str = Field(max_length=4000)

    @model_validator(mode="after")
    def exclusive(self):
        if self.action == "command" and (not self.command.strip() or self.findings):
            raise ValueError("Command step requires only a command")
        if self.action == "finish" and self.command:
            raise ValueError("Finish cannot execute a command")
        return self
