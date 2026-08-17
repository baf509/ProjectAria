"""
ARIA - Runtime Update Check

Purpose: tell Ben when an inference runtime this box depends on has moved
upstream, WITHOUT touching anything. It reports; it never pulls, builds, or
restarts. Upgrading a runtime here has repeatedly been a load-bearing decision
(quant support, kernel fixes, ABI breaks), so the human stays in the loop.

WHY THIS LIVES IN ARIA. Per ~/Development/CLAUDE.md: "Any loop that runs while
Ben is not talking lives in ARIA code." The alert-triage and paused-shell loops
were once Hermes cron prompts executed by a small local model; when that model
went away on 2026-08-10 the jobs errored, the gateway paused them, and Ben heard
nothing for five days with 31 alerts queued. A recurring update check is exactly
that shape of loop, so it belongs here and is driven by ARIA's scheduler.

WHY IT MATTERS ON THIS BOX SPECIFICALLY. Every one of these runtimes is pinned
to something narrow and load-bearing:
  - DwarfStar is the selected DS4 stack and is a young project moving fast.
  - Nathan's llama.cpp fork implements DS4 kernels mainline Vulkan disables; it
    is the ONLY reason DS4 ran here at all before DwarfStar.
  - vllm-radiance 0.5.8 is pinned, and a known-missing upstream commit (the
    Qwen3.8 chat template) is the reason the checkpoint's own template is used.
  - mainline llama.cpp is checked out on an UNMERGED PR branch for bailingmoe3
    (Ling-3.0-flash); if that PR merges, the branch should be retired.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Optional

import httpx

from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType

# --- What we track ----------------------------------------------------------
# Declarative on purpose: adding a runtime should be one entry here, not new code.
#
# kind:
#   "github_branch"  — compare a local git checkout's HEAD against an upstream branch
#   "github_release" — compare a recorded version against the latest GitHub release
#   "dockerhub"      — compare a pinned image tag against the newest tags published
#   "manual"         — nothing to query; surfaced so it is not silently forgotten
TRACKED: list[dict[str, Any]] = [
    {
        "name": "DwarfStar (antirez/ds4)",
        "kind": "github_branch",
        "repo": "antirez/ds4",
        "branch": "main",
        "local_path": "/home/ben/Development/dwarfstar",
        "why": "The SELECTED DS4 stack (:8112). Young, fast-moving, and its own "
               "STRIXHALO.md/gfx1151 backend is newer than the rest of the project — "
               "fixes there land for us first. Rebuild with `make strix-halo -j4`.",
        "upgrade_risk": "Rebuild is ~1 min and self-contained, but the GGUF it accepts "
                        "is narrow: it already rejects other DS4 quants outright. Check "
                        "release notes for quant-format changes before rebuilding.",
    },
    {
        "name": "llama.cpp mainline (bailingmoe3 PR branch)",
        "kind": "github_branch",
        "repo": "ggml-org/llama.cpp",
        "branch": "master",
        "local_path": "/home/ben/Development/llamacpp-bakeoff",
        "why": "Checked out on the UNMERGED bailingmoe3-support branch (PR #26608) so "
               "Ling-3.0-flash loads at all. Watch for that PR merging into master — "
               "at which point this should move back to a plain mainline checkout.",
        "upgrade_risk": "Rebuilding drops the PR branch unless it is re-merged. Do not "
                        "fast-forward this blindly; Ling stops loading if bailingmoe3 "
                        "support is lost.",
    },
    {
        "name": "vllm-radiance (Qwen3.8 on the R9700)",
        "kind": "dockerhub",
        "image": "stilldeadcode/vllm-radiance",
        "current": "0.5.8",
        "why": "Serves :8080, Hermes's default model. Pinned at 0.5.8 (built 2026-07-31). "
               "The upstream commit '[ADD] add qwen3.8 chat template' is NOT in this tag, "
               "which is why the checkpoint's own chat_template.jinja is what vLLM picks "
               "up — a newer tag may finally carry the curated template.",
        "upgrade_risk": "Ships its own ROCm 7.14.0 internally (host is 7.2.4). A tag bump "
                        "changes the whole stack under the model. MTP is verified "
                        "distribution-preserving on THIS build — re-verify after any bump.",
    },
    {
        "name": "Ember (otheru-ai/ember)",
        "kind": "github_branch",
        "repo": "otheru-ai/ember",
        "branch": "main",
        "local_path": "/home/ben/Development/ember",
        "why": "Not deployed — it lost the 2026-08-17 bakeoff on weights provenance, not "
               "on speed (it is the FASTEST stack measured here: 21.9 tok/s vs "
               "DwarfStar's 15.0). Worth watching in case it ships non-abliterated "
               "weights or its speed advantage grows.",
        "upgrade_risk": "None while it is not deployed.",
    },
    {
        "name": "Nathan's llama.cpp Strix Halo Vulkan fork",
        "kind": "manual",
        "current": "build 10350 (3be50ccc2)",
        "why": "Binary drop at infrastructure/ds4-halo-xxs/runtime/nathan-v0.6.1/vulkan — "
               "there is NO source tree or tracked remote on this box, so it CANNOT be "
               "checked automatically. Still serves :8108 (DS4 IQ3_XXS) and is the only "
               "runtime here that implements the DS4 Vulkan kernels mainline disables.",
        "upgrade_risk": "Unknown provenance for updates. Surfaced here so it is not "
                        "silently forgotten, not because ARIA can check it.",
    },
]

_GITHUB_API = "https://api.github.com"
_UA = {"User-Agent": "ARIA/0.2 (+runtime_update_check)", "Accept": "application/vnd.github+json"}


async def _local_head(path: str) -> Optional[str]:
    """Current commit of a local checkout, or None if it is not a git repo."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "rev-parse", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        sha = out.decode().strip()
        return sha or None
    except Exception:
        return None


