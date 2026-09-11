"""Validated input contracts; worker reports never contain authoritative state."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(StrictModel):
    attempts: int = Field(10, ge=1, le=100)
    attempts_per_task: int = Field(3, ge=1, le=10)
    turns: int = Field(30, ge=1, le=100)
    attempt_seconds: int = Field(900, ge=1, le=3600)
    run_seconds: int = Field(10800, ge=1, le=86400)
    tokens: int | None = Field(None, ge=1)
    context_chars: int = Field(48000, ge=4000, le=100000)


class WorkerConfig(StrictModel):
    backend: str = Field("llamacpp", min_length=1, max_length=80)
    model: str = Field("aria-resident", min_length=1, max_length=200)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None


def safe_relative(value: str) -> str:
    if (not value or value.startswith(("/", "-")) or "\\" in value
            or any(p in ("", ".", "..") for p in value.rstrip("/").split("/"))
            or any(ord(c) < 32 for c in value)):
        raise ValueError("Expected a repository-relative path without traversal")
    return value


class TaskSpec(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = Field(min_length=1, max_length=4000)
    specification_ref: str = Field(min_length=1, max_length=1000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    dependencies: list[str] = Field(default_factory=list, max_length=100)
    allowed_paths: list[str] = Field(min_length=1, max_length=100)
    out_of_scope: list[str] = Field(min_length=1, max_length=20)
    check_ids: list[str] = Field(min_length=1, max_length=10)
    human_review: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def paths(self):
        for path in self.allowed_paths:
            safe_relative(path)
            if ".git" in path.split("/"):
                raise ValueError("Git metadata is controller-owned")
        if any(len(x) > 4000 for x in self.acceptance_criteria + self.out_of_scope + self.human_review):
            raise ValueError("Task context entries must be at most 4000 characters")
        return self


class Plan(StrictModel):
    version: int = Field(1, ge=1)
    tasks: list[TaskSpec] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def dependencies_valid(self):
        import json
        if len(json.dumps(self.model_dump())) > 128000:
            raise ValueError("Plan exceeds 128000 characters; split it into smaller approved runs")
        tasks = {task.id: task for task in self.tasks}
        if len(tasks) != len(self.tasks):
            raise ValueError("Duplicate task IDs")
        visited, visiting = set(), set()

        def visit(key):
            if key in visiting or key not in tasks:
                raise ValueError("Unknown dependency or dependency cycle")
            if key in visited:
                return
            visiting.add(key)
            for dep in tasks[key].dependencies:
                visit(dep)
            visiting.remove(key)
            visited.add(key)

        for key in tasks:
            visit(key)
        return self


class CreateRun(StrictModel):
    project: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    specification: str = Field(min_length=1, max_length=16000)
    plan: Plan | None = None
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    limits: Limits = Field(default_factory=Limits)


class WorkerReport(StrictModel):
    outcome: Literal["ready", "blocked"]
    changed: str = Field(max_length=2000)
    checks: str = Field(max_length=2000)
    handoff: str = Field(max_length=2000)


ACTIVE = {"planning", "running", "verifying", "final_verifying"}
TERMINAL = {"ready_for_review", "blocked", "failed", "cancelled", "budget_exhausted", "human_review_required"}
