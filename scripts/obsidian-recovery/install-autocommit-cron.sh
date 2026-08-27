#!/usr/bin/env bash
# ARIA - Obsidian autocommit crontab installer (Corsair only)
#
# Phase: Obsidian LiveSync Corsair recovery (Phase 7)
# Purpose: install EXACTLY ONE autocommit cron line, preserving every other entry
#
# Related plan sections:
# - Section 7, Phase 7 step 5-6: one cron line, unrelated entries preserved
#
# The migration regression this repairs was caused by a script that rewrote the
# crontab wholesale and filtered one line out of it. So this installer is
# idempotent and additive: it removes only lines that reference the autocommit
# script itself, appends one canonical line, and refuses to write a crontab
# that lost any unrelated entry.
#
# Usage:
#   ./install-autocommit-cron.sh            # show the proposed crontab diff
#   ./install-autocommit-cron.sh --apply

set -euo pipefail

SCRIPT="${OBS_AUTOCOMMIT:-/home/ben/bin/obsidian-autocommit.sh}"
LOG="${OBS_AUTOCOMMIT_LOG:-/home/ben/.obsidian-autocommit.log}"
LINE="*/10 * * * * ${SCRIPT} >> ${LOG} 2>&1"
APPLY=0
if [[ "${1:-}" == "--apply" ]]; then
  APPLY=1
fi

current="$(crontab -l 2>/dev/null || true)"
# Match on the script path, not on the whole line, so a hand-edited schedule or
# redirection is still recognised as the same entry rather than duplicated.
kept="$(printf '%s\n' "$current" | grep -v -F "$SCRIPT" || true)"
proposed="$(printf '%s\n%s\n' "$kept" "$LINE" | sed '/^$/d')"

before_unrelated="$(printf '%s\n' "$kept" | awk 'NF { count++ } END { print count + 0 }')"
after_unrelated="$(printf '%s\n' "$proposed" | awk -v script="$SCRIPT" 'index($0, script) == 0 && NF { count++ } END { print count + 0 }')"
if [[ "$before_unrelated" != "$after_unrelated" ]]; then
  echo "install-autocommit-cron: refusing to write - unrelated entries would change ($before_unrelated -> $after_unrelated)" >&2
  exit 3
fi

entries="$(printf '%s\n' "$proposed" | grep -c -F "$SCRIPT" || true)"
if [[ "$entries" != "1" ]]; then
  echo "install-autocommit-cron: refusing to write - would install $entries autocommit lines, expected exactly 1" >&2
  exit 3
fi

echo "--- current crontab ---"
printf '%s\n' "$current"
echo "--- proposed crontab ---"
printf '%s\n' "$proposed"

if [[ "$APPLY" != "1" ]]; then
  echo "(dry run; re-run with --apply)" >&2
  exit 0
fi

backup_dir="${OBS_CRON_BACKUP_DIR:-$HOME/.migration-backups}"
backup="${backup_dir}/crontab.before-obsidian-autocommit-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$(dirname "$backup")"
printf '%s\n' "$current" > "$backup"
printf '%s\n' "$proposed" | crontab -
echo "installed; previous crontab saved to $backup" >&2
crontab -l | grep -F "$SCRIPT"