async def _local_branch(path: str) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return out.decode().strip() or None
    except Exception:
        return None


async def _check_github_branch(client: httpx.AsyncClient, entry: dict) -> dict:
    repo, branch, path = entry["repo"], entry.get("branch", "main"), entry.get("local_path")
    local = await _local_head(path) if path else None
    local_branch = await _local_branch(path) if path else None
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = dict(_UA)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await client.get(f"{_GITHUB_API}/repos/{repo}/commits/{branch}", headers=headers)
    if r.status_code != 200:
        return {"status": "check_failed",
                "detail": f"GitHub returned {r.status_code} for {repo}@{branch}"}
    data = r.json()
    remote = data.get("sha") or ""
    commit = (data.get("commit") or {})
    behind: Optional[int] = None
    if local and remote and local != remote:
        cmp_r = await client.get(f"{_GITHUB_API}/repos/{repo}/compare/{local}...{remote}",
                                 headers=headers)
        if cmp_r.status_code == 200:
            behind = cmp_r.json().get("ahead_by")
    return {
        "status": "up_to_date" if (local and remote and local == remote)
                  else ("update_available" if remote else "unknown"),
        "local": (local or "")[:12] or None,
        "local_branch": local_branch,
        "remote": remote[:12],
        "commits_behind": behind,
        "remote_message": (commit.get("message") or "").splitlines()[0][:120],
        "remote_date": (commit.get("committer") or {}).get("date"),
    }


