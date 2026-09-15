# Flash-Next GPTQ-calibrated MXFP4/FP8/INT6-PLE: Red-only assessment

Updated 2026-09-13 with Hermes's direct HF model-card/API/config verification.

**Verdict: worth pulling approximately 116 GB for a bounded Red-only comparison after the image/checkpoint compatibility gate passes.** This is a fresh GPTQ-calibrated MXFP4+FP8 successor to Red's RTN sibling, with INT6 PLE and an explicit two-R9700 offload recipe. It is a quality-calibration and deployment tradeoff experiment on Red, not a Halogen replacement or 3090 deployment.

**Verify FIRST: an immutable author-image digest paired with the full checkpoint revision, and that pair's tensor/packing/scale/INT6-PLE/MTP compatibility.** The exclusive-loader requirement makes a version mismatch a hard blocker before the weight pull.

Provenance: artifact-specific facts below are accepted from Hermes's supplied direct inspection, linked to their original sources. My browser still failed to retrieve the repository and Docker Hub tag metadata; a direct tag request failed DNS resolution. Those failures do not challenge the verified public artifact. An actual image digest, full model SHA and completed compatibility comparison remain pending; none is invented here. Only this report was changed. No model weights/container images were downloaded, no inference ran, and no model or ARIA state changed.

**1. Verified artifact**

| Property | Verified value |
|---|---|
| Repository | `tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8-GPTQ`; public, not gated |
| Freshness | Created/modified 2026-09-13; revision prefix `bf711965`; 0 downloads, 2 likes at inspection |
| Base / architecture | Quantized `Qwen/Qwen3.8-Flash-Next`; `Qwen4ExpForConditionalGeneration` / `qwen4_exp` |
| Storage | API `usedStorage=116,541,964,712` bytes: 116.54 GB / 108.54 GiB. Repository storage is not an audited sum of one revision's runtime tensors. |
| Parameter metadata | Approximately 175.4B reported; approximately 125B main + 51B PLE. Rounded architecture counts and HF stored-tensor counts need not agree; do not infer missing MTP from their arithmetic. |
| Runtime/device | Author's `tcclaviger/vllm:latest`; card specifies R9700-only and says other images cannot read the checkpoint |

