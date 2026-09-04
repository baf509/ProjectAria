# Obsidian LiveSync Corsair recovery — operator runbook

Executes
`vault/ProjectAria/Planning/OBSIDIAN_LIVESYNC_CORSAIR_RECOVERY_PLAN_20260826.md`.
The plan is the reasoning and the acceptance criteria; this is the command
sequence and the gates. **Read the plan first** — every stop gate here exists
because the plan explains what breaks without it.

**Status (2026-08-27):** recovery run `20260827-085218` reconciled and seeded
all three filesystem projections, restarted both headless bridges, passed a
bidirectional 5-second headless canary, restored Corsair Git autocommit, and
verified the same canary/hash through the reopened interactive Mac client.
Recovery is complete and in its 24-hour/72-hour monitoring period.

Tools live in `scripts/obsidian-recovery/`.

---

## What this repairs

At the 2026-08-23 migration cutover Corsair lost both halves of its Obsidian
filesystem path: the `obsidian-livesync-bridge-bridge-1` container was stopped,
and the ten-minute `obsidian-autocommit.sh` cron entry was filtered out by
`finalize-corsair-model-data-plane.sh` under the assumption "Obsidian moved to
the Mac". The later hybrid rollback brought coding agents back to Corsair but
not its vault peer. Three days of independent edits on each side followed.

**The Corsair bridge must not simply be restarted.** It is bidirectional with
`scanOfflineChanges: true`, so it would read the stale August 23 tree as current
intent and publish those deletions into CouchDB and every connected device.

---

## Prerequisites

| Requirement | Why |
|---|---|
| Ben's approval of a maintenance window | Phase 0 step 1 is a person, not a command |
| Interactive `sudo` on the Mac | the bridge is a system-domain LaunchDaemon; the root helpers prompt normally |
| SSH to Corsair | every Corsair phase |
| CouchDB credentials from a bridge config | the snapshot replica and the quiescence check |
| ~2× the vault size free on both hosts | snapshots and pre-seed backups |

One run identity is used throughout. Export it once and keep the shell:

```bash
cd scripts/obsidian-recovery
export OBS_RUN_ID=20260827-001500
eval "$(./couch-config.py --source /path/to/bridge/dat/config.json \
        --out "$HOME/.obs-recovery-curlrc" | sed 's/^/export /')"
```

⚠️ **Corsair is not reachable as a bare hostname.** There is no `corsair` entry in
`~/.ssh/config`, and the migration moved it to port 2222 behind a `HostKeyAlias`
and a dedicated known-hosts file. `lib.sh` therefore defaults to the connection
string recorded in the migration state document:

```
OBS_CORSAIR_HOST=ben@100.123.245.84
OBS_CORSAIR_SSH_OPTS='-p 2222 -o HostKeyAlias=corsair-ai.local \
  -o UserKnownHostsFile=/Users/ben/.config/devbox-migration/corsair-known-hosts-2222'
```

Override both if the route changes. Every Corsair call — `ssh` and `rsync`
alike — goes through `obs_corsair` / `obs_corsair_rsync_shell`, so there is one
place to change and no path that quietly falls back to port 22. Confirm the
route before starting:

```bash
./00-preflight.sh    # its Corsair checks are the connectivity test
```

`couch-config.py` writes a mode-0600 curl `--config` file and prints only the
endpoint. Credentials never reach argv, shell history, or any log — every script
scrubs its output through `obs_scrub` before it is written or displayed.

**Everything defaults to a dry run.** A destructive step requires `--apply`, and
the genuinely irreversible ones additionally require a typed confirmation.

---

## Phase 0 — Preflight and freeze

```bash
./00-preflight.sh                 # read-only; safe any time
OBS_APPLY=1 ./01-freeze.sh        # stops the Mac bridge, proves CouchDB is quiet
```

`00-preflight.sh` fails if the incident no longer matches the plan — Corsair's
Git HEAD has moved off `e7534f6`, the Corsair bridge is running, a `.git` marker
sits in a vault, or the Mac's launchd state cannot be determined. It reports
"unknown", never "stopped", when it cannot ask; treating an unanswerable query
as "stopped" is how you snapshot a vault that is still being written to.

`01-freeze.sh` boots the LaunchDaemon out of the system domain (`KeepAlive`
means killing the PID just relaunches it), confirms Corsair's container is down,
then reads CouchDB's `update_seq` twice `OBS_QUIESCE_WAIT` seconds apart. **A
moving sequence is a stop gate** — it means a phone or laptop is still writing,
which is the one writer this script cannot stop itself.

## Phase 1 — Immutable recovery copies

```bash
OBS_APPLY=1 ./02-snapshot.sh
```

Captures the interactive Mac vault, the Mac service-account vault, and the
Corsair vault (ownership/ACLs/`.git` pointer preserved), Corsair's external Git
directory, a `git bundle --all`, both bridge
configs and state directories, container inspect and logs, the crontab, and a
one-shot CouchDB replica `obsidian_recovery_<run-id>`.

