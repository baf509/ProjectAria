#!/usr/bin/env bash
# ARIA - Obsidian LiveSync recovery: Phase 1 immutable recovery copies
#
# Phase: Obsidian LiveSync Corsair recovery (Phase 1)
# Purpose: make every subsequent write reversible, and prove the copies are good
#
# Related plan sections:
# - Section 6: Recovery artifacts and naming
# - Section 7, Phase 1: Capture immutable recovery copies
#
# This is the phase whose output the rollback plan (Section 10) depends on, so
# it validates rather than assumes: manifests are regenerated from the copies,
# ten files per tree are re-read and compared byte-for-byte against the frozen
# source, and `git bundle verify` must pass. Nothing downstream runs if any of
# those fail.
#
# Usage:
#   OBS_RUN_ID=20260827-001500 OBS_APPLY=1 ./02-snapshot.sh

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

[[ "${1:-}" == "--apply" ]] && OBS_APPLY=1
obs_require_apply "capture recovery snapshots"

mac_root="$(obs_assert_outside_vaults "Mac recovery root" "$OBS_MAC_RECOVERY_ROOT")"
mkdir -p "$mac_root"
OBS_LOG_FILE="$mac_root/snapshot.log"
obs_log "Phase 1 snapshot, run ${OBS_RUN_ID} -> ${mac_root}"

obs_corsair_check
obs_corsair "mkdir -p ${OBS_CORSAIR_RECOVERY_ROOT}"

# --- Mac vault -------------------------------------------------------------
obs_log "copying the Mac vault"
mkdir -p "$mac_root/mac-vault"
# -E preserves extended attributes; the copy is the rollback source, so it is
# taken whole (including .obsidian) rather than filtered to the content set.
rsync -aE "$OBS_MAC_VAULT/" "$mac_root/mac-vault/"

# --- Corsair vault, Git, bridge state --------------------------------------
obs_log "capturing Corsair artifacts"
obs_corsair "set -e
  root=${OBS_CORSAIR_RECOVERY_ROOT}
  mkdir -p \"\$root\"/{corsair-vault,vault-git,bridge}
  # Ownership, ACLs, timestamps and the .git pointer all preserved (plan Phase 1).
  rsync -aAX --numeric-ids ${OBS_CORSAIR_VAULT}/ \"\$root\"/corsair-vault/
  rsync -aAX --numeric-ids ${OBS_CORSAIR_GITDIR}/ \"\$root\"/vault-git/
  git --git-dir=${OBS_CORSAIR_GITDIR} bundle create \"\$root\"/vault-all-refs.bundle --all
  # Config is secret-bearing: copy mode-preserved and lock it down (Section 3.6).
  cp -p ${OBS_CORSAIR_BRIDGE}/dat/config.json \"\$root\"/bridge/config.json
  chmod 600 \"\$root\"/bridge/config.json
  rsync -aAX ${OBS_CORSAIR_BRIDGE}/data/ \"\$root\"/bridge/data/ 2>/dev/null || true
  cp -p ${OBS_CORSAIR_BRIDGE}/docker-compose*.yml \"\$root\"/bridge/ 2>/dev/null || true
  docker inspect ${OBS_CORSAIR_CONTAINER} > \"\$root\"/bridge/inspect.json 2>/dev/null || true
  docker logs --tail 5000 ${OBS_CORSAIR_CONTAINER} > \"\$root\"/bridge/container.log 2>&1 || true
  crontab -l > \"\$root\"/crontab.before 2>/dev/null || true
  cp -p /home/ben/.local/state/obsidian-autocommit/last-status \"\$root\"/ 2>/dev/null || true
  cp -p /home/ben/.obsidian-autocommit.log \"\$root\"/ 2>/dev/null || true
  chmod -R go-rwx \"\$root\"
"

obs_log "verifying the Corsair Git bundle"
obs_corsair "git --git-dir=${OBS_CORSAIR_GITDIR} bundle verify ${OBS_CORSAIR_RECOVERY_ROOT}/vault-all-refs.bundle" \
  >/dev/null 2>&1 || obs_stop_gate "git-bundle" "git bundle verify failed on Corsair"

