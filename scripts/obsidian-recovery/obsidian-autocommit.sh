#!/usr/bin/env bash
# ARIA - Obsidian vault Git autocommit (Corsair only)
#
# Phase: Obsidian LiveSync Corsair recovery (Phase 7)
# Purpose: commit the already-converged Corsair vault to an EXTERNAL Git dir
#
# Related plan sections:
# - Section 3.5: Git is a single-writer backup, not a sync transport
# - Section 7, Phase 7: Restore the Corsair Git history writer
#
# Deployed to /home/ben/bin/obsidian-autocommit.sh and run every ten minutes by
# cron. Two properties matter more than anything else here:
#
#   1. The Git directory lives OUTSIDE the vault and is addressed with explicit
#      --git-dir/--work-tree. There must be no .git pointer inside the vault,
#      because a `gitdir:` file naming a machine-specific path would replicate
#      to every other device through CouchDB.
#   2. This is a BACKUP writer, not a sync engine. It only ever records what
#      the bridge has already converged; it never pulls, merges or resets, so
#      it cannot become a second, competing source of truth.
#
# Usage:
#   obsidian-autocommit.sh              # commit and push
#   obsidian-autocommit.sh --inspect    # print the staged diff, change nothing
#   obsidian-autocommit.sh --message M  # override the commit message

set -uo pipefail

GIT_DIR_PATH="${OBS_GIT_DIR:-/home/ben/.local/share/obsidian-vault-git}"
WORK_TREE="${OBS_VAULT:-/home/ben/Obsidian/vault}"
STATE_DIR="${OBS_STATE_DIR:-/home/ben/.local/state/obsidian-autocommit}"
STATUS_FILE="$STATE_DIR/last-status"
BRANCH="${OBS_BRANCH:-main}"

INSPECT=0
MESSAGE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --inspect) INSPECT=1; shift ;;
    --message) MESSAGE="${2:-}"; shift 2 ;;
    *) echo "obsidian-autocommit: unknown argument: $1" >&2; exit 2 ;;
  esac
done

# ISO timestamps the portable way. The GNU-only short form is silently
# empty on BSD date, and this script is also run by hand from the Mac
# during Phase 7 verification - an empty timestamp in the status file
# would defeat the staleness alert that watches it (plan Section 8.2).
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

log() { printf '[%s] %s\n' "$(now)" "$*"; }

# Every git invocation goes through this, so no code path can accidentally
# rely on an in-vault .git directory.
g() { git --git-dir="$GIT_DIR_PATH" --work-tree="$WORK_TREE" "$@"; }

write_status() {
  mkdir -p "$STATE_DIR"
  printf 'timestamp=%s\nresult=%s\nhead=%s\nahead=%s\nbehind=%s\ndetail=%s\n' \
    "$(now)" "$1" "$(g rev-parse --short HEAD 2>/dev/null || echo none)" \
    "${AHEAD:-?}" "${BEHIND:-?}" "${2:-}" > "$STATUS_FILE"
}

[[ -d "$GIT_DIR_PATH" ]] || { log "FATAL git dir missing: $GIT_DIR_PATH"; write_status error "missing-git-dir"; exit 1; }
[[ -d "$WORK_TREE"    ]] || { log "FATAL work tree missing: $WORK_TREE";  write_status error "missing-work-tree"; exit 1; }

# Invariant 4. A .git marker inside the vault means Git metadata is one
# replication cycle away from reaching every other device - refuse rather than
# commit around it.
if [[ -e "$WORK_TREE/.git" ]]; then
  log "FATAL $WORK_TREE/.git exists; Git metadata must stay outside the vault"
  write_status error "git-marker-in-vault"
  exit 1
fi

changes="$(g status --porcelain)"
if [[ -z "$changes" ]]; then
  log "no changes"
  AHEAD=0; BEHIND=0
  write_status clean "no-changes"
  exit 0
fi

if [[ "$INSPECT" == "1" ]]; then
  log "would stage $(printf '%s\n' "$changes" | wc -l | tr -d ' ') change(s):"
  printf '%s\n' "$changes"
  g add -A --dry-run 2>/dev/null || true
  g diff --stat
  exit 0
fi

g add -A || { log "FATAL git add failed"; write_status error "add-failed"; exit 1; }

commit_msg="${MESSAGE:-vault: autocommit $(now)}"
if ! g commit -q -m "$commit_msg"; then
  log "commit produced nothing (raced with a concurrent run?)"
  write_status clean "nothing-to-commit"
  exit 0
fi
log "committed: $(g rev-parse --short HEAD)"

if g push -q origin "$BRANCH" 2>&1; then
  push_result=ok
else
  push_result=push-failed
  log "WARN push failed; the commit is safe locally and will retry next run"
fi

g fetch -q origin "$BRANCH" 2>/dev/null || true
read -r AHEAD BEHIND <<<"$(g rev-list --left-right --count "${BRANCH}...origin/${BRANCH}" 2>/dev/null || echo '? ?')"
log "ahead=${AHEAD} behind=${BEHIND}"

if [[ "$push_result" == "ok" && "$AHEAD" == "0" && "$BEHIND" == "0" ]]; then
  write_status ok "synced"
else
  write_status degraded "$push_result"
fi
