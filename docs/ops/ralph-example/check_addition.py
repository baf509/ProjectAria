"""Install outside the worker repository as an operator-approved acceptance asset."""
import json
import subprocess
import sys

cases = [(2, 3), (-4, 7), (0, 0), (5, -9)]
program = (
    "import json,sys; sys.path.insert(0, '/workspace'); "
    "from calculator import add; "
    f"print(json.dumps([add(a,b) for a,b in {cases!r}]))"
)
result = subprocess.run(
    [sys.executable, "-I", "-B", "-c", program],
    capture_output=True, text=True, timeout=10,
)
assert result.returncode == 0, result.stderr
assert json.loads(result.stdout) == [a + b for a, b in cases], result.stdout
print("Independent integer-addition acceptance passed")
