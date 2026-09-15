# Session durability and cleanup — September 15, 2026

Completed and deployed. The active API/UI/node build is `dbcf639-a56381b9bd75`;
the independent MCP bundle `20260915T143633Z-d2e91eab9aa7` already matches master.
The restricted Mac restart helper passed its checks and activation completed
without an administrator command from Ben. Exact paths and hashes are in
`verification.json`; operational source matches the installed releases.

## Final durable state

- Red PARO-int5 remains enabled/running and is the main Hermes, steward, Mac Pi
  and ARIA Red Pi profile default. Both R9700s read back 250 W and retain their
  identity-matched udev rule. Both managed Pi catalogues contain PARO; Corsair
  Pi's independent CUDA/Halo default is intentionally preserved, as in promotion.
- NInfer on the Corsair RTX 3090 retains 94K (96256 tokens), INT8 KV, vision,
  MTP3 and lm-head draft. Its canonical/installed launcher, unit and live process
  flags match. The service is now enabled under `default.target`, with user
  lingering, so no interactive login is required at boot. The GPU exclusivity
  guard and `Restart=no` policy remain intact. Enablement changed without
  restarting the model. Unit validity and persistence were checked; no reboot
  was performed.
- Routine Hermes cron and existing vision/auxiliary routes remain on NInfer;
  global Hermes stays on Red. The redundant legacy `providers.aria.model` value
  was synchronized to the existing PARO default. Credentials were preserved.
- ARIA's stale 64K/16-pending/no-Hermes registry description and memory estimate
  now match 94K, two pending requests, vision/MTP3 and 22.75 GiB residency.
- The ordinary `aria-tools-check` now validates the installed Hermes minimum
  context against the cron model's configured window. A regression test rejects
  the original 32768-token configuration before a passing canary can hide it.
- `CorsairModelHost/ninfer3090/verify-profile.py` provides a repeatable, read-only
  comparison of canonical files, boot settings, live flags and Hermes routes.
  The native cron canary remains available separately. Recovery profiles for
  64K/MTP3 and 128K/non-speculative serving are retained.

## Evidence and housekeeping

25 API/contract regression tests, the production UI build/TypeScript/class lint,
all nine deployed functional checks, boot readiness and the actual native cron
agent canary passed. Vision routing was verified through stock Hermes and ARIA.
No stock scanner script or message delivery was replayed. Its next scheduled
run remains September 16 at 09:00 America/New_York; the five historical failures
are retained until a real successful scheduled run clears them.

The current topology document was reconciled; the September 8 version is
explicitly archived. The original activation report now records Ben's completed
activation and subsequent superseding releases. Temporary test environments
were removed. No owned temporary inference gateway or test forward remains.
Private configuration backups, immutable releases and benchmark evidence are
retained. Pre-existing unrelated model experiments and recovery worktrees were
not deleted or folded into this change.

Private evidence: `/Users/ben/Services/backups/aria-final-durability-20260915`.
API rollback receipt: `/Users/ben/Services/backups/aria-release-20260915T151028Z`.
Tracking task: `6aa95dd2a839573791069505`.
