# Loops

Loop turns an **operator-approved plan** into independently checked local Git
checkpoints. The controller chooses one eligible task, starts a new logical
conversation, stops the worker, verifies an immutable candidate, and accepts or
retains it. A model report cannot mark a task verified.

This is separate from the older `coding_loop_*` idle-nudge feature in the watched
CLI sessions. That feature resumes a conversation and recognizes a completion
marker; it is not an acceptance workflow. Loop uses a small tool-enabled harness
over Aria's existing `LLMManager`, or the controlled Codex connection below.

## Integration and setup

Implementation: `api/aria/loop/`; API: `/api/v1/loop`; UI: **Supervise → Loops** (`/supervise/loop`). Mongo holds runs, tasks, attempts, ownership, budgets,
and evidence. Existing `spawn_bg`, guard events, provider adapters, API auth,
session admin-key controls, and cockpit components are reused.

Loop is disabled by default. Set these **API process environment variables**:

```text
LOOP_ENABLED=true
LOOP_POLICY_FILE=/Users/ben/.aria/loop-policy.json
LOOP_STATE_DIR=/Users/ben/.aria/loop
LOOP_DOCKER_BINARY=docker
```

These fields use Aria's existing `Settings` environment and `.env` loading.
Deploy the source and set the service configuration deliberately; editing this
checkout does not activate the production service. No deployment is automatic.

Use an existing trusted Docker engine on the control-plane Mac. The existing
Lima engine can be selected with
`DOCKER_HOST=unix:///Users/ben/.lima/mongot/sock/docker.sock` if it is available.
Loop copies files through the Docker API into private volumes; no Mac home or
source-tree mount into the VM is needed. The engine must support init, private
PID/network namespaces, resource limits, read-only mounts, and `no-new-privileges`.
There is no unsandboxed fallback. Containers run on the configured engine, while
all orchestration, Mongo access, model calls, and Git acceptance remain on the Mac.

Preinstall a trusted, Linux-compatible toolchain image containing `/bin/sh`,
`/bin/chown`, `sleep`, and the project's offline build/test dependencies. The
policy requires its immutable `sha256:...` image ID or a digest-qualified image
reference. Loop never pulls an image or installs dependencies from the network
during execution. Images must not contain secrets or application credentials.

The operator-owned JSON policy maps project IDs to **exact Git repository roots**
and trusted check definitions. Policy, trusted assets, and state must be outside
the repository. API clients supply project IDs and approved check IDs, never
arbitrary filesystem roots or controller shell commands.

```json
{
  "projects": {
    "calculator-example": {
      "repository": "/Users/ben/Development/AgentWorkspaces/loop-example",
      "image": "sha256:REPLACE_WITH_64_HEX_IMAGE_ID",
      "assets": "/Users/ben/.aria/loop-checks/calculator-example",
      "checks": {
        "addition": {
          "argv": ["python3", "/checks/check_addition.py"],
          "timeout_seconds": 30,
          "version": "1"
        }
      },
      "regression_check_ids": ["addition"],
      "final_check_ids": ["addition"],
      "protected_paths": ["AGENTS.md", "CLAUDE.md", ".github/"],
      "allowed_backends": ["llamacpp"],
      "memory_mb": 2048,
      "cpus": 2
    }
  }
}
```

The policy digest includes verifier definitions, image identity, project scope,
and trusted asset contents. Changing them invalidates existing run approvals.
Update check versions when changing acceptance. Protect relevant in-repository
test/configuration paths explicitly using `protected_paths`. A trailing `/`
means a directory prefix; other scope entries mean exact files. No glob syntax.

`llamacpp` uses Aria's configured identified gateway by default, including local
and OpenAI-compatible deployments. Other existing `LLMManager` backends can be
explicitly allowed by policy; their existing credential and routing configuration
still applies. There is no automatic vendor switch or cloud fallback. Provider
credentials stay in the controller. Only model content and scoped tool output
flow through the adapter; worker and verifier containers receive no API keys.

### Stopped attempts carry forward what they established

An attempt stopped at a context, turn or wall-time limit never reaches its
completion report, so before this change the next attempt inherited only the
exception text and could repeat the approach that had just run out of room.

