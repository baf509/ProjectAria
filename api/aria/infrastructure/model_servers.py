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
import pathlib
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
import yaml
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings
from aria.infrastructure.gpu_devices import (
    POOL_HALO,
    POOL_HOST,
    POOL_R9700,
    POOL_REMOTE,
    process_gpu_bytes,
    process_uses_gpu,
    read_pool,
)

logger = logging.getLogger(__name__)

# Allowlist for free-form override values. Deliberately narrow: these are
# interpolated into a systemd Environment= line and read by a shell script.
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._,:/\-+=]+$")


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
class LaunchParam:
    """One knob of a deployment's launch configuration.

    These are NOT invented by ARIA. Every serve.sh under infrastructure/ is
    already written as `VAR="${VAR:-default}"`, and Ben already overrides them
    by hand with systemd drop-ins (ds4-halo-xxs.service.d/context.conf sets
    CTX, no-draft.conf sets DRAFT). A LaunchParam declares one of those
    existing env knobs so the same override can be made through ARIA — with
    validation, and visibly — instead of by editing a file.

    `env` is the environment variable the launch script reads. `default` is
    documentation of the script's own default, not something ARIA writes: an
    unset override means "let the script decide", which keeps ARIA out of the
    way of any hand-written drop-in.
    """

    name: str                 # stable API name, e.g. "ctx"
    env: str                  # the env var the launch script reads, e.g. "CTX"
    label: str
    kind: str = "str"         # "int" | "enum" | "path" | "str"
    default: Optional[str] = None
    choices: tuple[tuple[str, str], ...] = ()  # (value, what it means)
    description: str = ""

    def validate(self, value: str) -> str:
        """Return the normalised value, or raise ModelServerSafetyError.

        Values end up inside a systemd `Environment=` line and are passed to a
        shell script, so this is a security boundary as well as a typo check —
        it is an allowlist, never an escape.
        """
        text = str(value).strip()
        if not text:
            raise ModelServerSafetyError(f"{self.name}: empty value")
        if self.kind == "int":
            if not text.isdigit():
                raise ModelServerSafetyError(
                    f"{self.name} must be a positive integer, got {text!r}"
                )
            return text
        if self.kind == "enum":
            allowed = [c for c, _ in self.choices]
            if text not in allowed:
                raise ModelServerSafetyError(
                    f"{self.name} must be one of {', '.join(allowed)}, got {text!r}"
                )
            return text
        if self.kind == "path":
            # "none" is the documented sentinel every drafter knob accepts.
            if text != "none" and not os.path.isabs(text):
                raise ModelServerSafetyError(
                    f"{self.name} must be an absolute path or 'none', got {text!r}"
                )
            if text != "none" and not os.path.exists(text):
                raise ModelServerSafetyError(f"{self.name}: no such file: {text}")
            if any(ch in text for ch in "\n\r"):
                raise ModelServerSafetyError(f"{self.name}: illegal characters")
            return text
        if not _SAFE_VALUE_RE.match(text):
            raise ModelServerSafetyError(
                f"{self.name}: only letters, digits and .,_,-,/,: are allowed"
            )
        return text


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
    # The two -c-INVARIANT constants. Supply both and `resident_gib` stops being
    # hand-maintained: effective_resident_gib() computes the footprint from the
    # `-c` actually in the launch file, so changing context in the unit is the
    # whole change. `resident_gib` above stays as the fallback for entries that
    # have not been characterised this way.
    weights_gib: Optional[float] = None      # model weights alone
    kv_kib_per_token: Optional[float] = None # KV cost per token of served context
    overhead_gib: float = 2.1                # compute buffers, allocator slack
    gtt_resident: bool = True  # False = CPU-only, allocations never hit the GTT pool
    exclusive_with: tuple[str, ...] = ()
    onbox: bool = True  # False = ARIA cannot start/stop it (e.g. Ridge)
    startable: bool = True  # False = no working runtime/compose service exists yet
    not_startable_reason: Optional[str] = None
    consumers_note: Optional[str] = None  # descriptive, e.g. "Hermes auxiliary tasks + cron"
    # Off-box only: command that suspends the remote machine.
    sleep_command: Optional[tuple[str, ...]] = None

    # ── remote operate (added 2026-08-15) ─────────────────────────────────
    # Off-box servers ARIA CAN drive. Historically `onbox=False` meant "ARIA
    # refuses to touch it" and the only remote verb was sleep(); waking was an
    # implicit side effect of an inference request hitting the wake proxy. That
    # left a real hole: a box can be AWAKE with its model service STOPPED, and
    # nothing could fix it — observed on RED 2026-08-15 (ssh up, RedLlmGateway
    # "Ready", nothing on :8080). These fields close it.
    #
    # `onbox` keeps its original meaning (does the process run on corsair). The
    # new capability is `remotely_operable` below, so nothing that reads
    # `onbox` for accounting or gating changes behaviour.
    #
    # wake_command runs LOCALLY on corsair, never over ssh: Wake-on-LAN is an
    # L2 broadcast, and corsair is the only always-on host on the GPU boxes'
    # physical LAN. This mirrors wake-proxies/relay/wake_server.py.
    wake_command: Optional[tuple[str, ...]] = None
    # Commands run ON the remote host (ssh ...) to start/stop the model service.
    remote_start_command: Optional[tuple[str, ...]] = None
    remote_stop_command: Optional[tuple[str, ...]] = None
    # Probed to decide "is it actually serving?" — the readiness oracle for
    # remote start. Without this a remote start could only report "command
    # sent", which is the kind of unverified success this codebase avoids.
    remote_health_url: Optional[str] = None
    # Which telemetry surface this server exposes. Added 2026-08-17, when the
    # utilization endpoint was found reporting `null` for TWO of the three live
    # models because it only ever spoke llama.cpp's `/slots` + `/metrics`.
    #   "llamacpp"  — /slots + /metrics (the historical assumption; still default)
    #   "vllm"      — Prometheus /metrics with vllm:* names; NO /slots
    #   "dwarfstar" — /v1/models ONLY. No /metrics, no /slots, not even /health.
    # ⚠️ `null` in this API means UNKNOWN, never "fine". A family that cannot
    # report a field must say so via telemetry_hint rather than let a null be
    # read as a healthy zero.
    runtime_family: str = "llamacpp"
    # ── Last measured throughput, and WHEN ──────────────────────────────────
    # No backend here exposes a stable tok/s at rest: llama.cpp's rate gauges
    # read 0 while idle, vLLM gives a latency histogram, DwarfStar gives nothing
    # at all. So throughput is a BENCHMARK RESULT, recorded by hand, and the
    # date is the point — it is the staleness signal `resident_gib` never had
    # (that field sat 7.6 GiB wrong for weeks with nothing to flag it).
    # ⚠️ Re-measure and re-date after ANY change to quant, runtime, context, KV
    # type or speculation. A number with no date is a rumour.
    bench_decode_tok_s: Optional[float] = None
    bench_prefill_tok_s: Optional[float] = None
    bench_at: Optional[str] = None          # ISO date the run was taken
    bench_note: Optional[str] = None        # conditions the number is valid under
    # Which machine this server actually runs on, as an ontology entity slug.
    # Required for off-box servers: the ontology projection used to hardcode
    # `machine:ridge` for everything with onbox=False, so RED's server claimed
    # to run on Ridge in the knowledge graph — a false structural edge in the
    # one place that is supposed to be derived truth (found 2026-08-15). On-box
    # servers fall back to machine:corsair-ai.
    host_machine: Optional[str] = None
    # Seconds to get the box reachable after a wake (RED ~180, Ridge ~90 cold).
    remote_wake_deadline: float = 240.0
    # Seconds for the model service to answer health once the box is up.
    remote_ready_deadline: float = 240.0
    # Consumer-facing OpenAI-compatible endpoint override. Default is computed
    # from `port` (localhost + tailnet variants); set this when the URI isn't
    # port-derivable — e.g. Ridge, reached ONLY via the tailnet-bound proxy
    # (localhost:8092 is connection-refused, a repeatedly-misdiagnosed gotcha).
    endpoint_override: Optional[str] = None
    # systemd --user unit, for servers that are NOT docker containers. The
    # DS4 runtime is a sealed host bundle whose unit verifies
    # `sha256sum -c manifest/bundle.sha256` on every start, so containerising
    # it would break that provenance chain. When set, start/stop/_inspect use
    # systemctl and container_name/compose_file are not required.
    systemd_unit: Optional[str] = None

    # ── placement & parameterized launch (added 2026-08-14) ───────────────
    # Which physical memory pool this server draws from. The start-time gate
    # reads THIS pool and no other — the whole point of the two-GPU topology
    # is that a model on the R9700's own VRAM does not compete with one on the
    # Halo's shared system memory, so accounting them together would forbid
    # the dual-serving deployment that demonstrably works.
    memory_pool: str = POOL_HALO
    # Additional pools a split deployment also consumes (ds4-hybrid puts 20%
    # of its layers on the R9700). Display + conflict detection only; the gate
    # projects against `memory_pool`, which is where the bulk lands.
    also_uses: tuple[str, ...] = ()
    # Human-readable device placement, e.g. ("Strix Halo iGPU (Vulkan1)",).
    devices: tuple[str, ...] = ()
    # Deployment folder under infrastructure/ that owns model+runtime+serve.sh.
    # Entries sharing one of these are variants of the same physical pair.
    deployment: Optional[str] = None
    # Launch script (relative to infrastructure_root). Its env knobs are what
    # `parameters` declares; ARIA overrides them with a systemd drop-in rather
    # than by rewriting the script or building its own command line.
    launch_script: Optional[str] = None
    parameters: tuple[LaunchParam, ...] = ()
    # Which declared parameter carries the served context / slot count. Set
    # these on script-launched entries so served_ctx and the KV projection
    # still follow the effective configuration — `read_launch_geometry`
    # cannot find `-c` in a shell script the way it finds it in an ExecStart.
    ctx_param: Optional[str] = None
    slots_param: Optional[str] = None
    # Unit body for a deployment that has a serve.sh but no systemd unit of
    # its own (ds4-affine, ds4-hybrid). ARIA materialises `aria-<slug>.service`
    # from these, so the guard env stays explicit and reviewable here instead
    # of being implied by the launcher's defaults.
    unit_environment: tuple[tuple[str, str], ...] = ()
    unit_exec_start_pre: tuple[str, ...] = ()
    unit_oom_score_adjust: Optional[int] = 900

    @property
    def remotely_operable(self) -> bool:
        """Off-box AND ARIA has a declared way to start/stop its model service.

        Deliberately requires BOTH directions. A half-wired entry that could be
        started but not stopped is worse than one that refuses both, because it
        can strand a woken box holding VRAM with no way back.
        """
        return (
            not self.onbox
            and self.remote_start_command is not None
            and self.remote_stop_command is not None
        )


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
    # DS4 at ~86.5 GiB behaves like Laguna: it cannot share the GTT pool with
    # any other resident GPU model. Measured 86.42 GiB at -c 131072 on
    # 2026-08-05, leaving ~27 GiB under the 114 GiB margin.
    ("DS4-0731-ROCMFPX-affine-256k", "Laguna-S-2.1"),
    ("DS4-0731-ROCMFPX-affine-256k", "Chadrock-Laguna-S-2.1"),
    ("DS4-0731-ROCMFPX-affine-256k", "ROCmFP4-qwen3.6-35b-a3b"),
    ("DS4-0731-ROCMFPX-affine-256k", "qwen3.6-35b-a3b-Q4"),
    ("DS4-0731-ROCMFPX-affine-256k", "qwen3.6-27b-Q8"),
    ("DS4-0731-ROCMFPX-affine-256k", "Chadrock-ROCmFP6-qwen3.6-27b"),
    # Ling-3.0-flash at ~70 GiB clears the 114 GiB margin against the small
    # servers (qwen/chadrockv2/context1 all fit) but not against the
    # three big ones. Measured 64.81 GiB at -c 8192 on 2026-08-05; the entry's
    # 70 budgets the MLA KV at the served -c 131072.
    ("Ling-3.0-flash-MXFP4", "DS4-0731-ROCMFPX-affine-256k"),
    ("Ling-3.0-flash-MXFP4", "Laguna-S-2.1"),
    ("Ling-3.0-flash-MXFP4", "Chadrock-Laguna-S-2.1"),
    # The ROCmFP4 Ling is 68 GiB — same class as the MXFP4 entry, so it clears
    # the small servers and collides only with the big three plus its sibling
    # Ling quants (which also share port 8108).
    ("Ling-3.0-flash-ROCmFP4-STRIX-MTP", "Ling-3.0-flash-MXFP4"),
    ("Ling-3.0-flash-ROCmFP4-STRIX-MTP", "DS4-0731-ROCMFPX-affine-256k"),
    ("Ling-3.0-flash-ROCmFP4-STRIX-MTP", "Laguna-S-2.1"),
    ("Ling-3.0-flash-ROCmFP4-STRIX-MTP", "Chadrock-Laguna-S-2.1"),
    # Optional DS4 throughput profile (2026-08-10): target + drafter occupy
    # ~95.6 GiB GTT and left only ~15 GiB MemAvailable under six-way load.
    # It is exclusive with every other large GPU model, including the affine
    # DS4 fallback that shares its port.
    ("DS4-0731-IQ2M-DSpark-64k", "DS4-0731-ROCMFPX-affine-256k"),
    ("DS4-0731-IQ2M-DSpark-64k", "Laguna-S-2.1"),
    ("DS4-0731-IQ2M-DSpark-64k", "Chadrock-Laguna-S-2.1"),
    ("DS4-0731-IQ2M-DSpark-64k", "ROCmFP4-qwen3.6-35b-a3b"),
    ("DS4-0731-IQ2M-DSpark-64k", "qwen3.6-35b-a3b-Q4"),
    ("DS4-0731-IQ2M-DSpark-64k", "qwen3.6-27b-Q8"),
    ("DS4-0731-IQ2M-DSpark-64k", "Chadrock-ROCmFP6-qwen3.6-27b"),
    ("DS4-0731-IQ2M-DSpark-64k", "Ling-3.0-flash-MXFP4"),
    ("DS4-0731-IQ2M-DSpark-64k", "Ling-3.0-flash-ROCmFP4-STRIX-MTP"),
    # Qualified dual-Vulkan IQ3_S production profile. It spans the Strix Halo
    # iGPU and the OCuLink 9700, so the one-device GTT projection is not a
    # meaningful safety instrument; its systemd unit carries the 108/12 GiB
    # MemAvailable circuit breakers. Gemma is intentionally absent: its CPU
    # container is hard-capped at 8 GiB and was co-residency tested.
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "DS4-0731-IQ2M-DSpark-64k"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "DS4-0731-ROCMFPX-affine-256k"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "Laguna-S-2.1"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "Chadrock-Laguna-S-2.1"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "ROCmFP4-qwen3.6-35b-a3b"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "qwen3.6-35b-a3b-Q4"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "qwen3.6-27b-Q8"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "Chadrock-ROCmFP6-qwen3.6-27b"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "Ling-3.0-flash-MXFP4"),
    ("DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K", "Ling-3.0-flash-ROCmFP4-STRIX-MTP"),
)


