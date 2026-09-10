# Current model choices and Red switching — September 9, 2026

Ben requested one current Corsair choice (Qwen Flash Next), two Red choices
(Qwen3.8-27B and Qwen Flash Next), and removal of Gemma E4B from use.

Operate now groups choices by machine. Corsair and Red controls appear before
memory and temperature telemetry. Red's Switch action checks fresh residency
and request activity, unloads the previous Red model, waits for it to stop, then
loads the selected model and verifies readiness. A failed unload prevents the
start. No force flag or runtime override is sent. Busy or unknown request
activity blocks the shortcut; bound agents must release their assignments.
The hardware's existing service/expert-cache lock still prevents co-residency.

An existing Red route pin moves only after the replacement is ready. This uses
the existing admin-gated route API; missing credentials are rejected before any
unload. On phones enter the session admin key under More; on desktop use the
sidebar. A Corsair/default route is not changed by a Red switch. Loading a model
and changing the model-omitted route remain separate concepts.

Static archives remain in the registry for observation and recovery records.
`catalog_visible` determines current UI choices; unexpected archival residents
remain visible. Corsair archives, including Context-1 and DeepSeek, have
`startable=false`, `allow_force_start=false`, and `auto_route=false`. Gemma's
record is retained but removed from choices/use. Its existing native disabled
marker is `/Users/ben/Services/config/disabled/gemma`; no weights were deleted.
Ridge's separate on-demand contract remains unverified and unchanged.

Gemma's shell/ontology extraction and triage classifier defaults now select
Corsair Flash Next through Aria. The Mac service `.env` shell-extraction override
was updated with backup under
`/Users/ben/Services/backups/model-options-20260909/`. Existing running processes
pick up these defaults only during normal activation.

The accepted model runtime configurations are unchanged. Model/quant/HF/runtime
notes and fresh accuracy/performance results are maintained through the guarded
Obsidian writer under `ProjectAria/Design/Supported Models`.

Activation must use `scripts/aria-deploy-mac`: manifest verification, sudo
preflight, idle check, controlled restarts and rollback. Do not activate during
the ongoing `supported-models-20260909` benchmark. No live API/UI pointer has been
changed by this review. Tracking tasks: UI `6aa16c4fb2ea9c354a8d830e`; benchmarks
`6aa16604b2ea9c354a8d80b7`.
