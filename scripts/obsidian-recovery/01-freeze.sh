#!/usr/bin/env bash
# ARIA - Obsidian LiveSync recovery: Phase 0 freeze
#
# Phase: Obsidian LiveSync Corsair recovery (Phase 0)
# Purpose: stop every writer, then prove CouchDB has actually gone quiet
#
# Related plan sections:
# - Section 7, Phase 0: Announce and freeze the maintenance window
# - Section 5, invariant 5: no other device writes during the merge window
#
# The stop gate here is deliberately empirical. "I stopped the bridges" is a
# claim; two identical update sequences one replication interval apart is a
# confirmation. Phones and other laptops cannot be stopped from this script, so
# the sequence check is what actually catches them.
#
# Usage:
#   ./01-freeze.sh                # dry run: report what it would stop
#   OBS_APPLY=1 ./01-freeze.sh    # stop the Mac bridge and verify quiescence

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

: "${OBS_QUIESCE_WAIT:=120}"   # one replication interval, in seconds

[[ "${1:-}" == "--apply" ]] && OBS_APPLY=1

obs_log "Phase 0 freeze, run ${OBS_RUN_ID} (apply=${OBS_APPLY})"

# --- 1. Human gate ---------------------------------------------------------
# Step 1 of the plan is a person, not a command. There is no way to verify
# "Ben will not edit notes for the next hour" mechanically, so it is asked.
if [[ "$OBS_APPLY" == "1" ]]; then
  obs_confirm "Phase 0 step 1: has Ben approved this maintenance window, quit Obsidian on the Mac, and suspended LiveSync on phones/laptops?" "window-approved"
fi

# --- 2-4. Stop the Mac headless bridge -------------------------------------
# KeepAlive means killing the PID relaunches it; the job must be booted out of
# the system domain (plan Phase 0 step 4).
if [[ "$OBS_APPLY" == "1" ]]; then
  obs_log "booting out system/${OBS_MAC_BRIDGE_LABEL}"
  sudo launchctl bootout "system/${OBS_MAC_BRIDGE_LABEL}" 2>&1 | obs_scrub >&2 || true
  if sudo -n launchctl print "system/${OBS_MAC_BRIDGE_LABEL}" >/dev/null 2>&1; then
    obs_stop_gate "mac-bridge-stop" "the bridge is still loaded after bootout"
  fi
  obs_log "Mac bridge is out of the system domain"
else
  obs_log "DRY RUN would: sudo launchctl bootout system/${OBS_MAC_BRIDGE_LABEL}"
fi

# --- 5. Corsair bridge must remain stopped ---------------------------------
obs_corsair_check
cstate="$(obs_corsair "docker inspect -f '{{.State.Status}}' ${OBS_CORSAIR_CONTAINER} 2>/dev/null || echo absent")"
if [[ "$cstate" == "running" ]]; then
  obs_stop_gate "corsair-bridge-stopped" \
    "Corsair's bridge is running against the divergent vault (invariant 1)"
fi
obs_log "Corsair bridge container status: ${cstate}"

# --- 6-7. CouchDB must be quiet --------------------------------------------
couch_seq() {
  obs_couch_info | python3 -c 'import json,sys
d = json.load(sys.stdin)
# CouchDB 2+ sequences are "N-<opaque>"; the numeric prefix is what advances.
print(d["update_seq"].split("-")[0], d["doc_count"])'
}

read -r seq1 docs1 <<<"$(couch_seq)"
obs_log "CouchDB before wait: update_seq=${seq1} doc_count=${docs1}"
obs_log "waiting ${OBS_QUIESCE_WAIT}s for one replication interval"
sleep "$OBS_QUIESCE_WAIT"
read -r seq2 docs2 <<<"$(couch_seq)"
obs_log "CouchDB after wait:  update_seq=${seq2} doc_count=${docs2}"

if [[ "$seq1" != "$seq2" || "$docs1" != "$docs2" ]]; then
  obs_stop_gate "couch-quiescent" \
    "CouchDB is still changing (${seq1}->${seq2}); find the remaining writer before Phase 1"
fi

# Record the frozen sequence: Phase 5 compares against it to prove the Mac
# published exactly the reconciled change set and nothing else.
if [[ "$OBS_APPLY" == "1" ]]; then
  state_dir="$(obs_assert_outside_vaults "recovery root" "$OBS_MAC_RECOVERY_ROOT")"
  mkdir -p "$state_dir"
  printf 'run_id=%s\nfrozen_update_seq=%s\nfrozen_doc_count=%s\nfrozen_at=%s\n' \
    "$OBS_RUN_ID" "$seq2" "$docs2" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$state_dir/freeze-state.env"
  obs_log "frozen state recorded at ${state_dir}/freeze-state.env"
fi

obs_log "Phase 0 complete: all known writers stopped and CouchDB is quiescent"