The controller now makes one bounded salvage call on that path. It reads the
stopped attempt's own recorded model output, asks the run's **already-approved
backend and model** to state what the attempt was pursuing, what it established
or ruled out, how far it got and what remained, then prefixes the stop reason to
that summary as the next attempt's handoff. The handoff stays within its existing
2000-character bound. Backend dispatch is the worker's, identical to execution, so
a Codex run is summarized by Codex and never silently by another provider.

This is advisory context, exactly like a worker's own handoff. It is untrusted,
never reaches verification or acceptance, and cannot mark anything verified. The
salvage prompt states that the transcript is data rather than instructions and
must not be used to propose weakening acceptance criteria, checks or scope.

Bounds and accounting:

- It is a controller call, not a worker turn, so it never consumes the turn limit.
  Its reported tokens are added to attempt and run usage; a total the provider
  does not report increments `unknown_calls`, so a finite token limit still fails
  closed rather than under-counting.
- It is bounded by `min(120, attempt_seconds)` and runs at most once per stopped
  attempt.
- It is skipped when the attempt logged no model output, and when the run is
  cancelled or an emergency stop is active. Every failure is recorded as a
  `salvage_failed` log and leaves the original stop reason as the handoff,
  unchanged; the run continues either way.
- An attempt stopped before its first model output has nothing to summarize, so it
  keeps the bare stop reason. This is the expected outcome, not a failure.

**Codex.** `codex` has no `LLMManager` adapter, so its salvage runs over the same
app-server interface as execution: a new private stdio process, a new ephemeral
thread, one tool-less turn, then discarded. It neither resumes nor forks the
stopped worker thread. The turn sends no `outputSchema`, because the worker's
Step schema would force a summary into an action the salvage call is not permitted
to take; native environment access, tools, apps, hooks, MCP servers and delegation
stay disabled exactly as for a worker thread. Its reported thread total is the
call's own cost, since the thread serves one turn.

Planning attempts are unaffected: a planning failure still blocks the run.

### Codex workers

Allow `codex` in a project's `allowed_backends` and select, for example,
`{"backend":"codex","model":"gpt-6-astra","reasoning_effort":"xhigh"}`.
The existing `CODEX_BINARY` setting selects the installed CLI and its existing
account authentication. No authentication files are copied into containers.
Reasoning effort is optional; omitting it uses the CLI's configured value.

The worker creates a private stdio app-server process and a new ephemeral thread
for **each attempt**. It neither resumes nor forks an existing conversation.
Successive controller turns retain that thread and receive compact tool results.
The CLI only proposes structured actions. Native environment access is disabled
on both the thread and its turns; native tools, apps, hooks, plugins, configured
MCP servers and delegation are disabled. Only Loop executes repository commands,
through the same bounded Docker workspace as other workers. Provider thread IDs
are recorded in `provider_session` logs alongside the attempt's session ID.

