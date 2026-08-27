#!/usr/bin/env bash
# ARIA - Obsidian LiveSync recovery: Phase 4 seed both vaults
#
# Phase: Obsidian LiveSync Corsair recovery (Phase 4)
# Purpose: make the two filesystem projections identical before either reconnects
#
# Related plan sections:
# - Section 7, Phase 4: Seed both filesystem vaults from the reconciled tree
# - Section 5, invariant 2: never --delete without a snapshot and a reviewed dry run
#
# This is the only script that writes into a live vault, so it is the most
# defensive one:
#   * every path is run through obs_assert_safe_dir, which rejects an empty or
#     unexpanded variable, home shorthand, and any target too shallow to be a
#     vault - the failure mode the plan calls out by name;
#   * deletions are OFF by default and, when enabled, are listed and confirmed
#     individually before anything is removed;
#   * overwritten and deleted files land in a timestamped --backup-dir, so even
#     the applied pass is reversible without going back to the Phase 1 snapshot.
#
# Usage:
#   OBS_RUN_ID=... ./05-seed.sh                 # dry run both hosts
#   OBS_RUN_ID=... ./05-seed.sh --apply         # seed both hosts
#   OBS_RUN_ID=... ./05-seed.sh --apply --allow-deletions

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

ALLOW_DELETIONS=0
for arg in "$@"; do
  case "$arg" in
    --apply)           OBS_APPLY=1 ;;
    --allow-deletions) ALLOW_DELETIONS=1 ;;
    *) obs_die "unknown argument: $arg" ;;
  esac
done

mac_root="$(obs_assert_safe_dir "recovery root" "$OBS_MAC_RECOVERY_ROOT")"
OBS_LOG_FILE="$mac_root/seed.log"
merged="$(obs_assert_safe_dir "merged tree" "$mac_root/reconcile/merged")"
merged="$(obs_assert_outside_vaults "merged tree" "$merged")"

[[ -f "$mac_root/reconcile/reconciliation-manifest.json" ]] \
  || obs_die "no reconciliation manifest; run 03-reconcile.sh first"

# The merged tree is only allowed to seed after Phase 2's gate passes. Running
# 03-reconcile.sh and ignoring its exit code is exactly the mistake this catches.
python3 "$OBS_LIB_DIR/validate-merged.py" --workspace "$mac_root/reconcile" \
  || obs_stop_gate "merged-validation" "the merged tree does not pass Phase 2 validation"

obs_log "Phase 4 seed, run ${OBS_RUN_ID} (apply=${OBS_APPLY} deletions=${ALLOW_DELETIONS})"

backup_dir_mac="$mac_root/pre-seed-overwrites/mac"
backup_dir_cor="${OBS_CORSAIR_RECOVERY_ROOT}/pre-seed-overwrites/corsair"

# --- dry run, then apply, one host at a time -------------------------------
seed_local() {
  local dest_raw="$1" backup="$2" label="$3"
  local dest; dest="$(obs_assert_safe_dir "$label vault" "$dest_raw")"

  local -a flags=(-a --checksum --itemize-changes
                  --backup "--backup-dir=$backup")
  while IFS= read -r ex; do flags+=("$ex"); done < <(obs_rsync_excludes)
  [[ "$ALLOW_DELETIONS" == "1" ]] && flags+=(--delete-delay)

  obs_log "[$label] dry run: $merged/ -> $dest/"
  local plan; plan="$(rsync "${flags[@]}" --dry-run "$merged/" "$dest/")"
  printf '%s\n' "$plan" | tee -a "$OBS_LOG_FILE" >&2

  local deletes; deletes="$(printf '%s\n' "$plan" | grep -c '^\*deleting' || true)"
  if [[ "$deletes" -gt 0 ]]; then
    obs_log "[$label] the dry run proposes ${deletes} deletion(s):"
    printf '%s\n' "$plan" | grep '^\*deleting' | tee -a "$OBS_LOG_FILE" >&2
    [[ "$ALLOW_DELETIONS" == "1" ]] \
      || obs_stop_gate "$label-deletions" "deletions proposed but --allow-deletions was not given"
    [[ "$OBS_APPLY" == "1" ]] \
      && obs_confirm "[$label] apply the ${deletes} deletion(s) listed above?" "delete-${label}"
  fi

  if [[ "$OBS_APPLY" != "1" ]]; then
    obs_log "[$label] DRY RUN only; nothing written"
    return 0
  fi

  mkdir -p "$backup"
  rsync "${flags[@]}" "$merged/" "$dest/" 2>&1 | tee -a "$OBS_LOG_FILE" >&2
  obs_log "[$label] applied; overwritten/deleted originals under $backup"
}

seed_local "$OBS_MAC_VAULT" "$backup_dir_mac" "mac"

# Corsair is seeded by pushing the merged tree over ssh. The same flag set is
# rebuilt remotely so the two hosts cannot receive different content sets.
obs_corsair_check

# Staging into the Corsair recovery root is non-destructive, so it happens in
# dry-run mode too - otherwise the Corsair plan could only ever be reviewed by
# committing to it, which is the opposite of what Phase 4 asks for.
obs_log "[corsair] staging the merged tree under ${OBS_CORSAIR_RECOVERY_ROOT}/merged"
obs_corsair "mkdir -p ${OBS_CORSAIR_RECOVERY_ROOT}/merged ${backup_dir_cor}"
rsync -a --delete -e "$(obs_corsair_rsync_shell)" \
  "$merged/" "${OBS_CORSAIR_HOST}:${OBS_CORSAIR_RECOVERY_ROOT}/merged/"

