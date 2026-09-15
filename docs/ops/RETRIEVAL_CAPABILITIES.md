# Retrieval capability switches

Runbook for mongot search and the embeddings service. Production lives on the
Mac control plane; old Corsair container commands are historical.

Last verified: **2026-09-15**.

## Current state

Authenticated `GET /api/v1/capabilities/retrieval` reported:

| Capability | State |
|---|---|
| search/mongot | enabled; Lima container `devbox-mongot` running with a 1 GiB heap cap |
| embeddings | enabled; `com.ben.devbox.embeddings` running on the Apple GPU (MPS) |
| retrieval mode | `hybrid` (text + vector) |
| backfill | worker enabled; drained to zero on restore |

Never copy a backlog count forward. The endpoint is the authority.

## September 15 restoration

Restored at Ben's request after profiling the Mac. Search was re-enabled first,
then embeddings; the backfill embedded 24 memories and 56 ontology entities with
zero failures. (50 of those entities were project entities written by the
ontology projection minutes after the first pass; the projection deliberately
does not embed, so expect entity counts to reappear briefly after projection runs.)

Two changes live **outside this repository**. Each has a timestamped backup
beside it; neither is captured by git.

**mongot heap cap** — `~/mongo-config/compose.yml` inside the Lima `mongot` VM.
The mongot command now passes `--jvm-flags -Xmx1g` (the launcher's supported
flag; it sets no heap of its own). Without it the JVM defaults to 25% of the
VM, about 2 GiB, in an 8 GiB VM with no swap that also holds mongod's 3 GB
WiredTiger cache. The whole search index is about 200 MB. Recreate only this
container, never mongod, which AgentBenchPlatform shares:

```bash
limactl shell mongot -- sh -c 'export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock; \
  cd ~/mongo-config && docker compose up -d --no-deps mongot'
```

Lima maps guest `:8080` (mongot health) to Mac `:28080` and `:9946` to `:29946`,
so it does not collide with the Mac's `:8080` NInfer forward.

**Embedding batch cap** — `/Users/ben/Services/apps/embeddings/server.py`.
Despite the service description, sentence-transformers selects the **Apple GPU
(MPS)** in bfloat16, not the CPU. MPS memory lives in unified RAM but not in the
process RSS, so `ps`/`top` showed about 0.2 GiB while the driver held about
2 GiB — the likely reason this service was a memory problem without looking
like one. Measured on the M1 Pro:

| Configuration | GPU high-water | Speed |
|---|---|---|
| default batch (32) | 2.02 GiB | 102 ms/doc |
| batch 4 + `torch.mps.empty_cache()` | **1.02 GiB** | 107 ms/doc |
| CPU float32 | 2.3 GiB peak RSS | 2.3 s/doc |

The server now encodes in batches of 4 and releases the GPU cache after each
request. Vectors are unaffected: against batch 32, cosine ≥ 0.9963 and 100%
nearest-neighbour agreement over 40 varied-length texts, so existing embeddings
remain comparable. Measured production footprint after restore: 1,493 MB.

The TTS and Gemma disable markers remain in place; no LLM runs on the Mac.

## Semantics

| Search | Embeddings | Retrieval mode |
|---|---|---|
| on | on | hybrid vector + BM25 |
| on | off | lexical BM25 |
| off | either | mongod-native fallback |

Memory writes continue when embeddings are disabled. New documents are marked
`embedding_pending`; re-enabling embeddings wakes the backfill worker. Switches
live in Mongo and survive an API restart. Environment variables are only fresh
deployment defaults.

## September 7 shutdown and required embedding rehydration

Ben requested that local text-to-speech and embeddings be stopped to reduce Mac
memory pressure. The embeddings capability was disabled through the authenticated
API before stopping its process. Its persisted reason records the requirement to
rehydrate documents missing embeddings when the service is restored.

The user-owned launchers now exit successfully while these marker files exist,
preventing automatic model startup on launchd retries and reboots:

- `/Users/ben/Services/config/disabled/embeddings` guards `run-embeddings` (:8001).
- `/Users/ben/Services/config/disabled/tts` guards `run-tts` (:8002).

The system LaunchDaemons remain installed. Original launchers are backed up in
`/Users/ben/Services/backups/local-model-services-disabled-20260907`.

**When embeddings are intentionally re-enabled, complete the document backfill:**

1. Remove only the embeddings marker and start the local embeddings service
   (`sudo launchctl kickstart -k system/com.ben.devbox.embeddings`). Verify its
   health endpoint at `http://127.0.0.1:8001/health` before enabling callers.
2. Authenticated `PUT /api/v1/capabilities/retrieval` with
   `{"embeddings":true,"with_service":false,"reason":"restore embeddings and rehydrate pending documents","changed_by":"ben"}`.
   Leave the search/mongot switch unchanged unless separately requested.
3. Re-enabling wakes the backfill worker. It fills active memories marked
   `embedding_pending` or missing an embedding, and ontology entities missing an
   embedding. Existing valid vectors are retained; changing the model or vector
   dimensions requires a separate migration.
4. Inspect `GET /api/v1/capabilities/retrieval`. Let the worker drain the backlog,
   or run bounded `POST /api/v1/capabilities/retrieval/backfill` passes. Verify
   both `backfill.pending.memories` and `backfill.pending.entities` reach zero
   and investigate failures before declaring rehydration complete.

Text-to-speech has its own marker and can remain off when embeddings return.

## Inspect and change

Use a credential from the Mac service configuration without printing or copying
it into shell history:

```bash
curl -sS http://127.0.0.1:8200/api/v1/capabilities/retrieval \
  -H "X-API-Key: $ARIA_API_KEY" | jq

curl -sS -X PUT http://127.0.0.1:8200/api/v1/capabilities/retrieval \
  -H "X-API-Key: $ARIA_API_KEY" -H 'Content-Type: application/json' \
  -d '{"embeddings":false,"reason":"maintenance","changed_by":"ben","with_service":true}'

curl -sS -X PUT http://127.0.0.1:8200/api/v1/capabilities/retrieval \
  -H "X-API-Key: $ARIA_API_KEY" -H 'Content-Type: application/json' \
  -d '{"embeddings":true,"search":true,"reason":"maintenance complete","changed_by":"ben","with_service":true}'
```

`with_service:true` starts a service before enabling its switch and disables a
switch before stopping its service. When changing search and embeddings
differently, send separate requests.

One synchronous backfill pass:

```bash
curl -sS -X POST \
  http://127.0.0.1:8200/api/v1/capabilities/retrieval/backfill \
  -H "X-API-Key: $ARIA_API_KEY" | jq
```

## Troubleshooting

- Poor recall: inspect `retrieval_mode` before debugging ranking.
- Enabled but unreachable dependency: a genuine health failure; either repair it
  or deliberately disable the capability so ARIA degrades cleanly.
- A new memory missing from vector results after an off window: re-enable
  embeddings and drain `backfill.pending`.
- Health showing a deliberately disabled service as nonincident is expected.

The design rationale is in the vault at
`ProjectAria/Design/RETRIEVAL_CAPABILITIES.md`.
