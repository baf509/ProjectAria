# Local inference topology

Current operations guide for ARIA's model data plane. Historical benchmark and
DeepSeek-era measurements remain in dated vault analysis; they are not startup
instructions.

Last reconciled: **2026-08-29**.

## Boundary and routing

The Mac is the control plane and gateway. Corsair serves the primary models.
Consumers use:

- `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1`
- `http://bens-macbook-pro.tailb286a5.ts.net:8200/llm/v1-identified` for pinned/identified consumers including Pi, Hermes local roles, and the ARIA steward

Raw Corsair model endpoints are loopback-only and reach the Mac through managed
SSH forwards. Never publish them directly or configure clients to call them.

| Deployment | Device/pool | Raw listener | Current role |
|---|---|---|---|
| Qwen3.8 Radiance | R9700, 32 GiB discrete VRAM | `127.0.0.1:8080` | resident general model and Pi option |
| Qwen3.8 Flash Next UD-Q4_K_XL | Strix Halo, 124 GiB shared/GTT | `127.0.0.1:8120` | resident long-context Pi option, 1 slot × 256K with MTP/ngram speculative decoding |
| Gemma 4 E4B Q4 | Mac native | `127.0.0.1:8104` | auxiliary workers and side tasks |

DeepSeek V4 weights/runtimes may remain on Corsair for rollback, testing, and
model engineering. They are retained-but-inactive and are not default gateway
targets.

## Registry and observed truth

ARIA owns desired state. The backend readiness identity and Corsair process/unit
state own observed state. A shared open port is not model identity.

Routine starts/stops go through ARIA's restricted actuator. An authorized coding
or model-engineering agent may directly start a registered deployment for a
repair or test, but ARIA must observe, identify, record, and reconcile it.

The Flash registry slug still contains `2x256K` as a compatibility identifier,
but the reconciled registry reports the live 1 × 256K geometry, runtime
`8148b062e`, and MTP/ngram speculative mode. Continue comparing desired state
with backend identity and observed process state.

## Hardware facts that must not be guessed

- DRM `card0` is the R9700 discrete GPU; `card1` is the Strix Halo iGPU.
- The R9700 VRAM and Halo GTT are separate capacity pools, but loading a large
  checkpoint can still pressure host-wide memory.
- Vulkan/ROCm device numbering depends on the runtime build. Verify placement
  after a runtime change; do not copy a `-dev` flag across runtimes.
- GPU-offloaded unified memory is visible in DRM/GTT accounting, not reliably in
  `docker stats` or only `free -h`.
- llama.cpp `-c` is context per sequence. Total KV scales with `-c × -np`.

## Verification

```bash
# Mac control plane and forwards
curl -fsS http://127.0.0.1:8200/api/v1/health
nc -z 127.0.0.1 8080
nc -z 127.0.0.1 8120

# Corsair observed state
systemctl is-active qwen3.8-radiance.service qwen3.8-flash-next.service
ss -ltn | rg '127.0.0.1:(8080|8120)'
curl -fsS http://127.0.0.1:8080/v1/models
curl -fsS http://127.0.0.1:8120/v1/models
```

Use authenticated ARIA endpoints for the registry, model utilization, device
pools, and running infrastructure. Compare those results with the direct
readiness identity before declaring reconciliation complete.

## Pi policy

Pi has one provider (`aria`) and only the Radiance and Flash Next model entries.
Both go through the identified Mac gateway with an inference-only key. Fireworks,
raw Corsair ports, and all other models are forbidden in Pi configuration.

## Known gaps

- Add per-request Mongo usage logging for gateway passthrough traffic.

The Flash registry drift, duplicate Corsair Gemma listener, stale forward
mappings, Hermes/Pi gateway bypasses, and the steward's direct `:8080` route
were closed on 2026-08-29. The managed Mac forward job now carries only current
ports.
