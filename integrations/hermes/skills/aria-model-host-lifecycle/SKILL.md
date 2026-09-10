---
name: aria-model-host-lifecycle
description: Wake, load, switch, or sleep Aria's registered Red Linux models; handle failed readiness without duplicate starts or unmanaged wake commands.
---

Use Aria MCP for model lifecycle. Red is Linux with dual R9700 GPUs. Corsair
runs the wake relay; the Mac runs Aria and Hermes. The native Linux identity is
`red-linux.tailb286a5.ts.net` (observed `100.111.105.37`); the historical
Windows address `100.120.162.100` does not diagnose Linux reachability. Prefer
Aria observations over remembered host addresses.

For "wake Red and load Flash Next", call `red_model_status`, then
`select_red_model(model="qwen-flash-next")` once. Use `qwen3.8-27b` for Red's
27B model. The selection tool handles wake, safe switching and readiness; allow
its bounded wait to finish. Only `status=ready` confirms it is serving.

For pending/error, read `red_model_status` once. If still asleep, unreachable or
unknown, report that the operation did not confirm readiness and stop automatic
remediation. Request an operator observation of power light/screen/network.
Do not repeat start just because state remains asleep: the first action may
still be running. A new attempt requires evidence of recovery or an explicit
user request. Never bypass busy, assignment or route-pin refusals.

Do not use terminal/SSH polling, direct WoL or `/usr/local/bin/wake-red` as a
fallback. That legacy helper targets a removed motherboard. Aria's registered
HTTP relay targets the current Wi-Fi adapter. Approval-blocked shell commands
must not be rewritten to evade the guard; return to Aria observations and report
the unresolved operation.

`asleep` is Aria's failed reachability observation, not a power-state sensor.
Failed ping does not distinguish suspend, shutdown, Wi-Fi loss, boot failure,
firewall or Tailscale problems. A relay accepting a packet does not prove Red
received it. Do not assert Windows, ErP or BIOS causes without host evidence.
The tested suspend/resume path worked previously; full shutdown wake remains
unqualified. Physical power-on does not itself guarantee a model will load;
check Aria status before requesting a fresh selection.

For an explicitly requested sleep, use `sleep_model_server`, then verify status.
Distinguish an unreachable observation from proof of the underlying power state.

Loading Red does not select it as Hermes's conversation model. After readiness,
use Hermes `/model` to choose `Red-Qwen3.8-Flash-Next-MXFP4` under ARIA. Changing
Aria's default route or agent bindings is not equivalent to changing Hermes's
explicit model. Keep Corsair as Hermes's default unless the user asks otherwise.
