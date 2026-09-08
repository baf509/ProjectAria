# Retrieval capability switches

Runbook for mongot search and the embeddings service. Production lives on the
Mac control plane; old Corsair container commands are historical.

Last verified: **2026-09-07**.

## Current state

Authenticated `GET /api/v1/capabilities/retrieval` reported:

| Capability | State |
|---|---|
| search/mongot | disabled; Lima container `devbox-mongot` stopped |
| embeddings | disabled at user request; local service stopped and launcher disabled |
| retrieval mode | `fallback` (mongod-native scan) |
| backfill | worker enabled but pauses while embeddings are disabled; inspect the endpoint for current counts |

Never copy a backlog count forward. The endpoint is the authority.

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