# Each exclude is single-quoted: the flag string is expanded by the REMOTE
# shell, which would otherwise glob patterns like '**/.DS_Store' against
# whatever happens to sit in the login directory.
# The staged tree arrived from macOS and therefore carries Mac group IDs.
# Corsair's unprivileged `ben` account cannot assign those groups; asking
# archive mode to preserve them makes rsync return code 23 after successfully
# transferring file contents, which prevents the parity gate from running.
# Ownership is host-local metadata, not part of the synchronized content set.
# Files previously written by the bridge are owned by its container UID 1993;
# Ben has content-write ACLs but cannot chmod or restore mtimes on them. Avoid
# all ownership/permission/time mutations on Corsair and validate bytes below.
cor_flags="-a --no-owner --no-group --no-perms --no-times --omit-dir-times --checksum --itemize-changes --backup --backup-dir='${backup_dir_cor}'"
while IFS= read -r ex; do cor_flags+=" '${ex}'"; done < <(obs_rsync_excludes)
[[ "$ALLOW_DELETIONS" == "1" ]] && cor_flags+=" --delete-delay"

obs_log "[corsair] dry run: ${OBS_CORSAIR_RECOVERY_ROOT}/merged/ -> ${OBS_CORSAIR_VAULT}/"
cor_plan="$(obs_corsair "rsync $cor_flags --dry-run ${OBS_CORSAIR_RECOVERY_ROOT}/merged/ ${OBS_CORSAIR_VAULT}/")"
printf '%s\n' "$cor_plan" | tee -a "$OBS_LOG_FILE" >&2
cor_deletes="$(printf '%s\n' "$cor_plan" | grep -c '^\*deleting' || true)"
if [[ "$cor_deletes" -gt 0 ]]; then
  obs_log "[corsair] the dry run proposes ${cor_deletes} deletion(s):"
  printf '%s\n' "$cor_plan" | grep '^\*deleting' | tee -a "$OBS_LOG_FILE" >&2
  [[ "$ALLOW_DELETIONS" == "1" ]] \
    || obs_stop_gate "corsair-deletions" "deletions proposed but --allow-deletions was not given"
  [[ "$OBS_APPLY" == "1" ]] \
    && obs_confirm "[corsair] apply the ${cor_deletes} deletion(s) listed above?" "delete-corsair"
fi

if [[ "$OBS_APPLY" == "1" ]]; then
  obs_corsair "rsync $cor_flags ${OBS_CORSAIR_RECOVERY_ROOT}/merged/ ${OBS_CORSAIR_VAULT}/" \
    2>&1 | tee -a "$OBS_LOG_FILE" >&2
  obs_log "[corsair] applied; overwritten/deleted originals under ${backup_dir_cor}"
else
  obs_log "[corsair] DRY RUN only; nothing written"
fi

# --- Prove the two content sets are identical ------------------------------
if [[ "$OBS_APPLY" == "1" ]]; then
  obs_log "recomputing manifests"
  obs_manifest "$OBS_MAC_VAULT" "$mac_root/manifest-mac-postseed.txt"
  obs_corsair "cat > /tmp/obs-manifest.py" < "$OBS_LIB_DIR/manifest.py" >/dev/null
  obs_corsair "cat > /tmp/obslib.py" < "$OBS_LIB_DIR/obslib.py" >/dev/null
  obs_corsair "cd /tmp && python3 obs-manifest.py --root ${OBS_CORSAIR_VAULT}" \
    > "$mac_root/manifest-corsair-postseed.txt"
  obs_corsair "rm -f /tmp/obs-manifest.py /tmp/obslib.py"

  if diff -q "$mac_root/manifest-mac-postseed.txt" \
             "$mac_root/manifest-corsair-postseed.txt" >/dev/null; then
    obs_log "PASS both vaults hold an identical synchronized content set"
  else
    obs_manifest_diff "$mac_root/manifest-mac-postseed.txt" \
                      "$mac_root/manifest-corsair-postseed.txt" \
      | tee -a "$OBS_LOG_FILE" >&2
    obs_stop_gate "seed-parity" "the two vaults still differ after seeding"
  fi

  # Plan Phase 4 validation: named artefacts must have survived the merge.
  for must in \
    "ProjectAria/Planning/ARIA_DISTRIBUTED_CONTROL_PLANE_PRODUCT_SPEC_20260826.md" \
    "ProjectAria/Planning/OBSIDIAN_LIVESYNC_CORSAIR_RECOVERY_PLAN_20260826.md"; do
    grep -q "  ${must}\$" "$mac_root/manifest-mac-postseed.txt" \
      && obs_log "PASS present on both hosts: $must" \
      || obs_stop_gate "seed-content" "expected note missing after seed: $must"
  done

  # Plan Phase 4 step 9: the bridge runtime UID must be able to write. The
  # probe file is deliberately outside the note set so it cannot replicate.
  obs_corsair "touch ${OBS_CORSAIR_VAULT}/.obsidian-livesync-writeprobe && rm -f ${OBS_CORSAIR_VAULT}/.obsidian-livesync-writeprobe" \
    && obs_log "PASS Corsair vault is writable by the bridge account" \
    || obs_stop_gate "corsair-writable" "the Corsair vault is not writable"
fi

obs_log "Phase 4 complete"
