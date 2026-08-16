# Retrieval capability switches — mongot and the embeddings model

**Runbook. Read before starting, stopping, or debugging `shared-mongot` or `shared-embeddings`.**

Last updated: 2026-08-15T23:14:42-04:00

Design rationale (why two switches, why the flag is the queue):
`vault/ProjectAria/Design/RETRIEVAL_CAPABILITIES.md`.
Code: `api/aria/memory/capabilities.py`, `api/aria/memory/backfill.py`.

---

## CURRENT STATE — BOTH SWITCHED OFF since 2026-08-15T17:19-04:00

| | switch | container | set by | reason |
|---|---|---|---|---|
| **search** (mongot) | **OFF** | `shared-mongot` **still running** | ben, 2026-08-15T21:18:59Z | operator: mongot retrieval off |
| **embeddings** | **OFF** | `shared-embeddings` **stopped** (exit 137) | ben, 2026-08-15T21:19:02Z | operator: embeddings model shut down |

**`retrieval_mode` is therefore `fallback`.** Every memory search is served by the
mongod-native scan in `LongTermMemory._fallback_search` — token overlap +
importance, no BM25, no vectors. Results are real and useful (spot-checked
against the live collection) but materially worse than hybrid search.
**If recall looks bad, this is why — check the switches before debugging search.**

`shared-mongot` was deliberately left running: it is shared with
AgentBenchPlatform, so stopping the container is a cross-project decision, not
an ARIA one. ARIA simply no longer sends it queries. To reclaim its memory too,
see *Stopping mongot's container* below.

⚠️ **The re-embed backlog GROWS while the switch is off, and nothing drains it.**
`EmbeddingBackfillWorker.run_once` returns early when `embeddings_enabled` is
false — by design, or the switch would defeat itself. So every new memory lands
`embedding_pending: true` and stays there. Measured accumulation on 2026-08-15:
**826 → 1,849 memories in 5h40m, ≈180 memories/hour**, plus a trickle of ontology
entities. **Never quote a backlog number from a doc** — one was frozen into four
files on 2026-08-15 and all four were wrong within hours. Read it live:

```bash
KEY=$(grep -E '^API_KEY=' /home/ben/Development/ProjectAria/.env | cut -d= -f2-)
curl -s localhost:8200/api/v1/capabilities/retrieval -H "X-API-Key: $KEY" | jq '.backfill.pending'
```

`backfill.pending` on `GET /api/v1/capabilities/retrieval` is the only authority.
The same call returns both switch states, `retrieval_mode`, and live container
state.

---

## What "off" actually changes

| | embeddings ON | embeddings OFF |
|---|---|---|
| **mongot ON** | `$vectorSearch` + `$search`, RRF-fused → `hybrid` | `$search` (BM25) only, no query embedding computed → `lexical` |
| **mongot OFF** | mongod-native scan → `fallback` | mongod-native scan → `fallback` |

Nothing else changes. Specifically, while either is off:

- **Memory writes still succeed.** A memory that cannot be embedded is stored
  with `embedding_pending: true`. That flag **is** the backfill queue — nothing
  is dropped, and no write blocks on a service that is off.
- **Vector dedup is unavailable** (it needs mongot), so `create_memory` falls
  back to exact-content dedup. Near-duplicates that hybrid dedup would have
  absorbed will accumulate; expect some redundancy in memories created during
  a long off window.
- **Health stops paging.** `/health/services` reports both as `capability
  disabled` and the selfcheck worker skips them, so the Hermes alert-triage
  cron is not fed an incident with no fix. This is the whole point of using the
  switch instead of just stopping the container.
- **Ontology `kg search`** skips its vector branch and uses its existing lexical
  regex fallback.
- The **switches survive an `aria-api` restart** — they live in
  `db.capabilities` (`_id=retrieval`), not in `.env`. `EMBEDDINGS_ENABLED` /
  `SEARCH_ENABLED` are boot defaults for a fresh box only and do **not**
  override a persisted switch.

## Turning things off

