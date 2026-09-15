#!/usr/bin/env bash
# Activate the verified repair release; keep the password in Ben's Terminal.
set -euo pipefail
repair_root=/Users/ben/Development/Infrastructure/ProjectAria-tool-repairs
repair_dir="$repair_root/docs/ops/tool-repairs-20260915"
repair_python=/Users/ben/Services/apps/ProjectAria/api/.venv/bin/python
repair_release=/Users/ben/Services/releases/ProjectAria/20260915T091340Z-029986d-1a274be96a53
repair_deploy="$repair_release/scripts/aria-deploy-mac"
"$repair_python" "$repair_deploy" verify "$repair_release"
sudo -v
"$repair_python" "$repair_deploy" activate "$repair_release" | tee "$repair_dir/api-activation.json"
/Users/ben/Services/apps/hermes-agent/venv/bin/python "$repair_release/scripts/aria-tools-check" \
  --vision --output "$repair_dir/functional-after-activation.json"
"$repair_python" "$repair_dir/complete-activation.py"