# --- Pull the Corsair trees to the Mac so Phase 2 can run locally ----------
obs_log "pulling Corsair snapshot to the Mac"
mkdir -p "$mac_root/corsair-vault" "$mac_root/corsair-git"
cor_rsh="$(obs_corsair_rsync_shell)"
rsync -a -e "$cor_rsh" "${OBS_CORSAIR_HOST}:${OBS_CORSAIR_RECOVERY_ROOT}/corsair-vault/" "$mac_root/corsair-vault/"
rsync -a -e "$cor_rsh" "${OBS_CORSAIR_HOST}:${OBS_CORSAIR_RECOVERY_ROOT}/vault-git/"     "$mac_root/corsair-git/"
rsync -a -e "$cor_rsh" "${OBS_CORSAIR_HOST}:${OBS_CORSAIR_RECOVERY_ROOT}/vault-all-refs.bundle" "$mac_root/"

# --- Mac bridge definition and health --------------------------------------
mkdir -p "$mac_root/mac-bridge"
cp -p "$OBS_MAC_BRIDGE_PLIST" "$mac_root/mac-bridge/" 2>/dev/null || true
sudo -n launchctl print "system/${OBS_MAC_BRIDGE_LABEL}" \
  > "$mac_root/mac-bridge/launchctl-print.txt" 2>&1 || true

# --- CouchDB one-shot recovery replica -------------------------------------
recovery_db="${OBS_COUCH_DB}_recovery_${OBS_RUN_ID//-/_}"
obs_log "creating CouchDB recovery replica: ${recovery_db}"
before="$(obs_couch_info)"
# A prior stopped attempt may already have created the target. Reusing that
# run-specific database is safe: CouchDB replication is revision-idempotent,
# and the document-count gate below still has to pass.
if obs_couch_info "$recovery_db" >/dev/null 2>&1; then
  obs_log "CouchDB recovery replica already exists; resuming it"
else
  obs_couch_curl --request PUT "$(obs_couch_url)/${recovery_db}" >/dev/null \
    || obs_stop_gate "couch-replica" "could not create ${recovery_db}"
fi

# CouchDB 2+ requires fully qualified replication endpoints. A bare database
# name is expanded from the server's bind address; on this installation that
# address is `any`, which is not resolvable and makes _replicate fail with an
# authentication request to http://any:5984/_session. Build endpoint objects
# with explicit Basic auth from the mode-0600 curl config. The JSON travels on
# stdin, so neither credentials nor an Authorization header reach argv or a
# log file.
python3 - "$OBS_COUCH_CURL_CONFIG" \
          "$(obs_couch_url)/${OBS_COUCH_DB}" \
          "$(obs_couch_url)/${recovery_db}" <<'PYEOF' |
import json, re, sys
config_path, source_url, target_url = sys.argv[1:]
text = open(config_path, encoding="utf-8").read()
match = re.search(r'^user\s*=\s*"(.*)"\s*$', text, re.M)
if not match:
    raise SystemExit("CouchDB credential entry missing from curl config")
username, password = match.group(1).split(":", 1)
auth = {"auth": {"basic": {"username": username, "password": password}}}
json.dump({
    "source": {"url": source_url, **auth},
    "target": {"url": target_url, **auth},
}, sys.stdout)
PYEOF
obs_couch_curl --request POST "$(obs_couch_url)/_replicate" \
  --header 'Content-Type: application/json' \
  --data-binary @- \
  > "$mac_root/couch-replicate-result.json" \
  || obs_stop_gate "couch-replica" "_replicate to ${recovery_db} failed"

printf '%s' "$before" > "$mac_root/couch-source-before.json"
obs_couch_info                > "$mac_root/couch-source-after.json"
obs_couch_info "$recovery_db" > "$mac_root/couch-replica.json"