```bash
KEY=$(grep -E '^API_KEY=' /home/ben/Development/ProjectAria/.env | cut -d= -f2-)

# mongot: stop querying it (container left alone)
curl -s -X PUT localhost:8200/api/v1/capabilities/retrieval \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"search": false, "reason": "why", "changed_by": "ben"}'

# embeddings: stop calling it AND stop the container
curl -s -X PUT localhost:8200/api/v1/capabilities/retrieval \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"embeddings": false, "reason": "why", "changed_by": "ben", "with_service": true}'
```

`with_service: true` drives the container through the non-LLM service registry
in the safe order — **switch off, then stop** (disabling) and **start, then
switch on** (enabling). A failed container transition never rolls back the
switch: the switch is what keeps ARIA serving.

⚠️ **`with_service` applies to every switch in the same request.** Sending
`{"search": false, "embeddings": false, "with_service": true}` stops *both*
containers. Send separate requests when you want them treated differently
(which is exactly why the two calls above are separate).

⚠️ **A `stop` that reports a timeout has usually still worked.** The registry's
docker call times out at 10s, but `docker stop` itself waits 10s for SIGTERM
before SIGKILL — so a container that ignores SIGTERM reliably reports a timeout
*and then exits 137*. The route re-reads the real container state and says so
(`"shared-embeddings is exited (the stop call reported: ...)"`). Believe the
state, not the verb.

## Restoring

```bash
# Start the container, then re-enable — one call, right order.
curl -s -X PUT localhost:8200/api/v1/capabilities/retrieval \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"embeddings": true, "search": true, "reason": "back on", "with_service": true}'
```

⚠️ That form also starts `shared-mongot`; if it is already running, the registry
treats the start as a no-op, so this is safe in the current state.

**There is no repair step.** Re-enabling embeddings wakes the backfill worker
immediately (`RetrievalCapabilities.set_backfill_trigger` → `kick()`).

**Sizing the drain, since the backlog depends on how long the switch was off.**
The worker is bounded to `embedding_backfill_batch_size` (100) per tick on the
`embedding_backfill_interval_seconds` (300 s) timer — so **at most 1,200 docs/hour
unattended**, against ~180/hour still arriving. Net drain ≈1,000/hour. The bound
is deliberate: the embeddings service is CPU-only on this box and a greedy drain
starves live memory writes waiting on the same hardware.

So read `backfill.pending` first, divide by ~1,000, and if that is more than a few
minutes, **drive it by hand rather than leaving it to the timer**:

```bash
# One synchronous pass; returns counts + what is still pending. Repeat to drain.
curl -s -X POST localhost:8200/api/v1/capabilities/retrieval/backfill -H "X-API-Key: $KEY" | jq
```

Watch it finish:

```bash
docker exec shared-mongod mongosh "mongodb://localhost:27017/aria?directConnection=true&replicaSet=rs0" \
  --quiet --eval 'print(db.memories.countDocuments({status:"active", embedding_pending:true}))'
```

The worker only ever *adds* vectors to docs that lack one. It never re-embeds
an existing vector — changing the model or dimension is a migration, not a
backfill (see CLAUDE.md → *Embedding Dimensions (DO NOT CHANGE)*).

## Stopping mongot's container

Not done above, on purpose. `shared-mongot` is shared infrastructure — check
AgentBenchPlatform does not need `$search`/`$vectorSearch` first. Then:

```bash
curl -s -X POST localhost:8200/api/v1/infrastructure/services/shared-mongot/stop -H "X-API-Key: $KEY"
```

The `search` switch is already off, so ARIA sends it nothing either way; this
only reclaims the container's memory. **Restart order on the way back:** start
`shared-mongot` before flipping the `search` switch on.

## Troubleshooting

**"Recall got worse / returns odd results."** Check `retrieval_mode` first. Any
value other than `hybrid` means a switch is off and this is expected behaviour,
not a bug.

**"Memory search returns 503."** That is embeddings *enabled but broken* (a
genuine outage), which is deliberately still loud — the switch is how you make
it quiet, and doing so degrades recall to `lexical` instead of erroring.

**"A memory I just stored isn't findable."** While embeddings are off, new
memories have no vector, so they are only reachable by the lexical/fallback
paths until the backfill runs. Re-enable, run a backfill pass, then search.

**"Health shows mongot/embeddings green while they're down."** Correct, if the
matching switch is off — that is the "stopped on purpose" rule. Flip the switch
back on and the probes go red again immediately.
