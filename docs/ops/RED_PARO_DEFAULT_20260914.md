# Red PARO int5 default — 2026-09-14

Ben selected `Red-Qwen3.8-27B-PARO-INT5` as the Red and Hermes default. The
production API/UI/node release is `c67127d-c4c1df95047e`; independent MCP release
`20260914T101143Z-08d03b3fbc0a` exposes the PARO selector and verifies all three
Red choices. The native model uses the existing Mac 8094 private SSH forward
to Red loopback 8081. Its verified wire identity remains
`red-qwen3.8-27b-paro-int5-isolated`; observed geometry is 262144 context and eight
slots. All weights and mutable runtime directories remain isolated.

Hermes main/provider defaults and previous Red auxiliary references select PARO.
Both managed Pi catalogues (Mac and Corsair) offer PARO. Mac Pi and the ARIA
`pi-coding-red-qwen38-27b` profile select PARO; Corsair Pi retains its separately
selected Flash Next default. Existing explicit alternatives and saved conversation
selections remain available.

## Source and deployment ownership

The tested implementation is committed in this canonical Git repository on branch
`red-paro-default-20260914`, checked out at
`/Users/ben/Development/Infrastructure/ProjectAria-red-paro-default`.
Its commits are `9595246`, `31cd781`, `c67127d`, and `6d75f3d`.
The release was based on the installed production API and separately installed
MCP baseline, preserving other pending work in this main checkout.

Before the next deployment from this main checkout, reconcile that branch's
registry, observer, Red controls, MCP selectors, Pi catalogue validator and
verification changes. Do not deploy the older main registry over the promoted
production catalogue. Do not overwrite pending local changes to do the merge.
Use the normal manifest-pinned API and independent MCP deployment tools.

## Evidence and recovery

ARIA task: `6aa7bf6de93eb203d99cb36d`. Hardware artifacts, checks and live receipts:
`CorsairModelHost/red-r9700/paro-int5-isolated/{promotion,results/promotion}`.
The production functional gate passed instruction, JSON, tool invocation,
tool-result and vision checks. Actual Hermes and both Pi CLIs returned success.
Red's PARO user unit is enabled for boot; the old Radiance unit is disabled but
retained with its original model. The evaluation gateway and its private forward
have been retired. Production uses only the standing ARIA route and forward.

Rollback instructions and exact checkpoint/runtime pins are in the hardware
promotion runbook. Private client backups are under
`/Users/ben/Services/backups/red-paro-default-20260914`; the previous API release
receipt is `/Users/ben/Services/backups/aria-release-20260914T100444Z/activation.json`.
Stop the current model before loading an alternative through ARIA. Never force
an active request or restore an old configuration over subsequent edits blindly.

The benchmark favored the previous MXFP4 for speed. Its two-problem HumanEval+
lead was inconclusive; no KL comparison was measured. PARO is the user's chosen
default, not a claim of demonstrated coding-score superiority.
