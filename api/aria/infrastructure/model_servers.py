"""
ARIA - Model Server Registry & Control Plane

Purpose: Single source of truth for the local LLM model servers on this box —
which docker-compose service / runtime fork each one needs, whether they can
coexist in RAM, and which ARIA agent (or external consumer) each one is bound
to.

As of 2026-07-29, ALL model-server start/stop on this box is meant to go
through this module — no more hand-run `docker`/`docker compose` commands
against laguna/chadrock/qwen/gemma-aux. This replaces the old
LlamaCppModelSwitcher, which targeted the retired single-`llamacpp`-on-:8080
topology and had no concept of per-service compose files, profile gating,
runtime forks, or RAM exclusivity — none of that carries over to the current
multi-service topology, so it was removed rather than kept alongside this.

Each server on this box runs a DIFFERENT llama.cpp fork/build — mixing a model
with the wrong runtime either refuses to load or can wedge the GPU (RADV
device-lost). See runtime_repo/runtime_ref/backend_device per entry below.

RAM safety is two-layered:
  1. Static exclusive pairs (_EXCLUSIVE_PAIRS) — only the combinations that
     always overflow the ~124 GiB box regardless of what else is happening.
     In practice that is Laguna-S-2.1 (~87 GiB) against every other
     GPU-resident model; all smaller combinations are left to layer 2. The
     pairs are expanded symmetrically into each spec's exclusive_with, and
     the conflict check counts any memory-holding container state
     (running/paused/restarting), not just "running".
  2. A live GTT-usage read (/sys/class/drm/card0/device/mem_info_gtt_*, the
     same ground-truth signal `shells/selfcheck.py` uses — docker/cgroup
     memory limits do NOT see GPU-offloaded allocations on this unified-memory
     box) plus each entry's resident_gib, which is a SWAG, not a measurement.
     Skipped for CPU-only servers (gtt_resident=False): their allocations
     never appear in the GTT pool, so the projection would be meaningless.
Both gates can be bypassed with force=True; neither is bypassed by default.
All state-changing operations serialize on one asyncio.Lock so two concurrent
callers can't both pass the check-then-act gates.

Start dispatch: compose-managed containers (and missing ones) go through
`docker compose up -d`, which natively reconciles config drift — if the
compose file changed since the container was created, the container is
recreated with the new config instead of silently resurrecting the old argv.
Hand-run containers (no com.docker.compose.project label — chadrock and
qwen3.6-35b-a3b are hand-run as of 2026-07-29) get a raw `docker start`,
because `compose up` would hit a container-name conflict instead of adopting
them; the response carries a note that compose-file changes are NOT applied
on that path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings

logger = logging.getLogger(__name__)


class ModelServerError(Exception):
    """Base error for model-server operations."""


class ModelServerNotFound(ModelServerError):
    pass


class ModelServerSafetyError(ModelServerError):
    """Raised when start() is refused by the exclusivity or RAM-SWAG gate
    (or when an action is attempted against an off-box/unwired server)."""


class ModelServerBindingConflict(ModelServerError):
    """Raised when bind() targets a slug already bound to a different agent."""


@dataclass(frozen=True)
class ModelServerSpec:
    slug: str
    description: str
    runtime_repo: str
    runtime_ref: str
    backend_device: str
    model_file: Optional[str] = None  # relative to infrastructure_root
    port: Optional[int] = None
    compose_file: Optional[str] = None  # relative to infrastructure_root
    service_name: Optional[str] = None  # docker-compose service key
    container_name: Optional[str] = None  # actual `docker ps` container name
    profile: Optional[str] = None  # docker compose --profile, if gated
    resident_gib: Optional[float] = None  # SWAG resident footprint; None = unmeasured
    gtt_resident: bool = True  # False = CPU-only, allocations never hit the GTT pool
    exclusive_with: tuple[str, ...] = ()
    onbox: bool = True  # False = ARIA cannot start/stop it (e.g. Ridge)
    startable: bool = True  # False = no working runtime/compose service exists yet
    not_startable_reason: Optional[str] = None
    consumers_note: Optional[str] = None  # descriptive, e.g. "Hermes auxiliary tasks + cron"
    # Off-box only: command that suspends the remote machine (its wake path is
    # separate — e.g. Ridge's WoL proxy wakes it on the next inference request).
    sleep_command: Optional[tuple[str, ...]] = None
    # Consumer-facing OpenAI-compatible endpoint override. Default is computed
    # from `port` (localhost + tailnet variants); set this when the URI isn't
    # port-derivable — e.g. Ridge, reached ONLY via the tailnet-bound proxy
    # (localhost:8092 is connection-refused, a repeatedly-misdiagnosed gotcha).
    endpoint_override: Optional[str] = None


# Only the pairs that ALWAYS overflow the box, per the compose-file headers
# and arithmetic on the SWAGs below (92% of the 124 GiB GTT pool ≈ 114 GiB).
# Laguna at ~87 GiB can't share the GPU pool with any other GPU model except
# 16 GiB context1 (87+16=103, under margin — and their historical coexistence
# was never contradicted); every other combination fits statically and is
# guarded by the live GTT gate instead. Deliberately NOT here:
#   qwen-chat + qwen-agentic — designed to start together (`--profile qwen`,
#     ~61 GiB combined per laguna's compose header);
#   chadrock + ROCmFP4-qwen — the deliberate two-server split, measured
#     ~89.4 GiB combined (qwen3.6-35b-a3b compose header, 2026-07-28);
#   context1 + anything — it ran alongside both qwens historically.
_EXCLUSIVE_PAIRS: tuple[tuple[str, str], ...] = (
    ("Laguna-S-2.1", "Chadrock-Laguna-S-2.1"),        # chadrock compose: "CANNOT both be resident"
    ("Laguna-S-2.1", "qwen3.6-35b-a3b-Q4"),           # laguna compose: exclusive with the qwen pair
    ("Laguna-S-2.1", "qwen3.6-27b-Q8"),
    ("Laguna-S-2.1", "ROCmFP4-qwen3.6-35b-a3b"),      # 87+29 SWAG > margin; never validated together
    ("Laguna-S-2.1", "Chadrock-ROCmFP6-qwen3.6-27b"), # 87+30 > margin
    ("Laguna-S-2.1", "Qwythos-27b-Q8"),               # 87+35 > margin
)


def _exclusive_with(slug: str) -> tuple[str, ...]:
    """Symmetric expansion of _EXCLUSIVE_PAIRS for one slug — start() only
    consults the starting spec's own list, so both directions must be present."""
    return tuple(
        (b if a == slug else a) for a, b in _EXCLUSIVE_PAIRS if slug in (a, b)
    )


