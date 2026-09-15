# Weekly platform deployment qualification

The maintained specification is the vault's existing
`ProjectAria/Design/2026-09-15 WEEKLY_PLATFORM_IMPROVEMENT_SPEC 070125.md`.
This runbook describes the executable checks and recovery controls.

## Hermes

The initial automatic target is exactly
`Hermes/plugins/aria-flashnext/__init__.py`. Configuration containing credentials,
upstream Hermes, approval policy, SOUL, and the continuity policy are outside
this automatic target. The review can still recommend improvements there.

The frozen `scripts/weekly-checks/hermes_contract.py --defaults-only` check runs
in the weekly worker's existing isolated container. It checks all five registered
models, exact gateway scope, explicit controls, message/tool preservation and
unrelated-provider refusal. A baseline failure and candidate pass are required.

The installed health hook also runs the full contract against the pinned native
Hermes consumer with a disposable HERMES_HOME. This covers real plugin discovery,
session-specific context-engine instances, durable pending/cancelled tasks, and
compaction without rewriting stored history or inventing authorization.

`scripts/aria-weekly-hermes-control` supplies fixed `health`, `drain`, `reload`
and `release` operations. A transaction holds upstream's acknowledged new-turn
drain across file replacement, restart, health and any rollback. Existing work
must reach zero before reload. SIGUSR1 uses Hermes's graceful restart path; there
is no force-kill fallback. The control verifies gateway process identity, a new
PID after reload, Signal connection, and complete MCP tool selection. Drain
ownership uses parent PID plus birth time and the exact marker bytes. A dead
owner's exact marker can be recovered; a different operation's marker is refused.

Install these scripts outside candidate repositories and pin their hashes in the
operator registry. Hooks in a transaction must share the adapter parent process.
Do not run a drain in one terminal and attempt its release from another process.

Health returns `samples: 0`: deterministic consumer checks are not organic user
outcomes. The regression watcher must report insufficient samples rather than
claim a clean quality result. A functional failure can still trigger rollback.

## Model serving configuration

`platform_contracts.MODEL_PROFILES` defines two initial candidate scopes:

| Contract | Exact source | Eligible setting reductions |
| --- | --- | --- |
| red-paro-int5 | red-r9700/paro-int5-isolated/profile.env | MAXSEQS within 1–8; CHUNK from 8192 to 4096 |
| corsair-ninfer | ninfer3090/run-serve.sh | --max-pending-requests from 2 to 1 |

`Target.model_profile` requires the exact source path. Workspace verification
reads before/after immutable Git blobs and rejects changes outside literal
bounded numeric reductions before running acceptance checks. Runtime/image,
weights, GPU placement, port, context/KV geometry, vision, reasoning and arbitrary
launcher commands cannot change through these contracts. A smaller queue or
batch still needs measured latency, errors and quality checks; numeric validity
alone is insufficient.

These model contracts are **defined and tested, not enabled for deployment**.
On September 15, Red's source profile said PORT=8079 while its promoted live
profile said PORT=8081. CorsairModelHost also had unrelated uncommitted work.
Neither is permission to overwrite the deployment or the checkout. Halogen needs
a canonical versioned deployment profile before it can be a configuration target.

Model onboarding additionally needs an owned gateway admission hold respected by
every caller, target-native idle acknowledgement, a fixed remote file/actuator
mapping, real candidate canaries through Aria, and a tested host rollback. Keep
these targets report-only until that evidence exists. Never count an offline
fixture as a hardware rollout qualification. Runtime upgrades remain separately
scoped model engineering.

## Recovery tests

The generic adapter checks live full-file preimages against the reviewed source,
backs up exact bytes and modes, hashes backups, and refuses later edits or
symlink replacements. Admission remains held during failure recovery. Every
restore path validates all files before restoring any of them. Interrupted and
failed deployments retain receipts for reconciliation.

Run the contract and failure tests from an isolated Aria worktree:

```sh
PYTHONPATH=api .venv/bin/python -m pytest -q \
  api/tests/test_weekly_improvement.py \
  api/tests/test_weekly_platform_contracts.py \
  api/tests/test_weekly_deploy_failures.py \
  api/tests/test_weekly_hermes_control.py \
  api/tests/test_loop.py
```

September 15 evidence is in
`/Users/ben/.aria/weekly-platform-qualification-20260915/`.
The v4 operator bundle is
`/Users/ben/.aria/weekly-platform-controller-20260915-v4/`.
