"""
ARIA - Node Agent

The remote-machine side of the multi-machine fleet. Reuses the local tmux driver
and ANSI stripper; depends only on httpx + those (no Mongo/db), so it can run on
a MacBook against corsair over the tailnet.

Three concurrent loops:
  - heartbeat  — POST /nodes/{id}/heartbeat every ~10s (online/offline signal)
  - capture    — poll local claude-* panes; push snapshots + new tail lines
  - command    — long-poll /nodes/{id}/commands; run send_input/start_session/stop
                 against local tmux; post the result back
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import platform
import socket
import time
from typing import Any, Optional

import httpx

from aria.shells.ansi import strip_ansi
from aria.shells.tmux import TmuxClient, TmuxError, TmuxSessionNotFoundError

logger = logging.getLogger("aria.node")

AGENT_VERSION = "0.1.0"


class NodeAgent:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        node_id: str,
        *,
        prefix: str = "claude-",
        capture_interval: float = 2.0,
        heartbeat_interval: float = 10.0,
        snapshot_lines: int = 400,
    ):
        self.base = api_url.rstrip("/")
        self.node_id = node_id
        self.prefix = prefix
        self.capture_interval = capture_interval
        self.heartbeat_interval = heartbeat_interval
        self.snapshot_lines = snapshot_lines
        self.tmux = TmuxClient()
        self.http = httpx.AsyncClient(
            base_url=self.base,
            headers={"X-API-Key": api_key} if api_key else {},
            timeout=httpx.Timeout(35.0),
        )
        self._last_tail: dict[str, str] = {}   # name -> last line pushed
        self._snap_hash: dict[str, str] = {}   # name -> last snapshot hash
        self._known: set[str] = set()          # shells we've seen this run
        self._last_post: dict[str, float] = {} # name -> monotonic time of last ingest
        self._keepalive_seconds = 8.0          # re-assert a live idle shell this often

    # ---------------------------------------------------------------- http
    async def _post(self, path: str, json: dict, *, timeout: Optional[float] = None) -> dict:
        r = await self.http.post(path, json=json, timeout=timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _safe_post(self, path: str, json: dict) -> None:
        try:
            await self._post(path, json)
        except Exception as e:  # best-effort ingest — never crash the loop
            logger.debug("post %s failed: %s", path, e)

    # ------------------------------------------------------------ register
    async def register(self) -> None:
        await self._post(
            "/api/v1/nodes/register",
            {
                "node_id": self.node_id,
                "hostname": socket.gethostname(),
                "os": platform.system(),
                "arch": platform.machine(),
                "capabilities": ["shells", "coding"],
                "agent_version": AGENT_VERSION,
            },
        )
        logger.info("registered node %s → %s", self.node_id, self.base)

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                await self._post(f"/api/v1/nodes/{self.node_id}/heartbeat", {})
            except Exception as e:
                logger.warning("heartbeat failed: %s", e)
            await asyncio.sleep(self.heartbeat_interval)

    # ------------------------------------------------------------- capture
    async def capture_loop(self) -> None:
        while True:
            try:
                await self._capture_once()
            except Exception as e:
                logger.error("capture error: %s", e)
            await asyncio.sleep(self.capture_interval)

    async def _capture_once(self) -> None:
        try:
            names = await self.tmux.list_sessions(prefix=self.prefix)
        except Exception as e:
            logger.debug("list_sessions failed: %s", e)
            return
        live = set(names)

        # Shells that vanished → tell the central they stopped.
        for gone in self._known - live:
            await self._safe_post(
                f"/api/v1/nodes/{self.node_id}/events",
                {"shell_name": gone, "events": [], "stopped": True},
            )
            self._known.discard(gone)
            self._last_tail.pop(gone, None)
            self._snap_hash.pop(gone, None)

        for name in names:
            try:
                raw = await self.tmux.capture_pane(name, lines=self.snapshot_lines)
            except TmuxSessionNotFoundError:
                continue
            except TmuxError as e:
                logger.debug("capture_pane %s failed: %s", name, e)
                continue
            clean = strip_ansi(raw).rstrip()
            posted = False

            # Snapshot (what remote current_screen/get_output reads) if changed.
            h = hashlib.sha256(clean.encode("utf-8", "replace")).hexdigest()
            if self._snap_hash.get(name) != h:
                await self._safe_post(
                    f"/api/v1/nodes/{self.node_id}/snapshot",
                    {"shell_name": name, "content": clean},
                )
                self._snap_hash[name] = h
                posted = True

            # Events: newly-appended tail lines (first-cut heuristic — panes are
            # rewriting screens, not logs, so this captures the growing bottom
            # without flooding; full-fidelity scrollback is a later refinement).
            new_lines = self._delta_lines(name, clean)
            first_time = name not in self._known
            if new_lines or first_time:
                await self._safe_post(
                    f"/api/v1/nodes/{self.node_id}/events",
                    {
                        "shell_name": name,
                        "events": [
                            {"kind": "output", "text_raw": ln, "text_clean": ln, "source": "node-capture"}
                            for ln in new_lines
                        ],
                    },
                )
                posted = True

            # Keepalive: re-assert a live-but-idle shell as active even when its
            # pane hasn't changed, so it never gets stuck 'stopped' centrally
            # (an ingest re-registers the shell active). Throttled.
            if not posted and (time.monotonic() - self._last_post.get(name, 0.0)) > self._keepalive_seconds:
                await self._safe_post(
                    f"/api/v1/nodes/{self.node_id}/events",
                    {"shell_name": name, "events": []},
                )
                posted = True

            if posted:
                self._last_post[name] = time.monotonic()
            self._known.add(name)

    def _delta_lines(self, name: str, clean: str) -> list[str]:
        lines = [ln for ln in clean.splitlines() if ln.strip()]
        if not lines:
            return []
        last = self._last_tail.get(name)
        self._last_tail[name] = lines[-1]
        if last is None:
            return lines[-1:]  # first capture: just the current last line
        if last in lines:
            idx = len(lines) - 1 - lines[::-1].index(last)
            return lines[idx + 1:]
        return lines[-1:]  # couldn't locate anchor; push just the current last line

    # ------------------------------------------------------------ commands
    async def command_loop(self) -> None:
        while True:
            try:
                resp = await self.http.get(
                    f"/api/v1/nodes/{self.node_id}/commands", timeout=35.0
                )
                resp.raise_for_status()
                cmds = resp.json()
            except Exception as e:
                logger.debug("command poll failed: %s", e)
                await asyncio.sleep(2)
                continue
            for cmd in cmds:
                asyncio.create_task(self._handle(cmd))

    async def _handle(self, cmd: dict) -> None:
        cid, kind, args = cmd["id"], cmd["kind"], (cmd.get("args") or {})
        try:
            result = await self._exec(kind, args)
            await self._post(
                f"/api/v1/nodes/{self.node_id}/commands/{cid}/result", {"result": result}
            )
        except Exception as e:
            logger.warning("command %s (%s) failed: %s", cid, kind, e)
            try:
                await self._post(
                    f"/api/v1/nodes/{self.node_id}/commands/{cid}/result", {"error": str(e)}
                )
            except Exception:
                pass

    async def _exec(self, kind: str, args: dict) -> dict[str, Any]:
        if kind == "send_input":
            name = args["name"]
            await self.tmux.send_keys(
                name,
                args.get("text", ""),
                append_enter=args.get("append_enter", True),
                literal=args.get("literal", False),
            )
            screen = None
            wait_ms = int(args.get("wait_ms") or 0)
            if wait_ms > 0:
                await asyncio.sleep(wait_ms / 1000)
                try:
                    screen = strip_ansi(
                        await self.tmux.capture_pane(name, lines=self.snapshot_lines)
                    ).rstrip()
                except TmuxSessionNotFoundError:
                    screen = None
            return {"line": 1, "screen": screen}

        if kind == "stop":
            await self.tmux.kill_session(args["name"])
            return {"ok": True}

        if kind == "start_session":
            name = args["shell_name"]
            workdir = args.get("workdir") or None
            # Pre-trust the workspace so Claude Code's blocking folder-trust
            # dialog doesn't hang the detached session (best-effort).
            try:
                from aria.shells.claude_trust import ensure_trusted
                if workdir:
                    ensure_trusted(workdir)
            except Exception as e:
                logger.debug("ensure_trusted(%s) failed: %s", workdir, e)
            # Inject THIS node's PATH into the session so the agent binary is
            # found — a `bash -l` login shell rebuilds PATH via path_helper and
            # drops ~/.local/bin (where `claude` lives), so the launch would
            # otherwise fail with 'command not found' and the session would exit.
            await self.tmux.new_session(
                name,
                workdir=workdir,
                command=args.get("launch") or None,
                cols=int(args.get("cols") or 160),
                rows=int(args.get("rows") or 48),
                env={"PATH": os.environ.get("PATH", "")},
            )
            logger.info("start_session created %s in %s", name, workdir)
            return {"shell_name": name, "ok": True}

        raise ValueError(f"unknown command kind: {kind}")

    # ----------------------------------------------------------------- run
    async def run(self) -> None:
        # Register with retry — never crash the process if the API is momentarily
        # unreachable (e.g. corsair restarting); launchd would just respawn us
        # into the same failure. Heartbeats re-register on the fly too.
        while True:
            try:
                await self.register()
                break
            except Exception as e:
                logger.warning("register failed (%s); retrying in 5s", e)
                await asyncio.sleep(5)
        await asyncio.gather(
            self.heartbeat_loop(), self.capture_loop(), self.command_loop()
        )


def main() -> None:
    p = argparse.ArgumentParser("aria-node", description="Join this machine to the ARIA fleet.")
    p.add_argument("--api-url", default=os.getenv("ARIA_API_URL", "http://localhost:8200"))
    p.add_argument("--api-key", default=os.getenv("ARIA_API_KEY") or os.getenv("API_KEY", ""))
    p.add_argument("--node-id", default=os.getenv("ARIA_NODE_ID") or socket.gethostname())
    p.add_argument(
        "--prefix",
        default=os.getenv("ARIA_NODE_SHELL_PREFIX", "claude-"),
        help="only capture tmux sessions whose name starts with this (default claude-)",
    )
    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s aria-node: %(message)s"
    )
    agent = NodeAgent(args.api_url, args.api_key, args.node_id, prefix=args.prefix)
    logger.info("starting aria-node id=%s api=%s", args.node_id, args.api_url)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
