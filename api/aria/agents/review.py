"""
ARIA - Coding Session Review

Purpose: Produce review reports for completed coding sessions.

Two reviewers, deliberately different in kind:

- `review_session()` — the mechanical one that has always been here: numstat +
  whichever of pytest/ruff/npm/eslint the workspace actually has. It is an
  oracle with no model in it, which is why it stays.
- `review_diff()` — a DIFFERENT-MODEL-FAMILY read of the diff (proposal §7.2,
  decision D7). A verifier cascade only reduces error when the verifiers are
  uncorrelated; stacking a second same-family reviewer mostly re-confirms the
  first one's blind spots (arXiv 2607.13918). So a local model's diff is
  reviewed by the cloud tier, and — once there is outcome data to justify it —
  DS4's work by Qwen. `settings.outcome_review_family` picks the reviewer, and a
  reviewer that turns out to share the author's family is REFUSED rather than
  quietly run: a correlated review that reports "looks fine" is worse than no
  review, because the merge gate would then count it as a passed check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.agents.session import CodingSessionManager
from aria.config import settings

logger = logging.getLogger(__name__)

REVIEWS_COLLECTION = "session_reviews"

# Model families for the uncorrelated-reviewer rule. The unit is the family, not
# the model: two Claude models share training data and failure modes, and so do
# DS4 and a DS4 quant.
FAMILY_CLOUD = "cloud"      # Anthropic (claude_code / anthropic adapter)
FAMILY_OPENAI = "openai"    # codex / gpt-*
FAMILY_QWEN = "qwen"
FAMILY_DS4 = "ds4"          # DeepSeek-V4 Flash, every quant
FAMILY_UNKNOWN = "unknown"

# Preference order when the configured reviewer would be correlated with the
# author. Cloud first: it is the only family that is always reachable and is not
# competing for a local slot. DS4 is deliberately NOT a reviewer — it is pi's
# single 131K slot, and a review sent there evicts the coding agent's warm
# prefix (4.2 s warm vs 39.5 s cold). It stays a valid *author* family.
_REVIEWER_PREFERENCE = (FAMILY_CLOUD, FAMILY_QWEN)

# The diff is the review's evidence, and a truncated diff produces a review of
# the part that fit. Cap it, and say in the prompt that it was capped, so the
# reviewer can refuse rather than pretend to have seen the rest.
MAX_DIFF_CHARS = 60_000

_REVIEW_SYSTEM = """You are reviewing a code diff produced by a DIFFERENT AI \
model. You are the independent check on its work: assume nothing it claims is \
true, and judge only what the diff shows.

Reply with JSON only — no prose, no code fences.

Schema:
  {"verdict": "approve"|"concerns"|"reject",
   "confidence": <0.0-1.0>,
   "summary": "<one sentence>",
   "findings": [{"severity": "high"|"medium"|"low",
                 "file": "<path or null>",
                 "detail": "<what is wrong and why it matters>"}]}

Rules:
- "reject" for anything that would break the build, lose data, weaken a safety
  check, or commit a secret. "concerns" for real but non-blocking problems.
  "approve" only if you would merge it as-is.
- Report every issue you find, including low-severity ones; a later step filters
  by severity. Do not filter for importance yourself.
- If the diff is truncated or empty, say so in `summary` and use "concerns" —
  never approve a diff you could not read.
"""

_REVIEW_USER = """Task the diff was meant to accomplish:
<task>
{prompt}
</task>

Diff{truncated_note}:
```diff
{diff}
```

