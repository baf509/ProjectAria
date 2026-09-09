# Documentation reconciliation — September 8, 2026

Ben requested correction of the current deployment, publication and Hermes
documentation. This was documentation work only: no model, service, driver,
credential, controller policy or autonomy setting changed.

## Updated entry points

- ProjectAria README, current deployment, Hermes installation/update runbook,
  and architecture pointer.
- CorsairModelHost README, current-state mirror, hardware catalog and reboot
  checklist. Old R9700/DeepSeek recipes are explicitly historical, not executable
  instructions for the current RTX 3090 host.
- Hermes integration handoff: the sole Nous upstream engine and the separate
  private `baf509/hermes-aria-integration` repository are distinguished.
- Vault charter v0.5, under Ben's explicit scoped authorization: one upstream
  Hermes installation, standard upstream approvals, published code baselines,
  and current-document links. Existing policy and historical entries are preserved.

The previously pending code publication is recorded as a pinned baseline in
[CURRENT_DEPLOYMENT_20260908.md](CURRENT_DEPLOYMENT_20260908.md), not as a promise
that all subsequent documentation commits have already reached GitHub.

## Fresh notes published through ARIA

Paths below are relative to each vault root:

- `ProjectAria/Design/2026-09-08 CURRENT_DEPLOYMENT_20260908 215440.md`
- `ProjectAria/Design/2026-09-08 ARCHITECTURE_CURRENT_20260908.md`
- `ProjectAria/Design/2026-09-08 START_HERE_CURRENT_20260908.md`
- `Hermes/Design/2026-09-08 HERMES_SINGLE_UPSTREAM_20260908 215441.md`
- `Hermes/Design/2026-09-08 CURRENT_HERMES_FLASHNEXT_20260908 215442.md`

The charter and repository entry points link these current notes. The older
unsuffixed architecture/orientation notes and generated snapshots were not
clobbered; their retired model and custom-approval descriptions are superseded.
This is not a claim that every historical document in the vault was rewritten.

## Verification and recovery

The charter, five fresh notes and the previously missing single-upstream runbook
were byte/hash-verified in all three projections:

- Mac service: `/Users/ben/Services/data/obsidian/vault`
- Mac desktop: `/Users/ben/Obsidian`
- Corsair: `/home/ben/Obsidian/vault`

The charter SHA-256 is
`e81d3e8683ef3af2aa3ddcb2a446c9bed76fd735d396b1612378070b78738223`.
The updated model-host `CURRENT_STATE_20260908.md` was also deployed and
hash-verified in Corsair's existing author-runtime directory. All replaced
projection files had matching reviewed preimages; concurrent edits would abort.

Private local backups/receipts:
`/Users/ben/Services/backups/docs-reconcile-20260908-WSPeX0/`.
Remote Markdown backups/receipt:
`/home/ben/Development/infrastructure/flashnext-author-reproduction/results/docs-reconcile-20260908-rjrw6ml5/`.

The NAS CouchDB hub remains sync authority. File agreement at this checkpoint
does not certify continuous LiveSync health or change the transitional vault
Git-history writer. No archive, recovery artifact, transcript or secret was
deleted, committed or copied into the vault.

ARIA tracking task: `6aa0ba8978c0c535358ce1c6`.
