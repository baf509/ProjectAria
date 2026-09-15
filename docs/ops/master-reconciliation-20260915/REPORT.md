# ARIA master reconciliation — 2026-09-15

Ben requested that the deployed repair history and existing uncommitted ARIA work all be merged into `master`. The integration retains both histories, including the older isolated qualification gateways and reports. It does not redeploy or restart the current services.

## Preserved work

- Canonical master before reconciliation: `4ceefdb`.
- All nine pending canonical files were snapshotted byte-for-byte, then committed as `eb5e71e` on an isolated integration branch. This includes the PARO-MXFP4 registry, tests and MCP selection work, changelog, Hermes readiness hint and GPTQ assessment.
- Deployed repair branch through `440d776` was merged with full history, including its PARO-int5, NInfer-3090, Halogen and browser/tool repair ancestors.
- Recovery snapshot: `/Users/ben/Services/backups/aria-master-reconciliation-20260915`. It contains the original HEAD, tracked/index patches, file copies and SHA-256 manifest. A named recovery stash is retained when advancing the canonical checkout.

## Conflict resolution

- Both PARO variants remain distinct: `qwen-paro` selects PARO-MXFP4; `qwen3.8-27b-paro-int5` selects the promoted default. Original MXFP4 and Flash Next remain explicit alternatives.
- Registry tests, MCP schemas, Hermes hints/exposure verification and UI controls agree on the four Red variants. Switching from PARO-MXFP4 unloads it before loading int5.
- Fresh Red Pi profile seeding uses PARO int5 while preserving any existing stored profile.
- Red observation matches runtime geometry by model identity instead of relying on model-list position.
- The production PARO-int5 exclusivity list contains only registered production siblings. The GPTQ qualification option remains confined to its isolated gateway; it is not silently added to production.

## Verification

Verification passed: **448 API/contract tests, 28 phone/laptop model-switch tests, production UI build, TypeScript check and UI class lint**. All nine original pending files match their committed recovery snapshot. See `verification.json` for counts and recovery references. API tests cover registration and exclusivity, routing, both PARO selections, agent seeding, tool policy/validation, steward behavior, MCP exposure and deployment contracts. UI verification uses an isolated loopback server with intercepted API fixtures; it does not start or stop real models.

The active service release remains `029986d-1a274be96a53`. Source-only changes added by this reconciliation require a deliberate deployment if they are to become live. Retained repair/deployment worktrees and backups provide recovery references; there is no need to delete them to achieve a clean canonical `master`.
