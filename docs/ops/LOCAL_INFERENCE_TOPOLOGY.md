# Local inference topology

Last reconciled: **2026-09-15**. The September 8 topology is retained in
[the historical record](LOCAL_INFERENCE_TOPOLOGY_20260908.md). Its old client
defaults, ports and residency statements are not current launch instructions.

## Ownership and current roles

The Mac owns ARIA, MongoDB, Hermes/Signal, credentials, managed shells and canonical
projects. Corsair and Red provide model inference and hardware management.
Consumers use the authenticated ARIA gateway; raw model listeners stay loopback-only.
Hermes's named ARIA provider uses `http://localhost:8200/llm/v1-identified`.
Do not substitute a different hostname into saved cron overrides: the provider's
credential guard intentionally checks its configured endpoint.

| Deployment | Hardware | Current role |
|---|---|---|
| Red-Qwen3.8-27B-PARO-INT5 | Two R9700s, 32 GiB each | Main Hermes, steward, Mac Pi and ARIA Red Pi profile default |
| NInfer-3090-Qwen3.8-27B | Corsair RTX 3090, 24 GiB | Routine Hermes cron, vision and selected auxiliaries |
| Halogen-Qwen3.8-Flash-Next-W4B-Halo | Corsair Strix Halo | Retained long-context Hermes fallback |
| Original Red MXFP4, PARO-MXFP4 and Flash Next | Red R9700s | Explicit alternatives, mutually exclusive with Red PARO-int5 |
| CUDA/Halo Flash Next candidate | Corsair RTX 3090 + Halo | Retained explicit deployment; exclusive with NInfer on the 3090 |

Explicit client defaults are separate from the API's model-omitted selection
policy. NInfer remains excluded from automatic model-omitted fallback. Its
shorter context must not silently inherit a 262K main conversation.

## Durable model profiles

Red runs `red-paro-int5-isolated.service`, enabled at boot. Both R9700s retain
250 W caps through `/etc/udev/rules.d/99-red-r9700-power-limit.rules`, matched by
PCI identity and GPU unique ID. Live readback and installed persistence agree.
No power-cycle test was performed during this reconciliation.

NInfer runs `ninfer-3090.service` with **94K (96256 tokens)**, INT8 KV, vision,
MTP3, lm-head draft, 1024-token prefill chunks, one active request and two pending.
The unit is enabled under the user's `default.target`, with lingering enabled.
The existing CUDA exclusivity guard and visible-failure policy remain intact.
The Mac's restricted `:8080` forward carries this NInfer deployment; it is no
longer a retired-port mapping. Red's model forward remains `:8094`.

The canonical launcher, unit, drift verifier, native cron canary and recovery
instructions are in `CorsairModelHost/ninfer3090/`. The live launch path is
`/home/ben/staging/ninfer3090/deploy/run-serve.sh`. ARIA also carries the matching
unit in `scripts/corsair/ninfer-3090.service`.

The 94K profile passed a real 94040-token request, native Hermes cron agent
initialization/inference and stock Hermes vision. MTP was observed in runtime
logs; about 827 MiB GPU memory remained free. The tested 64K/MTP3 and
128K/non-speculative profiles are rollback options. 128K with MTP3 and vision
did not fit. These bounded checks do not claim a full task-quality benchmark.

## Hermes, Pi and tool boundary

Hermes main and steward use Red PARO-int5. `cron.model` names the NInfer slug,
`cron.model_provider` is `aria`, and its per-model context is 96256. The native
scheduler rereads this configuration on each run. Both managed Pi catalogues include PARO-int5. Mac Pi and ARIA’s Red Pi
profile select it by default; Corsair Pi retains its independently selected
CUDA/Halo candidate default, as the original promotion intentionally specified.

Stock Scanner keeps its normal schedule and has no redundant per-job model or
base-URL override. Its earlier 32K profile failed stock Hermes's 64000-token
minimum at agent construction. A simple inference or image canary alone could
not detect that error. `scripts/aria-tools-check` now checks the installed
Hermes context floor, and the model-host native cron canary checks real agent
construction without running the scanner script or delivering a message.
Historical failures remain until a real successful scheduled run.

Stock Hermes owns its native web, browser, files/PDF, vision and scheduling
flows. ARIA's existing fleet, memory, research and managed coding integrations
remain available through its independent MCP bundle. No optional research-start,
document-export, mail/calendar, GitHub or Linear connector was enabled here.
The stock Hermes engine remains unmodified by these repairs.

## Deployment and verification

API/UI/node releases use `scripts/aria-deploy-mac`; MCP has its own
`scripts/aria-deploy-mcp` release. Verify immutable manifests and idle state
before activation. The installed restricted service-restart helper permits
registered Mac service restarts without a cached administrator password.
An API deployment preserves the independently installed MCP boot check.

Use the live `current` symlinks and release receipts for exact build identities.
[Session deployment records](session-deployment-20260915/REPORT.md) distinguish
original activation from later releases. The final durability receipt records
current hashes, boot state, client routes and functional checks. Historical
qualification scripts, recovery copies and old worktrees do not imply running
models and should not be rerun as startup instructions.
