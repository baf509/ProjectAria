"""Isolated Red GPTQ registry entry, activated only by its qualification gateway.

The production registry and all automatic routes remain unchanged. Hardware
artifacts: CorsairModelHost/red-r9700/flash-next-gptq. ARIA task:
6aa6fe21e93eb203d99ca38f.
"""

SLUG = "Red-Qwen3.8-Flash-Next-GPTQ"


def make_spec(spec_type):
    ssh = (
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "UserKnownHostsFile=/Users/ben/.ssh/known_hosts_red_linux",
        "-o", "HostKeyAlias=red-linux", "ben@red-linux.tailb286a5.ts.net",
    )
    return spec_type(
        slug=SLUG,
        description="Isolated Qwen3.8 Flash Next activation-aware MXFP4 GPTQ, "
                    "FP8 attention/MTP/KV, INT6 n-gram table streamed from Lexar NVMe. "
                    "Upstream TP2 expert-offload recipe; functional, 249K retrieval "
                    "and 16-request concurrency checks passed on 2026-09-13.",
        runtime_repo="https://hub.docker.com/r/tcclaviger/vllm",
        runtime_ref="sha256:e9d41499ab82e09bd2fddca0bd8589143752705eb0e5e4db7baca78294bd896c",
        backend_device="2 x gfx1201 (Radeon AI PRO R9700, 32 GiB each)",
        runtime_family="vllm", onbox=False, startable=False,
        allow_force_start=False, auto_route=False, memory_pool="remote",
        not_startable_reason="Isolated qualification; use the documented Red systemd service.",
        devices=("Red R9700 0000:03:00.0", "Red R9700 0000:06:00.0"),
        host_machine="machine:red", deployment="red-r9700/flash-next-gptq",
        container_name="red-flashnext-gptq", port=18078,
        bench_decode_tok_s=110.50488896444844,
        bench_at="2026-09-13",
        bench_note="Median of six 256-token non-thinking samples through the isolated ARIA "
                   "gateway: 80.9–138.6 tok/s. Exact instruction, JSON/arithmetic, tools, "
                   "vision, 249581-token retrieval and 16 concurrent JSON checks passed. "
                   "Small qualification workload, not a broad model-quality evaluation.",
        exclusive_with=("Red-Qwen3.8-27B-MXFP4", "Red-Qwen3.8-Flash-Next-MXFP4",
                        "Red-Qwen3.8-27B-PARO-MXFP4"),
        remote_start_command=ssh + ("systemctl --user start red-flashnext-gptq",),
        remote_stop_command=ssh + ("systemctl --user stop red-flashnext-gptq",),
        remote_health_url="http://127.0.0.1:18078/health",
        remote_model_id="red-qwen3.8-flash-next-gptq",
        remote_ready_deadline=1800.0,
        endpoint_override="http://127.0.0.1:18078/v1",
        consumers_note="Qualification gateway only; no automatic routing or client defaults. "
                       "84 GiB container cap, no container swap, 60 GiB expert budget, "
                       "8 GiB PLE row cache. Configured TP2/MTP3, 262144 context, 16 sequences.",
    )