def _pairs_within(slugs: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """Every combination inside a group that can only have one member up."""
    return tuple(
        (a, b) for i, a in enumerate(slugs) for b in slugs[i + 1:]
    )


def _pairs_between(
    left: tuple[str, ...], right: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    return tuple((a, b) for a in left for b in right if a != b)


# Generated groups, added 2026-08-14 with the two-GPU topology. Writing these
# out by hand is how the list above grew to 40 lines and still missed pairs;
# the grouping states the actual reason for the conflict once.

# One Halo-resident big model at a time — each of these takes 86-100 GiB of a
# 124 GiB pool, so any two of them overflow it.
_HALO_BIG = (
    "DS4-0731-Q8Protected-Halo-DwarfStar",
    "Qwen3.8-Flash-Next-IQ4_XS-Halo",
    "DS4-0731-REAP150B-MXFP4",
    "DS4-0731-IQ3_S-Hybrid-ROCm-Dual",
    "DS4-0731-ROCmFPX-Affine-Quality",
    "DS4-0731-ROCMFPX-affine-256k",
    "DS4-0731-IQ2M-DSpark-64k",
    "DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K",
    "Laguna-S-2.1",
    "Chadrock-Laguna-S-2.1",
    "Ling-3.0-flash-MXFP4",
    "Ling-3.0-flash-ROCmFP4-STRIX-MTP",
    "Ling-3.0-flash-Q6_K",
    "Step-3.7-Flash-APEX-I-Compact",
    "Step-3.7-Flash-APEX-I-Quality",
)

# One model at a time on the R9700 — it has ONE 32 GiB pool and every entry
# here claims most of it. Note these do NOT conflict with the Halo group:
# separate cards, separate memory, and running one from each group at once is
# the whole point of the dual-serving deployment.
_R9700_RESIDENT = (
    "Qwen3.8-27B-R9700-Radiance",
    "Qwen3.8-27B-R9700-Radiance-G64",
    "Qwen3.8-27B-R9700-HIP",
    "Qwen3.8-27B-Q6_K-R9700-Vulkan-MTP",
    "Qwen3.8-27B-ROCmFP4-R9700-Vulkan",
)

_EXCLUSIVE_PAIRS = (
    _EXCLUSIVE_PAIRS
    + _pairs_within(_HALO_BIG)
    + _pairs_within(_R9700_RESIDENT)
    # The hybrid split is the one deployment that spans both cards: 80% of its
    # layers on the Halo, the rest plus the drafter in the R9700's VRAM. So it
    # is the single member of the Halo group that ALSO conflicts with every
    # dGPU resident.
    + _pairs_between(("DS4-0731-IQ3_S-Hybrid-ROCm-Dual",), _R9700_RESIDENT)
)


def _exclusive_with(slug: str) -> tuple[str, ...]:
    """Symmetric expansion of _EXCLUSIVE_PAIRS for one slug — start() only
    consults the starting spec's own list, so both directions must be present.

    Deduplicated: the hand-written pairs above and the generated groups below
    legitimately overlap, and a slug repeated here would be reported twice in
    the same conflict message."""
    seen: dict[str, None] = {}
    for a, b in _EXCLUSIVE_PAIRS:
        if slug == a:
            seen.setdefault(b, None)
        elif slug == b:
            seen.setdefault(a, None)
    return tuple(seen)


# ── shared parameter vocabularies ────────────────────────────────────────
# Declared once so the same knob means the same thing in every deployment
# that exposes it, and so the UI can render one control per concept.

_PARAM_PORT = LaunchParam(
    name="port", env="PORT", label="Port", kind="int",
    description="Listening port. Changing it moves the endpoint consumers "
                "dial, so only override this to run a second copy side by side.",
)
_PARAM_CTX = LaunchParam(
    name="ctx", env="CTX", label="Context per slot", kind="int",
    description="Tokens of context PER SEQUENCE, not a total to divide. Total "
                "KV = ctx x slots.",
)


REGISTRY: tuple[ModelServerSpec, ...] = (
    # ══════════════════════════════════════════════════════════════════════
    # LIVE DEPLOYMENTS — the model+runtime+placement pairs that exist on this
    # box today, one entry per self-contained folder under infrastructure/.
    #
    # Each folder owns model/, runtime/, and a serve.sh whose env knobs ARE
    # the "how to load it" axis: device placement, KV type, context, drafter.
    # Those knobs are declared as `parameters` below, so a start can choose
    # them without editing a file. Entries further down are the historical
    # frozen-unit and compose servers, most of which are now unstartable
    # because the 2026-08-11..14 consolidation moved their runtimes into
    # these folders.
    # ══════════════════════════════════════════════════════════════════════
    ModelServerSpec(
        slug="DS4-0731-IQ3_S-Hybrid-ROCm-Dual",
        description="DeepSeek V4 Flash 0731 UD-IQ3_S (108 GiB) SPLIT ACROSS BOTH "
        "GPUs — Strix Halo iGPU + Radeon AI PRO R9700 — on stock mainline llama.cpp "
        "built dual-arch for gfx1151;gfx1201, with the compact DSpark drafter on the "
        "dGPU. The higher-quality DS4 quant, bought by using both devices. Measured "
        "2026-08-14: 28.88 t/s shallow / 17.95 t/s at ~10K depth, 184-242 t/s prefill, "
        "adherence32 24/32, broad256 244/256.",
        runtime_repo="https://github.com/ggml-org/llama.cpp.git",
        runtime_ref="mainline pinned a94d563ed (build 10423), built dual-arch "
        "gfx1151;gfx1201 at ds4-hybrid/runtime/mainline-hip-dualarch",
        backend_device="ROCm1 (Strix Halo) + ROCm0 (R9700), HIP",
        devices=("Strix Halo iGPU (ROCm1)", "R9700 dGPU (ROCm0)"),
        memory_pool=POOL_HALO,
        also_uses=(POOL_R9700,),
        deployment="ds4-hybrid",
        model_file="ds4-hybrid/model/UD-IQ3_S/"
        "DeepSeek-V4-Flash-0731-UD-IQ3_S-00001-of-00004.gguf",
        port=18211,
        launch_script="ds4-hybrid/serve.sh",
        parameters=(
            LaunchParam(
                name="min_start_kib", env="DS4_MIN_START_KIB",
                label="Start-time MemAvailable floor (KiB)", kind="int",
                default="113246208",
                description="deepseek-v4-safe-launch.sh refuses to start below this. Its "
                            "default is 113246208 KiB = 108 GiB, which is a conservative "
                            "START gate, NOT the anti-OOM guard — DS4_MIN_RUN_KIB (12 GiB) "
                            "is that, and it is unaffected by this knob. The split "
                            "placement puts ~80% of a 109 GB model on the Halo, i.e. ~87 GB, "
                            "so 108 GiB carries ~20 GiB of slack. Lower it only for a "
                            "measured benchmark run and say so; DUAL-SERVING.md sets the "
                            "precedent for adjusting these floors deliberately rather than "
                            "silently. Empty = launcher default.",
            ),
            LaunchParam(
                name="min_run_kib", env="DS4_MIN_RUN_KIB",
                label="Run-time MemAvailable floor (KiB)", kind="int",
                default="12582912",
                description="THE anti-OOM guard: the launcher kills the server if "
                            "MemAvailable falls below this while running (exit 42). Default "
                            "12582912 KiB = 12 GiB. DUAL-SERVING.md already lowers it to "
                            "7340032 (7 GiB) for co-residency and calls that a deliberate, "
                            "measured trade — the same precedent applies to a solo benchmark "
                            "run, where this model alone steadies at ~11.2 GiB free and "
                            "would otherwise be killed at load. ⚠️ Only safe because "
                            "serve.sh caps --cache-ram at 1024 MiB; llama.cpp's 8192 MiB "
                            "default drains headroom at ~0.37 GiB/min and WILL trip any "
                            "floor you set. Empty = launcher default.",
            ),
            LaunchParam(
                name="runtime", env="RUNTIME_DIR", label="llama.cpp build", kind="enum",
                default="/home/ben/Development/infrastructure/ds4-hybrid/runtime/mainline-hip-dualarch",
                choices=(
                    ("/home/ben/Development/infrastructure/ds4-hybrid/runtime/mainline-hip-dualarch",
                     "runtime/mainline-hip-dualarch — build 10423, commit a94d563ed"),
                    ("/home/ben/Development/infrastructure/llamacpp-src/build-hip-cub/bin",
                     "build-hip-cub — build 10432, commit 9ce67ae55 = a94d563ed + PR #26592 "
                     "(hipCUB argsort/top_k), the arm for the ~16K prefill hang"),
                ),
                description="Both builds are dual-arch gfx1151;gfx1201 — a single-arch build "
                            "cannot place layers on the R9700 at all. ⚠️ Verify with "
                            "`llama-server --version`, not runtime/UPSTREAM_COMMIT.txt, which "
                            "recorded the wrong commit until 2026-08-18.",
            ),
            LaunchParam(
                name="placement", env="PLACEMENT", label="Device placement", kind="enum",
                default="split",
                choices=(
                    ("split", "80/20 layer split — better at depth, ~8 GiB more Halo "
                              "headroom. Use for long-context and agentic work."),
                    ("hybrid", "all non-routed weight + experts of layers 0-2 on the "
                               "R9700 — best shallow decode (31.56 t/s), but puts 93% "
                               "of the routed stack on the Halo and is NOT memory-safe "
                               "for 16K+ prefills."),
                ),
                description="The optimum is depth-dependent; there is no single winner.",
            ),
            LaunchParam(
                name="ctx", env="CTX", label="Context per slot", kind="int",
                default="65536",
            ),
            _PARAM_PORT,
        ),
        ctx_param="ctx",
        # ~86 GiB of the 108 GiB model lands on the Halo at the default 80/20
        # split; the remaining layers plus the 8.3 GiB drafter live in the
        # R9700's own VRAM and are gated separately.
        resident_gib=88,
        startable=False,
        not_startable_reason=(
            "DRAFTER DELETED 2026-08-26: ds4-hybrid/draft/ (DSpark Q3KExperts-Q8Dense, 8.3 GiB) is gone and serve.sh hard-requires it (--spec-type draft-dspark). The split was superseded anyway: Flash-Next is the Halo resident and its own dual-GPU split measured only +10-20%. Runtime bundle intact."
        ),
        exclusive_with=_exclusive_with("DS4-0731-IQ3_S-Hybrid-ROCm-Dual"),
        consumers_note="unbound — the quality-per-speed DS4 option when the R9700 "
        "is free. Known limit: a single ~16K-token prefill can hang (both GPUs "
        "idle, CPU spinning); upstream PR #26592 is built but the A/B never ran.",
        # No unit of its own — ARIA materialises one from the fields below.
        unit_environment=(
            ("DS4_GUARD_STATUS", "/run/user/%U/ds4-hybrid-guard.status"),
            ("DS4_MIN_START_KIB", "113246208"),   # 108 GiB, the standard start floor
            ("DS4_MIN_RUN_KIB", "12582912"),      # 12 GiB, the production live floor
            ("DS4_MAX_IDLE_GTT_BYTES", "2147483648"),
        ),
        unit_exec_start_pre=(
            # The R9700 must be awake — it resets to 'auto' every boot — and the
            # TTM pool must already be capped, or it retains a whole model's
            # pages after teardown (measured: 110 GiB held with GTT at 0.03).
            "/usr/bin/bash -c 'test \"$(cat /sys/bus/pci/devices/0000:c6:00.0/power/control)\" = on'",
            "/usr/bin/bash -c 'test \"$(cat /sys/module/ttm/parameters/page_pool_size)\" -le 1048576'",
        ),
    ),
    ModelServerSpec(
        slug="DS4-0731-ROCmFPX-Affine-Quality",
        description="DeepSeek V4 Flash 0731, Ben's hand-tuned ROCmFPX type-108 affine "
        "quant (85.26 GiB, 2.58 BPW) on the sealed O5 runtime. The QUALITY reference: "
        "238/256 broad and 24/24 long-context recall, the best long-recall of any DS4 "
        "artifact here. It is slow (~19.5 t/s shallow, target-only, no drafter) and it "
        "runs ONLY on the sealed O5 runtime — mainline llama.cpp cannot read type-108, "
        "so it must not be pointed at the ds4-hybrid binaries.",
        runtime_repo="https://github.com/baf509/rocmfpx-ds4.git",
        runtime_ref="sealed bundle o5-release (dr-xr-xr-x, permissions preserved on "
        "relocation to ds4-affine/runtime/o5-release)",
        backend_device="ROCm0, HIP",
        # The sealed O5 runtime is a gfx1151-only build, so it does not
        # enumerate the gfx1201 R9700 and its ROCm0 is the Halo. That is an
        # inference from the build, not a measurement — hence the caveat the
        # deployment README also carries.
        devices=("Strix Halo iGPU (ROCm0 — gfx1151-only build; verify placement "
                 "after any runtime change)",),
        memory_pool=POOL_HALO,
        deployment="ds4-affine",
        model_file="ds4-affine/model/DS4-0731-ROCMFPX-affine.gguf",
        port=8107,
        launch_script="ds4-affine/serve.sh",
        parameters=(
            LaunchParam(
                name="ctx", env="CTX", label="Context per slot", kind="int",
                default="65536",
                description="PER SEQUENCE. Total KV = ctx x slots, so raising this "
                            "with 6 slots costs six times what it looks like.",
            ),
            LaunchParam(
                name="slots", env="NP", label="Slots (-np)", kind="int",
                default="6",
                description="One slot per concurrent consumer. Six is the qualified "
                            "geometry: Hermes, system pi-coding, three pi sub-agents, "
                            "ARIA's background workers.",
            ),
            _PARAM_PORT,
        ),
        ctx_param="ctx",
        slots_param="slots",
        # The -c-invariant constants, so the footprint follows the chosen ctx
        # and slot count instead of a hand-maintained number. Measured basis is
        # unchanged from the retired 256k unit; see that entry's comments.
        resident_gib=86.5,
        weights_gib=85.26,
        kv_kib_per_token=6.71875,
        overhead_gib=15.6,
        exclusive_with=_exclusive_with("DS4-0731-ROCmFPX-Affine-Quality"),
        consumers_note="the quality/long-recall reference; unbound by default",
        unit_environment=(
            ("DS4_GUARD_STATUS", "/run/user/%U/ds4-affine-guard.status"),
            ("DS4_MIN_START_KIB", "113246208"),
            ("DS4_MIN_RUN_KIB", "12582912"),
            ("DS4_MAX_IDLE_GTT_BYTES", "2147483648"),
        ),
        unit_exec_start_pre=(
            "/usr/bin/bash -c 'test \"$(cat /sys/module/ttm/parameters/page_pool_size)\" -le 1048576'",
        ),
    ),
    ModelServerSpec(
        slug="DS4-0731-Q8Protected-Halo-DwarfStar",
        runtime_family="dwarfstar",
        bench_decode_tok_s=15.0,
        bench_prefill_tok_s=210.0,
        bench_at="2026-08-17",
        bench_note="Decode re-measured 2026-08-18 on local-eval/qwen38-quant-ab/decode_probe.py: 15.8 tok/s median at both ctx 65536 and 131072 (flat, confirming the sweep below). ⚠️ --batched-session is worth ~40% and is load-bearing: 15.66 tok/s at 1 vs 11.18 at 0. DSpark speculative decoding was evaluated 2026-08-18 and REJECTED (inert on the server path, -27% on the engine path; see the dspark param). Original: ds4-bench sweep, ctx 2048-16384, single session. Decode is flat "
        "across context (15.59 at 2k -> 14.47 at 16k); prefill 191-216 tok/s. "
        "--prefill-chunk 8192 changed nothing, so this is the real ceiling, not a "
        "tuning artefact. Ember measured 21.9 tok/s decode on the same box.",
        description="DeepSeek V4 Flash 0731 on the Strix Halo iGPU via DwarfStar "
        "(antirez/ds4), a native ROCm engine written specifically for DS4 rather than a "
        "general GGUF runner. SELECTED 2026-08-17 as the APU resident after a six-way "
        "measurement — see vault/infrastructure/Analysis/DS4_STACK_BAKEOFF_20260817.md.\n"
        "Why it won: quality TIED at the top (13/15 LiveCodeBench medium with truncated "
        "runs re-resolved at 16k tokens, level with Ember) and with the FEWEST genuine "
        "wrong answers of any stack (1, vs Ember's 2); it is the only top-scoring stack "
        "whose weights are neither ABLITERATED (Ember's are) nor EXPERT-PRUNED with "
        "stale saliency maps (REAP's are); upstream is maintained and ships its own "
        "validation tooling (ds4-eval, official-continuation NLL fixtures, logprob test "
        "vectors); and it loads 80.76 GiB in ~40 SECONDS against ~15 min for llama.cpp's "
        "97 GB IQ3_XXS.\n"
        "⚠️ It is NOT the fastest. Ember decodes ~22 tok/s to this stack's ~15 and "
        "finishes a 15-problem workload 26% sooner. That was traded away deliberately "
        "for weights provenance and operability. The tok/s gap overstates it: this stack "
        "writes ~36% fewer tokens per answer, so its MEDIAN problem is actually faster "
        "(300s vs 320s); Ember wins on the hard tail, not the typical case.\n"
        "⚠️ 'Not pruned' does not mean lossless — it avoids pruning by quantizing "
        "harder (IQ2XXS/Q2K bulk, with Q8 protection on attention projections, shared "
        "experts and output). The trade bought is damage you can reason about over "
        "damage nobody has characterized.\n"
        "Serves OpenAI /v1/chat/completions + /v1/completions, Anthropic /v1/messages, "
        "and /v1/responses, with tool calls, streaming, seed, reasoning_effort, and real "
        "prompt-cache telemetry (cached_tokens / cache_write_tokens) that llama.cpp does "
        "not report.",
        runtime_repo="https://github.com/antirez/ds4 (DwarfStar)",
        runtime_ref="/home/ben/Development/dwarfstar — own git checkout, NOT vendored "
        "into infrastructure. Built with `make strix-halo -j4` (ROCM_ARCH defaults "
        "gfx1151); built clean first try. Prereqs from its STRIXHALO.md were ALREADY "
        "satisfied on this box: kernel cmdline carries amd_iommu=off "
        "amdgpu.gttsize=126976 ttm.pages_limit=32505856, and hipcc, /opt/rocm and the "
        "rocWMMA internal headers are present.",
        backend_device="Native ROCm on the Strix Halo iGPU (gfx1151), via HIP_VISIBLE_DEVICES=1",
        devices=("Strix Halo iGPU (HIP device 1)",),
        memory_pool=POOL_HALO,
        deployment="dwarfstar-ds4",
        model_file="models/llm/DS4-0731-Flash-IQ2XXS-Q8Protected/"
        "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf",
        port=8112,
        launch_script="dwarfstar-ds4/serve.sh",
        parameters=(
            LaunchParam(
                name="ctx", env="CTX", label="Context", kind="int", default="131072",
                description="⚠️ NOT free — the cheap-scaling claim below was measured "
                            "from ds4-server's own under-reported figures. REAL measured "
                            "context buffers: 65536 -> ~1.70 GiB, 131072 -> ~2.93 GiB, "
                            "262144 -> ~5.72 GiB PER RESIDENT SESSION, and it multiplies "
                            "with batched_sessions. At 262144 the stack left 9 GiB free "
                            "with nothing else running; radiance + gemma need ~4.2 more. "
                            "131072 keeps ~7.6 GiB of headroom. Old note follows: "
                            "measured 81.46 GiB at 16641, "
                            "81.79 at 32768, 82.46 at 65536 — quadrupling context costs "
                            "~1 GiB, because most of the KV is compressed rows (16386 "
                            "compressed vs 4352 raw at 64k). 131k is reachable; --ctx "
                            "takes an arbitrary integer and the README's 100000 is an "
                            "example, not a ceiling.",
            ),
            LaunchParam(
                name="kv_disk_mb", env="KV_DISK_MB", label="Disk KV budget (MB)",
                kind="int", default="15360",
                description="On-disk KV checkpoints. DwarfStar also does exact "
                            "token-prefix reuse in front of this.",
            ),
            LaunchParam(
                name="dspark", env="DSPARK", label="DSpark speculative decoding",
                kind="enum", default="0",
                choices=(
                    ("0", "off — ordinary target decode, ~15.8 tok/s measured"),
                    ("1", "on — REJECTED 2026-08-18; the support GGUF was also DELETED, "
                           "so this now fails the launcher's existence check"),
                ),
                description="⚠️ EVALUATED AND REJECTED 2026-08-18 — leave at 0. On "
                            "the SERVER path DSpark is INERT (15.66 tok/s with it, "
                            "15.69 with --dspark-strict, 15.83 without; flat to 0.5% "
                            "across repeat/code/math/json/prose), so it costs 5.6 GiB "
                            "for nothing and reports no error. On the ENGINE path "
                            "(ds4 -p) it engages and LOSES 27%: 12.09 vs 16.60 tok/s "
                            "on `repeat`. Monotonic in confidence — 0.4 -> 11.55, "
                            "0.7 -> 12.09, 0.9 -> 15.17, off -> 16.60 — i.e. pure "
                            "overhead, the limit as speculation goes to zero is the "
                            "baseline. The knobs stay only so a future DwarfStar "
                            "release can be re-tested cheaply (40 s loads). Details: "
                            "vault DS4_DSPARK_SPECULATION_EVAL_20260818. "
                            "DeepSeek's auxiliary draft model for V4 Flash, verified "
                            "by the target. ⚠️ MEMORY is the constraint, not "
                            "compatibility: +5.6 GiB on a box whose steady state "
                            "leaves ~5-8 GiB, with NO circuit breaker on this stack "
                            "and ds4-server volunteering itself as first OOM victim "
                            "(oom_score_adj=1000). Free room first — stopping "
                            "gemma-4-e4b-Q4 is the cheapest lever. ⚠️ Output can "
                            "DIVERGE from non-DSpark decode (batched verifier groups "
                            "float ops differently); this is the coding-agent model, "
                            "so weigh that. There is NO draft-depth knob — DwarfStar "
                            "drafts up to five internally and dspark_confidence is "
                            "the only dial.",
            ),
            LaunchParam(
                name="dspark_confidence", env="DSPARK_CONFIDENCE",
                # default=None (not "") is how this registry says "unset": an
                # empty string fails validate(), so a declared "" could never
                # actually be applied as an override.
                label="DSpark confidence threshold", kind="float", default=None,
                description="Pruning threshold 0..1. Empty = ds4's own default, "
                            "which is 0.7 on ROCm (0.6 on Metal). Higher prunes more "
                            "aggressively. This is the analogue of llama.cpp's "
                            "--spec-draft-n-max, which DwarfStar does not have.",
            ),
            LaunchParam(
                name="dspark_strict", env="DSPARK_STRICT",
                label="DSpark strict (control arm)", kind="enum", default="0",
                choices=(
                    ("0", "normal — speculate"),
                    ("1", "load the support model but keep target-only decode"),
                ),
                description="The measurement control: pays the full 5.6 GiB memory "
                            "cost without speculating, so a DSpark-on vs "
                            "DSpark-strict pair isolates the speculation effect from "
                            "the memory pressure it introduces.",
            ),
            LaunchParam(
                name="batched_sessions", env="BATCHED_SESSIONS",
                label="Resident batched sessions", kind="int", default="1",
                description="⚠️ MULTIPLIES WITH ctx — context buffers are PER RESIDENT "
                            "SESSION. 6 @ ctx 262144 requested 34.31 GiB of buffers and "
                            "CRASHED THE BOX on 2026-08-17 (hard power-cycle). Measured: "
                            "~1.70 GiB/session at ctx 65536, ~5.72 GiB at ctx 262144, "
                            "against a total buffer budget of roughly 5 GiB once weights "
                            "(~98), radiance (~9), gemma (~4) and services (~8) are paid. "
                            "Raise ONE of ctx/batched_sessions at a time and measure.",
            ),
            _PARAM_PORT,
        ),
        ctx_param="ctx",
        # ⚠️ 100 GiB, MEASURED — do not trust DwarfStar's own accounting here. It logs
        # "KV 1.20 + buffers 0.50 + resident model 80.76 = 82.46 GiB planned", but actual
        # card1 GTT sits at 100 GiB in steady state (verified 2026-08-17: RSS is only
        # 0.4 GiB because the weights live in GTT, not process memory). The self-report
        # understates by ~17.5 GiB.
        # CONSEQUENCE: this is a WASH with the 97 GiB IQ3_XXS it replaces, NOT the ~15 GiB
        # saving an earlier version of this entry claimed. Measured co-residency:
        # IQ3_XXS + radiance left 12 GiB available; DwarfStar + radiance + gemma leaves
        # 8.8 GiB (gemma is 3.4 of that). It still co-exists with the dGPU model, but with
        # ~8 GiB of headroom, not ~40 — treat it as tight, not comfortable.
        # There is NO MemAvailable circuit breaker on this stack the way there is on the
        # Nathan/llama.cpp units; ds4-server instead sets its own oom_score_adj=1000, so
        # under pressure the kernel takes THIS down first (before radiance at 900 and
        # gemma at 500). That is a deliberate volunteer, not an accident.
        resident_gib=100,
        weights_gib=80.76,
        startable=False,
        not_startable_reason=(
            "WEIGHTS DELETED 2026-08-26 (80.76 GiB reclaimed): models/llm/DS4-0731-Flash-IQ2XXS-Q8Protected/ is gone. Qwen3.8-Flash-Next-IQ4_XS-Halo replaced it as the Halo resident the same day (22 vs 15 tok/s decode, 463 vs 210 prefill, and it beats DS4 on the published agentic rows). The DwarfStar runtime checkout and dwarfstar-ds4/serve.sh are intact; re-download the GGUF to revive."
        ),
        exclusive_with=_exclusive_with("DS4-0731-Q8Protected-Halo-DwarfStar"),
        consumers_note="⚠️ AS OF 2026-08-17 NO CONSUMER ROUTES HERE YET. Selected but "
        "not cut over: Hermes, pi-coding and ARIA still point at :8108 "
        "(DS4-0731-Q8Protected-Halo-DwarfStar). Cutover is a separate, deliberate step.",
    ),
    ModelServerSpec(
        slug="Qwen3.8-Flash-Next-IQ4_XS-Halo",
        runtime_family="llamacpp",
        bench_decode_tok_s=22.0,
        bench_prefill_tok_s=463.0,
        bench_at="2026-08-26",
        bench_note="llama-bench on the Halo alone (HIP, -fa on, -ub 2048), Radiance running "
        "on the R9700: tg128 22.0 shallow / 18.4 at 16K depth; pp512 426, pp2048 463 "
        "(-ub 2048 is +19% over 512; -b is irrelevant), pp8192 404 / 273 at 16K depth. "
        "Server-side decode at ~20K depth measured 12.5 (a 20K-token prompt at 352 pp) — "
        "the gap to llama-bench's 18.4 is NOT isolated yet. Vulkan on the same commit: "
        "+8% decode, -9% prefill; HIP chosen. Dual-GPU split (-ts 33/16 on the R9700, "
        "pipeline parallelism OFF) measured +10-20% and was NOT adopted: it costs the "
        "R9700, i.e. Radiance. ⚠️ With pipeline parallelism ON any two-device run halves "
        "decode (11-13 t/s) and the split server segfaulted at 35K prefill (QSA compute "
        "buffer OOM on the R9700).",
        description="Qwen3.8-Flash-Next — Qwen's Qwen4-architecture preview "
        "(general.architecture=qwen4exp): 125B/6B-active MoE, 512 experts top-10 + shared, "
        "36 Gated-DeltaNet + 12 Qwen-Sparse-Attention layers, a 51B n-gram (PLE) embedding "
        "table, 262,144 native context. Unsloth UD-IQ4_XS (87.24 GiB, imatrix): experts "
        "~4-bit, n-gram table IQ4_NL 26.8 GiB, dense Q8_0. Beats Qwen3.8-27B on nearly "
        "every published row (DeepSWE 58.7 vs 42.2, SWE-bench Multilingual 81.0 vs 73.8, "
        "Toolathlon 73.5 vs 67.1, JobBench 55.7 vs 33.4).\n"
        "Halo-only. Only the 12 QSA layers carry KV (~2.1 GiB per 64K ctx incl. the "
        "indexer cache), so 256K is the standing default: measured 19 GiB spare idle and "
        "17 GiB after a 20K-token prompt with Radiance up. Thinking is ON by default; "
        "clients disable it per request with chat_template_kwargs.enable_thinking=false "
        "(then use Qwen's non-thinking sampling: temp 0.7, top_p 0.8, top_k 20, "
        "presence_penalty 1.5).\n"
        "⚠️ No mmproj (vision) and no MTP GGUF published yet — text only, no speculation. "
        "Quality probes 2026-08-26: greedy code output identical to a Halo-only reference "
        "for ~1350 tokens (its own asserts pass), jug puzzle solved in thinking mode, "
        "needle-in-haystack at 42K tokens answered correctly.\n"
        "The 103.7 GiB UD-Q4_K_XL is also on disk (fits Halo-only at <=64K with 7 GiB "
        "spare, 20.8 tok/s) but is not served.",
        runtime_repo="https://github.com/unslothai/llama.cpp (branch qwen4exp/qwen3.8-flash-next)",
        runtime_ref="infrastructure/llamacpp-qwen4exp — git worktree of llamacpp-src on the "
        "Unsloth branch, commit 035e22731 (build 10656), built at build-hip/ for "
        "gfx1151;gfx1201 with ROCm 7.2.4 (GGML_HIP_GRAPHS on, VMM off). ⚠️ Upstream "
        "llama.cpp master has NO qwen4exp yet (2026-08-26): ggml-org PR #27742 (Unsloth, "
        "draft — this branch) and #27739 (Qwen) compete, and they name the n-gram tensor "
        "differently (per_layer_token_embd vs ple_ngram_embd), so the Unsloth GGUFs load "
        "only on the Unsloth branch. Re-verify the tensor names before moving the worktree.",
        backend_device="ROCm1 (Strix Halo iGPU, gfx1151), llama.cpp HIP — selected with -dev, "
        "HIP_VISIBLE_DEVICES deliberately unset",
        devices=("Strix Halo iGPU (ROCm1)",),
        memory_pool=POOL_HALO,
        deployment="qwen3.8-flash-next",
        model_file="models/llm/Qwen3.8-Flash-Next-UD-IQ4_XS-GGUF/"
        "Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf",
        port=8120,
        systemd_unit="qwen3.8-flash-next.service",
        launch_script="qwen3.8-flash-next/serve.sh",
        parameters=(
            LaunchParam(
                name="ctx", env="CTX", label="Context", kind="int", default="262144",
                description="KV is cheap on this arch: only 12 of 48 layers have one. "
                            "Measured per 64K: 1536 MiB QSA KV + 576 MiB indexer cache = "
                            "~2.1 GiB. 262144 -> ~8.4 GiB; 19 GiB spare idle with Radiance "
                            "up. ⚠️ QSA compute buffers grow with prefill length (a 6 GiB "
                            "reallocation was observed at 35K tokens), so keep >=10 GiB spare.",
            ),
            LaunchParam(
                name="slots", env="SLOTS", label="Slots", kind="int", default="1",
                description="Per-slot KV multiplies the ~2.1 GiB/64K figure. Untested above 1.",
            ),
            LaunchParam(
                name="ubatch", env="UBATCH", label="Batch / micro-batch", kind="int",
                default="2048",
                description="Prefill lever: 512 -> 388, 1024 -> 443, 2048 -> 463, 4096 -> 459 "
                            "tok/s at pp2048. 2048 is the knee; -b is set equal to it.",
            ),
            _PARAM_PORT,
        ),
        ctx_param="ctx",
        slots_param="slots",
        # Measured 2026-08-26 at 65536 x 1 slot with --no-mmap: 61.2 GiB ROCm + 26.8 GiB
        # host-side n-gram table + 0.6 host + 2.1 KV + 0.4 RS/compute = 91.2 GiB
        # (MemAvailable delta 90). At 262144: ~97.5 GiB, 19 GiB spare observed.
        resident_gib=98,
        weights_gib=87.24,
        kv_kib_per_token=33.0,
        overhead_gib=3.0,
        exclusive_with=_exclusive_with("Qwen3.8-Flash-Next-IQ4_XS-Halo"),
        consumers_note="Hermes default model as of 2026-08-26 (provider qwen38-flash -> :8120); "
        "Radiance (:8080) stays up on the R9700 as ARIA's steward/LLAMACPP_URL target and the "
        "vision-capable fallback.",
    ),
    ModelServerSpec(
        slug="DS4-0731-REAP150B-MXFP4",
        description="DeepSeek V4 Flash 0731 REAP-pruned to 150B total params, experts "
        "at NATIVE MXFP4, on the Strix Halo iGPU via Nathan's Vulkan fork. Added "
        "2026-08-16 as a CHALLENGER to the resident DS4, not a replacement. The thesis: "
        "0731's experts ship natively at 4-bit, so quantizing below that stacks a second "
        "and more expensive form of damage — REAP hits the size target the cheaper way, "
        "by pruning experts, and this artifact keeps the survivors at native precision "
        "instead of dropping to Q3/Q2. 79 GB vs the resident IQ3_XXS's 97 GB, which is "
        "what lets it coexist with the dGPU model (see resident_gib note).\n"
        "⚠️ UNVALIDATED PRUNE: every published 0731 REAP is pruned with saliency maps "
        "transferred from a PRIOR observation run rather than a fresh observation of the "
        "0731 weights — and 0731's value is post-training that moved Terminal Bench "
        "61.8 -> 82.7, which shifts which experts fire on agentic traces. None have been "
        "benchmarked against the unpruned model. Measure before trusting.",
        runtime_repo="Nathan's Strix Halo llama.cpp Vulkan fork (shared with ds4-halo-xxs)",
        runtime_ref="runtime/nathan-v0.6.1/vulkan, build 10350 (3be50ccc2) — verified to "
        "know mxfp4. ⚠️ VULKAN ONLY: HIP segfaults on gfx1151 in the ROCmFPX tree "
        "(rms_norm_mul_f32_cuda, architecture-level, reproduces with a plain Qwen3-1.7B).",
        backend_device="Vulkan1 (Strix Halo iGPU, gfx1151)",
        devices=("Strix Halo iGPU (Vulkan1)",),
        memory_pool=POOL_HALO,
        deployment="ds4-reap150b",
        model_file="models/llm/DS4-0731-REAP150B-MXFP4/"
        "DeepSeek-V4-Flash-0731-reap-150b-MXFP4_MOE.gguf",
        port=8109,
        launch_script="ds4-reap150b/serve.sh",
        parameters=(
            LaunchParam(
                name="ctx", env="CTX", label="Context per slot", kind="int",
                default="65536",
                description="KV is allocated lazily, so what costs memory is a FILLED "
                            "slot, not -c. Kept modest so the ~18 GB this artifact saves "
                            "over the 97 GB resident stays available as headroom.",
            ),
            LaunchParam(
                name="slots", env="NP", label="Slots (-np)", kind="int", default="1",
            ),
            LaunchParam(
                name="kv", env="KV", label="KV cache type", kind="enum", default="q8_0",
                choices=(("q8_0", "the qualified DSV4 setting"),
                         ("f16", "~2x the memory"),
                         ("q4_0", "smallest; never KL-gated on DSV4")),
            ),
            _PARAM_PORT,
        ),
        ctx_param="ctx",
        slots_param="slots",
        # ~79 GB weights + KV + buffers. THE POINT of this entry: at ~18 GB less than
        # DS4-0731-Q8Protected-Halo-DwarfStar it leaves ~28 GiB of host headroom with the dGPU
        # model resident, where the 97 GB one left ~0.4 GiB and tripped its OOM guard
        # three times on 2026-08-16 under benchmark load.
        resident_gib=84,
        weights_gib=79.2,
        startable=False,
        not_startable_reason="WEIGHTS DELETED 2026-08-17 (79.2 GiB reclaimed) after "
        "DS4-0731-Q8Protected-Halo-DwarfStar was selected as the APU resident. Kept as a "
        "record because the prune question it answered is worth not re-litigating.\n"
        "WHAT IT ESTABLISHED: the stale-saliency worry in this entry's description was "
        "tested directly and paired against the unpruned IQ3_XXS on 15 LiveCodeBench "
        "medium problems — REAP 10/15 vs unpruned 9/15, ONE discordant pair. No evidence "
        "of prune damage. (An accidental duplicate leg also showed run-to-run noise is "
        "+-1 question, i.e. the same size as that gap.)\n"
        "⚠️ It lost on grounds OTHER than measured quality, and its re-resolve at 16k "
        "tokens was STOPPED PART-WAY — so its corrected score is unknown and could well "
        "have matched the finalists' 13/15. It was retired because DwarfStar's weights "
        "are neither pruned nor abliterated, not because REAP was shown to be worse. "
        "Re-download from the published REAP GGUF if that question is ever reopened.",
        exclusive_with=_exclusive_with("DS4-0731-REAP150B-MXFP4"),
        consumers_note="Benchmark challenger only — no consumer ever routed to :8109.",
    ),
    ModelServerSpec(
        slug="Qwen3.8-27B-R9700-Radiance",
        runtime_family="vllm",
        bench_decode_tok_s=54.4,
        bench_prefill_tok_s=1850.0,
        bench_at="2026-08-16",
        bench_note="Prefill from the 2026-08-16 radiance cutover. Decode RE-MEASURED "
        "2026-08-18 on local-eval/qwen38-quant-ab/decode_probe.py (400 tok greedy, 5 "
        "reps, unique nonce so prefix caching cannot serve it): median 44.4 tok/s, "
        "range 43.5-47.8 — lower than the 54.4 recorded at the cutover, which used a "
        "different harness, so treat 44.4 as the current same-instrument number and "
        "do not read a regression into the difference. Perplexity re-measured the same "
        "day on the same corpus/geometry as the g64 challenger: 8.2109 (wikitext-2, "
        "100 chunks, c=512, 51,100 scored tokens). ⚠️ That 8.2109 is NOT comparable to "
        "the cutover's 6.6094 — different instrument; only compare it to the g64 arm. "
        "Run-to-run noise on this probe is 0.013%. The GGUF path it replaced did ~890 "
        "prefill / 27.2 decode.",
        description="Qwen3.8-27B int4 W4A16 (AutoRound) on the DISCRETE Radeon AI PRO "
        "R9700 via vllm-radiance — `qwen3.8-radiance.service`, the LIVE Qwen3.8 since "
        "2026-08-16. Replaced the llama.cpp/ROCmFPX GGUF path on :8080 after a "
        "measured head-to-head (same card, wikitext-2 test, 100 chunks @ c=512): "
        "quality is a WASH — perplexity 6.6094 here vs 6.6029 for the ROCmFP4 GGUF it "
        "replaced, inside the +-0.10 error bars — while prefill roughly doubles "
        "(~890 -> ~1850 tok/s) and decode doubles (27.2 -> 54.4 tok/s with MTP). "
        "MTP speculation is ON and VERIFIED distribution-preserving here (greedy "
        "output token-identical to unspeculated decoding on 8/8 prompts); the same "
        "test FAILED on the llama.cpp ROCmFPX build (6/8 diverged mid-content), which "
        "is why speculation must stay OFF on any llama.cpp path. Serves BOTH aliases "
        "`qwen3.8-27b-r9700` (Hermes main provider) and `qwen3.8-27b-rocmfp4-r9700` "
        "(Hermes auxiliary roles + ARIA config.steward_model) — the second is a "
        "historical misnomer kept so the cutover broke nothing. Multimodal (vision "
        "tower kept, LMONLY=0).",
        runtime_repo="https://codeberg.org/StillDeadcode/vllm-radiance",
        runtime_ref="docker.io/stilldeadcode/vllm-radiance:0.5.8 (image built "
        "2026-07-31; ships ROCm 7.14.0 internally vs the host's 7.2.4). NOTE: the "
        "Aug-14 upstream commit `[ADD] add qwen3.8 chat template` is NOT in this tag "
        "and no newer tag exists — the checkpoint's own chat_template.jinja is what "
        "vLLM picks up. A source build at main is the way to get the curated one.",
        backend_device="ROCm0 (R9700, gfx1201), vLLM/HIP",
        devices=("R9700 dGPU (ROCm0)",),
        memory_pool=POOL_R9700,
        deployment="qwen3.8-radiance",
        model_file="models/llm/Qwen3.8-27B-int4-AutoRound",
        port=8080,
        systemd_unit="qwen3.8-radiance.service",
        launch_script="qwen3.8-radiance/serve.sh",
        parameters=(
            LaunchParam(
                name="ctx", env="MAXLEN", label="Context (max_model_len)", kind="int",
                default="262144",
                description="⚠️ Unlike llama.cpp this is bounded by a PREALLOCATED KV "
                            "pool, not lazy allocation: the measured pool is ~236,790 "
                            "tokens (fp8 KV; the model is GDN-hybrid so only 16 of 64 "
                            "layers carry full attention KV). RAISED 196608 -> 262144 on "
                            "2026-08-18: that is max_position_embeddings, the model's own "
                            "ceiling, and at gpuutil 0.975 the measured pool is 277,038 "
                            "tokens = 1.06x concurrency at the full length. The retired "
                            "GGUF path's 327680 exceeds the model's position limit. "
                            "Hermes derives compaction as 0.5 x declared context, so "
                            "Hermes declares 245760 against it (2026-08-18), trigger 184,320. ⚠️ 0.5 is NOT the effective value: models under 512K are floored at 0.75 by _effective_threshold_percent(). Keep declared ctx UNDER the server's max_model_len — 245760 vs 262144 reserves 16,384 tokens of output headroom, exactly as 180224 vs 196608 did before.",
            ),
            LaunchParam(
                name="slots", env="MAXSEQS", label="Slots (max_num_seqs)", kind="int",
                default="1",
                description="1 is what buys the large KV pool; a second concurrent "
                            "conversation queues rather than evicting. Per-sequence GDN "
                            "state makes this expensive to raise.",
            ),
            LaunchParam(
                name="spec", env="SPEC", label="Speculative decoding", kind="enum",
                default="mtp",
                choices=(
                    ("mtp", "MTP self-speculation — 34.25 -> 54.42 tok/s decode, "
                            "verified lossless on radiance"),
                    ("off", "no speculation — 34.25 tok/s, frees ~42k tokens of KV"),
                ),
            ),
            LaunchParam(
                name="lmonly", env="LMONLY", label="Drop vision tower", kind="enum",
                default="0",
                choices=(
                    ("0", "keep vision (multimodal) — the standing default"),
                    ("1", "--language-model-only, returns ~0.8 GiB to the KV pool"),
                ),
            ),
            LaunchParam(
                name="gpuutil", env="GPUUTIL", label="GPU memory utilization",
                kind="float", default="0.975",
                description="vLLM sizes the KV pool from what is left after weights, "
                            "activation and graphs, so this is the KV dial. RAISED "
                            "0.94 -> 0.975 on 2026-08-18 to reach the native 262144; "
                            "needs _POOL_SAFETY_MARGIN[POOL_R9700]=0.97, since the "
                            "global 0.92 gate refuses anything above ~29.4 GiB.",
            ),
            _PARAM_PORT,
        ),
        ctx_param="ctx",
        slots_param="slots",
        # Measured 2026-08-18 at 262144 x 1 slot, gpuutil 0.975, vision + MTP:
        # 17.93 weights + 2.10 peak activation + 0.83 non-torch + 0.16 graphs
        # + 10.06 KV = 31.08 GiB. (Was 29 GiB at the old 196608 x 0.94 geometry.)
        resident_gib=31.1,
        weights_gib=17.93,
        exclusive_with=_exclusive_with("Qwen3.8-27B-R9700-Radiance"),
        consumers_note="Hermes DEFAULT provider 'qwen38-r9700' -> :8080 (declared "
        "245760 since 2026-08-18, compaction at 184,320); Hermes auxiliary roles "
        "(compression, skills_hub, ...) -> the same port via the "
        "'qwen3.8-27b-rocmfp4-r9700' alias; ARIA config.steward_model uses that alias "
        "too. DS4 on :8108 remains the coding-agent (pi) model.",
    ),
    ModelServerSpec(
        slug="Qwen3.8-27B-R9700-Radiance-G64",
        runtime_family="vllm",
        bench_decode_tok_s=41.1,
        bench_at="2026-08-18",
        bench_note="MEASURED AND REJECTED 2026-08-18. Same probes as the incumbent, same corpus, same geometry-independent perplexity instrument:\n  quality  ppl 8.2164 vs incumbent 8.2109 -- the challenger is 0.066% WORSE, against a measured run-to-run noise floor of 0.013%, so the difference is real and in the wrong direction.\n  speed    decode 41.1 tok/s median vs 44.4 -- 8% SLOWER, despite its bf16 draft head (the incumbent quantizes the MTP head to int4).\n  context  231,296 max vs 262,144 -- 31k SHORTER, because the bf16 MTP head costs more (18.92 GiB of weights) than the finer group size saves.\nLoses on all three axes; the incumbent stays. ⚠️ The interesting negative result: near-identical perplexity does NOT mean a near-identical model -- top-1 token agreement between the two is only 91.17% with KL 0.0259 nats (for scale, ROCmFP4-FAST was rejected at 87.97% / 0.0636). Two AutoRound runs of the same model at different group sizes land ~9% of their argmaxes apart while scoring the same. CONCLUSION: group size is NOT the explanation for the incumbent being 0.89% behind unsloth UD-Q4_K_XL; the calibration run itself dominates. Reopening that question means a different quantizer or a self-run calibration, not another group size.",
        startable=False,
        not_startable_reason="WEIGHTS DELETED 2026-08-18 (~19 GiB reclaimed) after being measured and rejected the same day -- worse quality, slower decode, less context than the incumbent (see bench_note). Re-download Vishva007/Qwen3.8-27B-W4A16-AutoRound-GPTQ if the question is reopened.",
        description="BENCHMARK CHALLENGER to Qwen3.8-27B-R9700-Radiance, added "
        "2026-08-17. Identical model, stack, launcher and kernel; the ONLY variable is "
        "the AutoRound checkpoint's quantization group size — 64 instead of 128 "
        "(Vishva007/Qwen3.8-27B-W4A16-AutoRound-GPTQ, AutoRound 0.15.0, 17.87 GiB vs "
        "the incumbent's 17.69). Reason to test it: the 2026-08-16 cutover left the "
        "incumbent 0.89% worse on perplexity than the unsloth UD-Q4_K_XL GGUF it beat "
        "on speed, and group size is the obvious suspect — scale+zero overhead is "
        "0.156 bits/weight at g128 and 0.3125 at g64. The RDNA hybrid kernel already "
        "declares SUPPORTED_GROUP_SIZES = [32, 64, 128], so no new kernel is needed, "
        "and at only +0.18 GiB of weights this should still reach the model's native "
        "262,144 ceiling — unlike the g32 attempt below, it does not trade context for "
        "quality. "
        "⚠️ WHY NOT g32: the first attempt used "
        "Pilcothink/Qwen3.8-27B-MixedInt4-AutoRound (g32 + 17 modules at int8) and it "
        "CANNOT RUN ON THIS CARD. vLLM enumerated every kernel for the int8 modules "
        "and all refused — RDNAHybrid/Triton are uint4-only, Exllama supports float16 "
        "activations only (this runs bf16), Conch supports group sizes [-1, 128] only, "
        "RDNA3 needs gfx1100, Marlin/Machete/AllSpark are CUDA. There is no uint8b128 "
        "path on gfx1201 at group_size 32 with bf16. Reaching one via --dtype float16 "
        "would change the numerics under measurement and was rejected. Before "
        "downloading any future mixed-bit checkpoint, check that a kernel accepts its "
        "SECOND bit width. "
        "⚠️ The tuned prefill tile table is keyed on (group_size, K, N, M-bucket) and "
        "has rows for gs=32 and gs=128 only, so at gs=64 every lookup misses and the "
        "kernel uses its stock heuristic. A prefill regression here is a tile-table "
        "artifact, not a property of g64.",
        runtime_repo="https://codeberg.org/StillDeadcode/vllm-radiance",
        runtime_ref="docker.io/stilldeadcode/vllm-radiance:0.5.8 — the SAME image and "
        "tuned-tile patch as the incumbent (see the gs=64 caveat above).",
        backend_device="ROCm0 (R9700, gfx1201), vLLM/HIP",
        devices=("R9700 dGPU (ROCm0)",),
        memory_pool=POOL_R9700,
        deployment="qwen3.8-radiance-g64",
        model_file="models/llm/Qwen3.8-27B-int4-AutoRound-g64",
        port=8110,
        # Hand-written, NOT ARIA-generated. _render_unit() emits no MemoryHigh, and this
        # deployment needs the same 24G page-cache guard the incumbent carries: streaming
        # an 18 GiB checkpoint counts against MemAvailable host-wide and twice on
        # 2026-08-16 that OOM-killed DS4 on the other GPU.
        systemd_unit="qwen3.8-radiance-g64.service",
        launch_script="qwen3.8-radiance-g64/serve.sh",
        parameters=(
            LaunchParam(
                name="ctx", env="MAXLEN", label="Context (max_model_len)", kind="int",
                default="196608",
                description="Bounded by a PREALLOCATED KV pool, as on the incumbent. "
                            "At +0.18 GiB of weights this should still clear the "
                            "native 262,144 ceiling.",
            ),
            LaunchParam(
                name="slots", env="MAXSEQS", label="Slots (max_num_seqs)", kind="int",
                default="1",
            ),
            LaunchParam(
                name="spec", env="SPEC", label="Speculative decoding", kind="enum",
                default="mtp",
                choices=(
                    ("mtp", "MTP self-speculation — the MTP head is quantized inline"),
                    ("off", "no speculation — frees KV"),
                ),
            ),
            LaunchParam(
                name="lmonly", env="LMONLY", label="Drop vision tower", kind="enum",
                default="0",
                choices=(
                    ("0", "keep vision (multimodal) — matches the incumbent's default"),
                    ("1", "--language-model-only, returns ~0.8 GiB to the KV pool"),
                ),
            ),
            LaunchParam(
                name="gpuutil", env="GPUUTIL", label="GPU memory utilization",
                kind="float", default="0.94",
            ),
            _PARAM_PORT,
        ),
        ctx_param="ctx",
        slots_param="slots",
        weights_gib=18.11,
        resident_gib=29,
        exclusive_with=_exclusive_with("Qwen3.8-27B-R9700-Radiance-G64"),
        consumers_note="Benchmark challenger only — nothing routes to :8110, and the "
        "served name is 'qwen3.8-27b-r9700-g64' precisely so a stray request for the "
        "production alias cannot land here. Promote by pointing the qwen3.8-radiance "
        "deployment's MODEL_DIR at this checkpoint, not by repointing consumers.",
    ),
    ModelServerSpec(
        slug="Qwen3.8-27B-R9700-HIP",
        startable=False,
        not_startable_reason="RETIRED 2026-08-16, superseded by "
        "`Qwen3.8-27B-R9700-Radiance` on the same port. Its unit is disabled, its "
        "ROCmFP4/Q6_K GGUFs and the `rocmfpx-src/build-rdna4-rocwmma` runtime were "
        "deleted (Ben: radiance is the only way Qwen3.8 gets deployed), so this cannot "
        "start even if re-enabled. Kept as the record of what the numbers were "
        "measured against: perplexity 6.6029, prefill ~890 tok/s, decode 27.2 tok/s, "
        "and 327,680 x 2 slots of lazily-allocated q4_0 KV — a context geometry the "
        "vLLM replacement cannot reach, which is why Hermes's declaration dropped "
        "250000 -> 196608.",
        description="Qwen3.8-27B (dense, GDN hybrid) on the DISCRETE Radeon AI PRO "
        "R9700 — `qwen-r9700.service`. Since 2026-08-15 the unit's ExecStart is "
        "`serve-rocmfp4.sh` (drop-in `rocmfp4.conf`): the ROCmFPX HIP gfx1201 build "
        "serving the AMD-native ROCmFP4 weights (16.5 GiB) with `-fit off`. Lives "
        "entirely in the card's own 32 GiB of VRAM, so it runs CONCURRENTLY with a "
        "Halo-resident DS4 — the dGPU half of the verified dual-serving deployment. "
        "Standing geometry (2026-08-15T16:20, `context.conf`): ONE unified KV pool of "
        "327,680 tokens shared by 2 slots — Hermes's main conversation up to the "
        "model's native 262,144, a second slot for crons — measured 23.7 GiB VRAM. "
        "(Slug renamed from Qwen3.8-27B-Q6_K-R9700-HIP 2026-08-15: the unit had been "
        "swapped to ROCmFP4 by drop-in while the registry still said Q6_K.)",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="~/Development/rocmfpx-src build-rdna4-rocwmma (gfx1201-only HIP; "
        "HIP_VISIBLE_DEVICES=0 is mandatory — it core-dumps on the gfx1151 iGPU)",
        backend_device="ROCm0 (R9700, gfx1201), HIP",
        devices=("R9700 dGPU (ROCm0)",),
        memory_pool=POOL_R9700,
        deployment="qwen-r9700",
        model_file="models/llm/Qwen3.8-27B-ROCmFPX-GGUF/Qwen3.8-27B-ROCmFP4.gguf",
        port=8080,
        systemd_unit="qwen-r9700.service",
        launch_script="qwen-r9700/serve-rocmfp4.sh",
        parameters=(
            LaunchParam(
                name="model", env="MODEL", label="Model file", kind="path",
                default="models/llm/Qwen3.8-27B-ROCmFPX-GGUF/Qwen3.8-27B-ROCmFP4.gguf",
                choices=(
                    ("/home/ben/Development/infrastructure/models/llm/"
                     "Qwen3.8-27B-ROCmFPX-GGUF/Qwen3.8-27B-ROCmFP4.gguf",
                     "ROCmFP4 (type 100, 4.50 bpw), 16.5 GiB — the standing default"),
                    ("/home/ben/Development/infrastructure/models/llm/"
                     "Qwen3.8-27B-GGUF/Qwen3.8-27B-Q6_K.gguf",
                     "Q6_K, 21.3 GiB — the 2026-08-14 qualified quant; ~5 GiB less "
                     "room for KV"),
                ),
                description="Any GGUF this ROCmFPX HIP build can read. Only ROCmFP4 "
                            "has been run on this script; Q6_K was qualified on the "
                            "mainline HIP `serve.sh` (still on disk, not the ExecStart).",
            ),
            LaunchParam(
                name="ctx", env="CTX", label="Context (per slot, or pool if unified)",
                kind="int", default="131072",
                description="With kv_unified=1 this is ONE shared pool any slot can "
                            "grow into (each slot still capped at n_ctx_train "
                            "262144); without it, every slot gets this much and "
                            "memory is ctx x slots. q4_0 KV measured ~34 KiB/token.",
            ),
            LaunchParam(
                name="slots", env="NP", label="Slots (-np)", kind="int", default="1",
                description="Dense model, so concurrent slots batch well — but a big "
                            "cold prefill on one slot visibly slows decode on the "
                            "other (measured 2026-08-15: ~6 t/s during a 13K prefill).",
            ),
            LaunchParam(
                name="kv_unified", env="KVU", label="Unified KV pool", kind="enum",
                default="0",
                choices=(
                    ("1", "one shared pool of `ctx` tokens across all slots — "
                          "heterogeneous conversations (256K + 64K) in a fixed budget"),
                    ("0", "each slot owns a full `ctx` cache; memory = ctx x slots"),
                ),
            ),
            LaunchParam(
                name="kv", env="KV", label="KV cache type", kind="enum", default="q4_0",
                choices=(
                    ("q4_0", "~34 KiB/token — what fits 320K on this card; low-risk "
                             "on a standard qwen arch (unlike DSV4)"),
                    ("q8_0", "~2x q4_0"),
                    ("f16", "~4x q4_0"),
                ),
            ),
            LaunchParam(
                name="cache_ram", env="CACHE_RAM", label="Prompt cache (MiB)", kind="int",
                default="1024",
                description="Host-RAM parked prompt cache — it WORKS on this model "
                            "(unlike DS4) but the RAM is the Halo's budget; keep it "
                            "small (<=2048).",
            ),
            _PARAM_PORT,
        ),
        ctx_param="ctx",
        slots_param="slots",
        # 23.7 GiB measured 2026-08-15 at the 320K unified pool (16.5 weights +
        # KV + buffers). Projected against the R9700's OWN 32 GiB VRAM pool.
        resident_gib=24,
        exclusive_with=_exclusive_with("Qwen3.8-27B-R9700-HIP"),
        consumers_note="Hermes DEFAULT provider 'qwen38-r9700' -> :8080 since "
        "2026-08-15T16:35 (declared 250000); pi provider 'qwen38-r9700' -> :8080. "
        "DS4 on :8108 is now the coding-agent (pi) model.",
    ),
    ModelServerSpec(
        slug="Qwen3.8-27B-Q6_K-R9700-Vulkan-MTP",
        startable=False,
        not_startable_reason="WEIGHTS DELETED 2026-08-16 in the radiance cutover — models/llm/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q6_K.gguf no longer exists. Marked unstartable 2026-08-17 after a sweep found it still advertised as startable, i.e. ARIA would have offered a start that could never succeed. Was: 39 tok/s with Vulkan MTP vs 22.9 without. \u26a0\ufe0f REVIVAL INSTRUCTION CORRECTED 2026-08-19: this used to say \"re-download the Q6_K GGUF\", which is no longer possible \u2014 unsloth DELETED Qwen3.8-27B-Q6_K.gguf from unsloth/Qwen3.8-27B-GGUF that day (they also dropped UD-IQ2_M and renamed UD-Q8_K_S -> UD-Q8_K_L). Only UD-Q6_K* variants remain; the closest match to the 21.3 GiB file this pointed at is UD-Q6_K_M (21.50 GiB), which is a DIFFERENT quant, so the 39 tok/s figure above would not carry over unmeasured. Reviving this is therefore a re-measurement, not a re-download \u2014 and it is very unlikely to be worth it: the llama.cpp path measured roughly half of radiance\u0027s speed (prefill ~1100 vs ~1850, decode 27.1 vs 54.4), and Vulkan/ROCmFPX MTP FAILED the correctness check on this box (6 of 8 greedy prompts diverged mid-content) where radiance\u0027s passed 8/8.",
        description="Qwen3.8-27B Q6_K on the R9700 through the Ciru ROCmFPX Vulkan "
        "build, with MTP self-speculative decode ON — the head ships inside the GGUF "
        "as blk.64, there is no separate draft file. MTP is +70% decode here: 39.02 "
        "vs 22.92 tok/s (measured 2026-08-14), which is why this variant exists "
        "alongside the HIP one. Vision is available (mmproj-F16.gguf is on disk) but "
        "disabled — it was never exercised, and headroom at 131072 ctx is only ~4.7 GiB.",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="chadrock-rocmfpx:latest image, build-laguna-strix-vulkan "
        "(shared with chadrock/chadrockv2)",
        backend_device="Vulkan0 (R9700, gfx1201)",
        # ⚠️ Vulkan0 means the R9700 on this box. Installing the dGPU inverted
        # it; every older compose file that says Vulkan0 meaning "the iGPU" is
        # now wrong, which is why those entries are marked unstartable below.
        devices=("R9700 dGPU (Vulkan0)",),
        memory_pool=POOL_R9700,
        deployment="qwen3.8-27b",
        model_file="models/llm/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q6_K.gguf",
        port=8110,
        compose_file="qwen3.8-27b/docker-compose.yml",
        service_name="qwen3.8-27b",
        container_name="qwen3.8-27b",
        # Measured VRAM ceiling: 24.1 GiB at 32768 ctx, 27.2 GiB at the
        # configured 131072, ~31.6 GiB projected at 262144 (no headroom).
        resident_gib=28,
        exclusive_with=_exclusive_with("Qwen3.8-27B-Q6_K-R9700-Vulkan-MTP"),
        consumers_note="unbound — the faster of the two Qwen3.8 variants, but "
        "compose-frozen: its launch flags live in the compose file, not in "
        "selectable parameters.",
    ),
    ModelServerSpec(
        slug="Qwen3.8-27B-ROCmFP4-R9700-Vulkan",
        startable=False,
        not_startable_reason="WEIGHTS DELETED 2026-08-16 in the radiance cutover — models/llm/Qwen3.8-27B-ROCmFPX-GGUF/Qwen3.8-27B-ROCmFP4.gguf no longer exists (same file the already-retired Qwen3.8-27B-R9700-HIP entry points at). Marked unstartable 2026-08-17 by the same sweep. vllm-radiance replaced this path: ~2x prefill and decode for a wash on perplexity (6.6094 vs 6.6029).",
        description="Qwen3.8-27B in ROCmFPX Q4_0_ROCMFP4 format (17.7 GB) on the "
        "R9700, same Ciru ROCmFPX Vulkan runtime as the Q6_K variant. The AMD-native "
        "weight format Ben asked for on the 9700: ~4.6 GiB smaller than Q6_K, which "
        "buys back headroom for context or the vision tower. Requires a ROCmFPX build "
        "— mainline llama.cpp cannot read these tensors.",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="chadrock-rocmfpx:latest image, build-laguna-strix-vulkan",
        backend_device="Vulkan0 (R9700, gfx1201)",
        devices=("R9700 dGPU (Vulkan0)",),
        memory_pool=POOL_R9700,
        deployment="qwen3.8-27b",
        model_file="models/llm/Qwen3.8-27B-ROCmFPX-GGUF/Qwen3.8-27B-ROCmFP4.gguf",
        port=8110,
        compose_file="qwen3.8-27b/docker-compose.yml",
        service_name="qwen3.8-27b-rocmfp4",
        container_name="qwen3.8-27b-rocmfp4",
        profile="rocmfp4",
        # 17.7 GB of weights vs Q6_K's 21.3, same q8_0 KV at the same context.
        # SWAG, not measured — this variant has not been brought up yet.
        resident_gib=24,
        exclusive_with=_exclusive_with("Qwen3.8-27B-ROCmFP4-R9700-Vulkan"),
        consumers_note="unbound — added 2026-08-14, never started. Verify decode "
        "speed (not just /health) on first run: a silently-failed VRAM fit serves "
        "correctly at ~0.4 tok/s.",
    ),
    ModelServerSpec(
        slug="DS4-0731-IQ2M-DSpark-64k",
        description="DeepSeek V4 Flash 0731, Unsloth UD-IQ2_M target plus Q8_0 "
        "DSpark drafter at width 4. Six 65,536-token slots with unified KV and "
        "prompt caching. Optional high-throughput profile: 63.83 aggregate tok/s "
        "for six clients, but the frozen 256-case gate found three new scored "
        "failures versus target-only. Affine is the quality-first default.",
        runtime_repo="https://github.com/ggml-org/llama.cpp.git",
        runtime_ref="08659901c43b51de735740f1cf61bb82fbe0c4e4 (ROCm 7.2.4, gfx1151)",
        backend_device="ROCm0 (gfx1151)",
        model_file="models/llm/unsloth-DS4-0731-IQ2M/UD-IQ2_M/"
        "DeepSeek-V4-Flash-0731-UD-IQ2_M-00001-of-00003.gguf",
        port=8107,
        systemd_unit="deepseek-v4-iq2m-dspark-64k.service",
        # Target (90.92 GB) + drafter (10.90 GB) are one inseparable serving
        # profile. Keep the measured conservative whole-profile figure instead
        # of pretending model_file alone describes the resident weights. The
        # static systemd geometry still reports six 64K slots, while the launch
        # wrapper independently enforces 108 GiB start / 12 GiB run tripwires.
        resident_gib=109.0,
        exclusive_with=_exclusive_with("DS4-0731-IQ2M-DSpark-64k"),
        startable=False,
        not_startable_reason="Runtime AND weights are gone: the unit's "
        "~/ds4-mainline-dspark/ tree no longer exists (checked 2026-08-14). This "
        "profile was not migrated in the infrastructure consolidation — nothing "
        "under infrastructure/ carries the IQ2_M target or its Q8_0 drafter. Use "
        "DS4-0731-Q8Protected-Halo-DwarfStar or DS4-0731-IQ3_S-Hybrid-ROCm-Dual instead.",
        consumers_note="Hermes default provider 'ds4'; pi coding agent provider 'ds4'",
        endpoint_override="http://100.123.245.84:8107/v1",
    ),
    ModelServerSpec(
        slug="DS4-0731-ROCMFPX-affine-256k",
        description="DeepSeek V4 Flash 0731, ROCmFPX affine 2.58 BPW (85.26 GiB), "
        "served as six guarded 65,536-token slots. Quality-first default selected "
        "2026-08-10 after tying IQ2_M at 238/256 while recovering all three deepest "
        "early-recall failures. The compatibility slug and unit filename retain "
        "'256k', but launch geometry is parsed from the unit and is authoritative. "
        "The sealed affine runtime, prompt caching, and 12 GiB live guard are active.",
        runtime_repo="https://github.com/baf509/rocmfpx-ds4.git",
        runtime_ref="branch decode-fusion (sealed bundle o5-release-86f0056d-20260803T231500-0400)",
        backend_device="ROCm0 (gfx1151)",
        # Production path, unchanged. Every on-box entry in this registry is also
        # packaged as a model+runtime pair under ~/Development/model-distros/,
        # one folder per slug, described by a pair.toml. Those are PUBLISHING
        # artifacts, not live paths: each model/ is a hardlink to the production
        # GGUF (same inode, no second copy) and each runtime/ is a copy. Moving
        # or deleting a pair folder does not affect this service.
        #
        # The pair slug MUST equal this spec's slug — `pairs doctor` cross-checks
        # model_file, port and systemd_unit between the two and fails on drift.
        # Run it after editing either side.
        model_file="models/llm/DS4-0731-ROCMFPX-affine.gguf",
        port=8107,
        systemd_unit="deepseek-v4-quality-256k.service",
        # NOT hand-declared any more. The footprint is computed from the `-c`
        # in deepseek-v4-quality-256k.service (see effective_resident_gib), so
        # the 2026-08-05 staleness — declared 86.5 while really holding 94.08
        # after a -c change — cannot recur. resident_gib is the fallback used
        # only if that unit ever becomes unparseable.
        #
        # weights: 85.26 GiB (2.58 BPW affine quant, from the GGUF).
        # kv: 6880 bytes/token = 6.71875 KiB, MEASURED 2026-08-09 — an OOM at
        # -c 1382400 -np 6 named the exact buffer it could not allocate
        # (57065472000 bytes = 1382400 * 6 * 6880), which is a cleaner reading
        # than the two-GTT-snapshot estimate it replaces (that one said 11.0 and
        # was wrong, because the snapshots differed by a 4 GiB prompt cache as
        # well as by -c). KV totals over -c * -np, not -c.
        resident_gib=86.5,
        weights_gib=85.26,
        kv_kib_per_token=6.71875,
        # 15.6, not the 2.1 default. Compute buffers scale with how much work is
        # in flight, so this was measured at the PEAK, not at rest — three
        # readings on 2026-08-09, same weights:
        #     94.56 GiB  loaded, idle              -> overhead  ~1.9
        #    104.82 GiB  one slot, small request   -> overhead ~10.5
        #    108.73 GiB  one slot, 56k prefill     -> overhead ~15.6
        #        (108.73 - 85.26 weights - 7.87 KV at -c 204800)
        # The gate exists to refuse overcommit, and overcommit happens at peak,
        # so the peak is the number it must carry. An earlier pass set 10.7 from
        # the middle reading; the box had already OOM-killed llama-server 8x that
        # day, which is the cost of sizing this optimistically.
        #
        # ⚠️ This constant cannot fix the gate's real blind spot: _read_gtt_gib()
        # sees GPU-visible memory ONLY, but on this unified-memory box the CPU
        # side draws from the same 124 GiB. gemma-aux (~2.6), mongod/mongot/
        # embeddings (~1.9) and the desk's claude sessions (~1.5) are invisible
        # to it, so the gate reads ~15 GiB free when ~8 is the truth.
        overhead_gib=15.6,
        exclusive_with=_exclusive_with("DS4-0731-ROCMFPX-affine-256k"),
        startable=False,
        not_startable_reason="SUPERSEDED by DS4-0731-ROCmFPX-Affine-Quality. The "
        "sealed O5 runtime moved from runtime-bundles/ into ds4-affine/runtime/ and "
        "the GGUF into ds4-affine/model/, so this unit's ExecStart and -m both point "
        "at paths that no longer exist. Same model, same runtime, same guards — the "
        "new entry adds selectable ctx/slots.",
        consumers_note="Hermes default provider 'ds4'; pi coding agent provider 'ds4'",
        # Binds the TAILNET IP ONLY - there is no localhost listener, so the
        # port-derived default would hand consumers a dead URL. Same gotcha as
        # Ridge. Note :8107 is also claimed by the stopped qwen3.6-35b-a3b-Q4
        # entry on localhost; see endpoints.env.
        # _TAILNET_IP is defined below REGISTRY, so hardcode as Ridge does.
        endpoint_override="http://100.123.245.84:8107/v1",
    ),
    ModelServerSpec(
        slug="DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K",
        description="DeepSeek V4 Flash 0731 quality/throughput profile: Unsloth "
        "UD-IQ3_S target plus compact Q3-expert/Q8-dense DSpark drafter, split "
        "80/20 over Radeon 8060S + OCuLink Radeon AI PRO R9700. Four unified-KV "
        "slots, 131,072 tokens per slot, F16 KV, continuous batching, idle-slot "
        "prompt preservation, and a 1 GiB reusable RAM prompt cache. The 4x256K "
        "capacity profile loaded, but tripped the 12 GiB guard after real Pi "
        "traffic while Gemma was resident; 4x128K is the co-resident profile.",
        runtime_repo="https://github.com/Nathanw1014/strix-halo-llamacpp.git",
        runtime_ref="Nathan-derived Vulkan runtime with dual-device DSpark support",
        backend_device="Vulkan1 (Strix Halo) + Vulkan0 (R9700), 80/20 layer split",
        model_file="ds4-sharded-experts/models-0731/UD-IQ3_S/"
        "DeepSeek-V4-Flash-0731-UD-IQ3_S-00001-of-00004.gguf",
        port=18211,
        systemd_unit="deepseek-v4-iq3s-dspark-dual-production.service",
        # Used for route ranking/documentation only. The footprint spans a
        # discrete-VRAM device and shared system memory, so the legacy
        # single-GTT projection cannot represent it. The launch wrapper's
        # MemAvailable guard remains authoritative.
        resident_gib=104.0,
        gtt_resident=False,
        exclusive_with=_exclusive_with(
            "DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K"
        ),
        startable=False,
        not_startable_reason="SUPERSEDED by DS4-0731-IQ3_S-Hybrid-ROCm-Dual. The "
        "ds4-sharded-experts/ tree this unit launches from no longer exists; the "
        "same UD-IQ3_S weights and compact DSpark drafter now live in ds4-hybrid/, "
        "served by the mainline HIP dual-arch build over ROCm rather than the "
        "Vulkan runtime that was removed with the old tree.",
        consumers_note="Hermes primary; regular Pi; ARIA watched-shell Pi coding",
        endpoint_override="http://127.0.0.1:18211/v1",
    ),
    ModelServerSpec(
        slug="Ling-3.0-flash-MXFP4",
        description="inclusionAI Ling-3.0-flash, MXFP4_MOE (65.05 GiB) — a 124B-total/"
        "5.1B-active hybrid-linear MoE: 35 KDA (Kimi Delta Attention) layers + 7 gated-MLA "
        "layers, 512 routed experts top-8 + 1 shared, plus a bundled MTP block. Served at "
        "131072 ctx (= the GGUF's context_length; the model card's 256K did NOT survive "
        "the quant). Like DS4 this is NOT a docker container: it runs as the systemd --user "
        "unit ling-3.0-flash.service from a runtime bundle, because its runtime is a "
        "host-built HIP fork with no image. Added 2026-08-05.",
        runtime_repo="https://github.com/baf509/rocmfpx-ds4.git",
        runtime_ref="branch bailingmoe3 (bundle bailingmoe3-89926145-20260805T115134-0400) — "
        "rocmfpx main @ 2b21cfb04 + upstream PR ggml-org/llama.cpp#26608 cherry-picked and "
        "its MTP/nextn + recurrent-layer API ported to this fork; PR still open upstream",
        backend_device="ROCm0 (gfx1151)",
        model_file="models/llm/Ling-3.0-flash-MXFP4_MOE/Ling-3.0-flash-MXFP4_MOE.gguf",
        port=8108,
        systemd_unit="ling-3.0-flash.service",
        # MEASURED 64.81 GiB GTT at -c 8192 (2026-08-05), full offload, no
        # --n-cpu-moe. Padded to 70 for the MLA KV at the served 131072 ctx:
        # only the 7 MLA layers hold a real cache (~8 KB/token across all of
        # them), and the 35 KDA layers keep a fixed-size recurrent state that
        # does not grow with context — so the ctx cost here is ~1 GiB, far
        # below what a same-size dense-attention model would need.
        resident_gib=70,
        exclusive_with=_exclusive_with("Ling-3.0-flash-MXFP4"),
        startable=False,
        not_startable_reason="Weights ALSO DELETED 2026-08-26 (66 GiB reclaimed, incl. the model-distros link); RUNTIME GONE: the bailingmoe3 bundle "
        "under runtime-bundles/ling-3.0-flash/ was removed in the 2026-08-11..14 "
        "consolidation and was not relocated. Ling needs that fork specifically — "
        "the ports of PR ggml-org/llama.cpp#26608 are not in any installed build. "
        "Rebuild the bundle to revive this entry.",
        consumers_note="unbound — new, not yet validated beyond a smoke test",
        # Binds 127.0.0.1 only, so the port-derived localhost default is
        # correct and no endpoint_override is needed. Deliberately NOT the
        # tailnet bind DS4 uses: nothing off-box consumes this yet, and the
        # tailnet-only bind is the repeatedly-misdiagnosed dead-localhost
        # gotcha. To expose it, change --host in the unit AND add an override.
    ),
    ModelServerSpec(
        slug="Ling-3.0-flash-ROCmFP4-STRIX-MTP",
        description="inclusionAI Ling-3.0-flash, Q4_0_ROCMFP4_STRIX (64.9 GiB) — the same "
        "124B-total/5.1B-active hybrid-linear MoE, quantized to this fork's own Strix Halo "
        "attn-K/V recipe with the MTP/NextN head preserved at Q8_0. The PREFERRED Ling: "
        "faster AND smaller than the Q5_K_M entry — 39.02 vs 34.05 tok/s decode at 20 GiB "
        "less. Runs a DIFFERENT runtime lineage from the other two: charlie12345/ROCmFPX "
        "main @ d3ca53726, which merged the BailingMoeV3 implementation from raulvidis "
        "(who also published these weights) plus three speculative-decode fixes and the "
        "per-layer SwiGLU clamp against garbage-token logit collapse — none of which the "
        "aetherbird PR 26608 port behind ling-3.0-flash.service has. MTP is verified "
        "working here but left OFF: it measures 39.60 vs 39.02, i.e. nothing, because at "
        "5.1B active Ling uses only ~35% of memory bandwidth and speculation is a "
        "bandwidth-amortisation trick. Added 2026-08-07.",
        runtime_repo="https://github.com/charlie12345/ROCmFPX.git",
        runtime_ref="main @ d3ca53726 (bundle rocmfp4-mtp-d3ca5372-20260807T164848-0400) — "
        "merges ROCmFPX #57 BailingMoeV3+MTP, #56 spec-state checkpoint restore, "
        "#59 spec replay livelock guard, #55 draft accept correction. Clean tree.",
        backend_device="ROCm0 (gfx1151)",
        # Split GGUF — llama.cpp opens part 1 and pulls in part 2 automatically.
        model_file="models/llm/Ling-3.0-flash-ROCmFP4-STRIX-MTP/Ling-3.0-flash-ROCmFP4-STRIX-MTP-Q4_0-00001-of-00002.gguf",
        port=8108,
        systemd_unit="ling-3.0-flash-rocmfp4.service",
        # MEASURED 66 GiB GTT at the served -c 131072 (2026-08-07), padded to 68.
        resident_gib=68,
        exclusive_with=_exclusive_with("Ling-3.0-flash-ROCmFP4-STRIX-MTP"),
        startable=False,
        not_startable_reason="Weights ALSO DELETED 2026-08-26 (65 GiB reclaimed, incl. the model-distros link); RUNTIME GONE: the rocmfp4-mtp bundle "
        "was removed in the 2026-08-11..14 consolidation. The chadrock-rocmfpx:latest "
        "image is still on the box but is a DIFFERENT lineage (ciru-ai, not "
        "charlie12345 @ d3ca53726) and is not known to carry BailingMoeV3 — do not "
        "assume it as a substitute without testing.",
        consumers_note="preferred Ling — fastest and smallest of the three. "
        "Q5_K_M was removed 2026-08-07 and replaced by Q6_K as the quality tier.",
        # Binds 127.0.0.1 only, so the port-derived localhost default is correct.
    ),
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
        startable=False,
        not_startable_reason="The GGUF is gone — models/llm/Laguna-S-2.1-GGUF/ no "
        "longer holds laguna-s-2.1-Q4_K_M.gguf (checked 2026-08-14). Retired "
        "2026-07-28; the laguna-rocm:latest image is still on the box.",
        memory_pool=POOL_HALO,
        devices=("Strix Halo iGPU (HIP ROCm0, single-GPU era)",),
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
        startable=False,
        not_startable_reason="The GGUF is gone — models/llm/Laguna-S-2.1-Chadrock-"
        "ROCmFP4/ no longer exists (checked 2026-08-14). Physically shut down by Ben "
        "2026-07-29. Its `Vulkan0` would also now resolve to the R9700, not the iGPU.",
        memory_pool=POOL_HALO,
        devices=("declared Vulkan0 — meant the iGPU when written, now the R9700; "
                 "needs a device audit before any revival",),
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
        startable=False,
        not_startable_reason="DEVICE AUDIT REQUIRED. Its compose file pins "
        "`--device Vulkan0`, written when the iGPU was the only card. Installing the "
        "R9700 inverted that: Vulkan0 is now the 32 GiB dGPU, and this service asks "
        "for 262144 ctx on top of ~20 GiB of weights, which will not fit. The weights "
        "are still on disk — fix the device (Vulkan1) and re-qualify the context, "
        "then clear this flag.",
        memory_pool=POOL_HALO,
        devices=("declared Vulkan0 — now the R9700; almost certainly meant to be "
                 "Vulkan1 (Strix Halo)",),
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
        # CORRECTED 2026-08-05: this used to read qwen-rocmfp4/models/... back when
        # the compose project mounted its own ./models dir. That directory was
        # deleted the same day and the GGUFs moved under models/llm/; the compose
        # file was updated to mount models/llm but this pointer was not, so it
        # dangled. Caught by `pairs doctor` (see ~/Development/model-distros).
        model_file="models/llm/Qwen3.6-35B-A3B-MTP/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
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
        # CORRECTED 2026-08-05: same dangling-path fix as qwen3.6-35b-a3b-Q4 above.
        model_file="models/llm/Qwen3.6-27B/Qwen3.6-27B-Q8_0.gguf",
        port=8093,
        compose_file="qwen-rocmfp4/docker-compose.yml",
        # renamed from qwen-agentic 2026-07-29 (service + container_name, safe
        # while not created) so the compose service matches this slug.
        service_name="qwen3.6-27b-Q8",
        container_name="qwen3.6-27b-Q8",
        profile="qwen",
        resident_gib=30,
        startable=False,
        not_startable_reason=(
            "WEIGHTS DELETED 2026-08-26 (27 GiB reclaimed): models/llm/Qwen3.6-27B/Qwen3.6-27B-Q8_0.gguf and the model-distros/qwen3.6-27b-Q8 link are gone. Superseded by Qwen3.8-27B Radiance on the R9700."
        ),
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
        memory_pool=POOL_HOST,
        devices=("CPU only",),
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
        startable=False,
        not_startable_reason="The GGUF is gone — models/llm/Chadrockv2-Qwen3.6-27B-"
        "ROCmFP6-STRIX-QUALITY/ no longer exists (checked 2026-08-14). Its Vulkan0 "
        "would also now resolve to the R9700.",
        memory_pool=POOL_HALO,
        devices=("declared Vulkan0 — meant the iGPU when written, now the R9700",),
    ),
    ModelServerSpec(
        slug="Ling-3.0-flash-Q6_K",
        description="inclusionAI Ling-3.0-flash, Q6_K (98.3 GiB) — the highest-fidelity "
        "Ling quant of the same 124B-total/5.1B-active hybrid-linear MoE. REQUIRES the "
        "bailingmoe3 bundle; the rocmfp4-mtp bundle will not load it. Shares port 8108 "
        "with the MXFP4 and ROCmFP4 builds, so only one Ling can be up.",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="branch bailingmoe3 (bundle bailingmoe3-89926145-20260805T115134-0400)",
        backend_device="ROCm0",
        model_file="models/llm/Ling-3.0-flash-Q6_K/Ling-3.0-flash-Q6_K.gguf",
        port=8108,
        systemd_unit="ling-3.0-flash-q6k.service",
        # 98.3 GiB weights + MLA KV at the served -c 131072. Padded to 105.
        resident_gib=105,
        exclusive_with=_exclusive_with("Ling-3.0-flash-Q6_K"),
        startable=False,
        not_startable_reason="Both the bailingmoe3 runtime bundle AND the Q6_K GGUF "
        "were removed in the 2026-08-11..14 consolidation (checked 2026-08-14).",
        consumers_note="unbound — added 2026-08-08 for benchmarking",
    ),
    ModelServerSpec(
        slug="Step-3.7-Flash-APEX-I-Compact",
        description="Step-3.7-Flash-APEX I-Compact (Q4_K, 84.1 GiB) — 198B total / ~11B "
        "active step35 MoE, 45 layers, 288 routed + 1 shared expert top-8. VISION-CAPABLE "
        "via the f16 mmproj tower; the only multimodal model on this box. Not "
        "hybrid-linear, so KV grows normally and bounds context (served at 131072, "
        "below the GGUF's 262144). Fits resident.",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="branch main @ d3ca53726 (bundle step-3.7-flash/rocmfp4-mtp-d3ca5372-"
        "20260807T164848-0400 — a copy of the ling rocmfp4-mtp bundle, the only installed "
        "lineage carrying step35)",
        backend_device="ROCm0",
        model_file="models/llm/Step-3.7-Flash-APEX/Step-3.7-Flash-APEX-I-Compact.gguf "
        "(+ mmproj-step3.7-flash-f16.gguf)",
        port=8110,
        systemd_unit="step-3.7-flash-compact.service",
        resident_gib=90,
        exclusive_with=_exclusive_with("Step-3.7-Flash-APEX-I-Compact"),
        startable=False,
        not_startable_reason="Weights and mmproj ALSO DELETED 2026-08-26 (models/llm/Step-3.7-Flash-APEX/, 88 GiB reclaimed); RUNTIME GONE: the "
        "step-3.7-flash rocmfp4-mtp bundle was removed in the 2026-08-11..14 "
        "consolidation. This is the only multimodal model on the box, so reviving "
        "the bundle is what restores vision.",
        consumers_note="unbound — added 2026-08-08 for benchmarking",
    ),
    ModelServerSpec(
        slug="Step-3.7-Flash-APEX-I-Quality",
        description="Step-3.7-Flash-APEX I-Quality (Q6_K, 114.5 GiB) — same step35 MoE and "
        "vision tower as I-Compact at higher fidelity. Does NOT fit resident: the unit runs "
        "--n-cpu-moe 12 + mmap to offload experts to CPU, at ctx 65536. MEASURED "
        "2026-08-08: 97 GiB GTT, 8.5 tok/s decode. --no-mmap must NOT be used here. Shares port 8110 with I-Compact.",
        runtime_repo="https://github.com/ciru-ai/ROCmFPX.git",
        runtime_ref="branch main @ d3ca53726 (bundle step-3.7-flash/rocmfp4-mtp-d3ca5372-"
        "20260807T164848-0400)",
        backend_device="ROCm0",
        model_file="models/llm/Step-3.7-Flash-APEX/Step-3.7-Flash-APEX-I-Quality.gguf "
        "(+ mmproj-step3.7-flash-f16.gguf)",
        port=8110,
        systemd_unit="step-3.7-flash-quality.service",
        # MEASURED 2026-08-08: 97 GiB GTT with --n-cpu-moe 12 + mmap at -c 65536
        # (105/124 GiB box total). Padded to 100. Throughput cost is steep:
        # 8.5 tok/s decode vs I-Compact's, so this is a quality-not-speed option.
        resident_gib=100,
        exclusive_with=_exclusive_with("Step-3.7-Flash-APEX-I-Quality"),
        startable=False,
        not_startable_reason="The whole models/llm/Step-3.7-Flash-APEX/ folder was deleted 2026-08-26; earlier, both the runtime bundle AND the I-Quality GGUF were "
        "removed in the 2026-08-11..14 consolidation (checked 2026-08-14); only "
        "I-Compact's weights survive.",
        consumers_note="unbound — added 2026-08-08; offload sizing unverified",
    ),
    ModelServerSpec(
        slug="Ridge-Qwen3.8-27B",
        description="Qwen3.8-27B on Ridge's RTX 3090 (NInfer 0.6.0), reached through "
        "corsair's ridge-llama-proxy (Wake-on-LAN, ~90s cold first byte). Off-box but "
        "fully operable by ARIA since 2026-08-15: wake, start, stop, sleep. Serves ONE "
        "request at a time. Wire id is `qwen3.8-27b` at 114688 ctx — NInfer VALIDATES "
        "the request model against its --model-id, so callers still sending the "
        "pre-2026-08-15 `qwen3.6-35b-a3b` get a 400, not a fallback.",
        runtime_repo="NInfer 0.6.0-rtx3090 (Don-Chad/ninfer-3090); D:\\ninfer\\bin-0.6.0",
        runtime_ref="remote",
        backend_device="remote CUDA",
        onbox=False,
        startable=True,
        memory_pool=POOL_REMOTE,
        devices=("Ridge RTX 3090 (remote CUDA)",),
        host_machine="machine:ridge",
        consumers_note="pi-coding-ridge",
        # ── remote operate (2026-08-15) ───────────────────────────────────
        # Previously startable=False: the only way Ridge's model came up was a
        # user request happening to hit ridge-llama-proxy, which WoLs on demand.
        # That is fine for the game path and useless for an agent that wants to
        # PREPARE the box. NInfer runs as scheduled task `NInferServer`
        # (D:\ninfer) — the same task ridge_proxy.py already bounces for
        # post-sleep CUDA wedge recovery, so this reuses a proven control point.
        # Cold load is ~90s and it serves ONE request at a time.
        wake_command=("/usr/local/bin/wake-ridge",),
        remote_start_command=(
            "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "ridge",
            "schtasks /run /tn NInferServer",
        ),
        remote_stop_command=(
            "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "ridge",
            "schtasks /end /tn NInferServer",
        ),
        remote_health_url="http://100.113.99.30:8080/health",
        remote_wake_deadline=240.0,
        remote_ready_deadline=240.0,
        # Ben keeps Ridge suspended when idle. `ssh ridge` is the established
        # path (Windows 11, PowerShell default shell, key already authorized);
        # SetSuspendState is the standard command-line suspend. Waking is NOT
        # ARIA's job — the proxy WoLs it on demand.
        # NOT rundll32 SetSuspendState — that silently no-ops over ssh (no
        # SeShutdownPrivilege in session 0) and returns exit 0 while the box
        # stays up. Measured 2026-08-15. See wake-proxies/windows/sleep-now.ps1.
        sleep_command=(
            "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "ridge",
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            "C:\\Windows\\Temp\\sleep-now.ps1",
        ),
        endpoint_override="http://100.123.245.84:8092/v1",
    ),
    ModelServerSpec(
        slug="Red-Qwen3.6-35B-A3B",
        description="Qwen3.6-35B-A3B on RED's RTX 5090, reached through corsair's "
        "red-proxy (:8094, Wake-on-LAN + fallback to corsair-local). The fastest "
        "node in the house (~218 tok/s). Off-box but fully operable by ARIA as of "
        "2026-08-15: wake, start, stop, sleep.",
        runtime_repo="RedLlmGateway (RED's own gateway — load/unload backends)",
        runtime_ref="remote",
        backend_device="remote CUDA (RTX 5090, 32 GB)",
        onbox=False,
        startable=True,
        memory_pool=POOL_REMOTE,
        devices=("RED RTX 5090 (remote CUDA)",),
        host_machine="machine:red",
        consumers_note="war-audio-game (via :8094), coding agents (T1)",
        # Control goes through gateway-ctl.ps1 on RED, NOT the RedLlmGateway
        # scheduled task. Three defects made the task unusable from here
        # (all diagnosed 2026-08-15, all verified):
        #   1. the task is `Logon Mode: Interactive only` / `At logon time`, so
        #      `schtasks /run` over ssh returns "SUCCESS: Attempted to run" and
        #      silently does nothing — the worst possible shape, a confident
        #      wrong answer. It is also why RED can sit awake-but-not-serving.
        #   2. its interpreter path `scoop\apps\python312\current\pythonw.exe`
        #      no longer resolves (the scoop junction is stale); the real one is
        #      `...\python312\3.12.10\pythonw.exe`.
        #   3. anything launched with Start-Process over ssh lands in the ssh
        #      session's job object and is killed when the connection closes,
        #      so the gateway died seconds after "starting". gateway-ctl.ps1
        #      uses Win32_Process.Create so it outlives the connection.
        # The script is idempotent (already-running / not-running) and reports
        # status, so ARIA's start/stop are safe to retry.
        # `RedLlmGatewayResumeUnload` is a resume hook, not a lifecycle control
        # — leave it alone; racing it reintroduces the post-resume CUDA wedge
        # that red_proxy already handles via /gateway/unload.
        wake_command=("/usr/local/bin/wake-red",),
        remote_start_command=(
            "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "red",
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            "C:\\Users\\benja\\Development\\infrastructure\\gateway\\gateway-ctl.ps1 "
            "-Action start",
        ),
        remote_stop_command=(
            "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "red",
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            "C:\\Users\\benja\\Development\\infrastructure\\gateway\\gateway-ctl.ps1 "
            "-Action stop",
        ),
        remote_health_url="http://100.120.162.100:8080/health",
        # RED_WAKE_TIMEOUT is 180 in ~/.config/red-llama/env; allow headroom.
        remote_wake_deadline=240.0,
        remote_ready_deadline=300.0,
        sleep_command=(
            "ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "red",
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            "C:\\Windows\\Temp\\sleep-now.ps1",
        ),
        # Consumers point at corsair's red-proxy, not RED directly: the proxy
        # owns wake-on-request and the corsair-local fallback.
        endpoint_override="http://100.123.245.84:8094/v1",
    ),
)

# This node's stable Tailscale IP — same constant every compose file binds to.
_TAILNET_IP = "100.123.245.84"

_BY_SLUG: dict[str, ModelServerSpec] = {spec.slug: spec for spec in REGISTRY}

# refuse start() if projected usage would exceed this fraction of the pool
_RAM_SAFETY_MARGIN = 0.92

# Per-pool override of that margin. 0.92 is a HALO number and was always a Halo
# number: that pool is 124 GiB of SHARED host memory, several models can be
# resident at once, llama.cpp allocates KV LAZILY (so a server grows into its
# neighbours long after it started), and the page cache competes for the same
# bytes. Eight percent of headroom is cheap insurance there, and it has been
# earned — DS4 has been OOM-killed on that pool, once by 18 MB.
#
# The R9700 is none of those things. It is a DEDICATED 32 GiB card that holds
# exactly one model at a time (see _R9700_RESIDENT), and everything on it is
# vLLM, which PREALLOCATES its KV pool during startup profiling. Once the server
# is up its VRAM usage is static — there is no lazy growth path for the margin
# to protect against, and a bad size fails cleanly at startup rather than
# strangling a neighbour at runtime. Holding back 8% of that card costs ~2.5 GiB
# of KV, which is ~66k tokens of context — the difference between reaching
# Qwen3.8-27B's native 262,144 ceiling and stopping short of it.
#
# Raised on 2026-08-17 (Ben's call) to reach that ceiling, and set to 0.98 rather
# than the 0.97 first tried: the MEASURED footprint of the 262144 geometry is
# 31.08 GiB of the 32 GiB pool = 97.1%, so a 0.97 gate refuses the exact
# configuration the raise was for. 0.98 is 31.36 GiB, which clears the measured
# 31.08 with ~0.28 GiB to spare. vLLM's own profiling is the real backstop here —
# it sizes the KV pool to fit and errors at startup if max_model_len will not.
#
# Note this ALSO un-breaks restarting the live :8080 server through ARIA at all:
# its footprint already exceeded 0.92 x 32 = 29.4 GiB, so every start was failing
# the gate and only ever succeeded because systemd brings it up at boot without
# consulting this check.
_POOL_SAFETY_MARGIN: dict[str, float] = {
    POOL_R9700: 0.98,
}


def _safety_margin(pool: str) -> float:
    return _POOL_SAFETY_MARGIN.get(pool, _RAM_SAFETY_MARGIN)

# Container states that hold their memory allocations. A paused container's
# process is frozen with all GTT allocations intact; a restarting one is
# crash-looping and repeatedly re-mapping memory — both conflict.
_MEMORY_HOLDING_STATES = ("running", "paused", "restarting")


_KFD_PROC = "/sys/class/kfd/kfd/proc"


async def _server_pid(spec: "ModelServerSpec") -> Optional[int]:
    """Host PID of a running server, however it is supervised.

    systemd units expose ExecMainPID; docker containers expose State.Pid (the
    host-namespace pid, which is what /proc and the DRM/KFD trees are keyed by).
    A safety wrapper can be the systemd main process while llama-server is its
    child; in that case return the first descendant that actually holds GPU
    memory, so live accounting does not silently measure the wrapper as zero.

    Descendants are checked for DRM allocations as well as KFD ones. KFD alone
    was not enough: it only covers HIP/ROCm, so every Vulkan-runtime server
    here fell through to the wrapper pid and measured as ~0 while holding
    ~98 GiB.
    """
    unit = unit_name(spec)
    if unit:
        rc, out, _ = await _run(
            "systemctl", "--user", "show", unit, "-p", "ExecMainPID", "--value"
        )
        if rc == 0 and out.strip().isdigit() and int(out.strip()) > 0:
            main_pid = int(out.strip())
            pending = [main_pid]
            seen: set[int] = set()
            while pending:
                pid = pending.pop(0)
                if pid in seen:
                    continue
                seen.add(pid)
                if os.path.isdir(os.path.join(_KFD_PROC, str(pid))):
                    return pid
                if process_uses_gpu(pid):
                    return pid
                try:
                    with open(f"/proc/{pid}/task/{pid}/children") as f:
                        pending.extend(int(value) for value in f.read().split())
                except (OSError, ValueError):
                    continue
            return main_pid
        return None
    if spec.container_name:
        rc, out, _ = await _run(
            "docker", "inspect", "-f", "{{.State.Pid}}", spec.container_name
        )
        if rc == 0 and out.strip().isdigit() and int(out.strip()) > 0:
            return int(out.strip())
    return None


def _gtt_bytes_for_pid(pid: int) -> Optional[int]:
    """GPU-mapped bytes held by one process, from the kfd sysfs tree.

    This is the ONLY signal that sees GPU-offloaded allocations on this
    unified-memory box — docker/cgroup accounting and RSS both miss them.
    A process with no kfd entry is not using the GPU at all.
    """
    d = os.path.join(_KFD_PROC, str(pid))
    if not os.path.isdir(d):
        return None
    total = 0
    try:
        for name in os.listdir(d):
            if name.startswith("vram_"):
                with open(os.path.join(d, name)) as f:
                    total += int(f.read().strip() or 0)
    except Exception:
        return None
    return total


def _rss_bytes_for_pid(pid: int) -> Optional[int]:
    """Resident host memory for one process. Used for CPU-only servers, whose
    allocations never appear in the GTT pool."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# Launch geometry — `-c` / `-np` are READ from the launch file, never declared
# --------------------------------------------------------------------------
# Served context used to live in five places and only one of them was
# authoritative: the unit's ExecStart. The other four (this registry's
# resident_gib, Hermes's `ds4` and `ds4-fast` provider context_length, and the
# coding-session concurrency cap) were hand-copied, so every past `-c` change
# silently invalidated them — see measure_resident_gib() below for the 7.6 GiB
# under-count that produced, feeding the very gate meant to prevent overcommit.
#
# So the launch file IS the source of truth and the registry reads it. No spec
# declares a context size; `resident_gib` is COMPUTED from what the unit
# actually serves. Change `-c` in one place and the footprint estimate, the API
# view and the start-time GTT gate all follow it.

_SYSTEMD_USER_DIR = os.path.expanduser("~/.config/systemd/user")

# `-cram`/`--cache-ram` must not be mistaken for `-c`: tokens are matched whole.
_CTX_FLAGS = frozenset(("-c", "--ctx-size", "--context-size"))
_SLOT_FLAGS = frozenset(("-np", "--parallel"))


@dataclass(frozen=True)
class LaunchGeometry:
    """What a server's launch file says it will serve. All fields optional —
    an unparseable launch file degrades to "unknown", never to a wrong number."""

    n_ctx: Optional[int] = None
    slots: Optional[int] = None
    source: Optional[str] = None

    @property
    def ctx_per_slot(self) -> Optional[int]:
        """Per-agent context — which is `-c` ITSELF, not `-c` divided by slots.

        llama.cpp reports `n_ctx_seq == -c` and allocates KV for
        `-c * -np` tokens; `-c` is per sequence. Verified 2026-08-09 by an OOM:
        `-c 1382400 -np 6` tried to allocate 57065472000 bytes of compressed KV,
        which is exactly `1382400 * 6 * 6880`. Getting this backwards sizes the
        server ~n_slots too large and it dies on startup."""
        return self.n_ctx

    @property
    def total_kv_tokens(self) -> Optional[int]:
        """Tokens of KV the server must actually hold: every slot, full."""
        if not self.n_ctx:
            return None
        return self.n_ctx * max(1, self.slots or 1)


def _as_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _argv_geometry(argv: list[str], source: str) -> LaunchGeometry:
    """Pull -c/-np out of a llama-server argv. First occurrence wins."""
    n_ctx: Optional[int] = None
    slots: Optional[int] = None
    for i, token in enumerate(argv):
        if "=" in token:
            flag, _, inline = token.partition("=")
            value = inline
        else:
            flag = token
            value = argv[i + 1] if i + 1 < len(argv) else None
        if flag in _CTX_FLAGS and n_ctx is None:
            n_ctx = _as_int(value)
        elif flag in _SLOT_FLAGS and slots is None:
            slots = _as_int(value)
    return LaunchGeometry(n_ctx=n_ctx, slots=slots, source=source)


def _systemd_geometry(unit: str) -> Optional[LaunchGeometry]:
    """Parse ExecStart= from a --user unit. Only ExecStart is read: ExecStartPre
    runs `sha256sum -c manifest/...`, whose `-c` would otherwise be read as a
    context size."""
    path = os.path.join(_SYSTEMD_USER_DIR, unit)
    try:
        with open(path) as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped.startswith("ExecStart="):
                    continue
                command = stripped.split("=", 1)[1].lstrip("-+!@")
                return _argv_geometry(shlex.split(command), unit)
    except (OSError, ValueError) as exc:
        logger.debug("model_servers: unit geometry unreadable for %s: %s", unit, exc)
    return None


def _compose_geometry(compose_file: str, service_name: Optional[str]) -> Optional[LaunchGeometry]:
    """Parse `command:` for one service out of a compose file. Handles both the
    list form and the folded-string form used across infrastructure/."""
    if not service_name:
        return None
    path = os.path.join(settings.infrastructure_root, compose_file)
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
        command = ((doc.get("services") or {}).get(service_name) or {}).get("command")
    except (OSError, yaml.YAMLError, AttributeError) as exc:
        logger.debug("model_servers: compose geometry unreadable for %s: %s", compose_file, exc)
        return None
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError:
            return None
    elif isinstance(command, list):
        argv = [str(part) for part in command]
    else:
        return None
    return _argv_geometry(argv, f"{compose_file}:{service_name}")


_GEOMETRY_CACHE: dict[str, tuple[float, LaunchGeometry]] = {}


def read_launch_geometry(spec: "ModelServerSpec") -> LaunchGeometry:
    """Served `-c`/`-np` for one server, read from its launch file.

    Cached against the launch file's mtime so an edit is picked up on the next
    call without a restart — the point of this whole mechanism is that changing
    the unit is sufficient.

    Two sources, in order. An explicit `-c`/`-np` in the ExecStart or compose
    command always wins. Where there is none — every serve.sh deployment, whose
    ExecStart is a shell script and whose context arrives through the
    environment — the value is taken from the effective launch parameters
    instead, so a script-launched server still reports what it will actually
    serve rather than "unknown"."""
    unit = unit_name(spec)
    geometry = LaunchGeometry()

    path = None
    if unit:
        path = os.path.join(_SYSTEMD_USER_DIR, unit)
    elif spec.compose_file:
        path = os.path.join(settings.infrastructure_root, spec.compose_file)

    if path is not None:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None
        if mtime is not None:
            cached = _GEOMETRY_CACHE.get(path)
            if cached is not None and cached[0] == mtime:
                geometry = cached[1]
            else:
                geometry = (
                    _systemd_geometry(unit)
                    if unit
                    else _compose_geometry(spec.compose_file or "", spec.service_name)
                ) or LaunchGeometry()
                _GEOMETRY_CACHE[path] = (mtime, geometry)

    if not spec.launch_script:
        return geometry

    # Script-launched deployment. Fill the gaps the ExecStart cannot answer,
    # cheapest-truth first: a literal in the script, then the effective launch
    # parameter. Not cached — the whole job of the parameter layer is to
    # reflect an override that may have just been written.
    n_ctx, slots, source = geometry.n_ctx, geometry.slots, geometry.source
    script = _script_geometry(spec)
    if n_ctx is None:
        n_ctx = script.n_ctx
    if slots is None:
        slots = script.slots
    if n_ctx is None and spec.ctx_param:
        n_ctx = _as_int(_effective_param_value(spec, spec.ctx_param))
        source = f"launch parameters ({unit or spec.launch_script})"
    if slots is None and spec.slots_param:
        slots = _as_int(_effective_param_value(spec, spec.slots_param))
    if source is None:
        source = spec.launch_script
    return LaunchGeometry(n_ctx=n_ctx, slots=slots, source=source)


def effective_resident_gib(
    spec: "ModelServerSpec", geometry: Optional[LaunchGeometry] = None
) -> Optional[float]:
    """Footprint estimate for a server that is NOT running, in GiB.

    Computed as weights + KV(served -c) + buffers whenever the spec carries the
    two -c-invariant constants, so it tracks the unit automatically. Falls back
    to the hand-declared `resident_gib` for entries not yet migrated, and for
    any server whose launch file could not be parsed.

    KV is sized on `-c * -np`, NOT on `-c` alone: `-c` is per sequence and every
    slot gets its own full-size cache. Using `-c` alone here under-counts by the
    slot count, which is precisely the class of under-count this function was
    written to eliminate."""
    geo = geometry if geometry is not None else read_launch_geometry(spec)
    tokens = geo.total_kv_tokens
    if spec.weights_gib is None or spec.kv_kib_per_token is None or not tokens:
        return spec.resident_gib
    kv_gib = tokens * spec.kv_kib_per_token / (1024 * 1024)
    return round(spec.weights_gib + kv_gib + spec.overhead_gib, 1)


# --------------------------------------------------------------------------
# Launch configuration — choosing HOW a model loads, not just WHICH one
# --------------------------------------------------------------------------
# Every deployment folder under infrastructure/ is already parameterised the
# same way: `VAR="${VAR:-default}"` in its serve.sh, overridden in practice by
# a hand-written systemd drop-in (ds4-halo-xxs.service.d/context.conf sets CTX,
# no-draft.conf sets DRAFT). ARIA uses that SAME mechanism rather than building
# its own command line, for three reasons:
#
#   1. The guards survive. ExecStartPre (R9700 awake, TTM pool capped), the
#      OOMScoreAdjust=900 backstop, and the launcher's MemAvailable floors all
#      still run. A hand-rolled command line would silently drop every one of
#      them — and those guards exist because this box has already OOM-killed
#      llama-server repeatedly.
#   2. It is inspectable and reversible from outside ARIA. The override is a
#      file Ben can read, edit, or delete, in the directory he already uses.
#   3. Hand-written drop-ins keep working. ARIA's file sorts last (`zz-`), so
#      it wins where they overlap and leaves everything else alone.
#
# The override file is rewritten on every parameterised start and REMOVED on a
# start with no overrides — a previous session's context size must not silently
# persist into a later "just start it" call.

_ARIA_DROPIN_NAME = "zz-aria-overrides.conf"
_ARIA_UNIT_PREFIX = "aria-model-"
_ARIA_UNIT_MARKER = "# Generated by ARIA (aria.infrastructure.model_servers)."

# `VAR="${VAR:-default}"` / `VAR=${VAR:-default}` as the serve.sh scripts write
# it. Deliberately NOT anchored to the start of a line: every script here packs
# several of these onto one — `PORT="${PORT:-8107}"; HOST=...; CTX="${CTX:-65536}"`
# — and a line-anchored pattern silently read only the first of each group.
_SCRIPT_DEFAULT_RE = re.compile(
    r'(?:^|[;&|(]\s*|\s)(?P<name>[A-Z_][A-Z0-9_]*)="?\$\{(?P=name):-(?P<value>[^}"]*)\}"?',
    re.MULTILINE,
)


def unit_name(spec: "ModelServerSpec") -> Optional[str]:
    """The systemd --user unit this spec is (or will be) served by.

    A deployment with its own hand-written unit keeps it. One that has only a
    serve.sh gets an ARIA-generated unit, so that everything downstream —
    start, stop, state, overrides, geometry — has a single mechanism.
    """
    if spec.systemd_unit:
        return spec.systemd_unit
    if spec.launch_script:
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", spec.slug)
        return f"{_ARIA_UNIT_PREFIX}{safe}.service"
    return None


def _abs_infra(path: str) -> str:
    """Resolve an infrastructure-relative path; absolute paths pass through."""
    return path if os.path.isabs(path) else os.path.join(settings.infrastructure_root, path)


def _dropin_path(unit: str) -> str:
    return os.path.join(_SYSTEMD_USER_DIR, f"{unit}.d", _ARIA_DROPIN_NAME)


def _unit_environment(unit: str) -> dict[str, str]:
    """Every `Environment=` in a unit and its drop-ins, later definitions winning.

    Mirrors systemd's own resolution order: the main unit first, then
    `<unit>.d/*.conf` sorted lexically — which is exactly why ARIA's file is
    named `zz-`.
    """
    env: dict[str, str] = {}
    files = [os.path.join(_SYSTEMD_USER_DIR, unit)]
    dropin_dir = os.path.join(_SYSTEMD_USER_DIR, f"{unit}.d")
    try:
        files += [
            os.path.join(dropin_dir, name)
            for name in sorted(os.listdir(dropin_dir))
            if name.endswith(".conf")
        ]
    except OSError:
        pass
    for path in files:
        try:
            with open(path) as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped.startswith("Environment="):
                        continue
                    assignment = stripped.split("=", 1)[1].strip().strip('"')
                    key, sep, value = assignment.partition("=")
                    if sep:
                        env[key.strip()] = value.strip().strip('"')
        except OSError:
            continue
    return env


def _script_defaults(spec: "ModelServerSpec") -> dict[str, str]:
    """The `${VAR:-default}` values a deployment's serve.sh falls back to.

    Read rather than duplicated into the spec: the script is the thing that
    actually runs, and a copied default is a copy that goes stale.

    The two path variables every one of these scripts uses — `$INFRA` for the
    infrastructure root and `$D` for the script's own directory — are expanded,
    so a default reads as a real path rather than as shell source.
    """
    if not spec.launch_script:
        return {}
    script_path = _abs_infra(spec.launch_script)
    try:
        with open(script_path) as fh:
            body = fh.read()
    except OSError:
        return {}
    expansions = {
        "$INFRA": os.path.abspath(settings.infrastructure_root),
        "${INFRA}": os.path.abspath(settings.infrastructure_root),
        "$D": os.path.dirname(script_path),
        "${D}": os.path.dirname(script_path),
        "$PWD": os.path.dirname(script_path),
    }
    out: dict[str, str] = {}
    for match in _SCRIPT_DEFAULT_RE.finditer(body):
        # First assignment wins, matching the shell: a later `VAR="${VAR:-x}"`
        # cannot change what an earlier one already set.
        if match.group("name") in out:
            continue
        value = match.group("value")
        for token, replacement in expansions.items():
            value = value.replace(token, replacement)
        out[match.group("name")] = value
    return out


def _script_geometry(spec: "ModelServerSpec") -> LaunchGeometry:
    """`-c`/`-np` written as LITERALS in a deployment's serve.sh.

    Several scripts hardcode what the unit cannot express — ds4-halo-xxs and
    ds4-hybrid both pin `-np 1` because speculation and multi-slot serving do
    not mix (upstream #26741). Only numeric literals are taken: a `$CTX` here
    is answered by the parameter layer instead, and guessing at a shell
    expansion would be worse than reporting nothing.
    """
    if not spec.launch_script:
        return LaunchGeometry()
    try:
        with open(_abs_infra(spec.launch_script)) as fh:
            body = fh.read()
    except OSError:
        return LaunchGeometry()
    # Line continuations first, so the multi-line `exec ... \` invocation reads
    # as the single argv it becomes.
    try:
        argv = shlex.split(body.replace("\\\n", " "), comments=True)
    except ValueError:
        return LaunchGeometry()
    geometry = _argv_geometry(argv, spec.launch_script)
    return LaunchGeometry(
        n_ctx=geometry.n_ctx if (geometry.n_ctx or 0) > 0 else None,
        slots=geometry.slots if (geometry.slots or 0) > 0 else None,
        source=spec.launch_script,
    )


def read_aria_overrides(spec: "ModelServerSpec") -> dict[str, str]:
    """Just the overrides ARIA itself set, keyed by parameter name."""
    unit = unit_name(spec)
    if not unit or not spec.parameters:
        return {}
    by_env = {p.env: p.name for p in spec.parameters}
    out: dict[str, str] = {}
    try:
        with open(_dropin_path(unit)) as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped.startswith("Environment="):
                    continue
                assignment = stripped.split("=", 1)[1].strip().strip('"')
                key, sep, value = assignment.partition("=")
                if sep and key.strip() in by_env:
                    out[by_env[key.strip()]] = value.strip().strip('"')
    except OSError:
        return {}
    return out


def resolve_parameters(spec: "ModelServerSpec") -> list[dict]:
    """Every declared knob with its effective value AND where that came from.

    The `source` field is the point of this function: "65536" means something
    different when it is ARIA's own override, a drop-in Ben wrote by hand, or
    the script's built-in fallback — and only the first is ARIA's to clear.
    """
    unit = unit_name(spec)
    aria = read_aria_overrides(spec)
    unit_env = _unit_environment(unit) if unit else {}
    script = _script_defaults(spec)

    out = []
    for param in spec.parameters:
        if param.name in aria:
            value, source = aria[param.name], "aria_override"
        elif param.env in unit_env:
            value, source = unit_env[param.env], "unit_dropin"
        elif param.env in script:
            value, source = script[param.env], "script_default"
        elif param.default is not None:
            value, source = param.default, "declared_default"
        else:
            value, source = None, "unset"
        out.append({
            "name": param.name,
            "env": param.env,
            "label": param.label,
            "kind": param.kind,
            "description": param.description,
            "declared_default": param.default,
            "choices": [{"value": v, "description": d} for v, d in param.choices],
            "value": value,
            "source": source,
        })
    return out


def _effective_param_value(spec: "ModelServerSpec", name: str) -> Optional[str]:
    for entry in resolve_parameters(spec):
        if entry["name"] == name:
            return entry["value"]
    return None


def validate_overrides(
    spec: "ModelServerSpec", overrides: Optional[dict]
) -> dict[str, str]:
    """Normalise a caller's overrides, or raise. Returns {ENV_VAR: value}."""
    if not overrides:
        return {}
    if not spec.parameters:
        raise ModelServerSafetyError(
            f"{spec.slug} has no selectable launch parameters — its configuration "
            f"is frozen in "
            f"{'its compose file' if spec.compose_file else 'its systemd unit'}. "
            f"Start it without overrides, or edit that file."
        )
    by_name = {p.name: p for p in spec.parameters}
    unknown = [k for k in overrides if k not in by_name]
    if unknown:
        raise ModelServerSafetyError(
            f"{spec.slug}: unknown parameter(s) {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(by_name))}."
        )
    return {
        by_name[name].env: by_name[name].validate(value)
        for name, value in overrides.items()
        if value is not None
    }


def _render_dropin(spec: "ModelServerSpec", env: dict[str, str]) -> str:
    lines = [
        _ARIA_UNIT_MARKER,
        "#",
        "# Launch overrides chosen through ARIA. Sorted last on purpose, so it",
        "# wins over hand-written drop-ins in this directory; everything those",
        "# set and this does not is left untouched.",
        "#",
        "# Safe to delete by hand — the next ARIA start without overrides removes",
        "# it anyway, returning the unit to its own defaults.",
        "[Service]",
    ]
    lines += [f"Environment={key}={value}" for key, value in sorted(env.items())]
    return "\n".join(lines) + "\n"


def _render_unit(spec: "ModelServerSpec", unit: str) -> str:
    """A unit for a deployment that ships only a serve.sh.

    The guard environment and ExecStartPre checks come from the spec rather
    than being invented here, so what protects an ARIA-launched server is
    reviewable next to the model it protects.
    """
    script = _abs_infra(spec.launch_script or "")
    workdir = os.path.dirname(script)
    lines = [
        _ARIA_UNIT_MARKER,
        f"# Deployment: {spec.deployment or spec.slug}",
        "# Regenerated whenever the registry entry changes; hand edits are lost.",
        "# To customise durably, add your own drop-in under "
        f"{unit}.d/ (ARIA's file is {_ARIA_DROPIN_NAME} and sorts last).",
        "",
        "[Unit]",
        f"Description={spec.slug} (ARIA-managed model server)",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={workdir}",
    ]
    lines += [f"Environment={key}={value}" for key, value in spec.unit_environment]
    lines += [f"ExecStartPre={check}" for check in spec.unit_exec_start_pre]
    lines.append(f"ExecStart={script}")
    # No automatic restart: a wedged GPU is not fixed by restarting into the
    # same wedge, which is the standing policy for every model server here.
    lines += [
        "Restart=no",
        "KillSignal=SIGTERM",
        "SendSIGKILL=no",
        "TimeoutStopSec=180",
    ]
    if spec.unit_oom_score_adjust is not None:
        # Make the model the kernel's preferred OOM victim rather than mongod
        # or the Hermes gateway — the same reasoning as the hand-written
        # ds4-halo-xxs oom.conf.
        lines.append(f"OOMScoreAdjust={spec.unit_oom_score_adjust}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def _write_if_changed(path: str, content: str) -> bool:
    """Write only on a real change, so daemon-reload is not run for nothing."""
    try:
        with open(path) as fh:
            if fh.read() == content:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.aria-tmp"
    with open(tmp, "w") as fh:
        fh.write(content)
    os.replace(tmp, path)
    return True


def _remove_if_present(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("model_servers: could not remove %s: %s", path, exc)
        return False


# --------------------------------------------------------------------------
# Live runtime stats — slot occupancy and throughput, read from the server
# --------------------------------------------------------------------------
# read_launch_geometry() above answers "how many slots SHOULD exist"; this
# answers "how many are busy right now". The two are deliberately separate:
# the static budget check (check_pi_slot_budget) can only catch a
# misconfiguration, whereas over-subscription in practice shows up here as
# `requests_deferred > 0` — requests queued because every slot was taken, which
# is the runtime signature of agents evicting each other.
#
# `/slots` is enabled by default and always available. `/metrics` requires the
# server to have been started with `--metrics` and 501s otherwise, so every
# field sourced from it is Optional and its absence is reported, never faked.

def base_url_for_spec(spec: "ModelServerSpec") -> Optional[str]:
    """Where to reach this server, spec-side (llm_route.base_url_for is the
    dict-side twin, for status rows).

    `endpoint_override` wins and is load-bearing: DS4 binds
    100.123.245.84:8107 ONLY, so a port-derived localhost URL is
    connection-refused even with the server up."""
    if spec.endpoint_override:
        return spec.endpoint_override.rstrip("/")
    if spec.port:
        return f"http://localhost:{spec.port}/v1"
    return None


# vLLM's Prometheus names. Mapped onto the SAME RuntimeStats fields as
# llama.cpp's where they mean the same thing, so a consumer does not need to
# know which engine served it:
#   num_requests_running -> requests_processing
#   num_requests_waiting -> requests_deferred  (drives `saturated` identically)
# The two vllm-only gauges land in their own fields.
# ⚠️ NAMES VERIFIED against the live server 2026-08-17, not taken from docs —
# two educated guesses were wrong: it is `kv_cache_usage_perc` (NOT
# `gpu_cache_usage_perc`), and there is NO hit-rate gauge at all; vLLM exposes a
# hits/queries COUNTER PAIR that has to be divided (see _vllm_derived below).
_VLLM_METRIC_FIELDS = {
    "vllm:num_requests_running": "requests_processing",
    "vllm:num_requests_waiting": "requests_deferred",
    "vllm:prompt_tokens_total": "prompt_tokens_total",
    "vllm:generation_tokens_total": "tokens_predicted_total",
    # Fraction 0..1 of the PREALLOCATED KV pool in use. Meaningful here in a way
    # it is not on llama.cpp, which allocates lazily.
    "vllm:kv_cache_usage_perc": "kv_cache_usage_pct",
}

# Counters we divide rather than map straight through.
_VLLM_RATIO_FIELDS = {
    "vllm:prefix_cache_hits_total": "_prefix_hits",
    "vllm:prefix_cache_queries_total": "_prefix_queries",
    # Cumulative prompt tokens served from cache — the absolute saving, next to
    # the ratio. `prompt_tokens_cached_total` is the vLLM-side twin of the
    # `prompt_tokens_details.cached_tokens` DwarfStar reports per response.
    "vllm:prompt_tokens_cached_total": "prompt_tokens_cached_total",
}


def _parse_prometheus_labels(text: str, metric: str) -> dict:
    """Labels of a single Prometheus series — where vLLM hides its config.

    `vllm:cache_config_info` reports a constant 1.0 and carries every fact worth
    knowing in its LABELS: kv_cache_size_tokens, block_size, enable_prefix_caching.
    A value-only parser sees "1.0" and learns nothing.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(metric + "{"):
            continue
        inner = line[len(metric) + 1: line.rfind("}")]
        # ⚠️ Label KEYS are unquoted, so the separator is `",` and NOT `","`.
        # Splitting on `","` matches nothing and silently yields one giant
        # "key" — which read as "prefix caching disabled" rather than as a
        # parse failure. Regex over key="value" pairs is unambiguous.
        return {m.group(1): m.group(2) for m in re.finditer(r'([A-Za-z_][\w]*)="([^"]*)"', inner)}
    return {}


def _vllm_derived(raw: dict) -> dict:
    """Turn vLLM's counter pairs into the ratios the API reports.

    ⚠️ These are CUMULATIVE counters since process start, so the ratio is a
    lifetime average, not a current rate. A freshly restarted server reports
    0 queries — which yields None (unknown), never 0.0 (a perfect miss rate).
    """
    out = {k: v for k, v in raw.items() if not k.startswith("_")}
    hits, queries = raw.get("_prefix_hits"), raw.get("_prefix_queries")
    if queries:  # non-zero and not None
        out["prefix_cache_hit_rate"] = round(hits / queries, 4) if hits is not None else None
    return out

_METRIC_FIELDS = {
    "llamacpp:requests_processing": "requests_processing",
    "llamacpp:requests_deferred": "requests_deferred",
    "llamacpp:prompt_tokens_total": "prompt_tokens_total",
    "llamacpp:tokens_predicted_total": "tokens_predicted_total",
    "llamacpp:prompt_tokens_seconds": "prompt_tokens_per_second",
    "llamacpp:predicted_tokens_seconds": "predicted_tokens_per_second",
    "llamacpp:n_busy_slots_per_decode": "avg_busy_slots_per_decode",
    "llamacpp:n_decode_total": "decode_calls_total",
}


@dataclass(frozen=True)
class RuntimeStats:
    """What a running llama.cpp server reports about itself right now."""

    total_slots: Optional[int] = None
    busy_slots: Optional[int] = None
    ctx_per_slot: Optional[int] = None
    metrics_available: bool = False
    metrics_hint: Optional[str] = None
    requests_processing: Optional[float] = None
    requests_deferred: Optional[float] = None
    prompt_tokens_total: Optional[float] = None
    tokens_predicted_total: Optional[float] = None
    prompt_tokens_per_second: Optional[float] = None
    predicted_tokens_per_second: Optional[float] = None
    avg_busy_slots_per_decode: Optional[float] = None
    decode_calls_total: Optional[float] = None
    # --- cross-family additions (2026-08-17) -------------------------------
    runtime_family: str = "llamacpp"
    # Why a server cannot report something, when it cannot. Distinguishes
    # "this backend has no such endpoint" from "the probe failed".
    telemetry_hint: Optional[str] = None
    # vLLM-only, and genuinely useful: it PREALLOCATES its KV pool, so usage is
    # a real occupancy fraction rather than a guess. llama.cpp allocates lazily
    # and has no equivalent.
    kv_cache_usage_pct: Optional[float] = None
    # vLLM-only. The number that says whether prompt caching is actually paying
    # off; llama.cpp exposes no equivalent and DwarfStar reports its own per
    # response (prompt_tokens_details.cached_tokens) rather than as a gauge.
    prefix_cache_hit_rate: Optional[float] = None
    # vLLM-only: cumulative prompt tokens served FROM cache. The absolute saving
    # alongside the ratio — and the direct analogue of the per-response
    # prompt_tokens_details.cached_tokens that DwarfStar reports.
    prompt_tokens_cached_total: Optional[float] = None
    # --- prompt-cache capacity (2026-08-17) --------------------------------
    # "Where does the prompt cache live and how big can it get" had no answer in
    # this API: only current usage was exposed, and only for one backend. Each
    # engine stores it somewhere different — VRAM pool / on-disk / plain RAM —
    # so the kind is reported alongside the number rather than assumed.
    prompt_cache_kind: Optional[str] = None      # "vram-pool" | "disk" | "ram"
    prompt_cache_capacity: Optional[str] = None  # human-readable ceiling
    prompt_cache_used: Optional[str] = None      # human-readable current
    # ⚠️ vLLM only, and load-bearing: prefix caching is BLOCK-granular, so a
    # prompt sharing fewer than this many leading tokens gets ZERO reuse. On
    # Qwen3.8 it is 1664 (raised so the attention page >= the Mamba page),
    # which is why short auxiliary prompts see a 0% hit rate.
    cache_block_size: Optional[int] = None
    # Mean time-to-first-token — the closest thing to a live prefill latency.
    mean_ttft_seconds: Optional[float] = None
    # Served context, when the backend will tell us (DwarfStar reports it on
    # /v1/models). Lets the declared-vs-observed drift check work for backends
    # with no /slots to read n_ctx from.
    served_ctx: Optional[int] = None

    @property
    def free_slots(self) -> Optional[int]:
        if self.total_slots is None or self.busy_slots is None:
            return None
        return max(0, self.total_slots - self.busy_slots)

    @property
    def slot_utilisation(self) -> Optional[float]:
        """Busy fraction, 0.0–1.0. The headline number for "how loaded is it"."""
        if not self.total_slots or self.busy_slots is None:
            return None
        return round(self.busy_slots / self.total_slots, 3)

    @property
    def saturated(self) -> Optional[bool]:
        """True when work is QUEUING — every slot busy and requests waiting.

        This is the condition that silently degrades prefix warmth: a queued
        request eventually lands in whichever slot frees first, which is not
        necessarily the one holding its prefix."""
        if self.requests_deferred is None:
            return None
        return self.requests_deferred > 0


def _parse_prometheus(text: str, fields: Optional[dict] = None) -> dict:
    """Minimal Prometheus text-format reader for the handful of gauges we want.

    `fields` selects the name->attribute map, so llama.cpp and vLLM can share
    one parser. Ignores HELP/TYPE lines and any metric not in the map. Labelled
    series (`name{label="x"} v`) are matched on the bare name."""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        # vLLM labels its series (e.g. vllm:num_requests_running{model_name=...}),
        # so match on the portion before the label block.
        bare = name.split("{", 1)[0]
        field = (fields if fields is not None else _METRIC_FIELDS).get(bare)
        if field is None:
            continue
        try:
            out[field] = float(value)
        except ValueError:
            continue
    return out


async def probe_runtime(spec: "ModelServerSpec", timeout: float = 4.0) -> Optional[RuntimeStats]:
    """Live occupancy + throughput for a running server, or None if unreachable.

    Dispatches on `spec.runtime_family`. Until 2026-08-17 this spoke ONLY
    llama.cpp's `/slots` + `/metrics`, so the two newest deployments — Qwen3.8
    on vllm-radiance and DS4 on DwarfStar — reported `null` for every field. In
    an API where `null` means UNKNOWN rather than "fine", that made two of the
    three live models invisible to the endpoint whose whole job is showing load.

    A stopped server is not an error here; it simply has no runtime to report.
    """
    base = base_url_for_spec(spec)
    if not base:
        return None
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    family = (spec.runtime_family or "llamacpp").lower()

    if family == "vllm":
        return await _probe_vllm(spec, root, timeout)
    if family == "dwarfstar":
        return await _probe_dwarfstar(spec, base, timeout)
    return await _probe_llamacpp(spec, root, timeout)


async def _probe_llamacpp(spec, root: str, timeout: float) -> Optional[RuntimeStats]:
    """`/slots` (always on) + `/metrics` (needs --metrics, 501s otherwise)."""
    slots_data = None
    metrics: dict = {}
    metrics_available = False
    metrics_hint = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            slots_resp, metrics_resp = await asyncio.gather(
                client.get(f"{root}/slots"),
                client.get(f"{root}/metrics"),
                return_exceptions=True,
            )
            if isinstance(slots_resp, httpx.Response) and slots_resp.status_code == 200:
                try:
                    slots_data = slots_resp.json()
                except ValueError:
                    slots_data = None
            if isinstance(metrics_resp, httpx.Response):
                if metrics_resp.status_code == 200:
                    metrics = _parse_prometheus(metrics_resp.text, _METRIC_FIELDS)
                    metrics_available = True
                elif metrics_resp.status_code == 501:
                    metrics_hint = (
                        "server started without --metrics; add it to the "
                        "launch file and restart to get throughput, queue "
                        "depth and busy-slot averages"
                    )
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("model_servers: llamacpp probe failed for %s: %s", spec.slug, exc)
        return None

    if slots_data is None and not metrics_available:
        return None

    total_slots = busy = ctx_per_slot = None
    if isinstance(slots_data, list):
        total_slots = len(slots_data)
        busy = sum(1 for s in slots_data if isinstance(s, dict) and s.get("is_processing"))
        ctxs = [s.get("n_ctx") for s in slots_data if isinstance(s, dict) and s.get("n_ctx")]
        ctx_per_slot = ctxs[0] if ctxs else None

    # llama.cpp keeps its prompt cache in HOST RAM, bounded by --cache-ram (MB).
    # Unlike the other two it is neither persistent nor introspectable: the server
    # exposes no "how full is it" number, so capacity is reported and usage is
    # honestly left unknown rather than guessed at.
    cache_cap = None
    cache_ram = _effective_param_value(spec, "cache_ram")
    if cache_ram:
        cache_cap = f"{cache_ram} MB in host RAM (--cache-ram)"

    return RuntimeStats(
        runtime_family="llamacpp",
        prompt_cache_kind="ram" if cache_cap else None,
        prompt_cache_capacity=cache_cap,
        prompt_cache_used=None,  # llama.cpp exposes no occupancy for this
        total_slots=total_slots,
        busy_slots=busy,
        ctx_per_slot=ctx_per_slot,
        served_ctx=ctx_per_slot,
        metrics_available=metrics_available,
        metrics_hint=metrics_hint,
        **metrics,
    )


async def _probe_vllm(spec, root: str, timeout: float) -> Optional[RuntimeStats]:
    """vLLM exposes Prometheus `/metrics` and NO `/slots`.

    Slot counts therefore come from the launch geometry rather than the server:
    vLLM's concurrency is `--max-num-seqs`, which is a launch parameter, and
    there is no per-slot introspection endpoint to read occupancy from. Busy
    slots are taken from `num_requests_running`, which is the same quantity
    llama.cpp reports as `requests_processing`.

    ⚠️ vLLM PREALLOCATES its KV pool, so `kv_cache_usage_pct` is a true
    occupancy fraction — unlike llama.cpp, where KV grows lazily and no
    equivalent number exists.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.get(f"{root}/metrics")
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("model_servers: vllm probe failed for %s: %s", spec.slug, exc)
        return None
    if resp.status_code != 200:
        return RuntimeStats(
            runtime_family="vllm",
            metrics_available=False,
            telemetry_hint=f"vLLM /metrics returned {resp.status_code}",
        )

    fields = {**_VLLM_METRIC_FIELDS, **_VLLM_RATIO_FIELDS}
    metrics = _vllm_derived(_parse_prometheus(resp.text, fields))
    geom = read_launch_geometry(spec)

    # Capacity comes from cache_config_info's LABELS, self-reported by the engine
    # rather than copied from a doc that can drift.
    cfg = _parse_prometheus_labels(resp.text, "vllm:cache_config_info")
    pool_tokens = cfg.get("kv_cache_size_tokens")
    block_size = cfg.get("block_size")
    apc_on = (cfg.get("enable_prefix_caching") or "").lower() == "true"
    used_pct = metrics.get("kv_cache_usage_pct")
    capacity = None
    if pool_tokens:
        capacity = f"{int(pool_tokens):,} tokens"
        conc = cfg.get("kv_cache_max_concurrency")
        if conc:
            try:
                capacity += f" ({float(conc):.2f}x concurrency at full context)"
            except ValueError:
                pass
    if not apc_on:
        capacity = (capacity or "") + " — PREFIX CACHING DISABLED"

    # Mean TTFT from the histogram's sum/count. A latency, not a rate — but it is
    # the only prefill-side number vLLM exposes without running a benchmark.
    ttft = None
    tt = _parse_prometheus(resp.text, {
        "vllm:time_to_first_token_seconds_sum": "_sum",
        "vllm:time_to_first_token_seconds_count": "_count",
    })
    if tt.get("_count"):
        ttft = round(tt["_sum"] / tt["_count"], 4)
    busy = metrics.get("requests_processing")
    return RuntimeStats(
        runtime_family="vllm",
        # From the launch file, not the server — see the docstring.
        total_slots=geom.slots,
        busy_slots=int(busy) if busy is not None else None,
        ctx_per_slot=geom.ctx_per_slot,
        served_ctx=geom.n_ctx,
        metrics_available=True,
        prompt_cache_kind="vram-pool",
        prompt_cache_capacity=capacity,
        prompt_cache_used=(f"{used_pct * 100:.1f}% of pool" if used_pct is not None else None),
        cache_block_size=int(block_size) if block_size and block_size.isdigit() else None,
        mean_ttft_seconds=ttft,
        telemetry_hint=(
            "slot count is the launch --max-num-seqs (vLLM has no /slots "
            "endpoint); kv_cache_usage_pct and prefix_cache_hit_rate are "
            "vLLM-only and come from its preallocated KV pool"
        ),
        **metrics,
    )


_DS_PROMPT_RE = re.compile(r"chat ctx=\d+\.\.\d+:(\d+) prompt start")
_DS_FINISH_RE = re.compile(r"gen=(\d+) .*finish=")
_DS_CACHE_RE = re.compile(r"live kv cache (hit|miss)[^\n]*?common=(\d+)")


async def _dwarfstar_usage(spec, timeout: float = 6.0) -> dict:
    """Cumulative tokens in / out / cache-matched for DwarfStar, from its log.

    DwarfStar exposes NO metrics endpoint (/metrics, /slots, /health all 404), so
    unlike the other two backends there is nothing to poll. It does, however, log
    every request with the numbers we want:

        chat ctx=0..13:13 prompt start          -> 13 tokens ingested
        chat ctx=0..13:13 gen=150 ... finish=    -> 150 tokens generated
        live kv cache miss ... common=13         -> 13 tokens of matched prefix

    Scoped to the CURRENT unit invocation, which deliberately matches the other
    backends: vLLM's and llama.cpp's counters also reset when the process does,
    so "since this server started" means the same thing everywhere and the three
    are comparable without a footnote.

    ⚠️ Only the `finish=` line is counted for generation. `gen=` also appears on
    the per-chunk progress lines, and summing those would multiply the total by
    the number of decode chunks.
    """
    unit = f"aria-model-{spec.slug}.service"
    try:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "--user", "-u", unit, "--no-pager", "-n", "50000",
            "--since", "@" + str(int(await _unit_start_epoch(unit))),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError, ValueError, TypeError):
        return {}
    text = out.decode(errors="replace")
    ingested = sum(int(m) for m in _DS_PROMPT_RE.findall(text))
    generated = sum(int(m) for m in _DS_FINISH_RE.findall(text))
    hits = misses = matched = 0
    for kind, common in _DS_CACHE_RE.findall(text):
        matched += int(common)
        if kind == "hit":
            hits += 1
        else:
            misses += 1
    total = hits + misses
    return {
        "prompt_tokens_total": float(ingested) or None,
        "tokens_predicted_total": float(generated) or None,
        # Tokens of prompt that matched something already resident. Reported as
        # the token count rather than a request-level hit/miss ratio, so it lines
        # up with vLLM's prompt_tokens_cached_total.
        "prompt_tokens_cached_total": float(matched) or None,
        "prefix_cache_hit_rate": round(hits / total, 4) if total else None,
    }


async def _unit_start_epoch(unit: str) -> float:
    """Unix time this unit last entered active, for scoping the log read."""
    proc = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "show", unit, "-p",
        "ActiveEnterTimestampMonotonic", "--value",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    mono_us = int((out.decode().strip() or "0"))
    boot = time.time() - float(open("/proc/uptime").read().split()[0])
    return boot + mono_us / 1_000_000


async def _probe_dwarfstar(spec, base: str, timeout: float) -> Optional[RuntimeStats]:
    """DwarfStar (antirez/ds4) exposes `/v1/models` and NOTHING else.

    Verified 2026-08-17 against the running server: `/metrics`, `/slots`,
    `/health`, `/stats` and `/status` all return 404. So there is no live
    occupancy or throughput to report, and this returns a row that says exactly
    that rather than a bag of nulls a reader could mistake for "idle and fine".

    What IS available: reachability, and the served context from /v1/models —
    which is what makes the declared-vs-observed drift check work for a backend
    with no /slots to read n_ctx from.

    Per-request cache telemetry (`prompt_tokens_details.cached_tokens` /
    `cache_write_tokens`) IS reported by DwarfStar, and is richer than anything
    llama.cpp gives — but it arrives per completion, not as a gauge, so it
    cannot be polled here. Capturing it would mean recording it at call sites.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.get(f"{base}/models")
    except (httpx.HTTPError, OSError) as exc:
        logger.debug("model_servers: dwarfstar probe failed for %s: %s", spec.slug, exc)
        return None
    if resp.status_code != 200:
        return None

    served_ctx = None
    try:
        for m in (resp.json().get("data") or []):
            if isinstance(m, dict) and m.get("context_length"):
                served_ctx = int(m["context_length"])
                break
    except (ValueError, TypeError):
        pass

    geom = read_launch_geometry(spec)

    # DwarfStar's prompt cache is ON DISK, not in GPU memory — the only backend
    # here where it survives a restart, and the only one whose ceiling is a byte
    # budget rather than a token pool. Both numbers come from the deployment
    # itself (the effective launch parameters), never a hardcoded path.
    cache_kind = cache_cap = cache_used = None
    kv_dir = _effective_param_value(spec, "kv_disk_dir") or "/home/ben/.ds4/server-kv"
    kv_mb = _effective_param_value(spec, "kv_disk_mb")
    if kv_mb:
        cache_kind = "disk"
        try:
            cache_cap = f"{int(kv_mb):,} MB on disk ({kv_dir})"
        except ValueError:
            cache_cap = f"{kv_mb} MB on disk ({kv_dir})"
        try:
            total = sum(
                f.stat().st_size for f in pathlib.Path(kv_dir).glob("*.kv") if f.is_file()
            )
            cache_used = f"{total / (1024 ** 3):.2f} GB"
        except OSError:
            cache_used = None

    usage = await _dwarfstar_usage(spec)
    return RuntimeStats(
        runtime_family="dwarfstar",
        **usage,
        prompt_cache_kind=cache_kind,
        prompt_cache_capacity=cache_cap,
        prompt_cache_used=cache_used,
        # Resident sessions are a launch parameter (--batched-session); the
        # server does not expose how many are occupied.
        total_slots=geom.slots,
        busy_slots=None,
        ctx_per_slot=served_ctx or geom.ctx_per_slot,
        served_ctx=served_ctx,
        metrics_available=False,
        telemetry_hint=(
            "DwarfStar exposes /v1/models ONLY — no /metrics, /slots or /health "
            "(all 404), so live OCCUPANCY and queue depth are UNAVAILABLE, not "
            "zero. Token counts and cache matching ARE available: they are parsed "
            "from the server's own per-request log lines, scoped to the current "
            "unit invocation so they reset with the process exactly like vLLM's "
            "and llama.cpp's counters do. ⚠️ Its disk cache has min=512, so "
            "prompts shorter than that are never persisted and will always show "
            "as uncached."
        ),
    )


def check_pi_slot_budget(
    slug: str = "DS4-0731-Q8Protected-Halo-DwarfStar",
) -> Optional[str]:
    """Complaint string if the coding-session cap over-subscribes the server's
    slots, else None.

    ⚠️ The default slug must name the server pi ACTUALLY runs on. Until
    2026-08-15 it named `DS4-0731-UD-IQ3-S-Dual-Vulkan-DSpark-4x128K`, retired
    with the rest of the :18211 deployment — `_BY_SLUG.get()` therefore returned
    None and this check silently returned "no complaint" for weeks while the cap
    (2 + 2 reserved) sat over a one-slot server. A budget check that cannot find
    its subject must not read as a pass; if you re-point pi's model, re-point
    this default in the same commit.

    The cap is policy (how many slots sub-agents may take) and the unit is
    mechanism (how many exist), so they are legitimately two numbers — but they
    must stay consistent. Over-subscribing does not fail loudly at runtime: it
    just makes agents evict each other's prefixes and pay a cold prefill per
    turn, which reads as "the model got slow" rather than as a misconfiguration.
    """
    spec = _BY_SLUG.get(slug)
    if spec is None:
        # Loud, not silent: an unknown slug means this check has no subject, and
        # "no complaint" would be a lie (see the docstring — that is exactly how
        # a 4:1 over-subscription hid for weeks).
        return (
            f"pi slot budget cannot be checked: no registry entry named {slug!r}. "
            "The slug was probably retired without re-pointing check_pi_slot_budget()."
        )
    slots = read_launch_geometry(spec).slots
    if not slots:
        return None
    reserved = int(settings.coding_pi_reserved_slots or 0)
    cap = int(settings.coding_max_concurrent_pi_sessions or 0)
    if cap + reserved > slots:
        return (
            f"coding_max_concurrent_pi_sessions={cap} + "
            f"coding_pi_reserved_slots={reserved} exceeds {slug}'s -np {slots}: "
            "pi sub-agents will evict warm prefixes and re-prefill every turn. "
            "Lower the cap or raise -np only after requalifying memory; -c is "
            "per slot, so total KV scales with -c times -np."
        )
    return None


async def measure_resident_gib(spec: "ModelServerSpec") -> Optional[float]:
    """ACTUALLY measured footprint of a running server, in GiB, or None.

    Exists because `spec.resident_gib` is a hand-maintained SWAG that silently
    goes stale: DS4 was declared 86.5 (measured at -c 131072) while really
    holding 94.08 after moving to -c 262144 with a 4 GiB prompt cache — a
    7.6 GiB under-count feeding the safety gate that is supposed to prevent
    overcommit. Prefer this over the declared value wherever it is available.

    GPU-resident servers are measured from amdgpu's per-device DRM accounting,
    falling back to the kfd tree; CPU-only ones from RSS. Returns None when the
    server is not running or the pid is unreadable, in which case callers fall
    back to the declared SWAG.

    The DRM read comes first because it is the only one that covers both
    runtimes on this box — KFD sees HIP/ROCm processes only — and because it
    reports per device, so a server is measured against the pool it actually
    draws from rather than against a box-wide total.
    """
    pid = await _server_pid(spec)
    if pid is None:
        return None
    if spec.memory_pool != POOL_HOST and spec.gtt_resident:
        by_pool = process_gpu_bytes(pid)
        held = by_pool.get(spec.memory_pool)
        if held:
            return held / 1024**3
        if by_pool:
            # It is on the GPU, just not the pool the registry claims — report
            # what it is really holding rather than zero, so the discrepancy is
            # visible instead of looking like an idle server.
            return max(by_pool.values()) / 1024**3
    raw = _gtt_bytes_for_pid(pid) if spec.gtt_resident else _rss_bytes_for_pid(pid)
    if raw is None and spec.gtt_resident:
        # Declared GPU-resident but holds no GPU memory — fall back to RSS
        # rather than reporting nothing, and let the caller see the mismatch.
        raw = _rss_bytes_for_pid(pid)
    measured = None if raw is None else raw / 1024**3

    # ── Containerised servers: fall back to the POOL reading ────────────────
    # Per-process DRM accounting cannot work for a container whose engine runs
    # as root: /proc/<pid>/fdinfo is unreadable as `ben`, so process_gpu_bytes
    # returns {} and the unit's MainPID is the `docker` CLIENT, which holds no
    # GPU fds at all. Measured 2026-08-17: vllm-radiance reported 0.02 GiB while
    # actually holding 29 GiB of R9700 VRAM — i.e. ARIA under-reported an entire
    # GPU, and `/model-servers/utilization` showed a dGPU model as using nothing.
    #
    # The pool read is reliable and the registry already guarantees the thing
    # that makes attribution sound: the big servers in a pool are MUTUALLY
    # EXCLUSIVE, so if exactly one is running, the pool's usage IS its
    # footprint. With more than one, attribution would be a guess — so this
    # deliberately refuses rather than dividing it up.
    if (measured is None or measured < 1.0) and spec.memory_pool != POOL_HOST:
        try:
            from aria.infrastructure.gpu_devices import read_pool
            live = read_pool(spec.memory_pool)
        except Exception:
            live = None
        if live is not None and live.used_gib and live.used_gib > (measured or 0.0):
            same_pool = [
                s for s in REGISTRY
                if s.memory_pool == spec.memory_pool and s.slug != spec.slug and s.startable
            ]
            others_running = 0
            for other in same_pool:
                if await _server_pid(other) is not None:
                    others_running += 1
            if others_running == 0:
                return live.used_gib
    return measured


def _read_gtt_gib(pool: str = POOL_HALO) -> Optional[tuple[float, float]]:
    """Best-effort live (used, total) for ONE memory pool, in GiB. None if unreadable.

    Was a hardcoded read of card0's GTT — the single-GPU assumption. Adding the
    OCuLink R9700 inverted DRM enumeration (card0 is now the dGPU, card1 the
    Strix Halo), so that read started reporting ~0 GiB used while the Halo held
    97 GiB, i.e. it would have approved starting a second ~100 GiB model on a
    full box. `gpu_devices` classifies the cards instead of trusting their
    order; see that module for the measurement.

    Keeping the (used, total) shape deliberately: this is still the one signal
    the gate consults, and `shells/selfcheck.py` alerts on the same numbers.
    """
    live = read_pool(pool)
    if live is None:
        logger.warning("model_servers: pool %s unreadable", pool)
        return None
    return live.used_gib, live.total_gib


async def _run(*args: str) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise ModelServerError(f"'{args[0]}' binary not found: {exc}")
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


# ── remote operate (2026-08-15) ──────────────────────────────────────────
# The ordering below deliberately mirrors wake-proxies/{red,ridge}/*_proxy.py,
# which is the production-proven sequence: probe -> wake -> poll -> act -> verify.
# The difference is that the proxies wake a box so a *single request* can be
# served, whereas these drive the model service's lifecycle explicitly.


async def _remote_health_ok(spec: "ModelServerSpec", timeout: float = 5.0) -> bool:
    """True if the remote model service answers its health probe.

    A connection error is a normal, expected answer here (box asleep, or awake
    with the service stopped) — not an exception worth propagating.
    """
    if not spec.remote_health_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(spec.remote_health_url)
            return resp.status_code < 500
    except Exception:
        return False


async def _remote_box_reachable(spec: "ModelServerSpec", timeout: float = 6.0) -> bool:
    """True if the machine is up, independent of whether the model is serving.

    Uses the ssh control path rather than the model port precisely because the
    two states differ: RED was observed awake-with-service-stopped, and a probe
    that conflates them would wake an already-awake box.
    """
    if spec.remote_start_command is None:
        return False
    # The declared command is ("ssh", <opts...>, host, <remote cmd>); replacing
    # the trailing remote command with a trivial one reuses the exact same
    # connection settings the real call will use.
    probe = tuple(spec.remote_start_command[:-1]) + ("exit",)
    try:
        rc, _, _ = await asyncio.wait_for(_run(*probe), timeout=timeout + 2)
        return rc == 0
    except (asyncio.TimeoutError, ModelServerError):
        return False


async def _wake_remote(spec: "ModelServerSpec") -> dict:
    """Wake the box and wait for it to answer ssh. Idempotent."""
    if await _remote_box_reachable(spec):
        return {"woken": False, "detail": "already reachable"}
    if not spec.wake_command:
        raise ModelServerSafetyError(
            f"{spec.slug} is unreachable and has no wake_command declared."
        )
    rc, out, err = await _run(*spec.wake_command)
    if rc != 0:
        raise ModelServerError(
            f"Wake command failed for {spec.slug}: {(err or out).strip()[-300:]}"
        )
    deadline = time.monotonic() + spec.remote_wake_deadline
    while time.monotonic() < deadline:
        await asyncio.sleep(5)
        if await _remote_box_reachable(spec):
            return {"woken": True, "detail": f"reachable after wake"}
    raise ModelServerError(
        f"{spec.slug}: woke {spec.slug} but it did not answer ssh within "
        f"{spec.remote_wake_deadline:.0f}s."
    )


# status() is a dashboard read and is called often. Probing a sleeping box costs
# a full ssh connect timeout, so an uncached remote probe would make every
# status() call hang for seconds per asleep host.
#
# A plain TTL cache is not enough, and the web UI measured why: with two asleep
# remotes and a 20s TTL, GET /infrastructure/model-servers took 8.8s once every
# 20 seconds (3s health timeout + 4s reachability timeout, per remote, on the
# request's own critical path) while the page polled it every 10s — so a slow
# tick overlapped the next one and an older payload could land after a newer.
#
# So reads are now STALE-WHILE-REVALIDATE: an expired entry is returned
# immediately and refreshed in the background, and only a completely unknown
# remote blocks. `fresh=True` (operations) still probes synchronously — a
# start/stop decision must never be made against a remembered state.
_REMOTE_STATE_TTL = 20.0
# How long a remembered state may still be served while a refresh runs. Past
# this the read blocks again, so a genuinely stuck probe cannot pin the UI to a
# state that is minutes old.
_REMOTE_STATE_MAX_AGE = 300.0
_remote_state_cache: dict[str, tuple[float, str]] = {}
# Single-flight: without it, N concurrent status() rows for the same remote each
# start their own probe and every one of them pays the full timeout.
_remote_state_inflight: dict[str, asyncio.Task] = {}


async def _probe_remote_state(spec: "ModelServerSpec") -> str:
    """The actual probe. Always writes the cache."""
    if await _remote_health_ok(spec, timeout=3.0):
        state = "running"
    elif await _remote_box_reachable(spec, timeout=4.0):
        state = "stopped"
    else:
        state = "asleep"
    _remote_state_cache[spec.slug] = (time.monotonic(), state)
    return state


def _remote_state_refresh(spec: "ModelServerSpec") -> asyncio.Task:
    """Start (or join) the one in-flight probe for this spec."""
    task = _remote_state_inflight.get(spec.slug)
    if task is not None and not task.done():
        return task
    task = asyncio.create_task(_probe_remote_state(spec))
    _remote_state_inflight[spec.slug] = task
    # Don't let a failed background probe surface as "task exception was never
    # retrieved"; the next read simply blocks and tries again.
    task.add_done_callback(lambda t: t.exception() if t.done() and not t.cancelled() else None)
    return task


async def _remote_state(spec: "ModelServerSpec", fresh: bool = False) -> str:
    """'running' | 'stopped' | 'asleep' for an operable remote.

    'stopped' (box up, model not serving) is the state that motivated all of
    this — it is actionable and was previously indistinguishable from 'asleep'.
    """
    if fresh:
        return await _remote_state_refresh(spec)

    hit = _remote_state_cache.get(spec.slug)
    if hit is not None:
        age = time.monotonic() - hit[0]
        if age < _REMOTE_STATE_TTL:
            return hit[1]
        if age < _REMOTE_STATE_MAX_AGE:
            # Serve what we know, refresh behind the read.
            _remote_state_refresh(spec)
            return hit[1]

    # Nothing usable remembered: this read has to wait, but it joins the
    # in-flight probe rather than starting a second one.
    return await _remote_state_refresh(spec)


async def _await_remote_ready(spec: "ModelServerSpec") -> bool:
    """Poll the model health endpoint until it serves, or the deadline lapses.

    Returns readiness rather than raising: a started-but-not-yet-ready service
    is a legitimate outcome to report (Ridge's cold load is ~90s, RED's longer),
    and the caller records it as `state: starting` rather than as a failure.
    """
    deadline = time.monotonic() + spec.remote_ready_deadline
    while time.monotonic() < deadline:
        if await _remote_health_ok(spec):
            return True
        await asyncio.sleep(5)
    return False


_INSPECT_FMT = '{{.State.Status}}|{{index .Config.Labels "com.docker.compose.project"}}'


async def _systemd_inspect(unit: str) -> tuple[str, bool]:
    """(state, compose_managed) for a systemd --user unit.

    States are normalised into the same vocabulary docker reports so the GTT
    gate, exclusivity checks and _MEMORY_HOLDING_STATES all work unchanged:
    active -> running, failed -> dead, everything else -> exited. Returns
    "not_created" when the unit file is absent.
    """
    rc, out, _ = await _run("systemctl", "--user", "list-unit-files", unit)
    if rc != 0 or unit not in out:
        return "not_created", False
    _, out, _ = await _run("systemctl", "--user", "is-active", unit)
    state = out.strip()
    if state == "active":
        return "running", False
    if state == "failed":
        return "dead", False
    if state in ("activating", "reloading"):
        return "restarting", False
    return "exited", False


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


def _endpoints_for(spec: "ModelServerSpec") -> dict:
    """What a consumer (e.g. Hermes's config.yaml) should dial for this spec.

    Shared by the full status() rows and the light running_summary() rows so
    the two views cannot disagree about where a server listens."""
    if spec.endpoint_override:
        return {"tailnet": spec.endpoint_override}
    if not spec.port:
        return {}
    return {
        "local": f"http://localhost:{spec.port}/v1",
        "tailnet": f"http://{_TAILNET_IP}:{spec.port}/v1",
    }


def _server_row(
    spec: "ModelServerSpec",
    state: str,
    geometry: "LaunchGeometry",
    pools: dict,
    gtt,
    spilling: dict,
    bindings: dict,
    measured: dict,
) -> dict:
    """One server's full status row. Shared by the fleet-wide _status_uncached
    and the single-spec one() so the two views cannot drift."""
    entry = {
        "slug": spec.slug,
        "description": spec.description,
        "state": state,
        "port": spec.port,
        "model_file": spec.model_file,
        "runtime_repo": spec.runtime_repo,
        "runtime_ref": spec.runtime_ref,
        "backend_device": spec.backend_device,
        # Computed from the launch file's -c where the spec allows it,
        # else the declared SWAG. Either way it tracks the unit.
        "resident_gib_estimate": effective_resident_gib(spec, geometry),
        # MEASURED from the OS (kfd tree for GPU-resident servers, RSS
        # for CPU-only), None when not running. The estimate above is a
        # projection — prefer this whenever it is present.
        "resident_gib_measured": measured.get(spec.slug),
        # Read from the unit/compose, never declared here. `ctx_per_slot`
        # is what one agent may actually send: llama.cpp divides the KV
        # budget across slots, so a consumer that configures `served_ctx`
        # will overflow by exactly the slot count.
        "served_ctx": geometry.n_ctx,
        "slots": geometry.slots,
        "ctx_per_slot": geometry.ctx_per_slot,
        "geometry_source": geometry.source,
        # Whether this server's memory lands in the GTT pool at all.
        # CPU-only servers must NOT be summed into a GPU total.
        "gtt_resident": spec.gtt_resident,
        # WHERE it runs and WHOSE memory it spends. Two servers in
        # different pools can be resident at once — that is the point
        # of the two-GPU topology, not an oversight.
        "memory_pool": spec.memory_pool,
        "also_uses": list(spec.also_uses),
        "devices": list(spec.devices),
        "deployment": spec.deployment,
        "pool_used_gib": (
            round(pools[spec.memory_pool][0], 1)
            if pools.get(spec.memory_pool) else None
        ),
        "pool_total_gib": (
            round(pools[spec.memory_pool][1], 1)
            if pools.get(spec.memory_pool) else None
        ),
        "pool_spilling": spilling.get(spec.memory_pool, False),
        # HOW it loads. `parameters` carries each knob's effective
        # value plus where that value came from, so an override ARIA
        # set is distinguishable from a drop-in Ben wrote by hand.
        "parameters": resolve_parameters(spec),
        "aria_overrides": read_aria_overrides(spec),
        "launch_script": spec.launch_script,
        "systemd_unit": unit_name(spec),
        "exclusive_with": list(spec.exclusive_with),
        "onbox": spec.onbox,
        "startable": spec.startable,
        "not_startable_reason": spec.not_startable_reason,
        "consumers_note": spec.consumers_note,
        "can_sleep": spec.sleep_command is not None,
        # Whether ARIA can wake/start/stop this remote's model service.
        # Without it a client cannot distinguish "off-box, nothing I can
        # do" from "off-box, but I can wake it" — which is why the web
        # UI greyed out Start for BOTH Ridge and RED and left no way to
        # wake either.
        "remotely_operable": spec.remotely_operable,
        "bound_agents": bindings.get(spec.slug, []),
        # What a consumer (e.g. Hermes's config.yaml) should dial.
        "endpoints": _endpoints_for(spec),
    }
    if gtt is not None:
        entry["gtt_used_gib"] = round(gtt[0], 1)
        entry["gtt_total_gib"] = round(gtt[1], 1)
    return entry


class ModelServerManager:
    """Start/stop/bind the local model servers. The single control plane —
    see the module docstring for why manual docker commands are retired."""

    def __init__(self):
        self.infrastructure_root = os.path.abspath(settings.infrastructure_root)
        # Last OS-measured footprint per slug, refreshed by status(). The
        # safety gate prefers it over spec.resident_gib when it is larger,
        # so a stale declaration cannot silently under-count an overcommit.
        self._last_measured: dict[str, float] = {}
        # Serializes every check-then-act sequence (start's safety gates,
        # bind's conflict probe). Without it, two concurrent MCP calls can
        # both pass the exclusivity/RAM checks and launch two ~90 GiB servers
        # together — the exact failure this module exists to prevent.
        self._lock = asyncio.Lock()
        # TTL cache for status(): the full computation is ~70-80 subprocess
        # spawns (one per spec, measured 8.57 s cold / 0.6 s warm) and every
        # /llm/v1 request used to pay it. start/stop/sleep invalidate it, so
        # a registry-driven change is visible immediately; anything started
        # out-of-band is visible within STATUS_CACHE_TTL.
        self._status_cache: tuple[float, list[dict]] | None = None
        self._status_lock = asyncio.Lock()

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
            # Pulled models are provisioned onto a container runtime on the
            # iGPU today; the field exists so a future pull targeting the
            # R9700 is gated against the right pool rather than the Halo's.
            memory_pool=doc.get("memory_pool", POOL_HALO),
            devices=tuple(doc.get("devices") or ()),
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
            if not spec.remotely_operable:
                return "external", False
            # Operable remotes get a real state, not a placeholder: the whole
            # point of remote operate is that "awake but not serving" is a
            # distinct, actionable condition. Cached — see _remote_state.
            return await _remote_state(spec), False
        unit = unit_name(spec)
        if unit:
            state, managed = await _systemd_inspect(unit)
            if state == "not_created" and spec.launch_script:
                # ARIA generates this unit on first start. "not_created" would
                # read as "broken"; it is simply not materialised yet.
                return "ready", False
            return state, managed
        if not spec.container_name:
            return "unwired", False
        info = await _container_inspect(spec.container_name)
        if info is None:
            return "not_created", False
        return info

    STATUS_CACHE_TTL = 10.0  # seconds

    def invalidate_status(self) -> None:
        """Drop the cached status(). Called by start/stop/sleep — the only
        things that change what the answer would be."""
        self._status_cache = None

    async def status(self, db: Optional[AsyncIOMotorDatabase] = None, *, force: bool = False) -> list[dict]:
        """Fleet status with a short TTL cache.

        The uncached computation spawns one or two subprocesses per spec
        (28 specs ≈ 70-80 spawns; 8.57 s cold / 0.6 s warm measured), and it
        sits on the critical path of every /llm/v1 request, every
        /health/services tick and every TUI/web poll. A 10 s TTL bounds the
        staleness; start()/stop()/sleep() invalidate immediately, so a
        registry-driven change is never stale for more than a tick. Pass
        force=True where a live answer is required (start's preflight, the
        utilization probes).
        """
        now = time.monotonic()
        if not force and self._status_cache is not None:
            ts, rows = self._status_cache
            if now - ts < self.STATUS_CACHE_TTL:
                return rows
        async with self._status_lock:
            if not force and self._status_cache is not None:
                ts, rows = self._status_cache
                if time.monotonic() - ts < self.STATUS_CACHE_TTL:
                    return rows
            rows = await self._status_uncached(db)
            self._status_cache = (time.monotonic(), rows)
            return rows

    async def _status_uncached(self, db: Optional[AsyncIOMotorDatabase] = None) -> list[dict]:
        # One read per pool, shared by every row, through the same seam the
        # start-time gate uses. The Halo figure is also kept under the
        # historical gtt_* keys so existing consumers keep working.
        pools = {
            name: _read_gtt_gib(name)
            for name in (POOL_HALO, POOL_R9700, POOL_HOST)
        }
        gtt = pools.get(POOL_HALO)
        # A discrete card holding GTT is serving out of system RAM — at which
        # point it is no longer an independent pool and a co-resident Halo
        # model is at risk. Read separately because the (used, total) seam
        # above deliberately carries only the two numbers the gate projects on.
        spilling = {
            name: bool(live and live.spilling)
            for name, live in ((n, read_pool(n)) for n in (POOL_HALO, POOL_R9700))
        }
        bindings: dict[str, list[str]] = {}
        dynamic_specs: list[ModelServerSpec] = []
        if db is not None:
            async for doc in db.agents.find({"model_server": {"$exists": True, "$ne": None}}):
                bindings.setdefault(doc["model_server"], []).append(doc.get("slug") or str(doc["_id"]))
            async for doc in db.model_servers.find({}):
                if doc["slug"] not in _BY_SLUG:
                    dynamic_specs.append(self._spec_from_doc(doc))

        all_specs = list(REGISTRY) + dynamic_specs

        # Off-box probes are the only expensive part of this read (a sleeping
        # host costs a 3s health timeout plus a 4s reachability timeout). The
        # per-spec loop below is sequential, so without this the two remotes
        # would serialise their timeouts on the very first call. Kick them off
        # together and let the loop join whatever is already in flight.
        for spec in all_specs:
            if not spec.onbox and spec.remotely_operable:
                _remote_state_refresh(spec)
        # Measure every running server once, concurrently — this is cheap
        # (sysfs + one systemctl/docker call each) and turns the whole view
        # from declared SWAGs into observed numbers.
        measured: dict[str, Optional[float]] = {}
        gathered = await asyncio.gather(
            *(measure_resident_gib(sp) for sp in all_specs), return_exceptions=True
        )
        for sp, val in zip(all_specs, gathered):
            measured[sp.slug] = val if isinstance(val, float) else None
            if isinstance(val, float):
                # Remembered so start()'s gate can use an observed footprint
                # instead of a stale declaration.
                self._last_measured[sp.slug] = val

        results = []
        for spec in all_specs:
            state, _ = await self._inspect(spec)
            results.append(
                _server_row(
                    spec, state, read_launch_geometry(spec),
                    pools, gtt, spilling, bindings, measured,
                )
            )
        return results

    async def one(self, slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> dict:
        """One server's full status row — probing ONLY that spec.

        The old route called the full status() (every spec probed, ~70-80
        subprocesses) to answer a question about one. This probes only the
        requested spec: one _inspect, the pool reads (three sysfs reads —
        cheap), and the same row shape as status() (shared builder).
        """
        spec = await self.resolve_spec(slug, db)
        state, _ = await self._inspect(spec)
        pools = {
            name: _read_gtt_gib(name)
            for name in (POOL_HALO, POOL_R9700, POOL_HOST)
        }
        gtt = pools.get(POOL_HALO)
        spilling = {
            name: bool(live and live.spilling)
            for name, live in ((n, read_pool(n)) for n in (POOL_HALO, POOL_R9700))
        }
        bindings: dict[str, list[str]] = {}
        if db is not None:
            async for doc in db.agents.find({"model_server": {"$exists": True, "$ne": None}}):
                if doc["model_server"] == spec.slug:
                    bindings.setdefault(spec.slug, []).append(doc.get("slug") or str(doc["_id"]))
        measured: dict[str, Optional[float]] = {}
        val = await measure_resident_gib(spec)
        measured[spec.slug] = val if isinstance(val, float) else None
        if isinstance(val, float):
            self._last_measured[spec.slug] = val
        return _server_row(
            spec, state, read_launch_geometry(spec),
            pools, gtt, spilling, bindings, measured,
        )

    async def running_summary(self, db: Optional[AsyncIOMotorDatabase] = None) -> list[dict]:
        """Cheap answer to "which servers are running" — for routing.

        `status()` is the full view (geometry, pools, parameters, measured
        footprint) and it is expensive: one or two subprocesses per spec. But
        routing (llm_route.select) only needs slug, model_file, state, onbox,
        port, endpoints and a footprint to compare magnitudes by — so this
        answers with at most TWO subprocesses: one
        `systemctl --user list-units --state=active --type=service` answers
        every unit-based spec at once, one `docker ps --filter status=running`
        answers every container spec. Off-box specs reuse the cached remote
        state (refreshed in the background by status()).

        Footprint: the last OS measurement (kept fresh by status() and
        start()) when available, else the declared estimate. A stale footprint
        degrades gracefully to the declaration — routing only ranks magnitudes.
        """
        specs = list(REGISTRY)
        if db is not None:
            async for doc in db.model_servers.find({}):
                if doc["slug"] not in _BY_SLUG:
                    specs.append(self._spec_from_doc(doc))

        unit_active: set[str] = set()
        if any(s.onbox and unit_name(s) for s in specs):
            rc, out, _ = await _run(
                "systemctl", "--user", "list-units",
                "--state=active", "--type=service", "--no-legend",
            )
            if rc == 0:
                unit_active = {
                    line.split()[0] for line in out.splitlines() if line.strip()
                }
        container_running: set[str] = set()
        if any(s.onbox and not unit_name(s) and s.container_name for s in specs):
            rc, out, _ = await _run(
                "docker", "ps", "--filter", "status=running", "--format", "{{.Names}}"
            )
            if rc == 0:
                container_running = {line.strip() for line in out.splitlines() if line.strip()}

        results = []
        for spec in specs:
            if spec.onbox:
                unit = unit_name(spec)
                if unit:
                    state = "running" if unit in unit_active else "exited"
                elif spec.container_name:
                    # `status=running` excludes paused containers — a paused
                    # container cannot serve, and ARIA has no unpause path.
                    state = "running" if spec.container_name in container_running else "exited"
                else:
                    state = "unwired"
            elif spec.remotely_operable:
                state = await _remote_state(spec)
            else:
                state = "external"
            measured = self._last_measured.get(spec.slug)
            results.append({
                "slug": spec.slug,
                "model_file": spec.model_file,
                "state": state,
                "onbox": spec.onbox,
                "port": spec.port,
                "endpoints": _endpoints_for(spec),
                "resident_gib_estimate": (
                    measured if measured is not None else effective_resident_gib(spec)
                ),
                # The 503 hint lists what a caller could start instead —
                # routing needs the flag even though it never acts on it.
                "startable": spec.startable,
            })
        return results

    def _apply_launch_config(
        self, spec: ModelServerSpec, unit: str, env_overrides: dict[str, str]
    ) -> dict:
        """Materialise the unit (if ARIA owns it) and write/clear its overrides.

        Returns whether a `daemon-reload` is needed and what to report back to
        the caller. Synchronous file work, called under the manager lock: these
        are four small writes, and doing them inline keeps "what will start"
        and "what did start" impossible to interleave.
        """
        changed = False
        report: dict = {}

        if spec.launch_script and not spec.systemd_unit:
            path = os.path.join(_SYSTEMD_USER_DIR, unit)
            if _write_if_changed(path, _render_unit(spec, unit)):
                changed = True
                report["unit_written"] = path

        dropin = _dropin_path(unit)
        if env_overrides:
            if _write_if_changed(dropin, _render_dropin(spec, env_overrides)):
                changed = True
            report["overrides_written"] = dropin
        elif _remove_if_present(dropin):
            # A previous session's overrides must not silently persist into a
            # plain "start it" — that is how a 131K context outlives the
            # experiment it was set for.
            changed = True
            report["overrides_cleared"] = dropin

        # Reported AFTER the write, so it is the configuration that will
        # actually launch rather than the one that was there a moment ago.
        report["launch_config"] = {
            entry["name"]: {"value": entry["value"], "source": entry["source"]}
            for entry in resolve_parameters(spec)
        }
        return {"reloaded": changed, "report": report}

    async def start(
        self,
        slug: str,
        force: bool = False,
        db: Optional[AsyncIOMotorDatabase] = None,
        overrides: Optional[dict] = None,
    ) -> dict:
        """Start a model server, optionally choosing HOW it loads.

        `overrides` maps declared parameter names to values — device placement,
        context, KV type, drafter, slots. They are validated against the spec's
        declared knobs, written as a systemd drop-in, and therefore visible and
        removable outside ARIA. Passing none clears any override ARIA set
        earlier, so a plain start always means the deployment's own defaults.
        """
        self.invalidate_status()  # a start changes what status() would answer
        spec = await self.resolve_spec(slug, db)
        if not spec.onbox:
            if not spec.remotely_operable:
                raise ModelServerSafetyError(
                    f"{slug} is off-box and has no remote start/stop declared — "
                    f"ARIA cannot start it directly."
                )
            # `startable` gates the remote path too. Without this an entry
            # whose remote launcher is known-broken would still be attempted,
            # and the caller would get a slow `starting` instead of the reason.
            if not spec.startable and not force:
                raise ModelServerSafetyError(
                    spec.not_startable_reason or f"{slug} has no working remote launcher."
                )
            # Remote path: no local RAM gate applies (POOL_REMOTE has no local
            # pool), no drop-in to write, no exclusivity to enforce against
            # corsair's pools. Overrides are refused rather than ignored — a
            # silently-dropped override is the kind of quiet wrong answer this
            # module exists to prevent.
            if overrides:
                raise ModelServerSafetyError(
                    f"{slug} is remote; launch overrides are not supported "
                    f"(its parameters live in the remote service definition)."
                )
            return await self._start_remote(spec)
        if not spec.startable and not force:
            raise ModelServerSafetyError(
                spec.not_startable_reason or f"{slug} has no working runtime/service yet."
            )
        # Validated BEFORE the lock and before any state change: a typo'd
        # parameter should cost nothing, not leave a half-written drop-in.
        env_overrides = validate_overrides(spec, overrides)

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
                    # A systemd-bundle server has no container_name — gating on
                    # it alone silently skipped every exclusivity pair among the
                    # BIGGEST models on the box (DS4 and both Lings), leaving the
                    # GTT projection below as the only thing standing between a
                    # 93 GiB server and an 88 GiB one. _inspect() has handled
                    # systemd since it was added; only this filter lagged.
                    if other is None or not other.onbox:
                        continue
                    if not (other.container_name or other.systemd_unit):
                        continue
                    other_state, _ = await self._inspect(other)
                    if other_state in _MEMORY_HOLDING_STATES:
                        conflicts.append(f"{other_slug} ({other_state})")
                if conflicts:
                    raise ModelServerSafetyError(
                        f"{slug} is mutually exclusive with active server(s): "
                        f"{', '.join(conflicts)}. Stop them first, or pass force=True."
                    )

                # Ports are a hard conflict independent of memory: two servers
                # cannot bind the same port, and several entries here share one
                # deliberately (the three :8110 Qwen variants, the DS4s on
                # :8107). Checked live rather than baked into exclusive_with,
                # so it also covers dynamically-pulled entries.
                if spec.port:
                    for other in REGISTRY:
                        if other.slug == slug or other.port != spec.port:
                            continue
                        if not other.onbox or not (other.container_name or unit_name(other)):
                            continue
                        other_state, _ = await self._inspect(other)
                        if other_state in _MEMORY_HOLDING_STATES:
                            raise ModelServerSafetyError(
                                f"Port {spec.port} is already held by {other.slug} "
                                f"({other_state}). Stop it first, override the port, "
                                f"or pass force=True."
                            )

                # Projected against the pool this server actually draws from —
                # the R9700's own VRAM for a dGPU model, the Halo's shared
                # system memory otherwise. Reading one number for the whole box
                # would forbid the dual-serving deployment that works.
                gtt = _read_gtt_gib(spec.memory_pool)
                # Projected from the `-c` the unit will actually launch with,
                # so raising context and forgetting to update a number here can
                # no longer slip an overcommit past this gate.
                projection = effective_resident_gib(spec)
                gated_pool = spec.gtt_resident and spec.memory_pool != POOL_HOST
                if gtt is not None and projection is not None and gated_pool:
                    used, total = gtt
                    # The projection is only as good as the number added to it.
                    # A stale SWAG under-counts and lets an overcommit through,
                    # so bias to the larger of projected-vs-last-measured.
                    claim = projection
                    seen = self._last_measured.get(slug)
                    basis = "projected"
                    if seen is not None and seen > claim:
                        claim, basis = seen, "measured"
                    projected = used + claim
                    margin = _safety_margin(spec.memory_pool)
                    if projected > total * margin:
                        raise ModelServerSafetyError(
                            f"Starting {slug} (~{claim:.0f} GiB {basis}) would push "
                            f"{spec.memory_pool} usage to ~{projected:.0f}/{total:.0f} "
                            f"GiB, over the {margin:.0%} safety margin. "
                            f"Stop something first, or pass force=True."
                        )

            note = None
            unit = unit_name(spec)
            if unit:
                applied = self._apply_launch_config(spec, unit, env_overrides)
                if applied["reloaded"]:
                    rc, out, err = await _run("systemctl", "--user", "daemon-reload")
                    if rc != 0:
                        raise ModelServerError(
                            f"Failed to reload systemd for {slug}: {(err or out).strip()}"
                        )
                rc, out, err = await _run("systemctl", "--user", "start", unit)
                if rc != 0:
                    raise ModelServerError(
                        f"Failed to start {slug}: {(err or out).strip()}"
                    )
                result = {
                    "slug": slug, "state": "starting", "action": "started",
                    "unit": unit,
                    "output": (out + err)[-2000:],
                    "note": (
                        "systemd unit; ExecStartPre guards (bundle manifest, dGPU "
                        "power state, TTM pool cap) run on every start. Model load "
                        "takes ~2-3 min before /health reports ok."
                    ),
                }
                result.update(applied["report"])
                return result
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

    async def _start_remote(self, spec: ModelServerSpec) -> dict:
        """Wake if needed, start the remote model service, verify it serves.

        Held under the same lock as local starts so two callers cannot race a
        wake, and so a remote start is serialised against local ones (they can
        contend for nothing physical, but they do contend for ARIA's own view
        of what it has asked for).
        """
        async with self._lock:
            if await _remote_health_ok(spec):
                _remote_state_cache[spec.slug] = (time.monotonic(), "running")
                return {"slug": spec.slug, "state": "ready", "action": "noop",
                        "detail": "already serving"}
            # Any action invalidates the read cache; the outcome below re-seeds it.
            _remote_state_cache.pop(spec.slug, None)

            wake = await _wake_remote(spec)
            rc, out, err = await _run(*spec.remote_start_command)
            # A scheduled task that is already running returns nonzero on some
            # Windows builds; readiness below is the real oracle, so a nonzero
            # exit is recorded but not treated as terminal.
            ready = await _await_remote_ready(spec)
            _remote_state_cache[spec.slug] = (
                time.monotonic(), "running" if ready else "stopped")
            detail = (err or out).strip()[-300:]
            if not ready:
                return {
                    "slug": spec.slug,
                    "state": "starting",
                    "action": "start_requested",
                    "woken": wake["woken"],
                    "detail": (
                        f"start issued (exit {rc}); not serving within "
                        f"{spec.remote_ready_deadline:.0f}s. {detail}"
                    ).strip(),
                }
            return {
                "slug": spec.slug,
                "state": "ready",
                "action": "started",
                "woken": wake["woken"],
                "detail": detail or f"serving (start exit {rc})",
            }

    async def _stop_remote(self, spec: ModelServerSpec) -> dict:
        """Stop the remote model service. Does NOT suspend the machine.

        Stopping a model and sleeping its host are separate decisions: freeing
        a 3090 for something else is not the same as putting the box to sleep,
        and conflating them would make stop() unexpectedly destructive. sleep()
        remains the explicit verb for suspending.
        """
        async with self._lock:
            if not await _remote_box_reachable(spec):
                _remote_state_cache[spec.slug] = (time.monotonic(), "asleep")
                return {"slug": spec.slug, "state": "asleep", "action": "noop",
                        "detail": "box unreachable — nothing to stop"}
            _remote_state_cache.pop(spec.slug, None)
            rc, out, err = await _run(*spec.remote_stop_command)
            serving = await _remote_health_ok(spec)
            _remote_state_cache[spec.slug] = (
                time.monotonic(), "running" if serving else "stopped")
            detail = (err or out).strip()[-300:]
            if serving:
                raise ModelServerError(
                    f"Failed to stop {spec.slug}: still serving after stop "
                    f"(exit {rc}). {detail}"
                )
            return {"slug": spec.slug, "state": "stopped", "action": "stopped",
                    "detail": detail or f"stopped (exit {rc})"}

    async def stop(self, slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> dict:
        self.invalidate_status()  # a stop changes what status() would answer
        spec = await self.resolve_spec(slug, db)
        if not spec.onbox:
            if not spec.remotely_operable:
                raise ModelServerSafetyError(
                    f"{slug} is off-box and has no remote start/stop declared — "
                    f"ARIA cannot stop it directly."
                )
            return await self._stop_remote(spec)
        unit = unit_name(spec)
        if unit:
            async with self._lock:
                state, _ = await self._inspect(spec)
                if state in ("not_created", "exited", "dead", "ready"):
                    return {"slug": slug, "state": state, "action": "noop"}
                rc, out, err = await _run("systemctl", "--user", "stop", unit)
                if rc != 0:
                    raise ModelServerError(
                        f"Failed to stop {slug}: {(err or out).strip()}"
                    )
                return {"slug": slug, "state": "stopped", "action": "stopped"}
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
        self.invalidate_status()  # a sleep changes what status() would answer
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
                _remote_state_cache[slug] = (time.monotonic(), "asleep")
                return {"slug": slug, "state": "asleep", "action": "noop",
                        "detail": "unreachable over ssh — already asleep"}
            rc, out, err = await _run(*spec.sleep_command)
            # The box suspending mid-command drops the ssh connection, so a
            # nonzero exit here is the EXPECTED success shape, not a failure.
            #
            # CAVEAT (measured 2026-08-15): that also means this call CANNOT
            # distinguish "suspended" from "command ran and the box stayed up".
            # Ridge was observed still serving after a clean `ssh exit 0` here.
            # So the returned state is `sleep_requested`, never `asleep`, and
            # the cache is INVALIDATED rather than seeded with an assumption —
            # the next status() re-probes and reports what is actually true.
            _remote_state_cache.pop(slug, None)
            # Verify from HERE. The box cannot report its own suspension, and
            # the previous implementation trusted the exit code — which is how
            # a sleep verb that never suspended anything went unnoticed.
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                if not await _remote_box_reachable(spec, timeout=4.0):
                    _remote_state_cache[slug] = (time.monotonic(), "asleep")
                    return {"slug": slug, "state": "asleep", "action": "slept",
                            "verified": True,
                            "detail": "confirmed unreachable from corsair"}
            _remote_state_cache.pop(slug, None)
            return {
                "slug": slug, "state": "awake", "action": "sleep_failed",
                "verified": False,
                "detail": (
                    "suspend was issued but the box is still reachable after 90s. "
                    "Check `powercfg /requests` for a held wakelock and "
                    "WakeOnPattern on the NICs (a pattern-armed NIC is revived by "
                    "the next Tailscale keepalive). "
                    + ((err or out).strip()[-200:] or f"ssh exit {rc}")
                ),
            }

    async def bind(
        self, db: AsyncIOMotorDatabase, slug: str, agent_id_or_slug: str, force: bool = False
    ) -> dict:
        await self.resolve_spec(slug, db)  # validates slug (static or pulled), raises ModelServerNotFound
        self.invalidate_status()  # bind changes the bound_agents field of every row
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
        self.invalidate_status()  # unbind changes the bound_agents field of every row
        agent = await _find_agent_doc(db, agent_id_or_slug)
        if agent is None:
            raise ModelServerNotFound(f"Unknown agent: {agent_id_or_slug}")
        await db.agents.update_one(
            {"_id": agent["_id"]},
            {"$unset": {"model_server": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}},
        )
        return {"agent": agent.get("slug", str(agent["_id"])), "model_server": None}
