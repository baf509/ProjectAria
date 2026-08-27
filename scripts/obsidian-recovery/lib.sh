#!/usr/bin/env bash
# ARIA - Obsidian LiveSync recovery: shared library
#
# Phase: Obsidian LiveSync Corsair recovery (all phases)
# Purpose: run-id handling, credential-safe logging, path guards, manifests
#
# Related plan sections:
# - Section 5: Safety invariants
# - Section 6: Recovery artifacts and naming
#
# Sourced by every 0N-*.sh script. Nothing here performs a destructive
# action; the guards exist so that the scripts that DO cannot be pointed at
# an unresolved variable, a home-directory shorthand, or a vault root.

set -euo pipefail

# ---------------------------------------------------------------------------
# Run identity and roots
# ---------------------------------------------------------------------------
# One timestamp for the whole run (plan Section 6). Callers export OBS_RUN_ID
# to resume an in-flight run; otherwise a new one is minted and printed.
: "${OBS_RUN_ID:=$(date +%Y%m%d-%H%M%S)}"

: "${OBS_MAC_VAULT:=/Users/ben/Obsidian}"
: "${OBS_MAC_SERVICE_VAULT:=/Users/devboxsvc/Services/data/obsidian/vault}"
: "${OBS_MAC_RECOVERY_ROOT:=/Users/ben/Obsidian-Recovery/${OBS_RUN_ID}}"
: "${OBS_CORSAIR_HOST:=ben@100.123.245.84}"
# Corsair is NOT reachable as a bare hostname: there is no `corsair` entry in
# ~/.ssh/config, and the migration moved it to port 2222 behind a HostKeyAlias
# and a dedicated known-hosts file. These defaults mirror the connection string
# recorded in vault infrastructure/Analysis/CORSAIR_TO_MAC_MIGRATION_STATE_20260824.
# Override the whole set with OBS_CORSAIR_SSH_OPTS if the route changes.
: "${OBS_CORSAIR_SSH_OPTS:=-p 2222 -o HostKeyAlias=corsair-ai.local -o UserKnownHostsFile=/Users/ben/.config/devbox-migration/corsair-known-hosts-2222}"
: "${OBS_CORSAIR_VAULT:=/home/ben/Obsidian/vault}"
: "${OBS_CORSAIR_BRIDGE:=/home/ben/Obsidian/bridge}"
: "${OBS_CORSAIR_GITDIR:=/home/ben/.local/share/obsidian-vault-git}"
: "${OBS_CORSAIR_RECOVERY_ROOT:=/home/ben/.local/state/obsidian-recovery/${OBS_RUN_ID}}"
: "${OBS_BASE_COMMIT:=e7534f61f7bea5ef730aea2198eda4f33f0018c8}"
: "${OBS_COUCH_DB:=obsidian}"
: "${OBS_MAC_BRIDGE_PLIST:=/Library/LaunchDaemons/com.ben.devbox.obsidian-bridge.plist}"
: "${OBS_MAC_BRIDGE_LABEL:=com.ben.devbox.obsidian-bridge}"
: "${OBS_CORSAIR_CONTAINER:=obsidian-livesync-bridge-bridge-1}"
: "${OBS_CORSAIR_COMPOSE_PROJECT:=obsidian-livesync-bridge}"

# Content that is never part of the synchronized note set (plan Sections 2.4,
# 4, 5.4 and 7 Phase 2). Used for both reconciliation and seeding, so the two
# can never disagree about what "the vault" means.
OBS_EXCLUDES=(
  '.git'
  '.git/**'
  '.gitignore'
  '.obsidian'
  '.obsidian/**'
  '.trash'
  '.trash/**'
  '.DS_Store'
  '**/.DS_Store'
  'bridge'
  'bridge/**'
  '.obsidian-livesync-*'
  '*.tmp'
  '*.swp'
  '*~'
)

obs_rsync_excludes() {
  local e
  for e in "${OBS_EXCLUDES[@]}"; do printf -- '--exclude=%s\n' "$e"; done
}

# ---------------------------------------------------------------------------
# Logging - credential scrubbing is mandatory (plan Section 3.6)
# ---------------------------------------------------------------------------
OBS_LOG_FILE="${OBS_LOG_FILE:-}"

