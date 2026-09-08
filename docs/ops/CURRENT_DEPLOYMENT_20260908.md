# Current model, clients and delivery state — September 8, 2026

This is the current-state entry point for today's work. Dated engine/driver
investigations are historical evidence, not instructions to restart experiments.
Live ARIA identity/readiness and activation receipts win over this snapshot.

## Hardware and ownership

| Host | Current hardware | Role |
|---|---|---|
| MacBook Pro | Mac-native services | Canonical ARIA/MongoDB/Hermes/Signal, gateway, managed shells, general code, vault bridge and credentials |
| Corsair | Strix Halo plus RTX 3090 FE 24 GiB via OCuLink | Flash Next model serving and bounded model engineering; no general control-plane migration |
| Red | Two Radeon AI PRO R9700 GPUs, 32 GiB each | Separate registered Qwen3.8-27B Radiance/MXFP4 deployment; existing choices and overrides preserved |
| Ridge | RTX 5090, confirmed by Ben | Secondary inference; asleep at this review, so new-card readiness and speed are not verified |

The R9700 formerly attached to Corsair is retired from that host. Red's R9700s
are legitimate and must not be removed by a blanket R9700 cleanup. The old
Ridge RTX 3090 description is stale inventory, not the current hardware.

## Running Corsair deployment

- ARIA slug: `Qwen3.8-Flash-Next-CUDA-Halo-Candidate` (retained compatibility name).
- Unit: `flashnext-cuda-halo.service`; raw endpoint `127.0.0.1:8131`.
- Consumers: authenticated Mac gateway `/llm/v1-identified`, never the raw port.
- Model: unsloth UD-Q4_K_XL Flash Next; author Q4_K_M MTP head on CUDA, depth 3.
- Placement: dense trunk/KV/draft on RTX 3090 CUDA; experts on Halo RADV Vulkan;
  disk-backed PLE table.
- Source: `ucicelos/flashnext-hybrid` at `e8bfdb53d32f2be8444b96e8b69add844acc2d26`,
  plus the local Top-K argsort fallback. Not unmodified upstream or the old HIP engine.
- NVIDIA driver 610.43.02, pinned CUDA 13.3 container/build.
- Context **262144 total / one slot**, q8_0 K/V, **8 GiB host prompt cache**,
  32 checkpoints, batch 2048 / microbatch **512**.
- `kv_unified=false`. One slot owns the full context; the old 16-GiB/unified-KV
  description does not apply. CUDA capture is off; independent graph reuse is on.

Profile SHA: `ac0c9deb8bdda4fdb197f6de0c3c42ad43deed63efb07a082c31a0144981854e`.
Image: `sha256:56e11bba0ea44f6ee57f1832492a5dbe11a4d1bcc4325840ba145c4432011d1c`.

## Acceptance, measurements and limits

Ben accepted this exact MTP-on profile for everyday use and ended further crash
investigation/soaks. **The intermittent CUDA fault is not fixed or qualified
away.** The failed sustained test and operator-ended shorter control remain
preserved. A startup approval is not a passed two-hour soak.

| Context | Prefill tok/s | Decode tok/s | Evidence scope |
|---|---:|---:|---|
| About 4K | 443.08 | 56.93 | One fixed 512-output-token, thinking-off code sample |
| About 8K | 462.42 | 56.44 | One sample; 155 prompt-prefix tokens cached |
| Warm 8K | Not a full-prefill measurement | 56.87 | 8352 prefix tokens reused |

These are not repeated medians or a deep-context qualification. Earlier
1000+ tok/s prefill came from the retired R9700/Halo runtime and different
settings. Other author-build context ladders and HumanEval+ results are dated
historical measurements, not proof for this current local variant.

Microbatch 1024 was suggested as an optional prefill experiment; it has **not**
been applied. No new driver/engine/tuning sweep or soak is scheduled.

## Hermes and Pi

Hermes's installed configuration and both managed Pi configurations (Mac and
Corsair) select the explicit candidate with context 262144 / max output 32768.
Compaction remains near **95K**, not near the full capacity limit. Pi retains
its 20K verbatim tail and explicit thinking mapping; default thinking stays off.

