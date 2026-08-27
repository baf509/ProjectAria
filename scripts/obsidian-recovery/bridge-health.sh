#!/usr/bin/env bash
# ARIA - Obsidian LiveSync bridge health probe
#
# Phase: Obsidian LiveSync Corsair recovery (Section 8, steady state)
# Purpose: emit the plan's health state for both filesystem peers as JSON
#
# Related plan sections:
# - Section 8.1: Required health states
# - Section 8.2: Metrics and alerts
#
# Prints one JSON object per run. Intended for the existing Mac monitoring and,
# eventually, ARIA's Fleet page - which is why the output is machine-readable
# and carries the raw measurements alongside the derived state, rather than
# only a verdict.
#
# States (worst wins): healthy | idle | degraded | stale | conflicted | stopped | failed
#
# Usage:
#   ./bridge-health.sh            # JSON to stdout
#   ./bridge-health.sh --exit-code  # also exit non-zero when not healthy/idle

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/lib.sh"

: "${OBS_STALE_REPLICATION_MIN:=15}"   # plan Section 8.2 threshold
: "${OBS_STALE_AUTOCOMMIT_MIN:=30}"

set +e   # a probe must report, not abort, when a component is unreachable

mac_state=failed
mac_detail=""
if [[ ! -f "$OBS_MAC_BRIDGE_PLIST" ]]; then
  mac_state=stopped; mac_detail="LaunchDaemon plist absent"
elif launchctl print "system/${OBS_MAC_BRIDGE_LABEL}" 2>/dev/null | grep -q 'state = running'; then
  mac_state=healthy; mac_detail="running"
elif launchctl print "system/${OBS_MAC_BRIDGE_LABEL}" >/dev/null 2>&1; then
  mac_state=degraded; mac_detail="loaded but not running"
elif sudo -n launchctl print "system/${OBS_MAC_BRIDGE_LABEL}" 2>/dev/null | grep -q 'state = running'; then
  mac_state=healthy; mac_detail="running"
elif sudo -n launchctl print "system/${OBS_MAC_BRIDGE_LABEL}" >/dev/null 2>&1; then
  mac_state=degraded; mac_detail="loaded but not running"
elif launchctl print-disabled system >/dev/null 2>&1 || sudo -n true 2>/dev/null; then
  mac_state=stopped; mac_detail="not loaded"
else
  mac_state=degraded; mac_detail="cannot query launchd"
fi

# The desktop plugin and LaunchDaemon are both legitimate only because they
# watch distinct roots. Report a conflict solely if those roots are ever made
# identical; liveSync=true by itself is expected.
plugin_data="$OBS_MAC_VAULT/.obsidian/plugins/obsidian-livesync/data.json"
dual_engine=false
if [[ -f "$plugin_data" ]]; then
  live="$(python3 -c 'import json,sys;print(str(json.load(open(sys.argv[1])).get("liveSync")).lower())' "$plugin_data" 2>/dev/null)"
  if [[ "$live" == "true" && "${OBS_MAC_VAULT%/}" == "${OBS_MAC_SERVICE_VAULT%/}" ]]; then
    dual_engine=true
    [[ "$mac_state" == "healthy" ]] && mac_state=conflicted
  fi
fi

cor_state=failed; cor_detail=""; cor_policy="unknown"; autocommit_age="null"; autocommit_result="unknown"
if obs_corsair 'echo ok' >/dev/null 2>&1; then
  read -r cstatus cor_policy <<<"$(obs_corsair "docker inspect -f '{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}' ${OBS_CORSAIR_CONTAINER} 2>/dev/null || echo 'absent none'")"
  case "$cstatus" in
    running) cor_state=healthy; cor_detail="container running" ;;
    exited|created) cor_state=stopped; cor_detail="container ${cstatus}" ;;
    absent) cor_state=failed; cor_detail="container does not exist" ;;
    *) cor_state=degraded; cor_detail="container ${cstatus}" ;;
  esac
  # Compose declares unless-stopped, but Docker has been observed with policy
  # "no" - the bridge then survives until the next daemon restart and silently
  # does not come back (plan Phase 3 step 7).
  [[ "$cor_state" == "healthy" && "$cor_policy" != "unless-stopped" ]] && {
    cor_state=degraded; cor_detail="running but restart policy is ${cor_policy}"; }

  status_raw="$(obs_corsair "cat /home/ben/.local/state/obsidian-autocommit/last-status 2>/dev/null" | tr -d '\r')"
  if [[ -n "$status_raw" ]]; then
    ts="$(printf '%s\n' "$status_raw" | sed -n 's/^timestamp=//p')"
    autocommit_result="$(printf '%s\n' "$status_raw" | sed -n 's/^result=//p')"
    if [[ -n "$ts" ]]; then
      epoch="$(python3 -c 'import sys,datetime;print(int((datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(sys.argv[1].replace("Z","+00:00"))).total_seconds()//60))' "$ts" 2>/dev/null)"
      autocommit_age="${epoch:-null}"
      if [[ -n "$epoch" && "$epoch" -gt "$OBS_STALE_AUTOCOMMIT_MIN" && "$cor_state" == "healthy" ]]; then
        cor_state=stale; cor_detail="autocommit status is ${epoch}m old"
      fi
    fi
  else
    autocommit_result="absent"
    [[ "$cor_state" == "healthy" ]] && { cor_state=degraded; cor_detail="no autocommit status file"; }
  fi
else
  cor_state=failed; cor_detail="unreachable over ssh"
fi

# Invariant 4 is checked continuously, not only during recovery: a .git marker
# appearing later would replicate Git metadata to every device.
git_marker=false
[[ -e "$OBS_MAC_VAULT/.git" ]] && git_marker=true
obs_corsair "test -e ${OBS_CORSAIR_VAULT}/.git" 2>/dev/null && git_marker=true

python3 - "$mac_state" "$mac_detail" "$cor_state" "$cor_detail" "$cor_policy" \
         "$autocommit_result" "$autocommit_age" "$dual_engine" "$git_marker" \
         "$OBS_MAC_VAULT" "$OBS_MAC_SERVICE_VAULT" <<'PYEOF'
import json, sys
(mac_state, mac_detail, cor_state, cor_detail, cor_policy,
 ac_result, ac_age, dual, marker, interactive_vault, service_vault) = sys.argv[1:12]
order = ["healthy", "idle", "degraded", "stale", "conflicted", "stopped", "failed"]
overall = max((mac_state, cor_state), key=lambda s: order.index(s) if s in order else 99)
print(json.dumps({
    "overall": overall,
    "mac_bridge": {"state": mac_state, "detail": mac_detail},
    "mac_vaults": {"interactive": interactive_vault,
                   "headless_service": service_vault},
    "corsair_bridge": {"state": cor_state, "detail": cor_detail,
                       "restart_policy": cor_policy},
    "autocommit": {"result": ac_result,
                   "age_minutes": None if ac_age == "null" else int(ac_age)},
    "dual_replication_engine_on_mac": dual == "true",
    "git_marker_in_a_vault": marker == "true",
}, indent=2))
PYEOF
overall_rc=$?

if [[ "${1:-}" == "--exit-code" ]]; then
  case "${mac_state}/${cor_state}" in
    healthy/healthy|healthy/idle|idle/healthy|idle/idle) exit 0 ;;
    *) exit 1 ;;
  esac
fi
exit "$overall_rc"
