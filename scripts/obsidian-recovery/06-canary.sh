#!/usr/bin/env bash
# ARIA - Obsidian LiveSync recovery: Phase 8 round-trip canaries
#
# Phase: Obsidian LiveSync Corsair recovery (Phase 8)
# Purpose: prove replication behaviour instead of inferring it from process state
#
# Related plan sections:
# - Section 7, Phase 8: Perform bidirectional round-trip tests
# - Section 13: "Process state is not evidence of correct replication"
#
# Tests A-E from the plan. Each waits for a hash to match rather than sleeping
# a fixed interval and declaring success, and test C only runs once A and B
# have passed - a deletion canary against a link that was never proven to work
# would delete a note into silence.
#
# Usage:
#   ./06-canary.sh            # tests A and B
#   ./06-canary.sh --deletion # also test C (needs A and B to pass first)
#   ./06-canary.sh --restart  # also test D

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

: "${OBS_CANARY_SLO:=180}"     # seconds a round trip is allowed to take

DO_DELETION=0; DO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --deletion) DO_DELETION=1 ;;
    --restart)  DO_RESTART=1 ;;
    *) obs_die "unknown argument: $arg" ;;
  esac
done

canary="Recovery/Canary/canary-${OBS_RUN_ID}.md"
mac_path="$OBS_MAC_VAULT/$canary"
cor_path="$OBS_CORSAIR_VAULT/$canary"

obs_corsair_check
obs_log "Phase 8 canary: $canary (SLO ${OBS_CANARY_SLO}s)"

mac_hash() { shasum -a 256 "$mac_path" 2>/dev/null | cut -d' ' -f1; }
cor_hash() { obs_corsair "sha256sum '$cor_path' 2>/dev/null | cut -d' ' -f1" | tr -d '\r'; }

# Polls for convergence and reports the elapsed time, because "it arrived" and
# "it arrived within the service-level objective" are different results.
await_hash() {
  local getter="$1" want="$2" label="$3" waited=0
  while (( waited < OBS_CANARY_SLO )); do
    [[ "$($getter)" == "$want" ]] && { obs_log "PASS $label converged in ${waited}s"; return 0; }
    sleep 5; waited=$((waited + 5))
  done
  obs_stop_gate "canary-$label" "no convergence within ${OBS_CANARY_SLO}s"
}

await_absent() {
  local getter="$1" label="$2" waited=0
  while (( waited < OBS_CANARY_SLO )); do
    [[ -z "$($getter)" ]] && { obs_log "PASS $label removed in ${waited}s"; return 0; }
    sleep 5; waited=$((waited + 5))
  done
  obs_stop_gate "canary-$label" "deletion did not propagate within ${OBS_CANARY_SLO}s"
}

# --- Test A: Mac -> Corsair -------------------------------------------------
obs_log "Test A: Mac -> Corsair"
mkdir -p "$(dirname "$mac_path")"
printf '# LiveSync canary %s\n\nMac marker: %s\n' "$OBS_RUN_ID" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$mac_path"
want="$(mac_hash)"
obs_log "created on the Mac, sha256=${want:0:12}"
await_hash cor_hash "$want" "A(mac->corsair)"

# --- Test B: Corsair -> Mac -------------------------------------------------
obs_log "Test B: Corsair -> Mac"
obs_corsair "printf 'Corsair marker: %s\n' \"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> '$cor_path'"
want="$(cor_hash)"
obs_log "appended on Corsair, sha256=${want:0:12}"
await_hash mac_hash "$want" "B(corsair->mac)"

obs_log "Test B note: also confirm another LiveSync device receives it once clients are unfrozen (Phase 9)"

# --- Test D: restart resilience ---------------------------------------------
if [[ "$DO_RESTART" == "1" ]]; then
  obs_log "Test D: restart the Corsair bridge and re-run a round trip"
  obs_corsair "docker restart ${OBS_CORSAIR_CONTAINER}" >/dev/null
  obs_corsair "printf 'Post-restart marker: %s\n' \"\$(date -u +%Y-%m-%dT%H:%M:%SZ)\" >> '$cor_path'"
  want="$(cor_hash)"
  await_hash mac_hash "$want" "D(post-restart)"
fi

# --- Test C: deletion -------------------------------------------------------
if [[ "$DO_DELETION" == "1" ]]; then
  obs_log "Test C: deletion, and that nothing else moves with it"
  before="$(mktemp)"; after="$(mktemp)"
  obs_manifest "$OBS_MAC_VAULT" "$before"
  rm -f "$mac_path"
  await_absent cor_hash "C(deletion)"
  obs_manifest "$OBS_MAC_VAULT" "$after"
  # The canary itself is the only line that may disappear.
  unrelated="$(diff "$before" "$after" | grep -c "^[<>]" || true)"
  if [[ "$unrelated" -ne 1 ]]; then
    obs_manifest_diff "$before" "$after"
    obs_stop_gate "canary-C" "the deletion moved ${unrelated} paths, expected exactly 1"
  fi
  obs_log "PASS deletion affected exactly one path"
  rm -f "$before" "$after"
else
  obs_log "canary left in place at $canary; re-run with --deletion once A and B have passed"
fi

# --- Test E: Git history ----------------------------------------------------
obs_log "Test E: Git history"
obs_corsair "/home/ben/bin/obsidian-autocommit.sh" 2>&1 | obs_scrub >&2 || true
if obs_corsair "git --git-dir=${OBS_CORSAIR_GITDIR} log --oneline --all -- '$canary' | head -5" | grep -q .; then
  obs_log "PASS the canary is recoverable from Git history"
else
  obs_stop_gate "canary-E" "the canary never reached Git history"
fi

obs_log "Phase 8 complete"
