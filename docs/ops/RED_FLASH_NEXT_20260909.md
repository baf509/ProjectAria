# Red Flash Next integration

Requested by Ben on 2026-09-09. Build task `6aa1566ab2ea9c354a8d7f0d`.

Adds the explicit `Red-Qwen3.8-Flash-Next-MXFP4` option on the same Red hardware
as `Red-Qwen3.8-27B-MXFP4`. The remote runtime uses davetha's MXFP4 LRU cache
with a file-backed FP8 PLE adaptation for Red's 96 GB installed RAM.
Hardware artifacts and qualification evidence belong in
`CorsairModelHost/red-r9700/flash-next/`.

Both options share the existing restricted Mac `:8094` → Red `:8081` SSH
forward. Their wire IDs, readiness checks and start/stop verbs differ.
The Red services hold a common GPU lock and refuse concurrent starts.
Stop the active option before starting the other. Sleep stops both before
suspending Red through the existing WoWLAN path.

The observer attributes context/slot geometry only to the matching loaded
model. The gateway's wire-ID cache includes the selected deployment, preventing
a quick model swap on a shared URL from reusing the previous model's ID.
The new option is startable and excluded from automatic selection. A rejected
shared-hardware launch surfaces immediately instead of waiting 20 minutes for
readiness after the remote launcher refused it.

Validation so far: 53 focused Red/gateway/auth tests and 149 model-manager,
measurement and launch-configuration tests pass (one pre-existing skip).
The SSD adapter's three tests pass inside the pinned runtime image; 4096
real PLE rows also match the checkpoint byte-for-byte. Inference qualification passed through the real staged Aria gateway:

- MTP4, TP2, 131072 configured context, one sequence, one image per prompt.
- Exact instruction, JSON/arithmetic, automatic tools, tool results and vision pass.
- Six 256-token samples: 60.3–130.5 tokens/s, median 97.9; TTFT 0.29–0.98 s.
- Retrieval passes at 8202 / 32765 / 119964 prompt tokens; TTFT 3.02 / 9.60 / 35.10 s.
- Minimum host memory headroom 10.88 GiB; peak VRAM 30.00 / 29.96 GiB.

The runtime required exact-size UVA host allocation to avoid ~17 GiB of
PyTorch pinned-buffer rounding. The public FP8 MTP FFNs also require 128
zero channels to make two 128-aligned GPU partitions; all original FP8
weights/scales are preserved. The adapter's three tests verify isolation,
byte preservation and equivalent FFN output after TP reduction. The 47.684
GiB PLE table stays file-backed on SSD. Full expert backing remains in RAM;
the GPU LRU cache holds additional copies.

These are small functional/performance probes, not a broad quality evaluation.
The public RTN checkpoint differs from davetha's local benchmark checkpoint.
Evidence with Aria trace IDs is in the hardware repository's
`red-r9700/flash-next/results/*-20260909.json`.
Release `52c814f-b77a9d2a415c` is built and manifest-verified. Final tests:
133 model-manager/Red tests pass (one pre-existing skip), all four served-build
checks pass, and Inbox/Operate responsive checks pass at 375, 390 and 1280px.
Aria's real manager confirmed the resident, stopped it cleanly, restarted it
in 120.3 seconds, and verified `RED_RESTART_OK` through the gateway. Flash Next
is stopped after qualification; neither model was running before this work.

Activation stopped at the standard deployment privilege preflight: this
session cannot satisfy Mac `sudo`. No live launcher or current-release pointer
was changed. Run both commands in the same Terminal:

```sh
sudo -v
/Users/ben/Services/releases/ProjectAria/20260909T133627Z-52c814f-b77a9d2a415c/scripts/aria-deploy-mac activate /Users/ben/Services/releases/ProjectAria/20260909T133627Z-52c814f-b77a9d2a415c
```

After activation, use **Operate → Red-Qwen3.8-Flash-Next-MXFP4 → Start**.
Select the model explicitly in a client or send its Aria slug as `model` to
`/llm/v1/chat/completions`. Stop Radiance first if it is running. Existing
automatic routing and Red's boot selection remain as before.

The release source is isolated at
`/tmp/aria-red-flashnext-release-source-20260909`, based on the previously
reviewed Inbox cleanup release plus this integration. The canonical checkout
contains other ongoing work and is not copied wholesale into this release.

## Context raised to 262144 (2026-09-09, later same day)

The qualification figures above were measured at the initial `MAXLEN=131072`.
That value was a conservative starting choice, not a memory limit. The
qualification run's own engine log reports the KV pool already built for the
profile:

```
Available KV cache memory: 3.98 GiB
GPU KV cache size: 351,511 tokens
Maximum concurrency for 131,072 tokens per request: 2.68x
```

351,511 allocated KV tokens covers a 262,144-token request with roughly 34%
spare, so `red-r9700/flash-next/profile.env` now sets `MAXLEN=262144`.
`HOT_GB` stays at 15 and `GPU_UTIL` stays at 0.95: the 15 GiB LRU expert cache
is untouched, so decode throughput is unaffected. Maximum concurrency becomes
1.34x, which is irrelevant at `NSEQ=1`.

Only 12 of the 48 layers are full-attention (`full_attention_interval=4`); the
other 36 are gated-delta-net with constant-size state. With
`attention.head_count_kv=2` at TP2 and fp8 KV, growth is roughly 6 KiB per
token per GPU, so the extra 131,072 tokens cost about 1.1 GiB per card out of a
pool that was already provisioned for it.

The installed `/opt/red-r9700/flash-next/profile.env` on Red carries the new
value; the previous file is backed up under
`~/red-r9700/flash-next/backups/`. Aria's registry note, Hermes's
`context_length` and both Pi installations now declare 262144.

**Serving at 262144 is not yet verified.** Red Radiance was resident and
`select_red_model` correctly refused to unload it while the `pi-coding-red`
agent is assigned. The next Flash Next start will pick up the new value; confirm
with `status` (`served_ctx`) and the engine's `GPU KV cache size` line before
treating 262144 as qualified. Qualification evidence remains at 131072.