{gate_note}JSON only."""


class CodingReviewService:
    """Generate and persist coding session review reports."""

    def __init__(self, db: AsyncIOMotorDatabase, session_manager: CodingSessionManager):
        self.db = db
        self.session_manager = session_manager

    async def review_session(self, session_id: str) -> dict:
        session = await self.session_manager.get_session(session_id)
        if not session:
            raise ValueError("Coding session not found")

        workspace = session["workspace"]
        if not await self._command_exists("git"):
            raise RuntimeError("git is not installed or not in PATH")
        diff_numstat = await self._run_command("git", "-C", workspace, "diff", "--numstat")
        test_result = await self._detect_and_run(workspace, [
            (["pytest", "-q"], "pytest"),
            (["npm", "test", "--", "--runInBand"], "npm test"),
        ])
        lint_result = await self._detect_and_run(workspace, [
            (["ruff", "check", "."], "ruff"),
            (["eslint", "."], "eslint"),
        ])

        if test_result["success"] and lint_result["success"]:
            status = "success"
        elif test_result["ran"] or lint_result["ran"]:
            status = "partial"
        else:
            status = "unknown"

        report = {
            "session_id": session_id,
            "workspace": workspace,
            "status": status,
            "diff_numstat": diff_numstat["stdout"],
            "tests": test_result,
            "lint": lint_result,
            "created_at": datetime.now(timezone.utc),
        }
        await self.db.session_reports.update_one(
            {"session_id": session_id},
            {"$set": report},
            upsert=True,
        )
        return report

    async def get_report(self, session_id: str) -> dict | None:
        return await self.db.session_reports.find_one({"session_id": session_id})

    # ------------------------------------------------------------------
    # Different-family review (proposal §7.2 / D7)
    # ------------------------------------------------------------------

    async def review_diff(
        self,
        session_id: str,
        *,
        reviewer_family: Optional[str] = None,
        diff: Optional[str] = None,
        gate_passed: Optional[bool] = None,
        max_diff_chars: int = MAX_DIFF_CHARS,
    ) -> dict:
        """Have an uncorrelated model family review this session's diff.

        Returns a verdict dict; `ran=False` with a `reason` when no independent
        reviewer was available. Never raises — a review that cannot run must
        leave the merge gate short of a check, not crash the caller.
        """
        session = await self.session_manager.get_session(session_id)
        if not session:
            raise ValueError("Coding session not found")

        author = model_family(
            session.get("backend"), session.get("model"), session.get("llm")
        )
        wanted = (reviewer_family or settings.outcome_review_family or FAMILY_CLOUD).lower()
        reviewer = pick_reviewer_family(author, wanted)
        if reviewer is None:
            return await self._store_review(session_id, {
                "ran": False,
                "reason": (
                    f"no uncorrelated reviewer for a {author}-family diff "
                    f"(configured reviewer: {wanted})"
                ),
                "author_family": author,
                "reviewer_family": None,
                "independent": False,
            })

        if diff is None:
            diff = await self._session_diff(session)
        diff = diff or ""
        if not diff.strip():
            # A review of an empty diff would be an approval of nothing. The
            # scorer already treats "no diff" as a failed session; saying so
            # here keeps the two from disagreeing.
            return await self._store_review(session_id, {
                "ran": False,
                "reason": "session produced no diff to review",
                "author_family": author,
                "reviewer_family": reviewer,
                "independent": reviewer != author,
            })

        truncated = len(diff) > max_diff_chars
        body = diff[:max_diff_chars]
        gate_note = ""
        if gate_passed is True:
            gate_note = "The project's own check command passed on this diff.\n\n"
        elif gate_passed is False:
            gate_note = "The project's own check command FAILED on this diff.\n\n"

        user = _REVIEW_USER.format(
            prompt=(session.get("prompt") or "(not recorded)")[:2000],
            truncated_note=(
                f" (TRUNCATED — first {max_diff_chars} of {len(diff)} chars)"
                if truncated else ""
            ),
            diff=body,
            gate_note=gate_note,
        )

        try:
            content, usage, model_id = await self._ask_reviewer(reviewer, _REVIEW_SYSTEM, user)
        except Exception as exc:  # noqa: BLE001 — reviewer outage is not a merge signal
            logger.warning("different-family review failed for %s: %s", session_id, exc)
            return await self._store_review(session_id, {
                "ran": False,
                "reason": f"reviewer unavailable: {exc}",
                "author_family": author,
                "reviewer_family": reviewer,
                "independent": reviewer != author,
            })

        parsed = _parse_review(content)
        if parsed is None:
            # ⚠️ Empty or unparseable content is a FAILURE, never a pass. Qwen3.8
            # emits reasoning_content before content, so a tight max_tokens
            # returns finish_reason=length with content="" — writing that as an
            # approving review is exactly how DS4 silently labelled every memory
            # with zero entities.
            return await self._store_review(session_id, {
                "ran": False,
                "reason": (
                    "reviewer returned no usable JSON"
                    + (" (empty content — raise max_tokens)" if not (content or "").strip() else "")
                ),
                "author_family": author,
                "reviewer_family": reviewer,
                "reviewer_model": model_id,
                "independent": reviewer != author,
                "raw": (content or "")[:1000],
            })

        await self._record_usage(session_id, reviewer, model_id, usage)

        parsed.update({
            "ran": True,
            "author_family": author,
            "reviewer_family": reviewer,
            "reviewer_model": model_id,
            "independent": reviewer != author,
            "diff_chars": len(diff),
            "diff_truncated": truncated,
            "gate_passed": gate_passed,
        })
        return await self._store_review(session_id, parsed)

    async def get_diff_review(self, session_id: str) -> dict | None:
        return await self.db[REVIEWS_COLLECTION].find_one({"session_id": session_id})

    async def _store_review(self, session_id: str, doc: dict) -> dict:
        doc = {**doc, "session_id": session_id, "created_at": datetime.now(timezone.utc)}
        try:
            await self.db[REVIEWS_COLLECTION].update_one(
                {"session_id": session_id}, {"$set": doc}, upsert=True
            )
        except Exception as exc:  # noqa: BLE001 — telemetry, not the verdict
            logger.warning("could not persist diff review for %s: %s", session_id, exc)
        return doc

    async def _session_diff(self, session: dict) -> str:
        """The diff to review: the guard's branch range when there is one, else
        the worktree's own uncommitted diff.

        The guard range is preferred because the guard commits checkpoints —
        once it holds the pen, `git diff` alone shows nothing at all, and a
        reviewer handed an empty diff would approve a session that changed a
        hundred files."""
        session_id = str(session.get("_id") or "")
        try:
            from aria.guard.gitguard import get_git_guard

            record = await get_git_guard(self.db).get_session(session_id)
        except Exception:  # noqa: BLE001 — guard is optional
            record = None

        if record and os.path.isdir(record.get("worktree") or ""):
            worktree = record["worktree"]
            base = record.get("start_tag") or record.get("source_branch") or "HEAD"
            merged = await self._run_command(
                "git", "-C", worktree, "diff", base, "HEAD", timeout=60
            )
            uncommitted = await self._run_command(
                "git", "-C", worktree, "diff", timeout=60
            )
            combined = "\n".join(
                part for part in (merged["stdout"], uncommitted["stdout"]) if part.strip()
            )
            if combined.strip():
                return combined

        workspace = session.get("workspace")
        if not workspace or not os.path.isdir(workspace):
            return ""
        result = await self._run_command("git", "-C", workspace, "diff", timeout=60)
        return result["stdout"]

    async def _ask_reviewer(
        self, family: str, system: str, user: str
    ) -> tuple[str, dict, str]:
        """One completion from `family`. Returns (content, usage, model_id)."""
        if family == FAMILY_CLOUD:
            from aria.agents.routing import judge_transport

            model_id = settings.coding_routing_judge_model
            if judge_transport() == "cli":
                from aria.core.claude_runner import ClaudeRunner

                runner = ClaudeRunner(
                    model=model_id,
                    timeout_seconds=max(60, settings.coding_routing_judge_timeout_seconds * 4),
                    allowed_tools=[],  # review only — no filesystem, no shell
                )
                output = await runner.run(f"{system}\n\n{user}")
                if output is None:
                    raise RuntimeError(runner.last_error or "ClaudeRunner returned nothing")
                return output, {}, model_id
            backend, base_url, max_tokens = settings.coding_routing_judge_backend, None, 2048
        elif family == FAMILY_QWEN:
            # Explicit endpoint, never the /llm/v1 "largest resident" auto-route:
            # that resolves to DS4, which is pi's single slot, so a review would
            # evict the coding agent's warm prefix (4.2s warm vs 39.5s cold).
            # ⚠️ Qwen3.8 is a reasoning model — it emits reasoning_content before
            # content, so a tight budget returns finish_reason=length with an
            # EMPTY string. steward_max_tokens is the generous budget; the empty
            # reply is still treated as a failure by the caller.
            backend = settings.steward_backend
            model_id = settings.steward_model
            base_url = settings.steward_endpoint
            max_tokens = settings.steward_max_tokens
        elif family == FAMILY_DS4:
            raise RuntimeError(
                "DS4 is pi's single coding slot — reviewing there would evict "
                "the agent's warm prefix. Configure a second DS4 deployment "
                "before naming it as a reviewer family."
            )
        else:
            raise RuntimeError(f"no transport for reviewer family {family!r}")

        from aria.llm.base import Message
        from aria.llm.manager import llm_manager

        adapter = llm_manager.get_adapter(backend, model_id, base_url=base_url)
        content, _tool_calls, usage = await asyncio.wait_for(
            adapter.complete(
                messages=[
                    Message(role="system", content=system),
                    Message(role="user", content=user),
                ],
                temperature=0.0,
                max_tokens=max_tokens,
            ),
            timeout=max(120, settings.coding_routing_judge_timeout_seconds * 6),
        )
        return content or "", _usage_dict(usage), model_id

    async def _record_usage(
        self, session_id: str, family: str, model_id: str, usage: dict
    ) -> None:
        """Reviews cost money on the cloud tier, and the weekly report divides
        dollars by merged changes — an unrecorded review makes every merge look
        cheaper than it was."""
        if not usage:
            return
        try:
            from aria.db.usage import UsageRepo

            await UsageRepo(self.db).record(
                model=model_id,
                source="coding:review",
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                session_id=session_id,
                backend=(settings.coding_routing_judge_backend
                         if family == FAMILY_CLOUD else settings.steward_backend),
                metadata={"reviewer_family": family},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("could not record review usage for %s: %s", session_id, exc)

    async def _detect_and_run(self, workspace: str, candidates: list[tuple[list[str], str]]) -> dict:
        for command, label in candidates:
            binary = command[0]
            if not await self._command_exists(binary):
                continue
            result = await self._run_command(*command, cwd=workspace)
            return {
                "ran": True,
                "command": label,
                "success": result["returncode"] == 0,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            }
        return {"ran": False, "success": False, "stdout": "", "stderr": ""}

    async def _command_exists(self, binary: str) -> bool:
        import shlex
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            f"command -v {shlex.quote(binary)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        return process.returncode == 0

    async def _run_command(self, *command: str, cwd: str | None = None, timeout: float = 120) -> dict:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
            }
        return {
            "returncode": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }


# --------------------------------------------------------------------------
# Family resolution + parsing (module level so the merge gate and the outcome
# scorer can reason about correlation without instantiating the service).
# --------------------------------------------------------------------------

def model_family(
    backend: Optional[str] = None,
    model: Optional[str] = None,
    llm: Optional[str] = None,
) -> str:
    """Which model family produced this work.

    Reads backend, model id and pi provider together because none of the three
    is sufficient alone: `backend="pi-code"` says nothing about which weights
    ran, and `llm="ridge"` names a machine, not a family.
    """
    b = (backend or "").strip().lower()
    m = (model or "").strip().lower()
    p = (llm or "").strip().lower()

    if b in ("claude_code", "claude-code", "anthropic") or m.startswith("claude-"):
        return FAMILY_CLOUD
    if b == "codex" or m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3"):
        return FAMILY_OPENAI
    for text in (m, p):
        if "qwen" in text:
            return FAMILY_QWEN
        if "ds4" in text or "deepseek" in text:
            return FAMILY_DS4
    return FAMILY_UNKNOWN


def pick_reviewer_family(author: str, wanted: str) -> Optional[str]:
    """The family that may review `author`'s diff, or None.

    None is a real answer: on a box where the only reachable reviewer is the
    author's own family, the honest output is "no independent review", not a
    review that shares the author's blind spots. An unknown author family is
    treated as correlated with nothing, so it can be reviewed by anyone.
    """
    author = (author or FAMILY_UNKNOWN).lower()
    wanted = (wanted or FAMILY_CLOUD).lower()
    if wanted and wanted != author:
        return wanted
    for candidate in _REVIEWER_PREFERENCE:
        if candidate != author:
            return candidate
    return None


def _usage_dict(usage) -> dict:
    """Normalise the adapter's usage into {input_tokens, output_tokens}.

    `complete()` returns whatever the provider SDK handed back — a dict for some
    adapters, a CompletionUsage object for others — so both shapes have to be
    read, or cloud review spend silently books as zero.
    """
    if not usage:
        return {}
    if isinstance(usage, dict):
        return {
            "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens") or 0,
        }
    return {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0) or 0,
    }


def _parse_review(raw: str) -> Optional[dict]:
    """Pull the verdict out of the reviewer's reply, or None.

    None means "no usable review" — including the empty-content case, which is
    what a reasoning model returns when it spends its whole budget thinking.
    Tolerates a ```json fence or surrounding prose, because models add them
    despite the instruction and a fenced-but-valid verdict is real evidence.
    """
    text = (raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("approve", "concerns", "reject"):
        return None

    findings = []
    for item in (data.get("findings") or []):
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "medium").strip().lower()
        findings.append({
            "severity": severity if severity in ("high", "medium", "low") else "medium",
            "file": item.get("file") if isinstance(item.get("file"), str) else None,
            "detail": str(item.get("detail") or "")[:1000],
        })

    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    return {
        "verdict": verdict,
        "confidence": max(0.0, min(1.0, confidence)),
        "summary": str(data.get("summary") or "")[:500],
        "findings": findings,
        "blocking": verdict == "reject" or any(f["severity"] == "high" for f in findings),
    }
