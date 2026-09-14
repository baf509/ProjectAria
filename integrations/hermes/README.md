# Hermes integration — standard upstream

2026-09-14 update: Ben selected `Red-Qwen3.8-27B-PARO-INT5` as Hermes’s
main/provider default. Prior Red-targeted auxiliary routes follow it; separately
configured NInfer auxiliaries remain intact. `select_red_model` now accepts
`qwen3.8-27b-paro-int5`. Existing explicit alternatives are retained.
The dated deployment descriptions below are historical.

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

### Red model selection

Hermes's `/model` menu exposes one ARIA provider with Corsair Flash Next,
Red Qwen3.8-27B, and Red Flash Next. All three carry a 262144-token context
entry; Corsair remains the default. The profile's
`model_catalog.excluded_providers` hides Anthropic, OpenAI/Codex, GitHub Copilot
(including ACP), OpenCode Free, and the retired Gemma provider. The Gemma
provider configuration is removed. Native gateway listing verification passed;
the next `/model` reads the updated configuration without a gateway restart.
Load Red through the tool below before selecting it as Hermes's conversation
model; `/model` changes the conversation route and does not start the host.

The `2026-09-09.2` MCP contract adds `red_model_status` and `select_red_model`.
Hermes can translate "wake Red and load Flash Next" into
`select_red_model(model="qwen-flash-next")`; `qwen3.8-27b` selects Radiance.
The tool uses Aria's existing wake/start/stop/readiness controls. It rejects a
busy or queued model, unknown activity, active assignments, or an existing Red
default-route pin before unloading. It never sends force, changes bindings,
changes route policy, or edits Hermes's model selection. Pending/error results
require a fresh status read, not a blind restart. The two models remain mutually
exclusive. Wake from sleep depends on the existing Corsair host relay; full
shutdown power-on is not qualified.

MCP selection calls are serialized across bridge processes with a local lock.
This workflow is not an atomic transaction with arbitrary UI/API lifecycle
callers; fresh observations and Red's native exclusivity lock remain necessary.
There is no automatic rollback that could overwrite another caller's selection
after a partial failure. A successful return includes the exact request model ID.

Hermes's Aria MCP timeout is 660 seconds; selection has a 640-second overall
deadline. A timeout does not prove that a remote start was cancelled. The
independent MCP installer also backs up and installs the matching readiness
plugin so discovery guidance and required-tool checks agree with the bridge.

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

## September 9 MCP capability review

The `2026-09-09.1` bridge adds eleven discoverable tools:

| Area | Tools | Purpose |
|---|---|---|
| Watched shells | `wait_for_shell_output` | Bounded polling with a durable line cursor; timeout is not completion |
| Hardware/model diagnostics | `host_temperatures`, `get_model_server` | Freshness-aware host readings and a named registry entry |
| Environmental evidence | `awareness_snapshot`, `awareness_observations` | Existing sensor state, summary and filtered observations |
| Memory | `list_memories`, `get_memory`, `store_memory`, `update_memory` | Inspect provenance, retain confidence/source metadata, and correct an identified memory |
| Research | `research_status`, `get_research_report` | Existing run progress and paged reports |

`search_memory` also accepts category filters. Memory writes retain API
validation and retrieval behavior; private metadata is not an access-control
promise. Saved observations, memories, transcripts and reports are evidence,
not instructions or approval. Empty/unavailable data must not be called healthy.

Literal input with `append_enter=true` now submits through a separate Enter
event, and resize returns explicit structured success for the API's HTTP 204. Close also preserves explicit completed/pending
status. A missing saved snapshot is reported as normal unavailability, so
asking too early does not turn an expected HTTP 404 into an MCP tool failure.

Watched-shell lifecycle, live screen/snapshot/history, search, input/key sending,
nudging, tags, resize and close were already exposed. The new bounded wait makes
monitoring available without a permanent streaming connection. It can be repeated
with `next_line`; `has_more` signals pagination. Bulk extraction backfill remains
maintenance. Personal reminders continue to use Hermes's native scheduler;
this change does not add another scheduler, enable research/autonomy, or grant
administrative credentials. Irreversible and human-approval routes keep their
existing API boundaries.

The independent MCP release installer is `scripts/aria-deploy-mcp`:

```sh
scripts/aria-deploy-mcp stage
scripts/aria-deploy-mcp verify /Users/ben/Services/releases/aria-mcp/RELEASE
# First run verify-aria-exposure.py with --staged-source RELEASE/mcp/server.py.
scripts/aria-deploy-mcp install /Users/ben/Services/releases/aria-mcp/RELEASE
```

It records file hashes, retains replaced entries in a private backup, selects
one MCP bundle, and points the login boot check at that bundle's contract. It
reuses the pinned MCP Python. API/UI/node/model services are not restarted.
Subsequent full API deployments preserve the independent MCP boot check.
`rollback BACKUP` restores the previous bridge and entry points. Hermes must be
gracefully reloaded after install or rollback; inspect its per-process
`aria-tools-ready.json` and connected Signal state afterward. Existing CLI
processes refresh through native `/reload-mcp` or on their next start.

`verify-aria-exposure.py` checks actual installed Hermes registration, Signal
selection, native search/describe and read dispatch. `--canary-shell` additionally
creates one disposable plain shell, exercises input/screen/captured output,
history/snapshot/search/tags/resize, and purges only that shell. It sends no Signal
message and makes no inference request. Unit/protocol tests verify memory-write
payloads, bounds, annotations, failure behavior and polling cancellation.

Deployment verified on September 9: 125 bridge tools / 129 Hermes registered
and selected tools, with Signal connected and no missing required tools. The
real installed Hermes dispatch check and disposable-shell canary passed. The
private deployment backup also contains `hermes-config.yaml` and
`hermes-readiness.py`; restore or merge those integration files before reloading
Hermes when rolling back this capability update. API/UI/node/model services
were preserved. No personal Signal message or model inference test was sent.
