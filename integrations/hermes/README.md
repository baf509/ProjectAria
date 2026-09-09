# Hermes integration — standard upstream

As of September 8, 2026, CLI and Signal share one unmodified
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) Git
installation. Ben explicitly chose standard upstream approval behavior. ARIA
integrations live outside the engine; do not reapply the retired core patches.

## One installation and one data home

| Purpose | Canonical path |
|---|---|
| Upstream application | `/Users/ben/Services/apps/hermes-agent` |
| Python environment | Application `venv/` (`.venv` is a compatibility symlink) |
| Configuration, credentials, sessions, skills and plugins | `/Users/ben/Services/data/hermes-home` |
| CLI entry point | `/Users/ben/.local/bin/hermes` |
| Signal gateway launcher | `/Users/ben/Services/apps/bin/run-hermes-gateway` |
| Existing launchd supervisor | `com.ben.devbox.hermes-gateway` |

`~/.hermes` links to the data home. Its `hermes-agent` entry links to the sole
application, preserving old virtualenv paths without a second installation.
The `Infrastructure/Hermes` repository contains integration sources and notes,
not another Hermes engine installation. Its private origin is
[baf509/hermes-aria-integration](https://github.com/baf509/hermes-aria-integration).
Update the engine from Nous upstream, not from this integration repository.

Installed baseline: **0.21.1**, clean `main`, commit
`c3ce41645cb08f39e7dd5739dcfd72527096896f`, Nous upstream origin. No local engine
commits, approval patches, credential rotation, or model deployment changes.

## Updating

From Ben's normal terminal:

```sh
hermes update --plan
hermes update --backup
```

Run the update between active conversations/approvals. The native updater owns
backup, dependency reconciliation and verification. The gateway carries the
upstream `--external-supervisor` flag so launchd, not a competing detached
gateway, owns relaunch. The read-only plan and native restart-mode selection
were verified against the live gateway. A future-version update was **not**
performed merely to test this procedure. Do not run another installer or edit
engine files; verify Signal, ARIA readiness and model routing after updating.

## ARIA-owned integrations retained

- `hermes` and `run-hermes-gateway` version the two installed launchers. Both use
  the existing dotenv launcher without printing or replacing credentials.
- `aria-readiness/` uses upstream plugin hooks and MCP discovery/reconnect.
  It observes selected tools and refreshes its receipt after recovery, including
  when recovery completes after the startup observer. It does not rewrite history.
- `../..` remains the ARIA control plane. `environment-hint.md` and
  `route-coding-to-aria.py` retain the reviewed ARIA-first coding workflow.
- `Infrastructure/Hermes/plugins/aria-flashnext` supplies scoped request
  middleware: persisted medium reasoning/2048 budget, explicit controls win.
- `tool-selection-evals.json` and `evaluate-tool-selection.py` retain routing
  regression fixtures. `verify-aria-exposure.py` now uses upstream 0.21 module
  imports, batch search/describe arguments and MCP 2 annotation access.

`approval_store.py` here is **historical recovery source only**, not installed
into upstream. Old durable approval records are retained in the personal database;
the old custom operation-ID adapters are retired. No old approval is replayed.

## Verification and recovery

Signal and webhook connected on upstream; the session store is healthy; all
**118 ARIA tools** are registered/selected with no required tools missing.
The actual installed AIAgent passed two read-only ARIA/model round trips through
native discovery. Plugin checks preserve explicit thinking-off and other routes.
This does not claim a new personal Signal conversation or sustained GPU reliability.

The original configuration/credential/SOUL bytes and all **5709 sessions / 26556
messages** were preserved at cutover. Both managed Pi configurations and the
accepted RTX 3090/Halo runtime were untouched.

Private recovery and receipts:
`/Users/ben/Services/backups/hermes-consolidation-20260908-4soEBh/`.
It contains the archived 0.19 runtime, unused legacy home (zero sessions),
original launchers/plugin, consistent pre-cutover SQLite snapshots, and
`final-verification.json`. A full native backup is also retained at
`/Users/ben/Services/data/hermes-home/backups/pre-update-2026-09-08-204104.zip`.
No recovery copy was deleted. Do not publish these private artifacts.

Recovery requires a drained/stopped gateway and deliberate app/data restoration;
never start the archived runtime alongside the active gateway or overwrite a live
database. The first cutover attempt safely rolled back on an import-check timeout.
The successful warm gateway restart took about six seconds; the earlier cold
startup watchdog failure is retained as diagnostic evidence, not concealed.