This connection is qualified for **codex-cli 0.153.2** and fails closed on other
versions because the environment-disable protocol is experimental. Validate a
CLI upgrade before updating that pin. It uses the documented
[app-server interface](https://learn.chatgpt.com/docs/app-server).
The hard turn limit counts controller-driven Codex turns, not Codex's private
internal inference calls. Wall time and attempt limits remain hard bounds;
reported tokens accumulate across turns and attempts, and missing accounting is
unknown. The Codex reasoning process is terminated before the workspace is
exported or verified. No native repository tools run in that host process.

An already-running watched Codex shell cannot be converted into a fresh attempt
in place. First obtain a safe handoff, preserve its uncommitted files and live
operations, and register the target with appropriate trusted checks. The initial
Docker verifier cannot qualify an external GPU deployment: remote source/build,
profile, assets and process identity need their own trusted verifier integration.
Do not enable the old idle-nudge loop as a substitute, silently import a dirty
tree as accepted work, or label CPU checks as GPU qualification.

Routine tests use deterministic Codex conversations. The opt-in test below uses
the existing Codex account, a real temporary Git repository and Docker checks:

```sh
cd api
LOOP_CODEX_INTEGRATION=1 LOOP_TEST_IMAGE=sha256:YOUR_INSTALLED_IMAGE_ID \
  python -m pytest tests/test_loop_codex.py -k live_codex -s
```

### Flash Next preparation activation — 2026-09-06

Project `flashnext-control-preparation` is enabled on the Mac API, with run
`72323e7cc9de4171b2e0750dec9e34d2`. The existing watched session
`claude-codex-projectaria-05b2c4` reached a safe handoff after its finite MTP-off
control. Its shell/history remain intact; `no-nudge` prevents the legacy sweep
from restarting it during the handoff.

The original watched agent subsequently resumed its separately authorized
engineering work at 21:17 UTC, while the Loop run was exhausted. It has newer
canonical runtime/tests and is preparing the remote control. The final Loop
attempt remains isolated on the earlier snapshot and does not supervise that
session or remote job. Review its checkpoint against those newer changes before
any application; never overwrite the canonical workspace with the snapshot.

The run uses Codex `gpt-6-astra` / `xhigh`, with three total/per-task attempts,
900 seconds per attempt and 7200 seconds total. The initial 12-turn / 200000-token
limits stopped two inspection-only attempts, with no accepted changes. Tool
output now exposes truncation and remaining turns explicitly. An admin amendment
on the same run permits 30 turns, 600000 cumulative reported tokens and 100000
context characters for its third and final attempt. All 202131 previously
reported tokens, 14 turns, two attempts, evidence and the original overall
deadline carry forward. These are bounded activation settings, not performance
recommendations.

**Observed outcome:** the final attempt exhausted 100000 context characters
before submitting its completion report. The run stopped as `budget_exhausted`
with `total_attempt_limit`: three attempts, 30 cumulative turns and 567400
reported tokens. No authoritative verification ran and no candidate was
accepted. The worker's 76 passing development tests do not change that status.
Its runtime/test changes and completed runbook are retained under
`~/.aria/loop/72323e7cc9de4171b2e0750dec9e34d2/task-prepare-mtp-graph-control`.
All worker containers stopped, ownership was released, and no further retry or
budget reset was scheduled. `followup-limits/final-run.json` and
`final-logs.json` preserve the outcome. This activation exposed a real task-size/
context limit; it is not a successful Flash Next acceptance demonstration.

Before another approved run, compare the newer canonical implementation and
retained candidate. The original agent has already implemented this switch.
Avoid another duplicate implementation task. If work remains, separate code
acceptance from the operational runbook and supply only the relevant context;
remote GPU acceptance still needs an independently trusted integration.

This run implements **one preparation task**: an opt-in CUDA-graph diagnostic
switch with unchanged defaults, development tests, and a bounded MTP control
runbook. Its 30 frozen runtime regression tests and five independently authored
preparation checks run in the existing isolated container runtime. The unchanged
baseline passes regression and fails the new acceptance checks, as expected.
Passing the task and final checks creates a local checkpoint and leaves the run
in `human_review_required` for the GPU experiment. It does not qualify or deploy
the runtime, launch a GPU test, or complete the wider delivery plan. Remote GPU
verification/actuation remains an explicit integration gap, not a passing check.

The isolated Git root is
`/Users/ben/Development/AgentWorkspaces/flashnext-loop-20260906`.
Its starting revision `6eaf3a15123ec7075edcd3009aefd3d0f993ee1b` is an **unverified
external handoff snapshot**, with file hashes and original revision recorded in
`HANDOFF_PROVENANCE.json`. It contains the runtime module's uncommitted source,
operating instructions and compact handoff. Original files, ignored build/source
trees, measurements and both canonical Git repositories were preserved. The
snapshot is not acceptance evidence. Checkpoints contain only the run's scoped
delta and are not automatically applied back to the canonical repository.

Trusted checks live in
`/Users/ben/.aria/loop-checks/flashnext-control-preparation`, outside the worker.
The operator policy protects the snapshot's profile, source/assets/toolchain pins
and lifecycle files. Loop has no remote actuator access and makes no changes to
the deployed model. The original engineering session owns subsequent GPU work;
consult its current evidence for live deployment state. September 7 delivery
target / September 8 closeout remain.

The Codex extension's release, rollback files, policy snapshots and JUnit reports
are retained at `/Users/ben/Services/staging/loop-codex-20260906T205515Z`.
Validation: 155 regression tests passed (one paid test skipped), then 14 Codex
tests passed with the live integration enabled, including real Git/Docker
acceptance and a host-file containment canary. The follow-up context/limit change
passed 37 tests (two live tests skipped), including restart and cancellation
checks, and the live limits route rejects requests without the admin key. Only
Loop runtime/routes and the existing `CODEX_BINARY` setting changed in the
deployed API; the existing launchd job restarted gracefully. Follow-up backups,
hashes, JUnit results and before/after run state are in `followup-limits/` within
that release. The existing Loop UI requires no new build.

Startup creates indexes on `loop_runs` and `loop_logs`. `loop_targets` uses
Mongo's unique `_id` for repository reservations. There is no data backfill,
second database, queue, or requirement for Mongo replica-set transactions. A run
embeds bounded task and attempt state so accepting the task and advancing the
accepted revision are one compare-and-set update. Full bounded log records live
in `loop_logs`; the run's durable event journal is projected into `guard_events`.

### Mac activation verified on 2026-09-06

The deployed Mac API now enables Loop for **`calculator-example`**. Open
<http://127.0.0.1:3000/supervise/loop> through the existing cockpit. Other
repositories require their own allowlisted policy and trusted acceptance assets.
The source configuration still defaults to disabled.

- Repository: `/Users/ben/Development/AgentWorkspaces/loop-example`.
- Policy: `/Users/ben/.aria/loop-policy.json`.
- Trusted checks: `/Users/ben/.aria/loop-checks/calculator-example`.
- Durable files: `/Users/ben/.aria/loop`; records use the existing `aria` Mongo database.
- Engine: the existing Lima `mongot` Docker socket described above. The example
  uses the Python 3.9 toolchain in the already-present community MongoDB image,
  with Loop's overridden entrypoint, pinned to
  `sha256:379fc543bf3b60c8f815b7b53053fd4ae884056f797d4e989c78cf544c7bb8b4`.

Live run `fd654cc837364704a743f82144582d0e` used the explicitly selected, already
running `Qwen3.8-Flash-Next-CUDA-Halo-Candidate` through the identified gateway.
It reached `ready_for_review` in one attempt, six model turns, and 35.376 seconds,
with 9060 reported tokens. Independent task and final checks passed for the same
commit, `12e0fbae0b2ca1c8433283524a47bdfd407b1300`. Its checkpoint repository is
`/Users/ben/.aria/loop/fd654cc837364704a743f82144582d0e/checkpoints.git`, branch
`loop/fd654cc837364704a743f82144582d0e`. The source repository remains at its
original baseline, with a clean working tree.

The earlier resident-alias run `e31a4965d6c84ada9a1592f89af0fcd1` reached Gemma
because the pinned fleet default was unavailable. That worker failed to repair
its code within two attempts and twenty total turns. Loop blocked the task,
retained its workspace and all 30984 reported tokens of usage, and accepted
nothing. It remains inspectable separately; selecting a different model did not
reset or alter the failed run. Model availability and alias routing are live
fleet state, not guaranteed by these historical example names.

The deployed UI reports build ID `loop-1c4a43888e5d`. The release manifest,
JUnit results, live API evidence, screenshots, and rollback files are retained at
`/Users/ben/Services/staging/loop-20260906T191349Z` (private to the service account).
The release was staged against the deployed tree, with file-hash checks before
installing Loop's changes. It preserves the existing model-routing/UI changes.
Only the four `LOOP_*` settings and `DOCKER_HOST` were added to the service
environment. The API restarted gracefully; launchd restarted the UI after its
complete build was selected. Existing job definitions and fleet defaults remain
the deployment mechanism. `make ui-deploy` is still intentionally disabled.

For rollback, first pause/cancel active Loop runs and confirm containment has
stopped. Verify current files against the release manifest before restoring the
recorded files from `backup/`; remove only new files still matching that manifest.
Restore `backup/ui/.next` and its matching `public/sw.js`/build metadata, and
restore the five changed environment keys from the private environment backup.
Preserve any subsequent operator edits. Restart the existing Mac API/UI jobs and
verify health and `/api/build`. Keep Mongo records and Loop state directories
for evidence and later recovery. No rollback should delete accepted checkpoints.

## Small example

Create a clean temporary Git repository containing `calculator.py`:

```python
def add(a, b):
    return a - b
```

Commit that baseline. Copy [check_addition.py](loop-example/check_addition.py)
to the operator policy's external `assets` directory. Use a preinstalled Python
image and obtain its immutable ID with `docker image inspect --format '{{.Id}}'
followed by the image name. Configure the example project above.

Create a draft with the UI or save this as `run.json`:

```json
{
  "project": "calculator-example",
  "specification": "calculator.add returns the arithmetic sum of two integers, including negative integers and zero.",
  "worker": {"backend": "llamacpp", "model": "aria-resident"},
  "limits": {"attempts": 3, "attempts_per_task": 2, "turns": 10, "attempt_seconds": 300, "run_seconds": 1800},
  "plan": {
    "version": 1,
    "tasks": [{
      "id": "fix-addition",
      "description": "Repair calculator.add after inspecting the existing implementation.",
      "specification_ref": "specification#integer-addition",
      "acceptance_criteria": ["Positive, negative, and zero integer addition return the correct result."],
      "dependencies": [],
      "allowed_paths": ["calculator.py", "tests/"],
      "out_of_scope": ["No new operations, dependencies, publishing, or unrelated refactors."],
      "check_ids": ["addition"],
      "human_review": []
    }]
  }
}
```

```bash
curl -fsS http://127.0.0.1:8200/api/v1/loop/runs \
  -H "X-API-Key: $ARIA_API_KEY" -H "X-Admin-Key: $ARIA_ADMIN_KEY" \
  -H 'Content-Type: application/json' --data-binary @run.json
```

Inspect the returned draft and approve its **current `version`** with
`POST /loop/runs/{id}/approve`, body `{"expected_version":0}`. Then
`POST /loop/runs/{id}/start`. All mutations require the existing `X-Admin-Key`;
the global API middleware also applies. Keys are provided by the operator, never
embedded in a task or passed to a container. Aria's current auth model is a
single-operator key split, not separate user tenants.

To propose a plan, omit `plan` at creation and use `POST /loop/runs/{id}/plan`.
The planner gets only read-file/list-file tools and a read-only repository. It
must successfully read a repository file before returning a schema-validated
plan. It cannot implement application changes. Inspect/edit the proposal in the
UI or `PUT /loop/runs/{id}/plan` with `plan` and `expected_version`, then approve.
Plan changes after approval require a new run; approved scope is never silently
expanded. Planner attempts, turns, time, and usage count toward the same budgets.

## Lifecycle and controls

| State | Meaning / next action |
|---|---|
| `draft` | Unapproved plan or specification awaiting planning |
| `approved` | Current specification/plan/policy approved; start explicitly |
| `planning` | Read-only repository inspection and plan proposal |
| `running` | One task's bounded coding attempt, or controller scheduling |
| `verifying` | Worker stopped; authoritative task/regression checks running |
| `final_verifying` | All tasks verified; integrated checks on combined revision |
| `paused` | Safe boundary or interrupted execution; explicit resume |
| `ready_for_review` | All required tasks and final automated checks passed |
| `human_review_required` | Automated checks passed; listed human acceptance remains |
| `blocked` | Scope/check/dependency/ambiguity issue requiring a new approved plan or investigation |
| `failed` | Model execution or infrastructure failure; inspect reason and evidence |
| `budget_exhausted` | Attempt, reported-token, or elapsed-time budget stopped work |
| `cancelled` | Operator cancelled; no further acceptance or scheduling |

Tasks use `pending`, `running`, `verified`, and `blocked`; attempts separately
record worker execution and verification outcomes. Empty, cyclic, duplicate-ID,
or unknown-dependency plans are rejected. An empty eligible queue with remaining
work is blocked, never successful.

* **Pause:** `POST /runs/{id}/pause` stops scheduling at the next attempt boundary.
  An active attempt and its independent checks may finish and be checkpointed.
* **Resume:** `POST /runs/{id}/resume` rechecks approval, policy, ownership,
  emergency stops, and cumulative budgets. It never resumes an old conversation.
* **Cancel:** `POST /runs/{id}/cancel` is observed during active work (normally
  within 250 ms plus database/daemon latency). Docker termination kills the PID
  namespace, including detached descendants. Acceptance CAS refuses a cancellation
  that arrived first. Already accepted checkpoints remain available.
* **Global e-stop/killswitch:** observed during work and at acceptance boundaries;
  stop active work and leave a paused run for operator reconciliation.
* **Retry:** a failed check retries the same task in its retained workspace with
  a compact failure handoff. An unchanged failing/uncertain tree is blocked rather
  than rerun to fish for a pass. Worker-declared blockage stops for clarification.
* **Repeated failure:** blocks that task with evidence and a request to split or
  clarify it. It does not start unrelated work on its broken changes.

Read operations: `GET /loop/policy`, `/loop/runs`, `/loop/runs/{id}`, and
`/loop/runs/{id}/logs?attempt_id=...&offset=0&limit=50`. Inspection includes
budgets, metrics, event history, session IDs, handoffs, changed paths, exact
candidate/checkpoint revisions, check argv/version/status/output excerpts, and
final verification. Logs include model/tool records, full captured check output,
and diffs; large process output is stopped at 1 MiB. API log pages contain at most
100 records. Generic secret scrubbing uses Aria's existing logging filters.

## Checkpoint and recovery protocol

1. Reserve the canonical Git common-directory target in Mongo; acquire its
   non-expiring local file lock. The target also records the controller host and
   state root, so another installation cannot bypass the lock with a different
   directory. There is one writer per target across Loop runs.
2. Clone local Git objects into the run's private bare repository, preserving
   source ancestry without copying source configuration/hooks. The source
   checkout, branches, dirty files, and remotes are never written.
3. Persist attempt/session/container identities and reserve each turn before
   inference. A fresh in-memory message list exists only for this attempt.
4. Stop the worker container; confirm it is stopped; retain its files. Raw
   retained tar files are never executed or blindly extracted. Traversal,
   symlinks, special files, Git metadata, and unsafe trees are rejected.
5. Create an immutable candidate commit in the private object store. Export raw
   blobs, including files that Git archive's `export-ignore` would omit. Run
   controller-defined checks against that exact candidate in separate containers.
6. Atomically save task acceptance, evidence, and the new accepted revision in
   Mongo. Only afterward update the convenience `refs/heads/loop/{run_id}` ref.
   Recovery can rebuild this ref from Mongo evidence. A commit message/ref alone
   is never verification evidence.

On startup, enabled Loop reconciles interrupted active runs under the same
target lock. It terminates recorded containers, retains useful work, and pauses;
it does **not** replay a possibly non-idempotent tool or model call automatically.
`POST /runs/{id}/recover` performs the same reconciliation after an infrastructure
repair. A live owner refuses recovery. If Docker cannot confirm termination, the
repository reservation remains fenced until recovery succeeds.

A crash before acceptance cannot create a verified task. A crash after acceptance
cannot duplicate it. A verification interrupted on an unchanged tree is exposed
as uncertainty rather than replayed; repair the candidate or investigate in a new
approved run. An interrupted/failed final integration check likewise requires
investigation, not repeated stochastic approval attempts. Source branch changes
made by a human during a run do not enter its pinned baseline; review/rebase them
explicitly later.

`GET /runs/{id}` returns `checkpoint_repository` and `checkpoint_ref`. Review or
fetch that local branch using ordinary Git. Loop does not push, merge, deploy,
or publish. `ready_for_review` is the default finish line.

## Trust boundaries, bounds, and retention

Worker containers have only their own workspace volume and scratch space. They
have no Git object database, controller filesystem mounts, Docker socket,
credentials, or network. Verifier containers have the candidate and independently
maintained checks in separate **read-only** volumes, plus writable `/tmp`.
Candidate code therefore cannot change its accepted tree during or after checks.
Checks that need build outputs should copy `/workspace` into `/tmp/build` and
operate there; do not grant writes to the candidate or protected check assets.
The trusted image, Docker daemon, OS/kernel, policy/check files, and controller
remain part of the trusted computing base. This is container isolation, not a
claim of resistance to kernel/daemon vulnerabilities.

The agent may run development tests and write new tests within scope. Those do
not replace the approved external acceptance checks. Test quality is the
operator's responsibility. Put requirements that cannot be checked automatically
in `human_review`; the result then remains `human_review_required`.

Default tuning bounds (not universal optimal values): 10 outer attempts, 3 per
task, 30 model turns per attempt, 900 seconds per attempt, 10800 seconds per run,
48000 characters of conversation context, at most 8 tool calls per model turn,
and 120 seconds per development command. Each check has its own finite timeout.
Worker containers also have finite lifetimes, 128 PIDs, configured CPU/memory,
and 256 MiB scratch. Cleanup can outlast the execution deadline.

Attempt/turn/time bounds are enforced by the controller and container lifecycle.
The optional `limits.tokens` stops admission between model calls using reported
usage; an in-flight request may exceed the remaining allowance. Missing or
interrupted usage is **unknown**, not zero. With a token budget configured,
unknown usage stops the run. Otherwise finite turn/time/attempt limits remain
effective. Cost is explicitly unknown; this slice does not claim monetary-spend
enforcement. Cumulative usage survives recovery/resume. Elapsed run time includes
pauses and approval time after planning starts; resuming never extends a deadline.

An operator can explicitly extend an idle approved, paused or budget-exhausted
run through `PUT /api/v1/loop/runs/{id}/limits` with the admin credential,
`expected_version`, a complete `limits` object, and an optional concise `handoff`
(2000 characters maximum). Limits may only increase; an existing finite token cap
cannot be removed. Usage, attempts, failed candidates and acceptance evidence
remain intact. The durable `limits_extended` event records before/after limits.
An exhausted run becomes paused and needs a separate resume. Its deadline remains
the original start plus the new overall duration. Cancellation, live ownership,
stale versions and changed verifier policy reject the amendment. This operation
does not revive implementation-blocked tasks or reroll an unchanged failed
candidate. The worker has no access to this operator operation.

Specifications, plan approval/controller state, concise task handoff, full attempt
logs, operating instructions, and accepted Git history remain separate. Only the
assigned task, approved specification, bounded root operating instructions, and
up to roughly 2 KiB of previous handoff enter a fresh attempt. Tool results are
bounded in context. Full transcripts are stored as log records and are not loaded
automatically into subsequent attempts. Repository text and handoffs cannot alter
controller policy. Nested operating instructions remain readable by the worker.

Retention policy: keep all Mongo evidence, candidate objects, stopped-worker
archives, prior workspace copies, and verification exports until the operator
exports/reviews them and deliberately removes the terminal run's directory.
Disposable Docker containers/volumes are removed after files are retained. No
automatic Git GC, destructive source reset, or automatic evidence TTL exists.
Candidate trees are limited to 30000 files, 16 MiB per file, 128 MiB total, and
plans to 128000 characters. A task's serialized changed-path list is limited to
16000 characters; oversized tasks block for a split, retaining their candidate
in Git. Oversized log records expose a `truncated` flag. Repositories containing symlinks or submodules are
unsupported in this slice. Offline toolchain preparation and native macOS-only
builds need an appropriate future runtime adapter. Retained host disk usage and
worker-volume disk usage do not have filesystem quotas; provision/monitor the
existing host storage and keep run bounds conservative.

## Tests

```bash
cd api
uv pip install --python .venv-test/bin/python -r requirements-test.txt
.venv-test/bin/python -m pytest tests/test_loop.py tests/test_loop_recovery.py tests/test_loop_adapters.py -q

# Optional real sandbox tests. Use an already-present image with python3.
DOCKER_HOST=unix:///Users/ben/.lima/mongot/sock/docker.sock \
LOOP_TEST_IMAGE=sha256:YOUR_LOCAL_IMAGE_ID \
.venv-test/bin/python -m pytest tests/test_loop_containers.py -q
```

Routine tests use deterministic fake providers/workers and fake Mongo, with real
temporary Git repositories and real process-based verification. The separate
container suite exercises the production runtime: failed candidate → repair →
verified checkpoint, verifier write/network/credential restrictions, cancellation,
timeouts, and detached descendant termination. It skips unless explicitly given
an image. Neither suite requires paid inference, a GPU, or a model service.

Implementation verification on 2026-09-06: the final targeted backend run passed
156 tests, including eight adapter-contract tests and four real-container tests,
against the existing Mac Lima engine. The suite covers existing-settings
integration, the actual provider message conversions and stream/usage handling,
and cleanup after process exit as well as cancellation and timeouts. The UI
passed 14 populated-page Playwright tests across all seven configured device
profiles, TypeScript, class lint, and a production build under Node 22. Both
error-layout and live API responsive checks passed on Supervise and Loop.
The live API rejects missing API credentials with 401 and mutations without an
admin key with 403. The activation record above documents the separate live
local-model runs and their independently observed outcomes.
