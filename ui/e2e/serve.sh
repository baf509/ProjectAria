#!/usr/bin/env bash
# Serve the production build on :3100 with the same runtime config the
# container uses, for the responsive gate.
set -euo pipefail
cd "$(dirname "$0")/.."

export PORT="${PORT:-3100}"
export ARIA_API_URL="${ARIA_API_URL:-http://127.0.0.1:8200}"
if [ -z "${ARIA_API_KEY:-}" ]; then
  export ARIA_API_KEY="$(grep '^API_KEY=' ../.env | cut -d= -f2)"
fi

exec npx next start -p "$PORT"
