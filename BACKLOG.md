# ARIA — current backlog

Only open, current work belongs here. Shipped work lives in `CHANGELOG.md`;
desired architecture lives in the vault-root `Architecture_Charter.md`.

Last reconciled: **2026-09-05**.

## Architecture reconciliation

- Move or make redundant Corsair's transitional vault Git-history writer, and
  eliminate unique active/unpushed general-project work from the data plane.

Closed 2026-08-29: Flash registry metadata, Hermes/Pi/steward gateway bypasses,
the duplicate Corsair Gemma service, and stale/manual Mac forward mappings.

## Inference scheduling

- Qualify the gateway's concurrent fleet-read coalescing and shorter idle pool
  expiry against mixed Hermes/Pi-shaped traffic. The production control showed
  intermittent 503 routing failures and a dropped stream without a model
  restart; the candidate also saw a 502 before receiving response headers.
  Five targeted regressions cover the new cache/pool behavior, but passing
  unit tests is not live reliability evidence. Activate the reviewed API change
  only between benchmark windows, then repeat the reference soak before
  judging the experimental engine. Never retry an accepted generation silently.
- Finish qualification and measured optimization of the original R9700 + Strix
  Halo engine in the sibling `CorsairModelHost/flashnext-engine` project.
  The updated source review is in
  `infrastructure/Planning/2026-09-05 Flash Next R9700 Halo Implementation Review.md`;
  it supersedes the original plan's technical assumptions and absolute speed targets.
  Indexed sparse attention has independent CPU/both-GPU numerical evidence;
  its original matrix-unit path remains opt-in. Generic chunked GDN is correct
  but failed its performance gate and must remain disabled. Operator findings:
  `infrastructure/Analysis/2026-09-05 Flash Next Original Engine Operator Qualification 015331.md`.
  Preserve the qualified `:8121` deployment as control.
  The experimental registry is not startable or auto-route eligible; full-model
  gateway qualification requires the restricted `:8122` forward. Ben approved
  that addition on 2026-09-05; its key restriction and loopback binding are
  verified. Launchd ownership is restored and the temporary forward is gone.
  Ben's API restart activated the reviewed registry/routing files. The candidate
  has loaded successfully and explicit gateway benchmarks have begun, with all
  new kernel flags disabled for the initial control. Exclusive-GPU test windows
  have an independent timed production rollback; live state is in the task.
  Promotion still requires matched depth-ladder performance, model correctness,
  cache, and mixed Hermes/Pi soak gates. Task: `6a9b9412db2209986aed4cd4`.
  The first always-on WMMA screen fails short-context performance despite a
  better 32K median. Startup profiling is now captured without a policy change;
  production is restored and the experiment is stopped. Findings and next tests:
  `infrastructure/Analysis/2026-09-05 2026-09-05 Flash Next Whole Model Screen and Prefill Trace.md`.
  Qualify depth gating, replay representative Halo Q4_K/Q5_1 expert matrix shapes,
  and establish transfer dependencies before attempting scheduling overlap.
  A single-cell 64K improvement is encouraging but not promotion evidence.
  Original GPU-routed Q4_K/Q5_1 expert kernels now compile for both AMD targets
  and pass the numerical replay suite, but their first four implementations fail
  the operator speed gate. `moe_prefill` remains opt-in/off; preserve native
  serving. Packed F16 and Q8 activations improved the custom paths but did not
  beat interleaved native controls. The Q8 candidate passes 320 numerical cases
  across both GPUs; full-model quality remains unqualified. Profiling confirms
  its matrix loop remains slower than native. New modes 5/6 retain native MMQ
  arithmetic while replacing route preparation; 240 numerical cases and 56
  timing calls match native output fingerprints on both GPUs. Mode 5 has small
  operator-only point gains; mode 6's small-group sorting adds no convincing
  benefit. Keep both off until full-model qualification. Canonical parameter
  declarations now accept 0..6, but these additions are not deployed to the API.
  Hardware topology is verified end-to-end:
  the upstream external link is Gen4 x4 despite the GPU endpoint's Gen5 x16 report.
  A verified 40-cell synthetic probe measures about 605/236 GB/s local reads on
  R9700/Halo, but only 7.02/6.72 GB/s peer payload transfers. Measure actual
  graph-boundary bytes, sustained behavior and overlap before scheduler changes.
  Evidence: `infrastructure/Analysis/2026-09-05 Flash Next Hardware Roofline and Original Expert Kernel Checkpoint.md`.
  Latest: `infrastructure/Analysis/2026-09-05 Flash Next Measured Transport Limits and Native MMQ Routing.md`.
  The depth-gated combined screen now recovers 4K/8K prefill and improves 64K
  prefill by about 35%; one 32K gateway failure leaves the comparison incomplete.
  Seven short full-model correctness checks pass, not a deep-context qualification.
  Before more expensive engine gates, activate the staged gateway fix and validate
  the reference protocol soak. Also compare Halo-only and hybrid with identical
  runtime/settings: improvements over the old hybrid do not establish OCuLink's
  net benefit. Production is restored; the experiment is stopped.
  Remote experimental parameter views currently show
  declared defaults; expose observed unit/process overrides without treating
  defaults as runtime evidence. This test's actual flags are recorded from
  the server process and journal in the active task.
