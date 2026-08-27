#!/usr/bin/env bash
# ARIA - Obsidian LiveSync recovery: Phase 0 preflight (read-only)
#
# Phase: Obsidian LiveSync Corsair recovery (Phase 0, before the freeze)
# Purpose: prove the environment matches the plan's assumptions; change nothing
#
# Related plan sections:
# - Section 2: Incident summary (what should be stopped, and is not)
# - Section 7, Phase 0: Announce and freeze
#
# Every check here is a read. Run it as often as you like, including outside a
# maintenance window - it is the "is this still the incident we planned for?"
# gate. A FAIL means the plan's preconditions no longer hold and the phase
# scripts must not be run until the difference is understood.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

fails=0
ok()   { obs_log "PASS  $*"; }
bad()  { obs_log "FAIL  $*"; fails=$((fails + 1)); }
note() { obs_log "INFO  $*"; }

obs_log "preflight for run ${OBS_RUN_ID}"

# --- Mac vault -------------------------------------------------------------
[[ -d "$OBS_MAC_VAULT" ]] && ok "Mac vault present: $OBS_MAC_VAULT" \
                          || bad "Mac vault missing: $OBS_MAC_VAULT"

if [[ -e "$OBS_MAC_VAULT/.git" ]]; then
  bad "Mac vault contains a .git marker (invariant 4)"
else
  ok "Mac vault has no .git marker"
fi

mac_files="$(obs_manifest "$OBS_MAC_VAULT" | wc -l | tr -d ' ')"
note "Mac synchronized content set: ${mac_files} files"

# --- Mac bridge ------------------------------------------------------------
if [[ -f "$OBS_MAC_BRIDGE_PLIST" ]]; then
  ok "Mac bridge LaunchDaemon plist present"
  # Distinguish "not loaded" from "cannot ask". On current macOS a regular
  # admin user can usually inspect a system-domain service directly; older or
  # more restricted installations may still require sudo. Try the unprivileged
  # read first so a non-interactive preflight does not falsely report UNKNOWN.
  #
  # Passwordless sudo is NOT required here: preflight only reads, so on a
  # terminal it is fine to prompt. Only a non-interactive caller that also
  # lacks NOPASSWD genuinely cannot answer the question.
  if launchctl print "system/${OBS_MAC_BRIDGE_LABEL}" >/dev/null 2>&1; then
    note "Mac bridge is LOADED (Phase 0 must bootout, not kill: KeepAlive)"
  elif launchctl print-disabled system >/dev/null 2>&1; then
    ok "Mac bridge is not loaded"
  else
    if ! sudo -n true 2>/dev/null && [[ -t 0 ]]; then
      note "querying the Mac bridge needs sudo; you may be prompted for a password"
      sudo -v || true
    fi
    if ! sudo -n true 2>/dev/null; then
      bad "cannot query the Mac bridge: launchctl access denied and sudo unavailable, so its state is UNKNOWN"
    elif sudo -n launchctl print "system/${OBS_MAC_BRIDGE_LABEL}" >/dev/null 2>&1; then
      note "Mac bridge is LOADED (Phase 0 must bootout, not kill: KeepAlive)"
    else
      ok "Mac bridge is not loaded"
    fi
  fi
else
  bad "Mac bridge LaunchDaemon plist missing: $OBS_MAC_BRIDGE_PLIST"
fi

# --- Mac's two distinct filesystem projections ----------------------------
# The desktop plugin replicates /Users/ben/Obsidian. The LaunchDaemon's
# storage peer replicates the service account vault. Both may be enabled: they
# are separate projections of the same CouchDB state, not two watchers on one
# directory.
plugin_data="$OBS_MAC_VAULT/.obsidian/plugins/obsidian-livesync/data.json"
if [[ -f "$plugin_data" ]]; then
  live="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("liveSync"))' "$plugin_data")"
  if [[ "$live" == "True" || "$live" == "true" ]]; then
    if [[ "${OBS_MAC_VAULT%/}" == "${OBS_MAC_SERVICE_VAULT%/}" ]]; then
      bad "Obsidian plugin and Mac headless bridge are configured for the same root: $OBS_MAC_VAULT"
    else
      ok "Obsidian plugin replication is on for its distinct interactive vault: $OBS_MAC_VAULT"
      note "Mac headless bridge projection: $OBS_MAC_SERVICE_VAULT"
    fi
  else
    note "Obsidian plugin continuous replication is off for the interactive vault"
  fi
else
  note "Obsidian LiveSync plugin config not found in the interactive vault"
fi

# --- Corsair ---------------------------------------------------------------
if obs_corsair 'echo ok' >/dev/null 2>&1; then
  ok "Corsair reachable over ssh as '$OBS_CORSAIR_HOST'"

  state="$(obs_corsair "docker inspect -f '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' ${OBS_CORSAIR_CONTAINER} 2>/dev/null || echo 'absent -'")"
  read -r cstate cpolicy <<<"$state"
  if [[ "$cstate" == "running" ]]; then
    bad "Corsair bridge is RUNNING against the divergent vault (invariant 1) - stop it before Phase 1"
  else
    ok "Corsair bridge container is not running (status: $cstate)"
  fi
  [[ "$cpolicy" == "unless-stopped" ]] \
    && ok "Corsair restart policy already unless-stopped" \
    || note "Corsair restart policy is '$cpolicy'; Phase 3 step 7 must recreate the container"

  if obs_corsair "test -e ${OBS_CORSAIR_VAULT}/.git"; then
    note "Corsair vault still holds a .git marker; Phase 3 relocates it"
  else
    ok "Corsair vault has no .git marker"
  fi

  head="$(obs_corsair "git --git-dir=${OBS_CORSAIR_GITDIR} rev-parse HEAD 2>/dev/null || echo none")"
  if [[ "$head" == "$OBS_BASE_COMMIT" ]]; then
    ok "Corsair vault Git HEAD is the expected base commit ${OBS_BASE_COMMIT:0:8}"
  else
    bad "Corsair vault Git HEAD is ${head:0:8}, not the planned base ${OBS_BASE_COMMIT:0:8} - re-audit before merging"
  fi

  cron_lines="$(obs_corsair 'crontab -l 2>/dev/null | grep -c obsidian-autocommit || true')"
  note "Corsair crontab obsidian-autocommit entries: ${cron_lines:-0} (Phase 7 installs exactly one)"
else
  bad "Corsair unreachable over ssh as '$OBS_CORSAIR_HOST' - set OBS_CORSAIR_HOST"
fi

# --- CouchDB ---------------------------------------------------------------
if [[ -n "${OBS_COUCH_CURL_CONFIG:-}" && -r "${OBS_COUCH_CURL_CONFIG:-}" ]]; then
  if info="$(obs_couch_info 2>/dev/null)"; then
    seq="$(printf '%s' "$info" | python3 -c 'import json,sys;print(json.load(sys.stdin)["update_seq"].split("-")[0])')"
    docs="$(printf '%s' "$info" | python3 -c 'import json,sys;print(json.load(sys.stdin)["doc_count"])')"
    ok "CouchDB '${OBS_COUCH_DB}' reachable: doc_count=${docs} update_seq=${seq}"
  else
    bad "CouchDB '${OBS_COUCH_DB}' unreachable with the supplied credentials"
  fi
else
  note "CouchDB not checked; generate credentials first:"
  note "  eval \"\$(./couch-config.py --source <bridge-config.json> --out \"\$HOME/.obs-recovery-curlrc\" | sed 's/^/export /')\""
fi

obs_log "preflight complete: ${fails} failure(s)"
[[ "$fails" -eq 0 ]] || exit 3