It then **validates rather than assumes**: manifests are regenerated from the
copies, ten random files per tree are re-read and compared byte-for-byte against
the frozen source, `git bundle verify` must pass, and the replica's document
count must equal the frozen source's. Any failure is a stop gate — this is the
artifact set the rollback plan depends on.

⚠️ Never point a LiveSync client at the recovery replica.

## Phase 2 — Three-way reconciliation (offline, repeatable)

```bash
./03-reconcile.sh
```

Extracts base commit `e7534f6` from the snapshotted Git directory with
`git archive` (never `checkout` — that Git dir is also a rollback source), then
runs `reconcile.py` over base / Mac / Corsair.

Resolution follows the plan's table exactly, with `ABSENT` as an explicit state.
Every row is pinned by a test in `api/tests/test_obsidian_recovery.py`.

**Preservation wins.** Absence on one peer is never treated as intent: those
paths are kept and listed in a generated
`Recovery/DeletionReview/<date>/INVENTORY.md` note. Files deleted on *both*
peers are also preserved by default; `--honor-concordant-deletions` opts out.

Both-changed files are merged with `git merge-file --diff3`, with the Mac,
Corsair and base originals retained under `originals/`. **A remaining conflict
is a stop gate, not a mtime tiebreak.** Resolve by hand in `merged/`, then:

```bash
./validate-merged.py --workspace "$OBS_MAC_RECOVERY_ROOT/reconcile"
```

Review `RECONCILIATION_LEDGER.md` and `reconciliation-manifest.json` before
going further. Nothing has been written to a live vault yet, so this phase can
be discarded and re-run freely.

## Phase 3 — Verify bridge roots and metadata boundaries

```bash
./bridge-config.py --config <mac-config.json>     --host mac --show
./bridge-config.py --config <corsair-config.json> --host corsair \
                   --show
```

The recovery established that peer names are host-local Deno checkpoint keys;
matching names on different hosts do not collide. Preserve the active names
and checkpoint state. Verify the storage roots instead:

- Mac headless bridge: `/Users/ben/Services/data/obsidian/vault/`
- Corsair container bridge: `/app/vault/` (host `/home/ben/Obsidian/vault`)
- Mac desktop plugin: `/Users/ben/Obsidian`

Credentials are preserved byte for byte, never printed, and the file is written
mode 0600 from creation with a `.bak-prerecovery` kept.

Also in this phase, by hand on Corsair:

- back up the bridge checkpoint/local state directory; preserve it unless a
  measured replication fault requires a reset;
- move the vault's `.git` pointer into the recovery directory (move, not delete);
- **recreate** the container so Compose's `restart: unless-stopped` actually
  applies — inspect currently reports policy `no` despite the declaration.

Do not disable the Mac plugin merely because the headless bridge is running.
They watch different roots and are two legitimate projections of CouchDB.

## Phase 4 — Seed all three vaults

```bash
./05-seed.sh                            # dry run
./05-seed.sh --apply                    # seed interactive Mac + Corsair
# Seed the protected Mac service vault with the root helper documented for the run.
./05-seed.sh --apply --allow-deletions  # only if the dry run's deletions are correct
```

Re-runs Phase 2's validation first, so a merged tree that failed its gate cannot
be seeded by skipping a step. Every path goes through `obs_assert_safe_dir`,
which rejects an empty or unexpanded variable, `~` shorthand, a relative path,
and anything too shallow to be a vault — the exact failure the plan names.

**Deletions are off by default.** When enabled they are listed in full and
confirmed by typing `delete-mac` / `delete-corsair`. Overwritten and deleted
files land in a timestamped `--backup-dir`, so even the applied pass is
reversible without returning to the Phase 1 snapshot.

Afterwards it recomputes both manifests and requires them to be identical,
asserts the named notes survived, and proves the Corsair vault is writable by
the bridge account via a probe file deliberately outside the note set.

## Phase 5 — Reconnect the Mac alone

By hand, with everything else still stopped:

```bash
sudo launchctl bootstrap system /Library/LaunchDaemons/com.ben.devbox.obsidian-bridge.plist
./bridge-health.sh
```

Watch until the storage peer reports watching and replication is stable. Compare
CouchDB's document count and `update_seq` against `freeze-state.env` from Phase
0 — **more than the reconciled change set is a stop gate.** Only once that is
stable, set `scanOfflineChanges: true` and restart.

## Phase 6 — Reconnect Corsair

Recreate the Compose bridge with fresh checkpoint state and
`scanOfflineChanges: false`. Confirm the restart policy took, watch both peers
report healthy, verify no mass deletion is proposed, and confirm Corsair's
hashes still match the reconciled set. Then enable `scanOfflineChanges: true`
and recreate.

## Phase 7 — Restore the Git history writer

```bash
scp obsidian-autocommit.sh corsair:/home/ben/bin/obsidian-autocommit.sh
ssh corsair /home/ben/bin/obsidian-autocommit.sh --inspect     # review the staged diff
ssh corsair "/home/ben/bin/obsidian-autocommit.sh --message \
  'vault: incident recovery merge (plan OBSIDIAN_LIVESYNC_CORSAIR_RECOVERY_PLAN_20260826, base e7534f6)'"
scp install-autocommit-cron.sh corsair:/tmp/ && \
  ssh corsair /tmp/install-autocommit-cron.sh          # dry run
  ssh corsair /tmp/install-autocommit-cron.sh --apply
```

