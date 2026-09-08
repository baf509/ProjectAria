# September 8 delivery review

Scope: outstanding code in canonical ProjectAria, CorsairModelHost and Hermes;
active deployment/client documentation; the explicitly authorized architecture
charter hardware/current-state correction. This is a source/test/security-boundary
review, not a new inference qualification or an exhaustive audit of unrelated
personal documents on every machine.

## Review and verification

- API full suite: 2414 passed, 11 skipped, three test failures. Two older GPU
  tests called the now database-injected public route instead of the scoped
  Corsair probe. The third treated Red's documented off-box model/proxy port
  overlap as an on-box model/service collision. Health already excludes off-box
  models from its deliberately-stopped map; this was not evidence of a masked
  Red outage. Updated these tests to the actual contracts, preserving strict
  on-box disjointness and allowing only the exact documented Red transport.
- Added regression coverage: unavailable Corsair telemetry must not hide Red.
  Reran the affected GPU/service/Red/model suites: 168 passed, one skipped.
  The entire eight-minute suite was not rerun after those narrow test repairs.
- Earlier focused candidate/gateway/client set:179 passed,one skipped.
- Author deployment helpers:188 tests passed. Earlier CUDA/Halo helpers:77
  passed. Engine harness:207 passed. These are CPU/synthetic tests, not new
  model performance numbers.
- UI typecheck and production build passed. Phone/laptop fixture tests:12
  passed, covering loadout gating, start/selection ordering, conflict refusal,
  no unrelated model stop, and Ralph review screens. No live controller was
  approved or started by those fixtures.
- Corrected misleading GPU-panel prose: discrete VRAM does not imply that
  hybrid models are independent of Halo/system memory.
- Candidate routine-start approval uses the explicit known-risk schema and
  exact profile/artifact/evidence hashes. It does not claim a soak passed.
- Reviewed scoped backend credential handling, restricted actuator/device
  identity, client routing, bounded benchmark supervision, and existing Ralph
  approval/containment boundaries. No controller policy or run-state change.
- Scanned255 changed/new files across the three roots for private-key/provider
  token/credential-assignment patterns and accidental runtime-artifact paths:
  no matches; no files above1MiB. Pattern scanning is not a mathematical proof
  that arbitrary prose cannot contain a secret. Raw results/private configs,
  model weights, worktrees, caches and backups remain excluded.

## Documentation and publication

[Current deployment](CURRENT_DEPLOYMENT_20260908.md) and
[topology](LOCAL_INFERENCE_TOPOLOGY.md) supersede older model instructions.
Author-engine status/runbooks and older engine entry points now distinguish
accepted current operation from historical failures/benchmarks. Hermes notes
reflect the actual gateway reload, auxiliary routing and client checks.

Ben explicitly authorized charter updates: Corsair RTX3090/Halo, Red dualR9700,
Ridge RTX5090. Both Mac charter copies were updated with private prior copies
retained; unrelated policy/history differences were preserved. The service-vault
charter update reached Corsair's vault projection.

ARIA published:

- `ProjectAria/Design/2026-09-08 CURRENT_DEPLOYMENT_20260908.md`
- `Hermes/Design/2026-09-08 CURRENT_HERMES_FLASHNEXT_20260908.md`

Both reached Corsair. The ProjectAria note has identical bytes on Mac service
vault and Corsair. The Mac desktop Obsidian application is closed and the new
notes are not yet present in that projection; do not claim full-device sync.
No sync/network settings or human approval policies were changed.

## Remaining delivery boundaries

- Ben-run `bash /tmp/flashnext-activate-accepted-20260908.sh`, then one normal
  ARIA lifecycle check. The current model remains running unchanged meanwhile.
- GitHub authentication is expired. Ben must refresh it before pushes can
  complete. Hermes also lacks a usable push remote; its obsolete local `safe`
  URL is not a publish destination. Do not invent a repository or force-push.
- Ridge hardware inventory is corrected in canonical code/documentation from
  Ben's confirmation. Its new GPU/runtime readiness has not been tested while
  asleep; the candidate-only activation does not deploy unrelated Ridge changes.
- No additional MTP soak, crash investigation or microbatch experiment.

Commits/pushes are not deployment receipts. Check Git and live activation
evidence separately before claiming all work delivered.