# Redacts anything that looks like a credential before it can reach a terminal
# or a log file. Applied to every line this library emits AND to every captured
# command output, because bridge logs and curl verbose output both carry
# CouchDB basic-auth material.
obs_scrub() {
  sed -E \
    -e 's#(https?://)[^/@[:space:]]+:[^/@[:space:]]+@#\1<redacted>@#g' \
    -e 's#("?(password|passphrase|obfuscatePassphrase|username|user|couchDB_PASSWORD|couchDB_USER|couchDB_URI|encryptedPassphrase|token|secret|api_?key)"?[[:space:]]*[:=][[:space:]]*)"[^"]*"#\1"<redacted>"#gI' \
    -e 's#(Authorization:[[:space:]]*[A-Za-z]+[[:space:]]+)[A-Za-z0-9+/=._-]+#\1<redacted>#gI' \
    -e 's#(-u[[:space:]]+)[^[:space:]]+#\1<redacted>#g'
}

obs_log() {
  local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
  if [[ -n "$OBS_LOG_FILE" ]]; then
    printf '%s\n' "$msg" | obs_scrub | tee -a "$OBS_LOG_FILE" >&2
  else
    printf '%s\n' "$msg" | obs_scrub >&2
  fi
}

obs_die() { obs_log "FATAL: $*"; exit 1; }

# A stop gate. The plan makes every phase boundary a decision point rather than
# a fall-through, so a failed check must halt the run, not warn and continue.
obs_stop_gate() {
  local name="$1"; shift
  obs_log "STOP GATE FAILED [$name]: $*"
  exit 3
}

# ---------------------------------------------------------------------------
# Apply gating - nothing destructive happens without an explicit opt-in
# ---------------------------------------------------------------------------
OBS_APPLY="${OBS_APPLY:-0}"

obs_require_apply() {
  [[ "$OBS_APPLY" == "1" ]] || obs_die "refusing to $* without --apply (dry-run is the default)"
}

obs_confirm() {
  local prompt="$1"
  local expected="$2"
  local answer
  [[ -t 0 ]] || obs_die "confirmation required for: $prompt (no TTY; run interactively)"
  printf '%s\nType exactly %q to proceed: ' "$prompt" "$expected" >&2
  read -r answer
  [[ "$answer" == "$expected" ]] || obs_die "confirmation mismatch; aborting"
}

