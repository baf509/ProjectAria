# Retrieval capability switches

Runbook for mongot search and the embeddings service. Production lives on the
Mac control plane; old Corsair container commands are historical.

Last verified: **2026-08-29**.

## Current state

Authenticated `GET /api/v1/capabilities/retrieval` reported:

| Capability | State |
|---|---|
| search/mongot | enabled; `shared-mongot` running |
| embeddings | enabled; `shared-embeddings` running |
| retrieval mode | `hybrid` |
| backfill | running; 0 pending memories and 0 pending entities |

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
