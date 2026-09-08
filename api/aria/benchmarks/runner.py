"""Detached benchmark supervisor: enforce a deadline and persist exit evidence.

Runs independently of the API so deadlines and completion survive API restarts.
Only BenchmarkService constructs its argv; no shell interpretation occurs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def run(argv: list[str], timeout: float, completion: Path) -> int:
    child = subprocess.Popen(argv, start_new_session=True)
    cancelled = False

    def stop(*_):
        nonlocal cancelled
        cancelled = True

    def signal_group(signum):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin can return EPERM while a group leader is exiting. Avoid
            # racing a second group signal; signal the owned child if still live.
            if child.poll() is None:
                try:
                    child.send_signal(signum)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.monotonic() + timeout
    timed_out = False
    while child.poll() is None:
        if cancelled or time.monotonic() >= deadline:
            timed_out = not cancelled
            signal_group(signal.SIGTERM)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                signal_group(signal.SIGKILL)
                child.wait()
            # The direct child can exit before a stubborn descendant. This run
            # owns the process group; clean up descendants after termination.
            signal_group(signal.SIGKILL)
            break
        time.sleep(0.1)
    rc = 124 if timed_out else child.returncode
    status = "timed_out" if timed_out else ("cancelled" if cancelled else ("succeeded" if rc == 0 else "failed"))
    result = {"returncode": rc, "status": status, "finished_at": time.time()}
    temporary = completion.with_suffix(".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(completion)
    return rc


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[3:], float(sys.argv[1]), Path(sys.argv[2])))
