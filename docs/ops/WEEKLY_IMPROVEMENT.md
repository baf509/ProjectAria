# Weekly platform improvement

The weekly review extends Steward/Improver. It reviews Hermes messages, Aria outcomes,
tasks, usage and rotated logs, and inspects only the registered assistant-platform
repositories. Thursday at **04:00 America/New_York**, one fresh watched Codex shell
gets at most **two hours**, with ten minutes reserved for reporting, and up to three
separate fixes. There is no spending cap. The previous Improver timer is suppressed
when this mode is enabled; its proposal history and APIs remain available.

## Authorization and execution

The operator-owned policy grants automatic implementation, verification, merge,
deployment and rollback only for exact registered paths and checks. The worker cannot
expand this scope. Historical messages and logs supply evidence, never authorization.
Each candidate needs a registered acceptance check that fails against its baseline,
passes against the candidate, and passes all registered regression checks. Missing
checks, uncertain findings and unsupported deployment targets become recommendations.
A verified candidate is retained even when deployment privilege is unavailable.

The watched tmux shell hosts the finite trusted controller. It uses Codex app-server
**0.154.0** with structured actions, no native tools, no inherited MCP servers, and no
native environments. This refines the specification's proposed `codex exec` transport:
the existing app-server integration provides an enforceable separation between model
requests and execution. There is no interactive continuation or provider fallback.
The existing Loop backend remains pinned to its separately qualified 0.153.2 contract.

Model-requested commands execute in credential-free Docker containers with no network,
no host mounts, no Git metadata and bounded lifetime. Reviews and trusted checks have
read-only workspaces. The controller owns private Git checkpoints, frozen check assets,
merge locks and hash-pinned deployment adapters outside every target repository.
A dirty destination or advanced branch blocks promotion. One run owns the platform slot;
Mongo generations fence writes and a physical lock prevents taking over a live owner.

## Prepare the Mac

Run as `ben` from a reviewed checkout:

```sh
scripts/aria-weekly-setup --destination ~/.aria/weekly-platform-controller-VERSION
```

This builds an API check image, records its immutable Docker image ID, freezes five
existing behavioral suites, installs trusted adapter/controller copies and writes
`policy.json`. It does not enable the schedule. An existing image can be supplied with
`--image sha256:...`. Use a new destination for a new policy version; never overwrite
assets while a run is using them.

The initial automatic target is the declared Aria usage, memory, outcome, pruning and
context implementation files. Hermes plugins/configuration and Corsair model-host source
are reviewed, with unsupported changes reported. Other projects are not targets. Add a
new automatic surface only with its exact paths, independent acceptance/regression
contracts, a qualified adapter and restoration evidence. In particular, the generic
`json_fields` adapter does not grant permission to change every model setting.

Install the restart entitlement **once in an administrator terminal**:

```sh
~/.aria/weekly-platform-controller-VERSION/scripts/install-aria-restart-helper
```

The root-owned helper allows only `check` and `restart` for the fixed Aria API/UI/node
and Hermes gateway service IDs. It rejects arbitrary services/commands, validates
root-owned launchd plists that run as `ben`, and never executes candidate code as root.
Normal `aria-deploy-mac activate`, rollback and `aria-deploy-current` use the same helper.
They require no `sudo -v` or password after installation. The helper and exact sudoers
rules live outside application releases and persist across reboot and upgrade.

After manual report-only, repair, rollback and delivery qualification, set these in the
service-owned environment and activate the reviewed Aria release:

```dotenv
WEEKLY_IMPROVEMENT_ENABLED=true
WEEKLY_IMPROVEMENT_POLICY_FILE=/Users/ben/.aria/weekly-platform-controller-VERSION/policy.json
WEEKLY_IMPROVEMENT_STATE_DIR=/Users/ben/.aria/weekly-improvement
```

Startup registers the Thursday schedule and a separate fifteen-minute regression watch.
Existing schedules keep their previous timezone interpretation. A missed Thursday is one
catch-up review. Duplicate ticks/manual triggers return the same occurrence or active run.
Disabling the feature prevents new runs; cancel an existing run through its API.

## Deployment and recovery

`scripts/aria-weekly-deploy-adapter` supports `preflight`, `apply`, `health`, `rollback`
and `reconcile`, with a private JSON request and an operator-owned JSON registry.

- `aria`: stage a clean clone at the exact verified revision through the existing release
  controller; check idle inference, restart capability, manifest hashes and readiness.
- `json_fields`: change only declared JSON fields with exact types/ranges/choices,
  preserve unrelated live fields, and refuse source/live drift.
- `files`: publish exact source-to-destination mappings with backups and fixed, hash-pinned
  preflight/restart/health commands. Register this only after consumer-specific qualification.

Each adapter persists intent and restoration bytes before writing. The watched controller
survives API/node restarts. Crash recovery reconciles receipts before attempting any new
mutation; it restores interrupted configuration writes only if every byte identity is
still owned by that transaction. Later human deployments or edits block rollback.

The watch lasts 72 hours. A health probe must return measured `samples`; insufficient
samples are **unmeasured**, never clean. The initial Aria readiness probe measures
availability only, so it cannot claim a quality improvement. Register a target-specific
quality probe before using quality measurements to qualify that target. Watch changes
update the existing report. An ambiguous notification acknowledgement is retained for
inspection rather than blindly resent.

## Use and inspect

Aria's **Autonomy → Weekly improvement review** card shows scheduling, reports and watched
shell links, and provides run/report-only/cancel controls using the existing admin key.
Read routes retain ordinary API authentication; mutations require the operator admin key.
The general schedules API applies the same admin boundary to improvement actions.

- `GET /api/v1/improve/weekly`: configuration, active/latest run and next occurrence.
- `GET /api/v1/improve/runs` and `/runs/{id}`: durable results and delivery state.
- `GET /api/v1/improve/runs/{id}/report`: report body and publication receipts.
- `POST /api/v1/improve/trigger`: `{ "policy_id": "platform", "report_only": false }`.
- `POST /api/v1/improve/runs/{id}/cancel`: cooperative cancellation.
- `GET /api/v1/improve/findings`: prior findings and dispositions.
- `POST /api/v1/improve/findings/{id}/disposition`: status and feedback reason.
- MCP `weekly_improvement_status`: read the same status or one run.

Reports live in Mongo even when vault publication fails. `ObsidianWriter.upsert_managed`
uses one deterministic `ProjectAria/Analysis/Weekly improvement {id}.md` note per run,
preserves human edits, and retries delivery without another Codex review. The existing
Signal daemon sends the requested short summary and Aria link, with no raw transcript
and no fabricated `needs_human` classification.

State is under `~/.aria/weekly-improvement`: evidence, container receipts, candidates,
reports and deployment requests are private controller artifacts. Raw Hermes SQLite is
opened read-only in a consistent transaction; coverage records truncation, gaps and
unavailable sources. Redaction runs before model input and report publication.
