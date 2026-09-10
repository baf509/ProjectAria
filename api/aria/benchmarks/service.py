"""
ARIA - Benchmark Service

Purpose: drive the `evalstack` benchmark harness (benchmark-tooling/evalstack)
from ARIA, so model comparisons can be launched from the UI instead of a shell.

Design notes:

* evalstack is invoked as a SUBPROCESS (`evalstack bench --json`), not imported.
  It lives in its own repo with its own venv and a large dependency set
  (inspect-ai, lm-eval, evalplus, torch); importing it into the API process would
  drag all of that into ARIA and couple their upgrade cycles.

* Runs are long (minutes to hours) and stop/start model servers, so a run is
  started detached and polled. State lives in a JSON registry beside the results
  so it survives an API restart.

* THE CHILD MUST ESCAPE THIS UNIT'S CGROUP. aria-api.service uses systemd's
  default KillMode=control-group, which SIGTERMs every pid in the unit's cgroup on
  stop/restart. `start_new_session=True` makes a new session but NOT a new cgroup,
  so an `aria-api` restart killed a live 2h23m benchmark mid-run (observed
  2026-08-07: returncode -15 while an affine target was still on batch 0/1). We therefore
  launch through `systemd-run --user --scope`, which places the child in its own
  transient scope cgroup so it survives API restarts.

* MODEL LIFECYCLE IS A SHARED CONCERN. ARIA's ModelServerManager already owns
  start/stop, knows which agent each model is bound to, and gates on live GTT.
  evalstack has its own VRAM guard, and the two can disagree. We therefore refuse
  to launch a run whose targets would disturb a model currently BOUND to an agent
  unless the caller passes force=True — otherwise a benchmark could stop the model
  Hermes is talking to, mid-conversation.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

from aria.core.bg import spawn_bg

DEFAULT_ROOT = Path.home() / "Development/benchmark-tooling/evalstack"


class BenchmarkError(RuntimeError):
    """Benchmark could not be started or inspected."""


class BenchmarkService:
    """Launch + track evalstack runs."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(os.environ.get("EVALSTACK_ROOT", root or DEFAULT_ROOT))
        self.results = self.root / "results"
        self.registry = self.results / "_aria_runs.json"
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ helpers ----

    @property
    def bin(self) -> Optional[str]:
        """The evalstack entry point from its own venv (never the ambient PATH —
        the runners shell out to lm_eval/inspect by bare name and need that venv)."""
        cand = self.root / ".venv/bin/evalstack"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
        return shutil.which("evalstack")

    def available(self) -> bool:
        return self.root.is_dir() and self.bin is not None

    def _require(self) -> str:
        if not self.root.is_dir():
            raise BenchmarkError(f"evalstack repo not found at {self.root} "
                                 f"(set $EVALSTACK_ROOT)")
        b = self.bin
        if not b:
            raise BenchmarkError(f"evalstack executable not found under {self.root}/.venv")
        return b

    def _read_registry(self) -> dict[str, Any]:
        try:
            return json.loads(self.registry.read_text())
        except Exception:
            return {"runs": {}}

    def _write_registry(self, data: dict[str, Any]) -> None:
        self.results.mkdir(parents=True, exist_ok=True)
        tmp = self.registry.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.registry)

    async def _yaml(self, path: Path) -> dict:
        import yaml
        return yaml.safe_load(path.read_text()) or {}

    # ------------------------------------------------------------ catalog ----

    async def list_suites(self) -> list[dict]:
        """Named suites (code, tool-use, performance, …) with their bench ids."""
        cat = await self._yaml(self.root / "suites/catalog.yaml")
        benches = {b["id"]: b for b in (cat.get("benchmarks") or [])}
        proc = await asyncio.create_subprocess_exec(
            str(self.root / ".venv/bin/python"), "-c",
            "import json; from evalstack.cli import RUNNERS; "
            "print(json.dumps({k: bool(v.available()) for k,v in RUNNERS.items()}))",
            cwd=str(self.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            availability = json.loads(stdout) if proc.returncode == 0 else {}
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            availability = {}
        out = []
        for name, ids in (cat.get("suites") or {}).items():
            out.append({
                "name": name,
                "available": all(availability.get(benches.get(i, {}).get("runner"), False) for i in ids),
                "unavailable_runners": sorted({benches.get(i, {}).get("runner", "unknown")
                                               for i in ids if not availability.get(benches.get(i, {}).get("runner"), False)}),
                "benches": [
                    {"id": i,
                     "what": benches.get(i, {}).get("what", ""),
                     "category": benches.get(i, {}).get("category", ""),
                     "flags": benches.get(i, {}).get("flags", []) or []}
                    for i in (ids or [])
                ],
            })
        return out

    async def list_targets(self) -> list[dict]:
        """Benchmarkable model targets, with the VRAM the guard charges them."""
        cfg = await self._yaml(self.root / "configs/targets.yaml")
        out = []
        for name, t in (cfg.get("targets") or {}).items():
            tags = t.get("tags") or []
            out.append({
                "name": name,
                "model": t.get("model"),
                "base_url": t.get("base_url"),
                "vram_gb": t.get("vram_gb") or 0,
                "deployment": t.get("deployment") or "",
                "cloud": "cloud" in tags,
                "tags": tags,
                "manages_lifecycle": bool(t.get("lifecycle")),
            })
        return out

    async def gpu_budget_gb(self) -> float:
        cfg = await self._yaml(self.root / "configs/targets.yaml")
        budget = cfg.get("gpu_budget_gb")
        return float(110 if budget is None else budget)

    # ---------------------------------------------------------------- run ----

    async def start_run(self, suites: list[str], targets: list[str],
                        run_id: Optional[str] = None, limit: Optional[int] = None,
                        allow_coresident: bool = False,
                        keep_up: bool = False, timeout_seconds: int = 300) -> dict:
        """Launch a benchmark run detached. Returns the registry record."""
        binary = self._require()
        if not 10 <= timeout_seconds <= 3600:
            raise BenchmarkError("timeout_seconds must be between 10 and 3600")
        if not suites:
            raise BenchmarkError("no suites selected")
        if not targets:
            raise BenchmarkError("no targets selected")

        suite_catalog = await self.list_suites()
        known_suites = {s["name"] for s in suite_catalog}
        bad = [s for s in suites if s not in known_suites]
        if bad:
            raise BenchmarkError(f"unknown suite(s): {', '.join(bad)}; "
                                 f"available: {', '.join(sorted(known_suites))}")
        unavailable = [s for s in suite_catalog if s["name"] in suites and not s.get("available", True)]
        if unavailable:
            raise BenchmarkError("Suite dependencies unavailable: " + "; ".join(
                f"{s['name']}: {', '.join(s['unavailable_runners'])}" for s in unavailable))
        known_targets = {t["name"] for t in await self.list_targets()}
        bad = [t for t in targets if t not in known_targets]
        if bad:
            raise BenchmarkError(f"unknown target(s): {', '.join(bad)}; "
                                 f"available: {', '.join(sorted(known_targets))}")

        run_id = run_id or f"aria-{int(time.time())}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", run_id):
            raise BenchmarkError("Invalid run_id")
        async with self._lock:
            reg = self._read_registry()
            if run_id in reg["runs"] or (self.results / run_id).exists():
                raise BenchmarkError(f"run '{run_id}' already exists; choose a new ID")
            # Heal stale records first. A run killed by a reboot or an OOM keeps
            # status="running" forever because its reaper died with it, and that
            # then blocks every future run. list_runs() already heals this; the
            # concurrency gate must too, or a crash bricks the whole feature.
            for r in reg["runs"].values():
                self._refresh(r)
                if r.get("status") == "running" and not self._alive(r.get("pid")):
                    r["status"] = "interrupted"
                    r["finished_at"] = r.get("finished_at") or time.time()
            self._write_registry(reg)
            running = [r for r in reg["runs"].values() if r.get("status") == "running"]
            if running:
                raise BenchmarkError(
                    f"another benchmark is already running ({running[0]['run_id']}); "
                    f"benchmarks stop and start model servers, so they must not overlap")

            run_dir = self.results / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            log_path = run_dir / "aria-bench.log"

            argv = [binary, "bench",
                    "--suites", ",".join(suites),
                    "--targets", ",".join(targets),
                    "--run", run_id, "--json"]
            if limit:
                argv += ["--limit", str(limit)]
            if allow_coresident:
                argv += ["--allow-coresident"]
            if keep_up:
                argv += ["--no-down"]

            completion = run_dir / "aria-completion.json"
            argv = [sys.executable, str(Path(__file__).with_name("runner.py")),
                    str(timeout_seconds), str(completion), *argv]

            # Escape aria-api's cgroup (see module docstring) so an API restart
            # cannot kill a running benchmark.
            if shutil.which("systemd-run"):
                argv = ["systemd-run", "--user", "--scope", "--collect",
                        f"--unit=evalstack-{run_id}", "--quiet"] + argv

            logf = open(log_path, "w")
            # PYTHONUNBUFFERED: without it evalstack's own progress lines sit in a
            # block buffer for the whole run (stdout is a file, not a tty), so the
            # log showed only subprocess output and the UI looked stalled.
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(self.root), stdout=logf, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,      # survive an API reload
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            rec = {
                "run_id": run_id, "pid": proc.pid, "status": "running",
                "suites": suites, "targets": targets, "limit": limit,
                "started_at": time.time(), "finished_at": None, "returncode": None,
                "log": str(log_path), "results_dir": str(run_dir),
                "argv": argv,
                "timeout_seconds": timeout_seconds, "completion": str(completion),
            }
            reg["runs"][run_id] = rec
            self._write_registry(reg)

        # spawn_bg: if this reaper is collected the run never leaves `running`
        # and its log file is never closed.
        spawn_bg(self._reap(run_id, proc, logf), name=f"bench:reap:{run_id}")
        return rec

    async def _reap(self, run_id: str, proc, logf) -> None:
        """Wait for the child and record its outcome."""
        try:
            rc = await proc.wait()
        except Exception:
            rc = -1
        finally:
            try:
                logf.close()
            except Exception:
                pass
        async with self._lock:
            reg = self._read_registry()
            rec = reg["runs"].get(run_id)
            if rec:
                rec["status"] = rec["status"] if rec["status"] == "cancelled" else ("succeeded" if rc == 0 else "failed")
                rec["returncode"] = rc
                rec["finished_at"] = time.time()
                self._refresh(rec)
                self._write_registry(reg)

    def _refresh(self, rec: dict) -> None:
        completion = rec.get("completion")
        if completion and Path(completion).is_file():
            try:
                done = json.loads(Path(completion).read_text())
                rec.update({key: done[key] for key in ("status", "returncode", "finished_at")})
            except (OSError, ValueError, KeyError):
                pass
        if rec.get("status") == "succeeded":
            summary = _extract_json_summary(self._tail(Path(rec["log"]), 2000))
            rows = summary.get("results", []) if isinstance(summary, dict) else []
            if rows and any(row.get("status") != "ok" for row in rows):
                rec["status"] = "failed"

    @staticmethod
    def _tail(path: Path, lines: int) -> list[str]:
        if not path.is_file():
            return []
        # Bound I/O as well as the returned text for long-running harnesses.
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 256_000))
            return stream.read().decode(errors="replace").splitlines()[-max(1, lines):]

    def _alive(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    async def list_runs(self, limit: int = 25) -> list[dict]:
        reg = self._read_registry()
        runs = sorted(reg["runs"].values(), key=lambda r: r.get("started_at") or 0,
                      reverse=True)
        for r in runs:                       # heal state after an API restart
            self._refresh(r)
            if r.get("status") == "running" and not self._alive(r.get("pid")):
                r["status"] = "unknown"
        return runs[:limit]

    async def get_run(self, run_id: str, tail: int = 80) -> dict:
        reg = self._read_registry()
        rec = reg["runs"].get(run_id)
        if not rec:
            raise BenchmarkError(f"unknown run '{run_id}'")
        rec = dict(rec)
        self._refresh(rec)
        if rec.get("status") == "running" and not self._alive(rec.get("pid")):
            rec["status"] = "unknown"
        log = Path(rec.get("log", ""))
        if log.is_file():
            lines = self._tail(log, 2000)
            rec["log_tail"] = "\n".join(lines[-tail:])
            rec["summary"] = _extract_json_summary(lines)
        rec["metrics"] = _read_metrics(Path(rec.get("results_dir", "")))
        return rec

    # A run record outlives the run by design (the log tail and results dir are
    # the point). Nothing removed them, though, so the panel accumulated every
    # terminated attempt forever — ten SIGTERMed runs from 2026-08-07/08 were
    # still the entire contents of the Benchmarks screen ten days later, with no
    # way to clear them from any surface. Dismissal is registry-only: the
    # results directory and logs on disk are left alone, because those are the
    # measurement and this is just the index.
    TERMINAL = ("succeeded", "failed", "cancelled", "interrupted", "unknown", "timed_out")

    async def dismiss(self, run_id: str) -> dict:
        """Drop one finished run from the registry. Refuses while it is alive."""
        async with self._lock:
            reg = self._read_registry()
            rec = reg["runs"].get(run_id)
            if not rec:
                raise BenchmarkError(f"unknown run '{run_id}'")
            if rec.get("status") == "running" and self._alive(rec.get("pid")):
                raise BenchmarkError(
                    f"run '{run_id}' is still running — cancel it before dismissing"
                )
            reg["runs"].pop(run_id, None)
            self._write_registry(reg)
        return {"run_id": run_id, "dismissed": True,
                "results_dir": rec.get("results_dir"),
                "detail": "removed from the run list; results and logs on disk are untouched"}

    async def dismiss_finished(self, keep: int = 0) -> dict:
        """Drop every finished run, optionally keeping the `keep` most recent.

        The bulk form exists because the failure mode is bulk: a model server
        goes away mid-sweep and every target in that sweep lands as `failed` at
        once.
        """
        async with self._lock:
            reg = self._read_registry()
            finished = [
                (rid, rec) for rid, rec in reg["runs"].items()
                if not (rec.get("status") == "running" and self._alive(rec.get("pid")))
            ]
            finished.sort(key=lambda kv: kv[1].get("started_at") or 0, reverse=True)
            drop = finished[keep:] if keep > 0 else finished
            for rid, _ in drop:
                reg["runs"].pop(rid, None)
            self._write_registry(reg)
        return {"dismissed": [rid for rid, _ in drop], "count": len(drop), "kept": keep}

    async def cancel(self, run_id: str) -> dict:
        async with self._lock:
            reg = self._read_registry()
            rec = reg["runs"].get(run_id)
            if not rec:
                raise BenchmarkError(f"unknown run '{run_id}'")
            pid = rec.get("pid")
            if rec.get("status") == "running" and self._alive(pid):
                try:
                    # the child runs in its own session; signal the whole group so
                    # lm_eval/inspect subprocesses die with it
                    if rec.get("completion"):
                        os.kill(pid, signal.SIGTERM)  # supervisor forwards and escalates
                    else:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception as ex:
                    raise BenchmarkError(f"could not stop pid {pid}: {ex}")
            rec["status"] = "cancelled"
            rec["finished_at"] = time.time()
            self._write_registry(reg)
        # Killing the sweep does NOT free the GPU: a model it started (possibly
        # still loading) keeps its memory, and the next run then stacks on top of
        # it. That sequence wedged the box on 2026-08-08, so tear down exactly
        # what this run started.
        await self._teardown(run_id)
        return rec

    async def _teardown(self, run_id: str) -> None:
        binary = self.bin
        if not binary:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "teardown", "--run", run_id, cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            for ln in (out or b"").decode(errors="replace").splitlines():
                print(f"[benchmarks:teardown] {ln}")
        except Exception as ex:
            print(f"[benchmarks:teardown] failed for {run_id}: {ex}")


def _extract_json_summary(lines: list[str]) -> Optional[dict]:
    """`evalstack bench --json` prints one JSON object last; recover it."""
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except Exception:
                continue
    return None


def _read_metrics(run_dir: Path) -> list[dict]:
    """Normalized rows evalstack wrote for this run (results.jsonl)."""
    f = run_dir / "results.jsonl"
    if not f.is_file():
        return []
    out = []
    for line in f.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("value") is not None:
                out.append({"target": r.get("target"), "benchmark": r.get("benchmark"),
                            "metric": r.get("metric"), "value": r.get("value"),
                            "n": r.get("n"), "source": r.get("source"),
                            "prompt_tokens": r.get("prompt_tokens"),
                            "prefix_cache_hits": r.get("prefix_cache_hits")})
    return out
