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
  The reviewed registry/routing files are staged in the live service tree;
  activate them with a Mac administrator API restart before live qualification.
  Promotion still requires matched depth-ladder performance, model correctness,
  cache, and mixed Hermes/Pi soak gates. Task: `6a9b9412db2209986aed4cd4`.
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
