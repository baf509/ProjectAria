# Inbox cleanup — September 9, 2026

Ben requested an audit of every Inbox lane and remediation of stale items.
Live API and Mongo state were compared with service health, relay heartbeats,
shell lifecycle, current architecture, project files, and vault write hashes.

| Lane | Before | After |
|---|---:|---:|
| Needs You | 5 | 0 |
| Proposed Tasks | 55 | 2 |
| Review Queue | 24 | 2 |
| FYI | 5 | 1 |
| Soul proposals | 0 | 0 |

Nine alerts were acknowledged with recorded reasons, 53 duplicate/superseded
proposals were dismissed, and 22 review items were acknowledged. Two active
tasks requesting this same duplicate cleanup were marked done. Records were
preserved, not deleted; no human approval or false-raise decision was fabricated.

The relay recovered at 10:13 UTC after the 10:12 UTC outage check, but its open
alert still carried its September 5 creation time. The selfcheck likewise
recovered at 11:29 UTC while its degraded alert remained open. The referenced
Codex sign-in shell is stopped. Other cleared alerts were an informational weekly
report, recovery history, an old Hermes plan notice, and an August spawn refusal.

Four recurring vault-write refusals came from missing provenance at the new Mac
vault root. Each file's full SHA-256 exactly matched its prior recorded ARIA write
under `/home/ben/Obsidian/vault`. The existing records' `aria_hash`, `frontmatter`,
and original `written_at` were restored at the current path with migration
history. File contents and human approval fields were untouched. The guarded
writer now returns `aria-owned` for all four plans. Old account-path review
duplicates and historical retired-service/container removals were also closed.

## Items retained because they remain unresolved

- The original real-device iOS/Android no-look voice playtest. The current game
  commit still explicitly says it is not device-verified.
- Verification/adaptation of the existing local coding-agent evaluation harness
  for current Hermes/ARIA. Its proposal now names the existing evalstack runner
  rather than requesting a duplicate implementation. No model run was started.
- Duplicate ARIA/ProjectAria registry records claiming the same project root.
  Resolving that requires consolidating project attribution, not merely hiding
  the review.
- An unregistered `model-hermes-evaluation` project with a vault charter. The
  review's obsolete nested path was corrected to the existing charter path.
- The dream-worker FYI. Dreaming is enabled, but no cycle has completed for about
  797 hours. Live status reported skipped active hours and a historical Linux
  Claude path. This is a current liveness problem, not resolved history.

The original active human-owned charter task remains active; this cleanup does
not invent success criteria or approve project plans.

## Recurrence and UI fixes

- Relay heartbeats and healthy watchdog ticks close older open relay-dead rows.
- Healthy selfchecks close older degraded rows, including after API restarts.
- Automatic resolution preserves human decisions and historical delivery data;
  later outages create fresh undelivered rows. Timestamp guards protect newer
  failures from delayed recovery updates.
- Weekly reports are informational and do not request human action.
- Inbox alert ages use the latest occurrence, review ages use the latest update,
  and group acknowledgements refresh the queue and count successful responses.

Verification: 76 focused API tests passed. The isolated staged release passed
type checking, UI class lint, production build, manifest verification, and Inbox
layout checks at 375, 390, and 1280 pixels. Browser assertions confirmed the
cleaned lane counts. The first test environment had incompatible Starlette and
FastAPI versions; a separate test environment using the deployed dependency
versions passed. Production dependencies were unchanged.

The staged release starts from the currently deployed manifest and includes only
these Inbox/notification changes, preserving unrelated working-tree changes:

`/Users/ben/Services/releases/ProjectAria/20260909T121644Z-52c814f-d76cddc6afbb`

Activation stopped at `sudo -n true`, before any live launcher, release pointer,
or service change. To activate from Ben's Terminal:

```sh
cd /Users/ben/Development/Infrastructure/ProjectAria
sudo -v
scripts/aria-deploy-mac activate /Users/ben/Services/releases/ProjectAria/20260909T121644Z-52c814f-d76cddc6afbb
scripts/aria-boot-check --wait 120
```

The database/provenance cleanup is already live. Until activation, the old
notification code can still leave a future recovered incident open.

Detailed before-state snapshots, per-item reasons, logs, and a browser screenshot
are retained under `/Users/ben/Services/backups/aria-inbox-audit-20260909`.
Mongo `operational_events` records `inbox_stale_cleanup` and
`inbox_stale_cleanup_refinements` with the individual changes.