REGISTRY: tuple[ModelServerSpec, ...] = (
    ModelServerSpec(
        slug="Laguna-S-2.1",
        description="poolside 118B-A8B MoE coding model. Retired 2026-07-28 in "
        "favor of the two-server split (ROCmFP4-qwen3.6-35b-a3b + gemma-4-e4b-Q4); "
        "not currently created.",
        runtime_repo="https://github.com/poolsideai/llama.cpp.git",
        runtime_ref="branch laguna",
        backend_device="HIP (ROCm0)",
        model_file="models/llm/Laguna-S-2.1-GGUF/laguna-s-2.1-Q4_K_M.gguf",
        port=8095,
        compose_file="laguna/docker-compose.yml",
        service_name="laguna",
        container_name="laguna",
        resident_gib=87,
        exclusive_with=_exclusive_with("Laguna-S-2.1"),
    ),
    ModelServerSpec(
        slug="Chadrock-Laguna-S-2.1",
        description="Laguna S 2.1 ROCmFP4 StrixKVSpine V4 on the ciru-ai ROCmFPX "
        "Vulkan runtime. Pool CLI's dedicated coding backend (--parallel 1). "
        "Physically shut down by Ben 2026-07-29.",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="branch agent/laguna-radv-device-lost-20260724 @ 090e317b4e2f998a9470faeb076cf841ba72b739",
        backend_device="Vulkan0 (not HIP — no /dev/kfd, no ROCm)",
        model_file="models/llm/Laguna-S-2.1-Chadrock-ROCmFP4/laguna-s-2.1-ROCmFP4-StrixKVSpine-v4.gguf",
        port=8102,
        compose_file="chadrock/docker-compose.yml",
        service_name="chadrock",
        container_name="chadrock",
        profile="chadrock",
        # ~60, not the "~90 GiB class" assumption in chadrock's own compose
        # header — that predates the measured truth in qwen3.6-35b-a3b's
        # header (2026-07-28): chadrock + qwen = ~89.4 GiB combined, and qwen
        # alone is ~29. Keeping 90 here made the RAM gate refuse the exact
        # chadrock+qwen coexistence the two-server split was designed for.
        resident_gib=60,
        exclusive_with=_exclusive_with("Chadrock-Laguna-S-2.1"),
        consumers_note="pool CLI (ProjectAria coding backend) only",
    ),
    ModelServerSpec(
        slug="ROCmFP4-qwen3.6-35b-a3b",
        description="Qwen3.6-35B-A3B-MTP, embF16/headQ6 mix, on the charlie12345 "
        "ROCmFPX Vulkan runtime. Currently the active Hermes/ARIA chat backend. "
        "ctx bumped 100000 -> 262144 (n_ctx_train) on 2026-07-30 at Ben's request; "
        "see the compose file's -c comment for the crash history this reverses "
        "and why concurrent GPU load (not ctx size) was the real trigger.",
        runtime_repo="https://github.com/charlie12345/ROCmFPX.git",
        runtime_ref="branch main (build-strix-rocmfp4-mtp.sh; needs a HIP toolchain to "
        "build even though it serves on Vulkan0)",
        backend_device="Vulkan0 (built with HIP/ROCm toolchain)",
        model_file="models/llm/Qwen3.6-35B-A3B-MTP-ROCmFP4/Qwen3.6-35B-A3B-MTP-ROCmFP4-STRIX-embF16-headQ6.gguf",
        port=8103,
        compose_file="qwen3.6-35b-a3b/docker-compose.yml",
        service_name="qwen3.6-35b-a3b",
        container_name="qwen3.6-35b-a3b",
        # MEASURED 2026-07-30: ~29 GiB solo at 262144 ctx (n_ctx_train) —
        # barely different from the prior 100000-ctx figure (24.7-29 GiB).
        # Real growth was far below the ~40 GiB projection; f16 KV apparently
        # doesn't scale as steeply here as the sublinear-but-still-present
        # curve chadrockv2 showed. Also stress-tested: concurrent 64.5K-token
        # prompts fired at qwen and chadrockv2 simultaneously (comparable
        # scale to the 2026-07-28 crash's 87-95K trigger) — both completed
        # cleanly, zero container state changes, peak 72.4 GiB combined.
        resident_gib=29,
        exclusive_with=_exclusive_with("ROCmFP4-qwen3.6-35b-a3b"),
        consumers_note="Hermes main chat + ARIA default/search chat agents (both "
        "currently disabled, so single active consumer in practice)",
    ),
    ModelServerSpec(
        slug="qwen3.6-35b-a3b-Q4",
        description="Qwen3.6-35B-A3B-MTP UD-Q4_K_XL on the charlie12345 rocmfp4-llama "
        "HIP runtime. Profile-gated (`qwen`), retired, not currently created. Designed "
        "to run TOGETHER with qwen3.6-27b-Q8 (~61 GiB pair). Moved off :8092 to :8107 "
        "on 2026-07-30 — ridge-llama-proxy holds :8092 on the tailnet IP, so this "
        "service could never have bound there while the proxy runs.",
        runtime_repo="https://github.com/charlie12345/rocmfp4-llama.git",
        runtime_ref="branch mtp-rocmfp4-strix",
        backend_device="HIP (ROCm0)",
        # NOT under models/llm/ — this compose project mounts its own ./models dir.
        model_file="qwen-rocmfp4/models/Qwen3.6-35B-A3B-MTP/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        # MOVED 8092 -> 8107 (2026-07-30): ridge-llama-proxy holds :8092 on the
        # tailnet IP, so this service could never bind there while the proxy runs.
        port=8107,
        compose_file="qwen-rocmfp4/docker-compose.yml",
        # renamed from qwen-chat 2026-07-29 (service + container_name, safe
        # while not created) so the compose service matches this slug.
        service_name="qwen3.6-35b-a3b-Q4",
        container_name="qwen3.6-35b-a3b-Q4",
        profile="qwen",
        resident_gib=35,
        exclusive_with=_exclusive_with("qwen3.6-35b-a3b-Q4"),
    ),
    ModelServerSpec(
        slug="qwen3.6-27b-Q8",
        description="Qwen3.6-27B Q8_0 on the charlie12345 rocmfp4-llama HIP runtime. "
        "Profile-gated (`qwen`), retired, not currently created. Designed to run "
        "TOGETHER with qwen3.6-35b-a3b-Q4 (~61 GiB pair).",
        runtime_repo="https://github.com/charlie12345/rocmfp4-llama.git",
        runtime_ref="branch mtp-rocmfp4-strix",
        backend_device="HIP (ROCm0)",
        # NOT under models/llm/ — this compose project mounts its own ./models dir.
        model_file="qwen-rocmfp4/models/Qwen3.6-27B/Qwen3.6-27B-Q8_0.gguf",
        port=8093,
        compose_file="qwen-rocmfp4/docker-compose.yml",
        # renamed from qwen-agentic 2026-07-29 (service + container_name, safe
        # while not created) so the compose service matches this slug.
        service_name="qwen3.6-27b-Q8",
        container_name="qwen3.6-27b-Q8",
        profile="qwen",
        resident_gib=30,
        exclusive_with=_exclusive_with("qwen3.6-27b-Q8"),
    ),
    ModelServerSpec(
        slug="context1-Q4",
        description="chromadb/context-1 20B Q4_K_M, same charlie12345 rocmfp4-llama "
        "HIP runtime as qwen3.6-35b-a3b-Q4/qwen3.6-27b-Q8. Backed ARIA's Search Agent; "
        "stopped and profile-gated (`optional`) since 2026-07-21, unused. Small enough "
        "(16 GiB) to coexist with anything — guarded by the live GTT gate only. "
        "Its port binding was 0.0.0.0 until 2026-07-30 (missed by the 07-21 "
        "loopback+tailnet sweep because it was already stopped); now matches.",
        runtime_repo="https://github.com/charlie12345/rocmfp4-llama.git",
        runtime_ref="branch mtp-rocmfp4-strix",
        backend_device="HIP (ROCm0)",
        model_file="models/llm/context-1/chromadb-context-1-Q4_K_M.gguf",
        port=8081,
        compose_file="qwen-rocmfp4/docker-compose.yml",
        service_name="context1",
        container_name="context1",
        profile="optional",
        resident_gib=16,
        exclusive_with=_exclusive_with("context1-Q4"),
        consumers_note="ARIA Search Agent (context1 backend) — currently disabled",
    ),
    ModelServerSpec(
        slug="gemma-4-e4b-Q4",
        description="Gemma 4 E4B-it Q4_0, CPU-only on mainline llama.cpp. Never "
        "contends with the GPU-resident servers.",
        runtime_repo="https://github.com/ggml-org/llama.cpp.git",
        runtime_ref="mainline (ghcr.io/ggml-org/llama.cpp:server image, no custom build)",
        backend_device="CPU only",
        model_file="models/llm/Gemma-4-E4B-it-Q4_0-GGUF/gemma-4-E4B-it-Q4_0.gguf",
        port=8104,
        compose_file="gemma-aux/docker-compose.yml",
        service_name="gemma-aux",
        container_name="gemma-aux",
        resident_gib=8,
        # CPU allocations never appear in mem_info_gtt_used, so projecting
        # this against the GTT pool is a category error — the compose file's
        # own mem_limit/oom_score_adj are the real guard here.
        gtt_resident=False,
        exclusive_with=_exclusive_with("gemma-4-e4b-Q4"),
        consumers_note="Hermes auxiliary side-tasks (~16, e.g. title_generation/"
        "compression/curator/triage) + 2 cron jobs (alert triage, stock scanner)",
    ),
    ModelServerSpec(
        slug="Chadrock-ROCmFP6-qwen3.6-27b",
        description="Chadrockv2 Qwen3.6-27B ROCmFP6 STRIX QUALITY (Q6_0_ROCMFPX bulk "
        "+ Q8 attention/FFN bands), text-only, MTP draft speculation, 262144 ctx "
        "(= n_ctx_train, bumped from 65536 on 2026-07-30 at Ben's request). "
        "Runs on the EXISTING ciru-ai ROCmFPX Vulkan image — its bundled profile's "
        "claim of needing a HIP build was an artifact of the author's own machine "
        "path; verified loading and generating on Vulkan0 2026-07-30.",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="branch agent/laguna-radv-device-lost-20260724 @ 090e317b4e2f998a9470faeb076cf841ba72b739 "
        "(shared with Chadrock-Laguna-S-2.1 — same chadrock-rocmfpx:latest image)",
        backend_device="Vulkan0 (not HIP — no /dev/kfd, despite the profile's DEVICE=ROCm0)",
        model_file="models/llm/Chadrockv2-Qwen3.6-27B-ROCmFP6-STRIX-QUALITY/"
        "Chadrockv2-Qwen3.6-27B-ROCmFP6-STRIX-QUALITY.gguf",
        port=8105,
        compose_file="chadrockv2/docker-compose.yml",
        service_name="chadrockv2",
        container_name="chadrockv2",
        # MEASURED 27 GiB at 4096 ctx, 30.69 GiB at 65536 ctx, 38.68 GiB at
        # 262144 ctx = n_ctx_train (all 2026-07-30). Loaded clean, no
        # warnings, verified generating correctly at the new ctx. The linear
        # KV-scaling projection (~52-60 GiB) overshot the real number —
        # actual overhead growth was sublinear past 65536, likely because
        # part of the fixed compute-buffer/graph overhead doesn't scale with
        # ctx. Padded modestly above measured for headroom.
        resident_gib=42,
        exclusive_with=_exclusive_with("Chadrock-ROCmFP6-qwen3.6-27b"),
    ),
    ModelServerSpec(
        slug="Qwythos-27b-Q8",
        description="Qwythos-27B-v1 MTP Q8_0 (Empero) with the F16 vision projector — "
        "multimodal, MTP draft speculation, 65536 ctx (weights go to 1M; KV cost is "
        "why they don't here). Standard GGUF, served on the same ciru-ai ROCmFPX "
        "Vulkan image; verified 2026-07-30 driving mmproj + draft-mtp together.",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="branch agent/laguna-radv-device-lost-20260724 @ 090e317b4e2f998a9470faeb076cf841ba72b739 "
        "(chadrock-rocmfpx:latest — chosen for being already built + Vulkan; any "
        "recent MTP-capable llama.cpp would also serve this standard GGUF)",
        backend_device="Vulkan0",
        model_file="models/llm/Qwythos-27B-v1-GGUF/Qwythos-27B-MTP-Q8_0.gguf "
        "(+ mmproj-Qwythos-27B-F16.gguf)",
        port=8106,
        compose_file="qwythos/docker-compose.yml",
        service_name="qwythos",
        container_name="qwythos",
        # MEASURED 32 GiB at 8192 ctx WITH mmproj + MTP draft ctx (2026-07-30);
        # budgeted for the larger KV at the configured 65536 ctx.
        resident_gib=40,
        exclusive_with=_exclusive_with("Qwythos-27b-Q8"),
        consumers_note="unbound — the only vision-capable local model on this box",
    ),
    ModelServerSpec(
        slug="Ridge-Qwen3.6-35B-A3B",
        description="Qwen3.6-35B-A3B on Ridge's RTX 3090 (NInfer), reached through "
        "corsair's ridge-llama-proxy (Wake-on-LAN, ~90s cold first byte). Off-box: "
        "ARIA has no start/stop control, only a descriptive binding.",
        runtime_repo="NInfer (Ridge's own inference stack — not a corsair llama.cpp fork)",
        runtime_ref="remote",
        backend_device="remote CUDA",
        onbox=False,
        startable=False,
        not_startable_reason="Off-box — the ridge-llama-proxy wakes it (WoL) on the next "
        "inference request; there is nothing to 'start' from here.",
        consumers_note="pi-coding-ridge",
        # Ben keeps Ridge suspended when idle. `ssh ridge` is the established
        # path (Windows 11, PowerShell default shell, key already authorized);
        # SetSuspendState is the standard command-line suspend. Waking is NOT
        # ARIA's job — the proxy WoLs it on demand.
        sleep_command=(
            "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "ridge",
            "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        ),
        endpoint_override="http://100.123.245.84:8092/v1",
    ),
)

# This node's stable Tailscale IP — same constant every compose file binds to.
_TAILNET_IP = "100.123.245.84"

_BY_SLUG: dict[str, ModelServerSpec] = {spec.slug: spec for spec in REGISTRY}

_GTT_TOTAL_PATH = "/sys/class/drm/card0/device/mem_info_gtt_total"
_GTT_USED_PATH = "/sys/class/drm/card0/device/mem_info_gtt_used"
_RAM_SAFETY_MARGIN = 0.92  # refuse start() if projected usage would exceed this fraction of GTT total

# Container states that hold their memory allocations. A paused container's
# process is frozen with all GTT allocations intact; a restarting one is
# crash-looping and repeatedly re-mapping memory — both conflict.
_MEMORY_HOLDING_STATES = ("running", "paused", "restarting")


def _read_gtt_gib() -> Optional[tuple[float, float]]:
    """Best-effort live (used, total) GTT in GiB. None if unreadable.

    Same sysfs signal `shells/selfcheck.py` alerts on — docker/cgroup memory
    limits do not see GPU-offloaded allocations on this unified-memory box.
    """
    try:
        with open(_GTT_TOTAL_PATH) as f:
            total = int(f.read())
        with open(_GTT_USED_PATH) as f:
            used = int(f.read())
        gib = 1024**3
        return used / gib, total / gib
    except Exception as exc:
        logger.warning("model_servers: GTT read failed: %s", exc)
        return None


async def _run(*args: str) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise ModelServerError(f"'{args[0]}' binary not found: {exc}")
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


_INSPECT_FMT = '{{.State.Status}}|{{index .Config.Labels "com.docker.compose.project"}}'


async def _container_inspect(container_name: str) -> Optional[tuple[str, bool]]:
    """(docker State.Status, compose_managed) — or None if the container
    doesn't exist. A docker-daemon failure raises ModelServerError instead of
    being conflated with "not created": that conflation would make stop()
    report success while the server is still running, and would blind the
    exclusivity gate for every peer."""
    rc, out, err = await _run("docker", "inspect", "--format", _INSPECT_FMT, container_name)
    if rc != 0:
        lowered = (err or out).lower()
        if "no such object" in lowered or "no such container" in lowered:
            return None
        raise ModelServerError(
            f"docker inspect {container_name} failed (daemon down?): {(err or out).strip()[:300]}"
        )
    status, _, compose_project = out.strip().partition("|")
    return status, bool(compose_project)


async def _find_agent_doc(db: AsyncIOMotorDatabase, agent_id_or_slug: str) -> Optional[dict]:
    if ObjectId.is_valid(agent_id_or_slug):
        doc = await db.agents.find_one({"_id": ObjectId(agent_id_or_slug)})
        if doc:
            return doc
    return await db.agents.find_one({"slug": agent_id_or_slug})


async def resolve_endpoint(slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> Optional[str]:
    """OpenAI-compatible base_url for a bound model server, or None.

    This is what turns an agent's `model_server` binding from a label into
    real routing: the orchestrator hands the result to the LLM adapter
    instead of the backend's static *_URL, so re-binding an agent to a
    different local model actually moves its traffic.

    Prefers loopback — this runs ON the box that hosts the servers. An
    endpoint_override wins (Ridge is only reachable through its tailnet-bound
    proxy; localhost there is refused).
    """
    spec = _BY_SLUG.get(slug)
    if spec is None and db is not None:
        doc = await db.model_servers.find_one({"slug": slug})
        if doc:
            spec = ModelServerManager._spec_from_doc(doc)
    if spec is None:
        return None
    if spec.endpoint_override:
        return spec.endpoint_override
    return f"http://localhost:{spec.port}/v1" if spec.port else None


class ModelServerManager:
    """Start/stop/bind the local model servers. The single control plane —
    see the module docstring for why manual docker commands are retired."""

    def __init__(self):
        self.infrastructure_root = os.path.abspath(settings.infrastructure_root)
        # Serializes every check-then-act sequence (start's safety gates,
        # bind's conflict probe). Without it, two concurrent MCP calls can
        # both pass the exclusivity/RAM checks and launch two ~90 GiB servers
        # together — the exact failure this module exists to prevent.
        self._lock = asyncio.Lock()

    def specs(self) -> tuple[ModelServerSpec, ...]:
        return REGISTRY

    def get_spec(self, slug: str) -> ModelServerSpec:
        spec = _BY_SLUG.get(slug)
        if spec is None:
            raise ModelServerNotFound(f"Unknown model server: {slug}")
        return spec

    @staticmethod
    def _spec_from_doc(doc: dict) -> ModelServerSpec:
        """Build a spec from a dynamic db.model_servers doc (created by the
        model-pull provisioning pipeline). Dynamic entries carry no static
        exclusivity — their RAM safety rides on the live GTT gate."""
        return ModelServerSpec(
            slug=doc["slug"],
            description=doc.get("description", ""),
            runtime_repo=doc.get("runtime_repo", ""),
            runtime_ref=doc.get("runtime_ref", ""),
            backend_device=doc.get("backend_device", ""),
            model_file=doc.get("model_file"),
            port=doc.get("port"),
            compose_file=doc.get("compose_file"),
            service_name=doc.get("service_name"),
            container_name=doc.get("container_name"),
            profile=doc.get("profile"),
            resident_gib=doc.get("resident_gib"),
            gtt_resident=doc.get("gtt_resident", True),
            consumers_note=doc.get("consumers_note"),
        )

    async def resolve_spec(self, slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> ModelServerSpec:
        """get_spec, extended to the dynamic (pulled) entries when a db is
        available. Static registry wins on a name collision (the pull
        pipeline refuses to create one, but be deterministic anyway)."""
        spec = _BY_SLUG.get(slug)
        if spec is not None:
            return spec
        if db is not None:
            doc = await db.model_servers.find_one({"slug": slug})
            if doc:
                return self._spec_from_doc(doc)
        raise ModelServerNotFound(f"Unknown model server: {slug}")

    async def _inspect(self, spec: ModelServerSpec) -> tuple[str, bool]:
        """(state, compose_managed) for a spec. Synthetic states for entries
        docker can't answer for: external (off-box), unwired (no container
        configured), not_created (container doesn't exist)."""
        if not spec.onbox:
            return "external", False
        if not spec.container_name:
            return "unwired", False
        info = await _container_inspect(spec.container_name)
        if info is None:
            return "not_created", False
        return info

    async def status(self, db: Optional[AsyncIOMotorDatabase] = None) -> list[dict]:
        gtt = _read_gtt_gib()
        bindings: dict[str, list[str]] = {}
        dynamic_specs: list[ModelServerSpec] = []
        if db is not None:
            async for doc in db.agents.find({"model_server": {"$exists": True, "$ne": None}}):
                bindings.setdefault(doc["model_server"], []).append(doc.get("slug") or str(doc["_id"]))
            async for doc in db.model_servers.find({}):
                if doc["slug"] not in _BY_SLUG:
                    dynamic_specs.append(self._spec_from_doc(doc))

        results = []
        for spec in list(REGISTRY) + dynamic_specs:
            state, _ = await self._inspect(spec)
            entry = {
                "slug": spec.slug,
                "description": spec.description,
                "state": state,
                "port": spec.port,
                "model_file": spec.model_file,
                "runtime_repo": spec.runtime_repo,
                "runtime_ref": spec.runtime_ref,
                "backend_device": spec.backend_device,
                "resident_gib_estimate": spec.resident_gib,
                "exclusive_with": list(spec.exclusive_with),
                "onbox": spec.onbox,
                "startable": spec.startable,
                "not_startable_reason": spec.not_startable_reason,
                "consumers_note": spec.consumers_note,
                "can_sleep": spec.sleep_command is not None,
                "bound_agents": bindings.get(spec.slug, []),
                # What a consumer (e.g. Hermes's config.yaml) should dial.
                "endpoints": (
                    {"tailnet": spec.endpoint_override}
                    if spec.endpoint_override
                    else {
                        "local": f"http://localhost:{spec.port}/v1",
                        "tailnet": f"http://{_TAILNET_IP}:{spec.port}/v1",
                    }
                    if spec.port
                    else {}
                ),
            }
            if gtt is not None:
                entry["gtt_used_gib"] = round(gtt[0], 1)
                entry["gtt_total_gib"] = round(gtt[1], 1)
            results.append(entry)
        return results

    async def start(
        self, slug: str, force: bool = False, db: Optional[AsyncIOMotorDatabase] = None
    ) -> dict:
        spec = await self.resolve_spec(slug, db)
        if not spec.onbox:
            raise ModelServerSafetyError(f"{slug} is off-box — ARIA cannot start it directly.")
        if not spec.startable and not force:
            raise ModelServerSafetyError(
                spec.not_startable_reason or f"{slug} has no working runtime/service yet."
            )

        async with self._lock:
            state, compose_managed = await self._inspect(spec)

            # Idempotent noop FIRST — before the safety gates, which would
            # otherwise double-count an already-running server's own memory
            # (it's already in mem_info_gtt_used) and refuse the restart.
            if state == "running":
                return {"slug": slug, "state": "running", "action": "noop"}
            if state == "paused":
                raise ModelServerError(
                    f"{slug} container is paused, not stopped — `docker unpause "
                    f"{spec.container_name}` it manually; ARIA has no unpause path."
                )

            if not force:
                conflicts = []
                for other_slug in spec.exclusive_with:
                    other = _BY_SLUG.get(other_slug)
                    if other is None or not other.onbox or not other.container_name:
                        continue
                    other_state, _ = await self._inspect(other)
                    if other_state in _MEMORY_HOLDING_STATES:
                        conflicts.append(f"{other_slug} ({other_state})")
                if conflicts:
                    raise ModelServerSafetyError(
                        f"{slug} is mutually exclusive with active server(s): "
                        f"{', '.join(conflicts)}. Stop them first, or pass force=True."
                    )

                gtt = _read_gtt_gib()
                if gtt is not None and spec.resident_gib is not None and spec.gtt_resident:
                    used, total = gtt
                    projected = used + spec.resident_gib
                    if projected > total * _RAM_SAFETY_MARGIN:
                        raise ModelServerSafetyError(
                            f"Starting {slug} (~{spec.resident_gib:.0f} GiB SWAG) would push "
                            f"GTT usage to ~{projected:.0f}/{total:.0f} GiB, over the "
                            f"{_RAM_SAFETY_MARGIN:.0%} safety margin. Stop something first, "
                            f"or pass force=True."
                        )

            note = None
            container_exists = spec.container_name and state not in ("not_created", "unwired")
            if container_exists and not compose_managed:
                # Hand-run container: `compose up` would hit a name conflict
                # rather than adopt it, so raw `docker start` is the only
                # path — with the caveat that it resurrects creation-time
                # config and ignores any compose-file edits since.
                rc, out, err = await _run("docker", "start", spec.container_name)
                note = (
                    "hand-run container restarted with its creation-time config; "
                    "compose-file changes are NOT applied on this path. To adopt "
                    "compose config: docker rm the container, then start again."
                )
            else:
                if not spec.compose_file or not spec.service_name:
                    raise ModelServerSafetyError(f"{slug} has no compose service configured.")
                # compose up -d also covers an existing compose-managed
                # container: docker natively recreates it if the compose file
                # changed since creation, so config drift can't silently
                # resurrect a stale argv.
                args = ["docker", "compose", "-f", os.path.join(self.infrastructure_root, spec.compose_file)]
                if spec.profile:
                    args += ["--profile", spec.profile]
                args += ["up", "-d", spec.service_name]
                rc, out, err = await _run(*args)

            if rc != 0:
                raise ModelServerError(f"Failed to start {slug}: {(err or out).strip()}")
            result = {"slug": slug, "state": "starting", "action": "started", "output": (out + err)[-2000:]}
            if note:
                result["note"] = note
            return result

    async def stop(self, slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> dict:
        spec = await self.resolve_spec(slug, db)
        if not spec.onbox:
            raise ModelServerSafetyError(f"{slug} is off-box — ARIA cannot stop it directly.")
        if not spec.container_name:
            return {"slug": slug, "state": "unwired", "action": "noop"}

        async with self._lock:
            state, _ = await self._inspect(spec)
            if state in ("not_created", "exited", "created", "dead"):
                return {"slug": slug, "state": state, "action": "noop"}

            # Raw `docker stop` always works regardless of whether the container was
            # compose-managed or hand-run — sidesteps the compose-stop-is-a-silent-noop
            # gotcha for hand-run containers (see the model-server-control-plane memory
            # note). Also cleanly kills paused containers.
            rc, out, err = await _run("docker", "stop", spec.container_name)
            if rc != 0:
                raise ModelServerError(f"Failed to stop {slug}: {(err or out).strip()}")
            return {"slug": slug, "state": "stopped", "action": "stopped"}

    async def sleep(self, slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> dict:
        """Suspend an off-box machine (e.g. Ridge). Its wake path is separate
        and automatic — the wake proxy WoLs it on the next inference request —
        so ARIA only ever needs the sleep direction."""
        spec = await self.resolve_spec(slug, db)
        if spec.sleep_command is None:
            raise ModelServerSafetyError(
                f"{slug} has no sleep command — sleep only applies to off-box "
                f"machines like Ridge; on-box servers are stopped, not slept."
            )
        async with self._lock:
            # Reachability probe first: if the box is already asleep, ssh can't
            # connect and the suspend "failure" would be meaningless noise.
            probe = spec.sleep_command[:-1] + ("exit",)
            rc, _, _ = await _run(*probe)
            if rc != 0:
                return {"slug": slug, "state": "asleep", "action": "noop",
                        "detail": "unreachable over ssh — already asleep"}
            rc, out, err = await _run(*spec.sleep_command)
            # The box suspending mid-command drops the ssh connection, so a
            # nonzero exit here is the EXPECTED success shape, not a failure.
            return {"slug": slug, "state": "sleeping", "action": "sleep_requested",
                    "detail": (err or out).strip()[-300:] or f"suspend sent (ssh exit {rc})"}

    async def bind(
        self, db: AsyncIOMotorDatabase, slug: str, agent_id_or_slug: str, force: bool = False
    ) -> dict:
        await self.resolve_spec(slug, db)  # validates slug (static or pulled), raises ModelServerNotFound
        agent = await _find_agent_doc(db, agent_id_or_slug)
        if agent is None:
            raise ModelServerNotFound(f"Unknown agent: {agent_id_or_slug}")

        # Same lock as start(): the conflict probe below is check-then-set,
        # and two concurrent binds must not both see "no conflict".
        async with self._lock:
            conflict = await db.agents.find_one({"model_server": slug, "_id": {"$ne": agent["_id"]}})
            if conflict and not force:
                raise ModelServerBindingConflict(
                    f"{slug} is already bound to agent "
                    f"'{conflict.get('slug', str(conflict['_id']))}'. Pass force=True to add another slot."
                )

            await db.agents.update_one(
                {"_id": agent["_id"]},
                {"$set": {"model_server": slug, "updated_at": datetime.now(timezone.utc)}},
            )
        return {
            "agent": agent.get("slug", str(agent["_id"])),
            "model_server": slug,
            "extra_slot": bool(conflict),
        }

    async def unbind(self, db: AsyncIOMotorDatabase, agent_id_or_slug: str) -> dict:
        agent = await _find_agent_doc(db, agent_id_or_slug)
        if agent is None:
            raise ModelServerNotFound(f"Unknown agent: {agent_id_or_slug}")
        await db.agents.update_one(
            {"_id": agent["_id"]},
            {"$unset": {"model_server": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
        return {"agent": agent.get("slug", str(agent["_id"])), "model_server": None}