async def _check_dockerhub(client: httpx.AsyncClient, entry: dict) -> dict:
    image, current = entry["image"], str(entry.get("current", ""))
    url = f"https://hub.docker.com/v2/repositories/{image}/tags?page_size=25&ordering=last_updated"
    r = await client.get(url, headers={"User-Agent": _UA["User-Agent"]})
    if r.status_code != 200:
        return {"status": "check_failed", "detail": f"Docker Hub returned {r.status_code}"}
    tags = [t.get("name", "") for t in (r.json().get("results") or [])]
    # Compare only version-looking tags, so "latest"/"dev" never masquerade as newer.
    def _ver(t: str):
        m = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$", t)
        return tuple(int(x or 0) for x in m.groups()) if m else None
    cur_v = _ver(current)
    newer = sorted({t for t in tags if _ver(t) and cur_v and _ver(t) > cur_v},
                   key=lambda t: _ver(t), reverse=True)
    return {
        "status": "update_available" if newer else "up_to_date",
        "local": current,
        "remote": newer[0] if newer else current,
        "newer_tags": newer[:5],
        "recent_tags": tags[:8],
    }


async def _check_one(client: httpx.AsyncClient, entry: dict) -> dict:
    base = {"name": entry["name"], "kind": entry["kind"],
            "why": entry["why"], "upgrade_risk": entry.get("upgrade_risk")}
    try:
        if entry["kind"] == "github_branch":
            base.update(await _check_github_branch(client, entry))
        elif entry["kind"] == "dockerhub":
            base.update(await _check_dockerhub(client, entry))
        elif entry["kind"] == "manual":
            base.update({"status": "manual_only", "local": entry.get("current"),
                         "detail": "No tracked remote — cannot be checked automatically."})
        else:
            base.update({"status": "check_failed", "detail": f"unknown kind {entry['kind']}"})
    except Exception as exc:  # never let one bad endpoint sink the whole report
        base.update({"status": "check_failed", "detail": f"{type(exc).__name__}: {exc}"[:200]})
    return base


class RuntimeUpdateCheckTool(BaseTool):
    @property
    def name(self) -> str:
        return "check_runtime_updates"

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def description(self) -> str:
        return (
            "Check whether the inference runtimes this box depends on (DwarfStar, "
            "llama.cpp, vllm-radiance, Ember) have newer upstream versions. "
            "REPORTS ONLY — never pulls, builds, or restarts anything."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="only_updates", type="boolean", required=False, default=False,
                          description="Return only runtimes that have an update available "
                                      "or failed to check."),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        only_updates = bool(arguments.get("only_updates", False))
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                results = await asyncio.gather(*[_check_one(client, e) for e in TRACKED])
        except Exception as exc:
            return ToolResult(tool_name=self.name, status=ToolStatus.ERROR,
                              error=f"{type(exc).__name__}: {exc}")

        updates = [r for r in results if r.get("status") == "update_available"]
        failed = [r for r in results if r.get("status") == "check_failed"]
        shown = (updates + failed) if only_updates else list(results)

        lines = []
        for r in shown:
            mark = {"update_available": "UPDATE", "up_to_date": "current",
                    "manual_only": "manual", "check_failed": "CHECK FAILED"}.get(r.get("status"), "?")
            detail = ""
            if r.get("status") == "update_available":
                if r.get("commits_behind") is not None:
                    detail = f" — {r['commits_behind']} commits behind ({r.get('remote')})"
                elif r.get("newer_tags"):
                    detail = f" — newer tags: {', '.join(r['newer_tags'])}"
                else:
                    detail = f" — remote {r.get('remote')}"
            elif r.get("status") == "check_failed":
                detail = f" — {r.get('detail')}"
            lines.append(f"[{mark}] {r['name']}{detail}")

        return ToolResult(
            tool_name=self.name,
            status=ToolStatus.SUCCESS,
            output={
                "summary": f"{len(updates)} update(s) available, {len(failed)} check(s) failed, "
                           f"{len(results)} runtime(s) tracked",
                "updates_available": [r["name"] for r in updates],
                "check_failed": [r["name"] for r in failed],
                "report": "\n".join(lines),
                "runtimes": shown,
                "note": "Reports only. Nothing was pulled, built, or restarted. "
                        "Read each entry's upgrade_risk before acting.",
            },
        )
