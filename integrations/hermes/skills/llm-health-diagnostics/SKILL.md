---
name: llm-health-diagnostics
description: Diagnose Aria model availability and selfcheck alerts through read-only MCP observations before a scoped repair.
---

The Mac hosts Aria and Hermes. Corsair is the model data plane. Start with
`operator_snapshot(model=<client model>)`, `get_model_server`, `inference_backend`
and `inference_traces`. Use `red_model_status` for Red and the
`aria-model-host-lifecycle` skill for a user-requested wake/load operation.
Missing telemetry is unknown; HTTP 503 or a failed connection alone does not
identify a GPU, model-runtime or host-power failure.

Do not acknowledge an alert before diagnosis/resolution just to clear the queue.
Explain the evidence, unresolved cause and next concrete action. Do not revive
Gemma or historical runtime loadouts as a fallback. Avoid obsolete Corsair Aria
paths: the canonical workspace is
`/Users/ben/Development/Infrastructure/ProjectAria` on the Mac.

If investigation requires coding, use Aria's registered shell/session tools and
that workspace. Do not automatically spawn a coding session for each alert.
Use read-only diagnostics until the requested repair is clear; preserve model
assignments and active requests. Reply in the current conversation; a diagnostic
skill does not authorize sending a separate Signal message.
