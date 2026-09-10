# Consistency corrections — 2026-09-08

Follow-up to [the read-only review](CONSISTENCY_REVIEW_20260908.md). This receipt
distinguishes deployed corrections from the Mac release awaiting activation.

## Already applied and checked

- Removed the Mac Tailscale TCP publications for raw Red `8094` and Ridge `8092`.
  Private loopback model access remains. API `8200`, UI `3000`/HTTPS `443` and the
  separate War Audio artifact server `8099` retain their registrations.
- Corrected the actual Corsair actuator under
  `/home/ben/.local/share/aria-model-actuator/api`: all three retired Flash Next
  loadouts reject forced starts. The unrelated dirty compatibility checkout was
  preserved, and the accepted CUDA/Halo model was not restarted.
- Installed Red Linux Wi-Fi wake persistence with Netplan and udev; the root
  sleep helper checks the connected Wi-Fi identity and armed wake settings.
  Corsair's permanent relay targets `f8:3d:c6:88:54:92`, is active and enabled,
  and its temporary recovery unit is inactive/unloaded.
- ARIA's registered sleep/start actions confirmed sleep, resumed the same Linux
  boot over Wi-Fi in 30.13 seconds, and returned Red's model to `ready`. A
  four-minute RTC recovery alarm was cleared after network wake. The initial
  test harness expected a success state named `running`; its evaluation was
  corrected to the API's actual `ready` state using the recorded observations.
  The wake preflight passed again after resume and the RTC alarm was empty.
  Reboot persistence is configured but has not been tested with a reboot.
- Automatic idle suspend remains disabled. Explicit **Sleep host** and **Start**
  in Operate are the supported power controls.
- Installed the explicit untracked operator launcher and updated Mac/Corsair
  routers. Pi retains normal gateway access through `/llm/v1` without caller
  identity headers, and stores history outside ARIA's watched tree. Claude
  disables transcript persistence and the ARIA MCP connection for this launch;
  Codex disables existing ARIA MCP declarations without changing global config.
  See [untracked session behavior and limits](UNTRACKED_SESSIONS.md).

Mac network/launcher backups and the wake receipt are under
`/Users/ben/Services/backups/aria-consistency-20260908T221851/`.
Corsair actuator/relay originals are under
`/home/ben/.local/share/aria-model-actuator/backups/consistency-20260908T222847/`.
Red originals are under `/var/lib/aria-wake-backups/1788920855/`.

## Prepared Mac corrections

- Reconcile the deployed registry and Operate loadouts with the source's retired
  model guards and accepted CUDA/Halo candidate; deploy the existing identified
  backend query fix.
- Honor disabled retrieval and TTS in health reporting; show the Mongo host
  tunnel port separately from its guest port.
- Make boot verification fail on required failures, discover and exercise MCP
  over stdio, compare installed bridge hashes, and check fresh required node
  heartbeats without waking on-demand hosts.
- Deploy API, UI and `mac-agents` from one content-identified release. Source
  edits no longer affect the production node after activation. The deployment
  helper verifies hashes and idle inference, checks restart privilege before
  changes, preserves recovery copies, and supports activation rollback.
- Remove retired current guidance from CLAUDE/BACKLOG and current UI docs;
  document model topology, service state, deployment and recovery. Mark the old
  Linux setup path historical and require an explicit opt-in.
- Remove unused `8080/8120/8121/8122` Corsair forwards. The installed launcher
  has been updated; its running launchd job still needs a privileged restart.

The final API/UI/node release is
`/Users/ben/Services/releases/ProjectAria/20260909T030128Z-52c814f-13a172cfae02`.
Activation and post-activation acceptance remain pending. The terminal operation
requires Mac system launchd administrator authentication; the agent cannot
supply it. See [deployment procedure](MAC_DEPLOYMENT.md). Final validation and
activation receipts may postdate the release snapshot.

The untracked launchers are separately installed host configuration, outside the
API/UI/node release. Their final Pi preference-persistence correction postdates
this snapshot; activation does not overwrite them. Their current installation
hashes are in `/Users/ben/Services/backups/aria-untracked-20260909T025716Z/installation.json`.

Final responsive validation and manifest verification passed. Activate from the Mac Terminal with:

```sh
/Users/ben/Services/backups/aria-consistency-20260908T221851/activate-reviewed-release
```

This helper obtains `sudo` authentication locally, verifies and activates the
exact release, checks idle inference again before restarting the trimmed SSH
forwards, runs the boot check, and tests live health, registry retirement guards,
both gateway model selectors, API/UI/node working directories and publication
boundaries. API/UI/node restart failures trigger the deployer's rollback. Failed
acceptance checks remain failures; they do not silently claim a completed rollout.

## Validation

150 targeted consistency tests passed across health/service semantics, boot verification,
deployment guards, gateway admission/authentication/catalogue, retired models,
MCP operations, readiness, local shell and Pi migrations. The deployment guard
subset also passed after the privilege preflight was added, including simulated
activation failure restoring the original launchers and legacy tree. Production Python
dependencies were not modified to install test tools.

The untracked launcher/router suite passed 39 tests (16 new tests plus existing
wrapper coverage). Real Pi and Codex requests completed without creating an ARIA
shell/coding session; Pi used the standard gateway. Claude startup reached the native
CLI, but the organization's subscription policy rejected the model request;
no watched transcript was created. End-to-end Claude generation remains unverified
because of that account restriction.

The live installed MCP bridge passed actual scoped discovery (114 tools),
contract status and API readiness, and both bridge source hashes matched.
The source boot check passed with fresh always-on node heartbeats. The UI
production build, type check, class lint and 21 responsive page/viewport checks
passed on the final release; exact service credentials were absent from browser
bundles. Manifest verification passed for build `52c814f-13a172cfae02`. The preview
process was stopped after the checks. API/UI/node activation and live acceptance
remain pending the Mac Terminal command above.

No new inference soak, Ridge wake/readiness test, backup restore or full test
suite was performed. Historical review findings remain in the original report
as the pre-fix snapshot.