# The three documents are handed over as files. Interpolating server JSON into
# a heredoc would make the snapshot's correctness depend on CouchDB never
# emitting a character that happens to be Python syntax.
if python3 - "$mac_root" "$OBS_COUCH_DB" "$recovery_db" <<'PYEOF'
import json, sys
root, db, recovery_db = sys.argv[1], sys.argv[2], sys.argv[3]
load = lambda n: json.load(open(f"{root}/{n}.json"))
src_before = load("couch-source-before")
src_after = load("couch-source-after")
dst = load("couch-replica")
state = {
    "database": db,
    "recovery_database": recovery_db,
    "source_doc_count_before": src_before["doc_count"],
    "source_doc_count_after": src_after["doc_count"],
    "source_update_seq": src_after["update_seq"].split("-")[0],
    "recovery_doc_count": dst["doc_count"],
    "recovery_update_seq": dst["update_seq"].split("-")[0],
}
json.dump(state, open(f"{root}/couch-state.json", "w"), indent=2)
print(json.dumps(state, indent=2))
# The source must not have moved during replication, and the replica must have
# received all of it.
sys.exit(0 if state["source_doc_count_before"] == state["source_doc_count_after"]
                == state["recovery_doc_count"] else 1)
PYEOF
then
  obs_log "CouchDB recovery replica verified"
else
  obs_stop_gate "couch-replica" \
    "recovery replica document count does not match the frozen source"
fi

# --- Manifests and spot verification ---------------------------------------
obs_log "generating manifests"
obs_manifest "$mac_root/mac-vault"     "$mac_root/manifest-mac.txt"
obs_manifest "$mac_root/corsair-vault" "$mac_root/manifest-corsair.txt"

# A manifest of a copy proves the copy is internally consistent, not that it
# matches the source. Ten random files per tree are re-read from the live
# frozen vault and compared (plan Phase 1 validation).
spot_check() {
  local live="$1" snap="$2" label="$3" rel bad=0
  while read -r rel; do
    if ! cmp -s "$live/$rel" "$snap/$rel"; then
      obs_log "SPOT CHECK MISMATCH [$label]: $rel"
      bad=1
    fi
  # cut by column, not by field: a manifest line is 64 hex + two spaces +
  # the path, and re-splitting on whitespace would mangle paths with runs of
  # spaces in them.
  done < <(obs_manifest "$snap" | cut -c 67- \
           | awk 'BEGIN{srand(7)} {print rand()"\t"$0}' | sort -n | head -10 | cut -f2-)
  [[ "$bad" -eq 0 ]] || obs_stop_gate "spot-check" "$label snapshot does not match its source"
  obs_log "spot check passed: $label"
}
spot_check "$OBS_MAC_VAULT" "$mac_root/mac-vault" "mac"

# The Corsair snapshot is verified ON Corsair, against its own live source.
# Comparing the pulled copy with the pulled copy would only prove rsync is
# deterministic. A full manifest match is also stricter than the plan's ten
# random files, for one extra remote python run.
obs_log "verifying the Corsair snapshot against its live source"
obs_corsair "cat > /tmp/obs-manifest.py" < "$OBS_LIB_DIR/manifest.py" >/dev/null
obs_corsair "cat > /tmp/obslib.py"      < "$OBS_LIB_DIR/obslib.py"   >/dev/null
cor_live="$(obs_corsair "cd /tmp && python3 obs-manifest.py --root ${OBS_CORSAIR_VAULT}")"
cor_snap="$(obs_corsair "cd /tmp && python3 obs-manifest.py --root ${OBS_CORSAIR_RECOVERY_ROOT}/corsair-vault")"
obs_corsair "rm -f /tmp/obs-manifest.py /tmp/obslib.py"
if [[ "$cor_live" == "$cor_snap" ]]; then
  obs_log "spot check passed: corsair (full manifest match)"
else
  diff <(printf '%s\n' "$cor_live") <(printf '%s\n' "$cor_snap") | tee -a "$OBS_LOG_FILE" >&2
  obs_stop_gate "spot-check" "the Corsair snapshot does not match its live source"
fi

# The pulled copy must equal what Corsair holds, or Phase 2 merges the wrong tree.
if [[ "$(obs_manifest "$mac_root/corsair-vault")" == "$cor_snap" ]]; then
  obs_log "spot check passed: corsair snapshot transferred intact"
else
  obs_stop_gate "spot-check" "the Corsair snapshot changed in transit to the Mac"
fi

obs_log "Phase 1 complete. Recovery root: ${mac_root}"
obs_log "Corsair recovery root: ${OBS_CORSAIR_RECOVERY_ROOT}"
obs_log "CouchDB recovery replica: ${recovery_db} (do NOT point any client at it)"