- Add a Jobs view over gateway usage/admission telemetry: caller class,
  requested model, selected deployment/node, queue time, run time, token counts,
  cache hit rate, and outcome. Include a cheap deterministic route canary from
  the dashboard. Do not store prompts or generated text.
- Add explicit `available` / `draining` / `reserved` node intent. New work must
  avoid a drained node without treating normal sleep, gaming, or interactive
  GPU use as a service failure.
- When the exact same model is independently resident on at least two nodes,
  add eligibility filtering (health, model identity, free slots, GPU pressure)
  and least-loaded placement. Prefer an already-warm model/prefix when safe;
  use declared memory pools and estimated prompt/output size rather than only a
  coarse GPU-utilization average. Keep aging as the starvation bound. Pin each
  request to one node for its full lifetime; the Corsair hybrid process is one
  deployment, not two nodes.

Closed 2026-09-03: single-slot priority admission, cancellation-safe release,
30-second starvation-preventing aging, live queue diagnostics, per-request
queue accounting, and background labeling for Aria-owned inference.

## UI publication and deployment

- Ben must publish the Mac's loopback UI on tailnet HTTPS `:443`; agents are not
  permitted to change Tailscale configuration. After acceptance, remove the
  stale Corsair `:443 -> 127.0.0.1:3000` publication.
- Replace the retired Docker implementation behind the Makefile's `ui-deploy`
  target with a tested, atomic Mac service-tree deployment procedure. Until then
  the target fails with an explanatory message instead of touching the wrong
  host/runtime.
- Add a production build-identity acceptance check to the Mac deployment.

## Control-plane resilience

- Define and test a logical MongoDB export to an off-Mac recovery target; Time
  Machine alone is not a logical database backup.
- Decide the cloud exception rule for Claude Code/Codex and record how exceptions
  are observed when the gateway cannot proxy the provider.
- Define a least-privilege secret distribution model for autonomous node agents;
  Corsair's shared `ben` identity is not a containment boundary.

## Code/data quality

- Type backend/status/kind API boundary fields with enums/Literals and add a test
  that MCP-documented values match dispatch values.
- Schedule or explicitly retire memory confidence decay; the maintenance route
  alone does not implement a policy.
- Add response models for high-use operational endpoints and durable build
  identity for the UI.
- Decide the three retention defaults before lowering any value that triggers
  irreversible TTL deletion.

## Evidence-gated features

- Research/triage/improver phases remain gated by their documented human or data
  thresholds.
- Local-model A3 remains gated by clean A2 merge history and measured tool-call
  reliability.

Dated steward, migration, recovery, and UI plans in the vault are retained as
decision/execution records. Their unchecked boxes are not automatically current
backlog items.