Hermes main persisted reasoning is medium with a 2048-token budget through the
scoped `aria-flashnext` plugin. Explicit wire controls win; the current middleware
does not expose session-local effort changes. Sixteen auxiliary routes use the
same model with thinking off; compression timeout floor is 600 seconds and
other changed auxiliaries 300 seconds. Longer existing limits are preserved.
The intentionally stopped Mac Gemma is no longer the auxiliary/fallback route.

Hermes was gracefully restarted and its new process reconnected to **118 ARIA
tools**, with no missing required tools. Installed Pi and Hermes executor
fixtures, native MCP routing, and the installed compaction path passed bounded
tests. The compaction test recovered all four planted facts. These are synthetic
fixtures, not a new personal Signal conversation or exhaustive agent benchmark.

No credential was rotated for this cutover. Red model choices and existing
explicit Red overrides are preserved. The client default does not change
ARIA's model-omitted default route. Search/mongot and Mac Gemma remain off.

## Routine-start delivery boundary

The exact release manifest is active and verified on Corsair:
`54f20c4c4af452d80bdfecbca3c6fee463fde09c2dea437ee488d94c9990574a`.
Its separate operator-acceptance schema retains `production_qualified=false`
and `two_hour_protocol_soak=false`. It cannot bypass the artifact, client,
backend-authentication or lifecycle evidence checks.

Ben completed candidate-only activation at **23:34 UTC**. It installed the pinned
normal-start wrapper and restarted ARIA API, preserving unrelated registry bytes,
UI, routing/defaults and controller policy. Previous files are backed up.

One normal ARIA stop/start then passed, with both admission queues checked idle.
The new service started at **23:42:13 UTC**, the model became ready at **23:45:58**,
and ARIA readiness was verified at **23:46:06**. Service PID2151525/container
`9afc0b321a1cd0ad3718957593adf5ca8813ae151ce7be80ecc7c066dc818aa6`
is running in verified **serve-release** mode, not the former task-bound launch.
The image, release pin, 256K/one-slot geometry and MTP settings are unchanged.
Backend authentication/CORS checks passed again after restart.

The installed Pi SDK also passed two bounded tool roundtrips/four identified
requests on the restarted model, without fallback or retry. Turns took4.78s
and2.02s and reused576/733 prompt-prefix tokens. This confirms basic client
operation/cache reuse after restart, not sustained reliability or a new speed
benchmark. Evidence: `accepted-routine-pi-executor-20260908/summary.json`.

Evidence: `accepted-activation-z4h1cbx_.json`,
`accepted-routine-lifecycle-20260908.json`,
`container-jr4sudfp.json` and `accepted-routine-backend-auth-20260908.json`
under the author runtime's `results/` directory.
No further soak, boot autostart, automatic restart or route change was enabled.
Git publication remains separate: GitHub authentication still returns401 and
Hermes still needs a usable push repository URL.

## Source, evidence and documentation

Canonical code roots are `ProjectAria`, `CorsairModelHost` and `Hermes` under
`/Users/ben/Development/Infrastructure`. Deployment source/docs are copied to
Corsair only where needed for model hosting. Model files, private configurations,
raw results, backups and `.work` are excluded from Git; preserve them in place.

Relevant reports in `CorsairModelHost/flashnext-author-reproduction/results/`:

- `user-mtpon-speed-check-20260908/results.jsonl`
- `mtpon-hermes-native-executor-answer-20260908/summary.json`
- `accepted-pi-executor-20260908/summary.json`
- `accepted-hermes-compaction-20260908/summary.json`
- `accepted-backend-auth-20260908.json`
- `accepted-hermes-reload-20260908.json`
- `topk-sort-fallback-soak-2h-20260908/summary.json` (failed evidence)

Vault notes are published through ARIA. Ben explicitly authorized the September
8 hardware/current-state charter correction. The two pre-existing charter
projection histories differ; unrelated policy and history are preserved, not
silently overwritten. Publication and cross-projection synchronization are
separate checks. No Ralph/controller acceptance or policy is changed here.
