#!/usr/bin/env bash
# Activate only the manifest-pinned, tested release prepared for this session.
set -euo pipefail
session_python=/Users/ben/Services/apps/hermes-agent/venv/bin/python
session_script=/Users/ben/Services/backups/aria-session-deploy-20260915/activate.py
"$session_python" - "$session_script" <<'PYVERIFY'
import hashlib, pathlib, sys
assert hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() == 'efe286d416406f33416fcc11d8d8d829c103f11d2fee663e0d77ae72e51289a8', 'Activation script changed since review'
PYVERIFY
"$session_python" "$session_script" --check-only
sudo -v
exec "$session_python" "$session_script"
