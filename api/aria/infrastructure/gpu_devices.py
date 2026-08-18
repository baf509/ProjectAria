"""
ARIA - GPU device discovery & per-pool memory accounting

Phase: Infrastructure / model-server control plane
Purpose: Answer "which physical device is this, and how much of ITS memory is
free" for a box that now has two GPUs with separate memory pools.

Related Spec Sections:
- docs/ops/LOCAL_INFERENCE_TOPOLOGY.md (device map, placement rules)

WHY THIS MODULE EXISTS
----------------------
Until the OCuLink Radeon AI PRO R9700 was added, corsair-ai had exactly one
GPU and exactly one memory pool, so `/sys/class/drm/card0/device/mem_info_gtt_*`
was an unambiguous read of "GPU memory pressure on this box". Both
`model_servers.py` and `shells/selfcheck.py` hardcoded that path.

Adding the dGPU INVERTED the enumeration. Measured 2026-08-14:

    card0 = 0000:c6:00.0  Radeon AI PRO R9700 (gfx1201)  31 GiB VRAM, discrete
    card1 = 0000:c8:00.0  Radeon 8060S / Strix Halo      124 GiB GTT, integrated

So the hardcoded card0 read now reports the *dGPU's* GTT — which is ~0 while
the Halo holds 97 GiB. A safety gate reading that number would cheerfully
approve starting a second ~100 GiB model on top of a full box. That is the
class of failure the gate exists to prevent, so device identity has to be
discovered, not assumed.

THE POOLS ARE GENUINELY SEPARATE
--------------------------------
This is the whole point of the dual-device topology Ben runs: a model on the
R9700 lives in that card's own 32 GiB of VRAM and does NOT compete with a
model on the Halo, which draws from the 124 GiB of shared system memory. Two
models can be resident at once precisely because they are in different pools
(see infrastructure/DUAL-SERVING.md — verified live 2026-08-14, DS4 on the
Halo + Qwen3.8 on the R9700). Accounting them against one number would forbid
the deployment that actually works.

The one coupling: a dGPU model that does NOT fit its VRAM spills into GTT,
which is system RAM — i.e. it starts eating the Halo's pool. `spilling` on the
R9700 pool reports exactly that, and it is why every dGPU launch here passes
`-fit off`.

...BUT `halo-gtt` AND `host-ram` ARE NOT SEPARATE FROM EACH OTHER
----------------------------------------------------------------
Only TWO physical pools exist on this box, not three. The Strix Halo iGPU has
no memory of its own: its GTT allocation is carved out of system RAM, so
`halo-gtt` and `host-ram` are two measurements of the same DIMMs, and
`host-ram`'s used figure already CONTAINS `halo-gtt`'s (measured 2026-08-17:
MemTotal 124.45, MemAvailable 6.09 -> host used 118.4, of which the Halo held
102.11 through GTT). Anything rendering one bar per pool therefore claims
~248 GiB of capacity on a 124 GiB machine and double-counts ~102 GiB of it —
which is exactly what the web UI did until Ben pointed it out.

`pool_snapshot()` now carries `backing` ("system" | "device") and `overlaps` so
a consumer can see the relationship, and `system_memory_snapshot()` returns the
one honest composite (total / igpu / other / available). The per-pool numbers
themselves are unchanged, because the start-time gate and selfcheck project
against them and their semantics are load-bearing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_DRM_ROOT = "/sys/class/drm"
_GIB = 1024**3

# Pool identifiers. A ModelServerSpec names one of these as its primary pool;
# the start-time memory gate reads that pool and no other.
POOL_HALO = "halo-gtt"       # Strix Halo iGPU — shared system memory (GTT)
POOL_R9700 = "r9700-vram"    # Radeon AI PRO R9700 — the card's own VRAM
POOL_HOST = "host-ram"       # CPU-only servers — never touch a GPU pool
POOL_REMOTE = "remote"       # off-box (Ridge) — no local pool to gate against

# A dGPU is "discrete enough to have its own pool" when it reports real VRAM.
# The Halo reports ~1 GiB of carve-out VRAM plus a 124 GiB GTT aperture; the
# R9700 reports ~31 GiB VRAM. 8 GiB cleanly separates the two on any plausible
# future card without hardcoding a PCI address.
_DISCRETE_VRAM_FLOOR_GIB = 8.0

# Above this much GTT on a discrete card, it is spilling into system RAM and
# has started competing with the Halo's pool. Same 1 GiB threshold the
# ds4-halo-xxs launcher uses before it will allowlist a co-resident dGPU model.
_SPILL_THRESHOLD_GIB = 1.0


@dataclass(frozen=True)
class GpuDevice:
    """One DRM card, classified by what kind of memory it owns."""

    card: str                    # "card0"
    pci_address: Optional[str]   # "0000:c6:00.0"
    discrete: bool
    vram_total_gib: float
    vram_used_gib: float
    gtt_total_gib: float
    gtt_used_gib: float

    @property
    def pool(self) -> str:
        return POOL_R9700 if self.discrete else POOL_HALO

    @property
    def label(self) -> str:
        return "R9700 (discrete)" if self.discrete else "Strix Halo (integrated)"


@dataclass(frozen=True)
class MemoryPool:
    """Live capacity of one memory pool, in GiB.

    `used`/`total` are what the start-time gate projects against. `spilling`
    is only ever True for a discrete card and means it has started consuming
    system RAM, i.e. the two pools are no longer independent.
    """

    pool: str
    label: str
    used_gib: float
    total_gib: float
    source: str
    spilling: bool = False

    @property
    def free_gib(self) -> float:
        return max(0.0, self.total_gib - self.used_gib)


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return None


def _pci_address(card: str) -> Optional[str]:
    try:
        target = os.path.realpath(os.path.join(_DRM_ROOT, card, "device"))
    except OSError:
        return None
    tail = os.path.basename(target)
    # Real PCI leaf names look like 0000:c6:00.0
    return tail if tail.count(":") == 2 else None


def discover_devices() -> list[GpuDevice]:
    """Every amdgpu DRM card on this box, newest read each call.

    Deliberately uncached: enumeration changes when a card is hot-plugged or
    the box reboots, and a stale device map is exactly the failure this module
    replaces. The reads are four small sysfs files per card.
    """
    devices: list[GpuDevice] = []
    try:
        cards = sorted(
            name for name in os.listdir(_DRM_ROOT)
            if name.startswith("card") and name[4:].isdigit()
        )
    except OSError as exc:
        logger.warning("gpu_devices: cannot list %s: %s", _DRM_ROOT, exc)
        return devices

    for card in cards:
        base = os.path.join(_DRM_ROOT, card, "device")
        gtt_total = _read_int(os.path.join(base, "mem_info_gtt_total"))
        if gtt_total is None:
            continue  # not an amdgpu card (or no memory accounting) — skip
        gtt_used = _read_int(os.path.join(base, "mem_info_gtt_used")) or 0
        vram_total = _read_int(os.path.join(base, "mem_info_vram_total")) or 0
        vram_used = _read_int(os.path.join(base, "mem_info_vram_used")) or 0
        vram_total_gib = vram_total / _GIB
        devices.append(
            GpuDevice(
                card=card,
                pci_address=_pci_address(card),
                discrete=vram_total_gib >= _DISCRETE_VRAM_FLOOR_GIB,
                vram_total_gib=round(vram_total_gib, 2),
                vram_used_gib=round(vram_used / _GIB, 2),
                gtt_total_gib=round(gtt_total / _GIB, 2),
                gtt_used_gib=round(gtt_used / _GIB, 2),
            )
        )
    return devices


def _host_pool() -> Optional[MemoryPool]:
    """MemAvailable-derived view for CPU-only servers.

    Reported for display only — `read_pool()` callers gate GPU pools; the CPU
    servers on this box are small and container-capped, and MemAvailable moves
    for reasons that have nothing to do with whether one more 8 GiB CPU model
    fits.
    """
    total = avail = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) / (1024 * 1024)
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) / (1024 * 1024)
                if total is not None and avail is not None:
                    break
    except (OSError, ValueError, IndexError):
        return None
    if total is None or avail is None:
        return None
    return MemoryPool(
        pool=POOL_HOST,
        label="host RAM",
        used_gib=round(total - avail, 1),
        total_gib=round(total, 1),
        source="/proc/meminfo",
    )


def read_pool(pool: str) -> Optional[MemoryPool]:
    """Live (used, total) for one pool, or None when it cannot be read.

    None means "unknown", never "empty": callers must skip the gate rather
    than treat an unreadable pool as free space.
    """
    if pool == POOL_HOST:
        return _host_pool()

    devices = discover_devices()
    if pool == POOL_R9700:
        card = next((d for d in devices if d.discrete), None)
        if card is None:
            return None
        return MemoryPool(
            pool=POOL_R9700,
            label=f"R9700 VRAM ({card.card})",
            used_gib=card.vram_used_gib,
            total_gib=card.vram_total_gib,
            source=f"{_DRM_ROOT}/{card.card}/device/mem_info_vram_*",
            # A discrete card holding GTT is serving weights out of system RAM.
            # That both tanks its own throughput and starts draining the Halo's
            # pool, so it is surfaced rather than silently folded into a number.
            spilling=card.gtt_used_gib > _SPILL_THRESHOLD_GIB,
        )

    if pool == POOL_HALO:
        card = next((d for d in devices if not d.discrete), None)
        if card is None:
            # Single-GPU box, or classification failed. Falling back to "the
            # card with the biggest GTT aperture" keeps the gate working
            # rather than disabling it, which is the safer degradation.
            card = max(devices, key=lambda d: d.gtt_total_gib, default=None)
            if card is None:
                return None
        return MemoryPool(
            pool=POOL_HALO,
            label=f"Strix Halo GTT ({card.card})",
            used_gib=card.gtt_used_gib,
            total_gib=card.gtt_total_gib,
            source=f"{_DRM_ROOT}/{card.card}/device/mem_info_gtt_*",
        )

    return None


def _pdev_to_pool() -> dict[str, str]:
    return {d.pci_address: d.pool for d in discover_devices() if d.pci_address}


def process_gpu_bytes(pid: int) -> dict[str, int]:
    """How much of each pool one process is holding, from amdgpu DRM fdinfo.

    This is the measurement that works for BOTH runtimes on this box. The KFD
    tree (/sys/class/kfd) only sees HIP/ROCm processes — a RADV Vulkan server
    has no KFD entry at all, so the DS4 deployments that run on Nathan's Vulkan
    fork measured as zero while holding ~98 GiB. amdgpu's per-fd accounting
    covers both, and reports PER DEVICE, which is what makes a split
    deployment legible: measured live on 2026-08-14, one llama-server showed
    97.73 GiB of GTT on the Halo and 0.20 GiB on the R9700 in the same reading.

    Uses `drm-resident-*` (what is actually in that memory now) rather than
    `drm-total-*`, and takes GTT for the shared-memory pools and VRAM for the
    discrete card, matching how each pool's capacity is defined.

    Returns {} when the process has no GPU file descriptors — which is a real
    answer ("not using the GPU"), not a failure.
    """
    fdinfo_dir = f"/proc/{pid}/fdinfo"
    pools = _pdev_to_pool()
    # A process opens several fds onto the same DRM client; counting each would
    # multiply the footprint. Key by (pdev, client-id) so duplicates collapse.
    clients: dict[tuple[str, str], dict[str, int]] = {}
    try:
        names = os.listdir(fdinfo_dir)
    except OSError:
        return {}
    for name in names:
        try:
            with open(os.path.join(fdinfo_dir, name)) as fh:
                text = fh.read()
        except OSError:
            continue
        if "drm-pdev" not in text:
            continue
        fields: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        pdev = fields.get("drm-pdev", "")
        client = fields.get("drm-client-id", name)
        if not pdev:
            continue
        sizes = {}
        for key in ("drm-resident-gtt", "drm-resident-vram"):
            raw = fields.get(key, "0").split()
            sizes[key] = int(raw[0]) * 1024 if raw and raw[0].isdigit() else 0
        clients[(pdev, client)] = sizes

    out: dict[str, int] = {}
    for (pdev, _), sizes in clients.items():
        pool = pools.get(pdev)
        if pool is None:
            continue
        held = sizes["drm-resident-vram"] if pool == POOL_R9700 else sizes["drm-resident-gtt"]
        out[pool] = out.get(pool, 0) + held
    return out


def process_uses_gpu(pid: int) -> bool:
    """Whether a pid holds any GPU memory at all — used to find the real
    llama-server underneath a launcher wrapper."""
    return any(v > 0 for v in process_gpu_bytes(pid).values())


# Which physical memory each pool actually consumes. This is the fact that a
# flat list of pools cannot express and that a UI drawing one bar per pool gets
# WRONG: `halo-gtt` and `host-ram` are the SAME DIMMs. The iGPU has no memory of
# its own — its GTT allocation comes out of system RAM — so `host-ram`'s
# used figure already CONTAINS `halo-gtt`'s. Rendered as two independent bars
# they claim ~248 GiB on a 124 GiB machine and double-count ~102 GiB of it.
POOL_BACKING = {
    POOL_HALO: "system",
    POOL_HOST: "system",
    POOL_R9700: "device",
}


def system_memory_snapshot() -> Optional[dict]:
    """The one honest view of system RAM, broken down by who is holding it.

    `halo-gtt` and `host-ram` are two measurements of one pool, so the display
    surface needs a composite rather than two bars:

        total      MemTotal
        igpu       what the Strix Halo has pinned through GTT
        other      everything else in use (MemTotal - MemAvailable - igpu)
        available  MemAvailable

    `other` is what the start-time gate's MemAvailable floor actually protects,
    and it is why a dGPU model can break an iGPU model's preflight despite
    sharing no VRAM: vllm-radiance holds ~8-10 GiB of host RAM permanently.
    """
    host = _host_pool()
    if host is None:
        return None
    halo = read_pool(POOL_HALO)
    igpu = halo.used_gib if halo else 0.0
    total = host.total_gib
    available = max(0.0, total - host.used_gib)
    # Clamped: GTT accounting and MemAvailable are sampled from different
    # places and can disagree by a few hundred MiB.
    other = max(0.0, host.used_gib - igpu)
    return {
        "total_gib": round(total, 1),
        "igpu_gib": round(igpu, 1),
        "other_gib": round(other, 1),
        "available_gib": round(available, 1),
        "igpu_source": halo.source if halo else None,
        "source": host.source,
        "note": (
            "The Strix Halo iGPU has no memory of its own: its GTT allocation "
            "is system RAM. halo-gtt and host-ram measure the same DIMMs."
        ),
    }


def pool_snapshot() -> list[dict]:
    """Every pool, for the API/UI device panel.

    `backing` and `overlaps` say which pools share physical memory — without
    them a consumer cannot tell that two of these three bars are the same RAM.
    The numbers themselves are unchanged: the start-time gate and selfcheck read
    them, and their semantics are load-bearing.
    """
    out = []
    for pool in (POOL_HALO, POOL_R9700, POOL_HOST):
        live = read_pool(pool)
        if live is None:
            continue
        backing = POOL_BACKING.get(live.pool, "unknown")
        out.append({
            "pool": live.pool,
            "label": live.label,
            "used_gib": round(live.used_gib, 1),
            "total_gib": round(live.total_gib, 1),
            "free_gib": round(live.free_gib, 1),
            "spilling": live.spilling,
            "source": live.source,
            "backing": backing,
            # Pools drawing on the same physical memory as this one.
            "overlaps": [
                other for other, kind in POOL_BACKING.items()
                if kind == backing and other != live.pool
            ],
        })
    return out


def device_snapshot() -> list[dict]:
    """Every discovered card, for the API/UI device panel."""
    return [
        {
            "card": d.card,
            "pci_address": d.pci_address,
            "label": d.label,
            "pool": d.pool,
            "discrete": d.discrete,
            "vram_used_gib": d.vram_used_gib,
            "vram_total_gib": d.vram_total_gib,
            "gtt_used_gib": d.gtt_used_gib,
            "gtt_total_gib": d.gtt_total_gib,
        }
        for d in discover_devices()
    ]