The autocommit script uses explicit `--git-dir`/`--work-tree` for every
invocation and **refuses to run** if a `.git` marker exists inside the vault —
a `gitdir:` file naming a machine-specific path is one replication cycle away
from every other device. It is a backup writer only: it never pulls, merges or
resets, so it cannot become a second source of truth.

The cron installer is additive and idempotent. It matches on the script path
(so a hand-edited schedule is recognised, not duplicated), collapses duplicates
to exactly one canonical line, and **refuses to write if the count of unrelated
entries would change** — the inverse of the wholesale rewrite that caused this
incident.

## Phase 8 — Round-trip canaries

```bash
./06-canary.sh              # tests A and B
./06-canary.sh --restart    # plus D
./06-canary.sh --deletion   # plus C, only after A and B pass
```

Each test waits for a hash to match rather than sleeping and declaring victory,
and reports the elapsed time — "it arrived" and "it arrived within the SLO" are
different results. Test C additionally diffs the whole-vault manifest before and
after and fails unless **exactly one** path moved.

## Phase 9 — Unfreeze and monitor

Reopen Obsidian on the Mac with plugin replication enabled for its distinct
interactive vault, verify the recovery canary, then re-enable one other
LiveSync client at a time.

```bash
./bridge-health.sh --exit-code    # non-zero unless both peers are healthy/idle
```

Monitor at 24h and 72h. Retain recovery snapshots ≥30 days. **Do not clean
stale CouchDB `.git` documents or deleted-file metadata during this incident** —
that is a separately backed-up maintenance task for after the soak.

---

## Health model

`bridge-health.sh` emits one JSON object carrying the raw measurements
alongside the derived state, so a monitor can alert on either:

```json
{
  "overall": "conflicted",
  "mac_bridge": {"state": "healthy", "detail": "running"},
  "corsair_bridge": {"state": "healthy", "detail": "container running",
                     "restart_policy": "unless-stopped"},
  "autocommit": {"result": "ok", "age_minutes": 4},
  "dual_replication_engine_on_mac": false,
  "git_marker_in_a_vault": false
}
```

States, worst wins: `healthy` · `idle` · `degraded` · `stale` · `conflicted` ·
`stopped` · `failed`. Alert thresholds follow plan §8.2 — bridge not healthy for
5 minutes, no Corsair replication for 15 minutes while CouchDB is reachable,
autocommit status older than 30 minutes, any unreviewed conflict.

Two faults are configuration rather than runtime and are reported even while
everything looks up: `dual_replication_engine_on_mac` (only true if the plugin
and LaunchDaemon are configured for the same filesystem root) and
`git_marker_in_a_vault`.

---

## Rollback

Trigger on any of: a mass deletion beginning, an unexpected conflict burst,
either vault losing unique notes, credentials or Git metadata appearing in
synchronized content, the two bridges repeatedly overwriting the same paths, or
permissions blocking expected reads.

1. Stop the Mac bridge (`launchctl bootout`) and the Corsair container; close
   other clients.
2. Record CouchDB's sequence and capture fresh incident snapshots. Do **not**
   run autocommit until the filesystem is understood.
3. Restore both vaults from their Phase 1 snapshots; restore Corsair's Git
   directory and bridge state/config if needed. Verify hashes before
   reconnecting anything.
4. **Never overwrite the live `obsidian` database in place as a first
   response.** Preserve the incident database, validate the recovery replica,
   replicate it into a *new* name, test one isolated client, and only then
   repoint the rest.
5. Recovery commits are reverted or branched, never history-rewritten, unless
   the remote contains exposed secrets.

---

## Tool reference

| Tool | Phase | Writes |
|---|---|---|
| `lib.sh` | all | — (shared guards, scrubbing, manifests) |
| `obslib.py` | 1, 2, 4 | — (the one definition of the content set) |
| `manifest.py` | 1, 4 | manifest files only |
| `couch-config.py` | 0, 1, 5 | mode-0600 curl config |
| `00-preflight.sh` | 0 | nothing |
| `01-freeze.sh` | 0 | Mac launchd state, `freeze-state.env` |
| `02-snapshot.sh` | 1 | recovery roots, CouchDB replica |
| `03-reconcile.sh` | 2 | reconciliation workspace only |
| `reconcile.py` | 2 | reconciliation workspace only |
| `validate-merged.py` | 2 | nothing |
| `bridge-config.py` | 3 | a bridge config (backup kept) |
| `05-seed.sh` | 4 | **both live vaults** |
| `06-canary.sh` | 8 | one canary note |
| `obsidian-autocommit.sh` | 7 | Corsair's external Git dir |
| `install-autocommit-cron.sh` | 7 | Corsair's crontab (backup kept) |
| `bridge-health.sh` | 8, 9 | nothing |

Tests: `api/tests/test_obsidian_recovery.py` — 21 cases covering every row of
the resolution table, the deletion policy, the content-set boundary, and the
agreement between the rsync and reconciler exclude lists.
