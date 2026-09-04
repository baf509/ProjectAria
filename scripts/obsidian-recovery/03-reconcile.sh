#!/usr/bin/env bash
# ARIA - Obsidian LiveSync recovery: Phase 2 reconciliation workspace
#
# Phase: Obsidian LiveSync Corsair recovery (Phase 2)
# Purpose: check out the August 23 base and drive the three-way merge
#
# Related plan sections:
# - Section 7, Phase 2: Construct a three-way reconciliation workspace
#
# Reads only the Phase 1 snapshots. The live vaults are never touched, so this
# is safe to run, review, discard and re-run until the ledger is acceptable.
#
# Usage:
#   OBS_RUN_ID=20260827-001500 ./03-reconcile.sh

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

mac_root="$(obs_assert_safe_dir "recovery root" "$OBS_MAC_RECOVERY_ROOT")"
OBS_LOG_FILE="$mac_root/reconcile.log"
ws="$mac_root/reconcile"

for d in mac-vault corsair-vault corsair-git; do
  [[ -d "$mac_root/$d" ]] || obs_die "missing Phase 1 artifact: $mac_root/$d (run 02-snapshot.sh)"
done

# --- Base tree: the August 23 commit, extracted from the snapshotted Git dir
base="$mac_root/base"
if [[ ! -d "$base" ]]; then
  obs_log "extracting base commit ${OBS_BASE_COMMIT:0:8} from the snapshotted Git directory"
  mkdir -p "$base"
  # archive|tar rather than checkout: the snapshot Git dir has no work tree and
  # must stay untouched - it is also the rollback source (Section 10.5).
  git --git-dir="$mac_root/corsair-git" archive --format=tar "$OBS_BASE_COMMIT" \
    | tar -x -C "$base" \
    || obs_die "cannot extract ${OBS_BASE_COMMIT} from $mac_root/corsair-git"
fi
obs_log "base tree: $(obs_manifest "$base" | wc -l | tr -d ' ') files"

# --- Merge -----------------------------------------------------------------
rm -rf "$ws"
set +e
python3 "$OBS_LIB_DIR/reconcile.py" \
  --base "$base" \
  --mac "$mac_root/mac-vault" \
  --corsair "$mac_root/corsair-vault" \
  --workspace "$ws" \
  --run-id "$OBS_RUN_ID" \
  --base-commit "$OBS_BASE_COMMIT" \
  "$@" 2>&1 | tee -a "$OBS_LOG_FILE" >&2
rc="${PIPESTATUS[0]}"
set -e

obs_manifest "$ws/merged" "$mac_root/manifest-merged.txt"

if [[ "$rc" -ne 0 ]]; then
  obs_log "review $ws/RECONCILIATION_LEDGER.md, resolve each conflict in $ws/merged,"
  obs_log "then re-run validation with: $OBS_LIB_DIR/validate-merged.py --workspace $ws"
  obs_stop_gate "reconcile" "reconciliation did not validate (exit ${rc})"
fi

obs_log "Phase 2 complete. Review before seeding:"
obs_log "  ledger:   $ws/RECONCILIATION_LEDGER.md"
obs_log "  manifest: $ws/reconciliation-manifest.json"
obs_log "  merged:   $ws/merged"