[Model card](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8-GPTQ), [HF API](https://huggingface.co/api/models/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8-GPTQ), directly inspected by Hermes.

The base has 125B main parameters, 6B active, 512 routed experts/top-10 plus a shared expert, 36 Gated DeltaNet and 12 QSA layers, approximately 51B n-gram embeddings and 262,144 native context. Its one-layer MTP component is trained for multiple speculative steps. [Official base](https://huggingface.co/Qwen/Qwen3.8-Flash-Next)

| Component | Confirmed encoding |
|---|---|
| Export | `quant_method=compressed-tensors`; `format=mxfp4-pack-quantized` |
| group_0 | General Linear weights: MXFP4, with specific overrides below |
| group_1 | Self-attention q/k/v/o and linear-attention input/output projections: FP8 |
| group_2 | MTP routed/shared expert weights: FP8 |
| PLE | INT6; verified listing includes `model-ple-int6-095..127.safetensors` within a 128-plus-shard layout |
| KV | Calibrated FP8 KV-cache scales supplied; serving uses FP8 KV |

[Candidate config](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8-GPTQ/blob/main/config.json), [file tree](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8-GPTQ/tree/main), supplied inspection.

GPTQ is the calibration algorithm choosing MXFP4 weight values, not a separate GPTQ-INT4 serialization or CUDA requirement. AMD Quark supports GPTQ-for-MXFP4. Do not force a generic GPTQ loader from the name. [AMD Quark documentation](https://quark.docs.amd.com/release-0.9/release_note.html)

FP8 covers weights as well as KV. The supplied config summary does not enumerate every activation/scale/block field; inspect those at the pinned revision rather than copying sibling values. The complete index must establish exact shard count and MTP/PLE tensor coverage. Multimodal and MTP components are documented; a separate GGUF draft or mmproj is not established. This is a safetensors/custom-loader deployment.

**2. Recipes, context and published evidence**

| Recipe | Configuration |
|---|---|
| TP4, four R9700s | 524,288 context, YaRN ×2, MTP3, 16 sequences, FP8 KV, chunked prefill |
| **TP2, Red's two R9700s** | `--recipes tp2-expert-mem-60gb-ple-cache-8gb`; 60 GiB expert RAM offload; NVMe PLE plus 8 GiB row cache; 262,144 context, YaRN ×1, MTP3 |

The TP2 card claims **100 tok/s single-request decode**, **323 tok/s aggregate at 16 concurrent requests**, and **82 GiB system RAM**. Its fresh BetterBench report is dated September 13, named `Next-Throughput-INT6PLE-28.04.8`, with two-page PNG/PDF attachments. These are author measurements, not Red measurements; the report name is not an image digest. No numeric prefill result was supplied. [Card and attached report](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8-GPTQ)

Card-listed multimodal results: ERQA 72.3; LVBench 76.6; RealWorldQA 88.5; MathVision 90.6/95.7; CharXiv 84.6/90.6. The supplied excerpt does not identify paired-column meanings or establish a quantized-versus-base methodology. These do not prove improved coding/tool use over RTN or Halogen. [Evaluation table](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8-GPTQ)

Thinking defaults on; reasoning_effort supports xhigh (default), medium and low. Explicitly select medium for the bounded A/B and verify rendered template/settings. [Template files](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8-GPTQ/tree/main)

INT6 PLE is another quality change alongside calibrated experts: measured differences cannot all be attributed to GPTQ. PLE is a learned local n-gram lookup, not the prompt cache or context-limit mechanism. Correct prefix caching also needs attention/GDN/PLE state handling; verify a shared-prefix follow-up. Native TP2 context remains 262,144; TP4's 524,288 uses extension. [Architecture](https://github.com/QwenLM/Qwen3.8-Flash-Next)

**3. Fleet fit**

| Deployment | Existing evidence | Candidate role |
|---|---|---|
| Halogen W4B/Halo, current default | 262,144/request; four slots share 524,288 positions; prompt cache, MTP1; approximately 17–18 tok/s per loaded stream | Unsupported hardware for this artifact. Preserve Halogen's role. |
| **Red Flash-Next RTN** | 262,144 configured; one sequence; SSD FP8 PLE, RAM experts and 15 GB/rank GPU cache; FP8 KV, prefix cache, MTP4. Short qualification median 97.9 tok/s; retrieval through 119,964 input tokens | Direct baseline for calibrated experts + INT6 PLE + author TP2 runtime. Potential quality/storage benefit, not yet measured. |
| Previous CUDA-Halo candidate | One 262,144 slot; q8_0 KV; GGUF MTP3; approximately 443/57 and 462/56 prefill/decode tok/s at 4K/8K; unresolved intermittent CUDA faults | Different format/runtime; new R9700 weights do not repair those faults. |
| NInfer/RTX 3090 | Existing 27B deployment | No trial: checkpoint unsupported on this device. |

[Red qualification](../CorsairModelHost/red-r9700/flash-next/README.md), [Red launcher](../CorsairModelHost/red-r9700/flash-next/serve.sh), [CUDA-Halo evidence](docs/ops/LOCAL_INFERENCE_TOPOLOGY.md), [Halogen incident](/Users/ben/Development/benchmark-tooling/evalstack/suites/halogen-halo-unresponsive.md). Current roles follow operator context; these historic samples are not fresh live observations.

Red has about 89–89.6 GiB usable host RAM. An 82 GiB footprint leaves only 7–8 GiB nominal headroom. Check accounting for startup peak, pinned backing, PLE cache and other processes; do not blindly reuse the old 82 GiB container limit. Retain baseline files and budget disk for both checkpoints and any derived tables. NVMe PLE is essential: checkpoint disk size is not RAM residency. [Existing memory design](../CorsairModelHost/red-r9700/flash-next/README.md)

**4. Minimal Red-only test — planned, not executed**

**A. Before downloading weights: compatibility gate**

1. Resolve `tcclaviger/vllm:latest` using registry manifest inspection. Record immutable digest, linux/amd64 platform digest where applicable, version/source labels if available, and full HF commit matching `bf711965`. Later run `tcclaviger/vllm@sha256:<verified-digest>`. A release tag/source commit alone is not an image pin. Actual resolution remains pending because registry access failed here.
2. Inspect only README/config/index/template/file metadata at that revision against Red RTN commit `f6adddb64dc6a54cfcc3ae9786f26a6b42b2a39d`. Compare expert tensor names, MXFP4 packing and group/block dimensions; scale names/dtypes/shapes; FP8 attention/MTP layout; KV-scale loading; INT6 PLE packing, addressing, scales and offload/cache support.
3. An index maps names to shards, normally not shapes/dtypes. Where needed, inspect bounded safetensors headers and the exact loader source/version. Header range reads must stop after the header; never accept a full shard because a server ignores Range.
4. **Do not reuse Red's FP8 PLE adapter unchanged.** Its `ssd_ple.py` and `ngram.fp8.bin` expect FP8; the candidate requires INT6. Use the author's pinned TP2 recipe. Do not overlay old PLE/UVA/LRU/MTP source mounts until each is checked against that image. Similar offload architecture is not loader/ABI compatibility.
5. Verify TP2/MTP alignment: existing 640-channel FP8 FFNs split into 320/rank, which is not divisible by a 128-wide block. Red's adaptation appends 128 zero channels globally, yielding 384/rank. Preserve equivalent handling if still necessary, or document new kernel/layout support that removes the need. Check scale shapes and target/draft isolation; do not double-pad. [Existing adaptation](../CorsairModelHost/red-r9700/flash-next/README.md)

**Pass this gate before pulling 116 GB.** The author's new image is the required starting point; do not substitute the old davetha image merely because both use expert offload.

**B. Prepare before serving**

Use the verified TP2 recipe on Red: FP8 KV, 262,144 context/YaRN ×1, MTP3, chunked prefill, `qwen3_coder` tool parser and `qwen3` reasoning parser. Start with one admitted sequence. Rewrite `--host 0.0.0.0` to **`--host 127.0.0.1`**, preserving Red's loopback forwarding boundary. Inspect expanded recipe options to confirm explicit overrides take effect.

Use a distinct candidate ID, pinned local checkpoint path, existing Red exclusivity controls and sequential baseline/candidate windows. Neither shares the cards with Radiance. Preserve baseline image/weights/config for rollback. Check startup RAM peak, loaded KV scales, MTP3 acceptance and INT6 row-cache accounting; avoid needless 16-sequence preallocation. These are future pre-serve requirements, not changes executed or authorized by this read-only update.

**C. Only three small evalstack benches**

Match native 262,144 context, selected inputs, explicit medium reasoning, sampling and token budgets. Inspect chat-template semantics; record unavoidable template differences. Keep actual inputs within 8K tokens, rejecting oversized samples identically rather than truncating silently. If baseline retains MTP4 versus candidate MTP3, label results an end-to-end deployment comparison; matched-MTP3 isolation can be later work.

| Bench | Work per arm | Bounds |
|---|---|---|
| `perf_throughput` | 1K/4K **word** prompts, three reps each; record actual token counts | 512 output tokens; concurrency 1 |
| `livecodebench` | Four fixed release_v6 IDs, including medium difficulty; same grader/tests | 8,192 total output tokens including reasoning; concurrency 1; 300-second request budget; zero retries |
| `bfcl` | Ten fixed cases: two each simple_python, multiple, parallel, parallel_multiple, irrelevance | 2,048 output tokens; one connection; 120-second request/sample budget; zero retries |

Use run-local settings, not the general flagship/newmodel suites. Current catalog defaults include LCB concurrency 4/16,384 tokens and BFCL 32,768 tokens/retries/3,600-second timeouts: override them. BFCL categories concatenate, so an unshuffled limit of ten is not a balanced subset. Fix IDs before either arm. Verify the grading container/result capture first; earlier LCB grading failed on a mount path. [Catalog](/Users/ben/Development/benchmark-tooling/evalstack/suites/catalog.yaml), [prior incident](/Users/ben/Development/benchmark-tooling/evalstack/suites/halogen-halo-unresponsive.md)

Record pass counts, BFCL AST accuracy, cap hits, parser errors, TTFT, prefill/decode rates, wall time, reasoning/output counts, cache hits, MTP acceptance and memory pressure. Add one 8K-prefix/follow-up pair (512-token caps), without globally clearing cache. One bounded cancellation check must show the slot drains before another request; if release is ineffective/unobservable, stop submissions rather than retry.

Budget **25 minutes per arm, 50 minutes paired**, excluding pull/startup, and report incomplete work. Stop scheduling on timeout, loader/KV/MTP errors, failed cancellation or memory-pressure collapse. Cap hits are separate from wrong answers; no automatic cap increases. This is a small regression screen, not a card-score reproduction or native-context/reliability qualification. No overnight run or 16-way soak.

**5. Deadline-aware limits**

```text
queue + prefill + max_tokens / slowest_stream_rate + overhead < 1800 s

For approximately equal streams:
decode_time ≈ concurrency × max_tokens / aggregate_rate(concurrency)
```

The gateway config is an HTTPX 1,800-second read timeout, not a universal total timer; streaming chunks may reset it. Budget end-to-end anyway for the non-streaming/no-response path that failed. Bound queueing and prevent retries while abandoned requests retain slots. [Gateway](api/aria/api/routes/llm_proxy.py:100), [incident](/Users/ben/Development/benchmark-tooling/evalstack/suites/halogen-halo-unresponsive.md)

At C=16 the card implies only **323/16 = 20.19 tok/s per stream on average**, not 100. That average does not guarantee the slowest stream.

| max_tokens × concurrency | Decode at card rate | Half card rate + 300 s queue/prefill/overhead |
|---|---:|---:|
| 8,192 × 1 | 81.9 s | 463.8 s |
| 16,384 × 1 | 163.8 s | 627.7 s |
| 8,192 × 16 | 405.8 s | 1,111.6 s |
| 16,384 × 16 | 811.6 s | 1,923.2 s — over deadline |
| 32,768 × 16 | 1,623.2 s | 3,546.4 s — inadequate margin even without derating |
| 65,536 × 16 | 3,246.4 s | 6,792.7 s — over deadline outright |

These are calculated planning budgets, not validated production limits. Reserve 300 seconds and aim for ≤1,200 seconds total: at half card rate, the C×max_tokens ceilings are 45,000 for C=1 and 145,350 for C=16. Different aggregate-rate assumptions apply; do not interpolate to C=2/4/8 without measurement.

**Initial bounds: C=1; code 8,192, BFCL 2,048, perf 512.** An 8,192-token code response within 300 seconds requires about 33 tok/s when prefill takes 50 seconds; measure first and reduce caps/stop if this fails. **8,192 × 16** is a later candidate only after the slowest stream sustains about 10.1 tok/s, queue/prefill/overhead stays within 300 seconds, and memory/KV admission passes. It is outside the minimal test. Serial 16,384 is also a later measured option.

No queued waves during testing. Bound admitted requests, not just client connections. Separately enforce prompt+reserved-output per window and across the actual candidate KV pool. Sixteen-way throughput does not prove sixteen full 262K contexts fit.

**6. Remaining risks and decision**

- Freshness is verified; maturity is not. Low download count is a snapshot, not a quality verdict.
- Moving `:latest` and changing weights require paired immutable pins. Throughput report label `28.04.8` is not a digest.
- The author's [sibling discussion](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8/discussions/1) documents TP/MTP alignment failures, changing images/weights, FP8 scale-loading/KV fixes and PLE swap-related degradation. Historic fixes do not prove the new release's compatibility.
- Sibling snapshots differ: `ffafcb1` tree versus `0144a5e` fetched config. Compare with Red's actual pinned files, not mixed web-cached main snapshots. [Tree](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8/tree/main), [config](https://huggingface.co/tcclaviger/Qwen3.8-Flash-Next-MXFP4-FP8/blob/main/config.json)
- INT6 PLE requires the new loader; old FP8 adapters/source overlays are a hard risk. GPTQ quality gains and INT6 losses must be assessed together.

**Yes: pull and test on Red after the immutable-image/packing/scales/INT6/MTP gate passes and memory/disk budgets fit.** Failure at that first gate blocks the download until resolved. Passing this small A/B earns further qualification, not automatic promotion. No Halo, 3090 or fleet-default change is proposed.