# ---------------------------------------------------------------------------
# Path guards (plan Section 5.2 and Phase 4)
# ---------------------------------------------------------------------------
# rsync's --delete makes a wrong destination unrecoverable, so a destination is
# only accepted if it is absolute, already exists, canonicalizes to itself, and
# is not a bare home or filesystem root. This is the check that makes the
# plan's "no unresolved variable, home-directory shorthand, or broad directory"
# rule mechanical rather than a matter of care.
obs_assert_safe_dir() {
  local role="$1" p="$2"
  [[ -n "$p" ]]                || obs_die "$role path is empty (unresolved variable?)"
  [[ "$p" != *'$'* ]]          || obs_die "$role path contains an unexpanded variable: $p"
  [[ "$p" != '~'* ]]           || obs_die "$role path uses home shorthand: $p"
  [[ "$p" == /* ]]             || obs_die "$role path is not absolute: $p"
  [[ -d "$p" ]]                || obs_die "$role path is not an existing directory: $p"
  local real; real="$(cd "$p" && pwd -P)"
  case "$real" in
    /|/Users|/Users/*/|/home|/home/*/|/var|/etc|/System*)
      obs_die "$role path is too broad to be a rsync target: $real" ;;
  esac
  local depth; depth="$(printf '%s' "${real#/}" | tr -cd '/' | wc -c | tr -d ' ')"
  [[ "$depth" -ge 2 ]] || obs_die "$role path is too shallow to be a rsync target: $real"
  printf '%s' "$real"
}

# Recovery roots and reconciliation workspaces must live outside every
# synchronized vault root (plan Section 5.4) - otherwise the snapshot becomes
# part of what replicates and the bridge starts publishing its own backups.
obs_assert_outside_vaults() {
  local role="$1" p="$2"
  [[ "$p" == /* ]] || obs_die "$role path is not absolute: $p"

  # Normalised LEXICALLY, not with pwd -P: a recovery root usually does not
  # exist yet when it is checked, and a resolver that silently degrades on a
  # missing directory turns this guard into a no-op - which is exactly how a
  # snapshot ends up inside the vault it is meant to protect.
  local norm; norm="$(printf '%s' "$p" | sed -e 's#//*#/#g' -e 's#/\.\{1,\}$##' -e 's#/$##')"
  [[ "$norm" != *'/../'* && "$norm" != *'/..' ]] \
    || obs_die "$role path contains '..' and cannot be checked safely: $p"

  local v vnorm
  for v in "$OBS_MAC_VAULT" "$OBS_MAC_SERVICE_VAULT" "$OBS_CORSAIR_VAULT"; do
    vnorm="$(printf '%s' "$v" | sed -e 's#//*#/#g' -e 's#/$##')"
    if [[ "$norm" == "$vnorm" || "$norm" == "$vnorm"/* ]]; then
      obs_die "$role ($norm) is inside synchronized vault root $vnorm (invariant 4)"
    fi
  done
  printf '%s' "$norm"
}

# ---------------------------------------------------------------------------
# Corsair access
# ---------------------------------------------------------------------------
# All Corsair work goes through one helper so that connection settings, the
# batch-mode requirement (an interactive prompt inside a maintenance window is
# a hang, not a question) and output scrubbing are applied uniformly.
# shellcheck disable=SC2206  # deliberate word splitting: these are ssh flags
OBS_CORSAIR_SSH_OPT_ARR=($OBS_CORSAIR_SSH_OPTS)

obs_corsair() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes \
      "${OBS_CORSAIR_SSH_OPT_ARR[@]}" "$OBS_CORSAIR_HOST" "$@"
}

# rsync needs the same connection settings, or it silently tries port 22 and
# fails after every other Corsair step in the phase has already succeeded.
obs_corsair_rsync_shell() {
  printf 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes %s' \
    "$OBS_CORSAIR_SSH_OPTS"
}

obs_corsair_check() {
  obs_corsair 'echo ok' >/dev/null 2>&1 \
    || obs_die "cannot reach Corsair over ssh as '$OBS_CORSAIR_HOST' (set OBS_CORSAIR_HOST)"
}

# ---------------------------------------------------------------------------
# Manifests (plan Section 6)
# ---------------------------------------------------------------------------
# Emits "<sha256>  <relative-path>" sorted by path, honouring OBS_EXCLUDES so a
# manifest describes the synchronized content set and nothing else.
OBS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

obs_manifest() {
  local root="$1" out="${2:-}"
  if [[ -n "$out" ]]; then
    python3 "$OBS_LIB_DIR/manifest.py" --root "$root" --out "$out"
  else
    python3 "$OBS_LIB_DIR/manifest.py" --root "$root"
  fi
}

obs_manifest_diff() {
  local a="$1" b="$2"
  diff -u "$a" "$b" || true
}

# ---------------------------------------------------------------------------
# CouchDB - authenticated without ever putting credentials in argv or logs
# ---------------------------------------------------------------------------
# Credentials come from the host-local bridge config and are handed to curl
# through a mode-0600 --config file, so they never appear in the process table,
# shell history, or any captured output.
obs_couch_curl() {
  local cfg="${OBS_COUCH_CURL_CONFIG:-}"
  [[ -n "$cfg" && -r "$cfg" ]] || obs_die "OBS_COUCH_CURL_CONFIG is unset or unreadable (run 00-preflight.sh first)"
  curl --config "$cfg" --silent --show-error --fail-with-body "$@"
}

obs_couch_info() {
  obs_couch_curl --request GET "$(obs_couch_url)/${1:-$OBS_COUCH_DB}"
}

obs_couch_url() {
  [[ -n "${OBS_COUCH_URL:-}" ]] || obs_die "OBS_COUCH_URL is unset (run 00-preflight.sh first)"
  printf '%s' "${OBS_COUCH_URL%/}"
}
