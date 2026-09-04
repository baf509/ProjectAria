"""
ARIA - Non-LLM Service Registry

Phase: Ontology Memory Map · Phase 0
Purpose: Know what is *supposed* to be running on this box, and whether it is.

Related Spec Sections:
- vault/ProjectAria/Design/ARCHITECTURE.md (Ontology Memory Map) — the ontology
  projects `service` entities from this registry rather than holding rows itself.

WHY THIS IS NOT `model_servers.REGISTRY`
=======================================
Adding mongod/aria-api/samba to the model-server registry is the obvious cheap
path and it breaks three ways — all verified against the code, 2026-08-07:

1. `llm_route.match_requested()` matches a request's `model` field against
   registry slugs, so a caller sending `model: "shared-mongod"` would have LLM
   traffic proxied to :27017.
2. `llm_route.rank_resident()` scores a missing `resident_gib_estimate` as
   `0.0` rather than excluding the row, so non-LLM entries become auto-route
   candidates whenever no real model is resident.
3. Decisively: `api/routes/health.py` builds `stopped_on_purpose` keyed by port
   from `model_servers.status()`, because the big LLM servers are mutually
   RAM-exclusive and *are* meant to be down most of the time. Non-LLM services
   have the opposite semantics — mongod being down is always an incident.
   Sharing the registry would make "mongod is down" read as "stopped on
   purpose" and silence the very alert this module exists to raise.

Hence `expected_state`: it carries the distinction that (3) turns on. Only an
`on_demand` service may ever be reported healthy while stopped.

This module deliberately imports NO model-server *specs* — only the two generic
process probes, which have no LLM semantics. Keeping the registries disjoint is
enforced by tests (`test_service_registry.py::test_registries_are_disjoint`).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.config import settings

logger = logging.getLogger(__name__)

STATE_COLLECTION = "services"

ExpectedState = Literal["always_up", "on_demand"]

# States in which a unit/container is actually doing its job.
_LIVE_STATES = ("running", "active")


class ServiceError(Exception):
    """Base error for non-LLM service operations."""


class ServiceNotFound(ServiceError):
    pass


class ServiceNotManageable(ServiceError):
    """Raised when start/stop is attempted on a service ARIA must not drive."""


@dataclass(frozen=True)
class ServiceSpec:
    """A non-LLM service on this box.

    Deliberately absent, compared to `ModelServerSpec`: `runtime_repo`,
    `runtime_ref`, `backend_device`, `model_file`, `resident_gib`,
    `gtt_resident`, `exclusive_with`, `sleep_command`, `endpoint_override`,
    and agent binding. None of them mean anything for a database or a file
    share, and `resident_gib`/`exclusive_with` are precisely the fields that
    would make an entry look like a routable model to `llm_route`.
    """

    slug: str
    description: str
    # What "down" means. `always_up` → down is an incident that must reach the
    # alert cron. `on_demand` → down is normal and must never page.
    expected_state: ExpectedState
    # Rough grouping for the operator view; carries no behaviour.
    kind: str = "service"

    # Exactly one of these three addressing modes should be set.
    user_unit: Optional[str] = None  # systemd --user unit
    system_unit: Optional[str] = None  # system-wide systemd unit (e.g. smbd)
    container_name: Optional[str] = None  # `docker ps` name

    # Native macOS control-plane addressing.  The compatibility fields above
    # remain the Linux deployment description; when ARIA runs on Darwin these
    # take precedence so health never asks systemd/docker about launchd jobs.
    darwin_label: Optional[str] = None  # system LaunchDaemon label
    darwin_process: Optional[str] = None  # exact process name (pgrep -x)
    # Some Mac services are containers inside a Lima VM. A launchd label only
    # reports the VM wrapper and cannot distinguish two containers in the same
    # guest (mongod and mongot are exactly that case).
    darwin_lima_instance: Optional[str] = None
    darwin_lima_container: Optional[str] = None

    # Where it listens, when it listens anywhere. Used by the operator view and
    # by the disjointness test against the model-server registry.
    port: Optional[int] = None
    # Path appended to http://localhost:<port> for an HTTP liveness probe.
    # None = process-state only (correct for mongod, samba, tmux).
    health_path: Optional[str] = None

    # Compose provenance, for humans. ARIA does not shell out to compose here;
    # start/stop go through systemd or `docker start/stop` on the container.
    compose_file: Optional[str] = None
    service_name: Optional[str] = None

    # False = ARIA reports state but refuses start/stop. aria-api cannot
    # restart itself from inside its own request handler, and the system-level
    # units need root ARIA does not have.
    manageable: bool = True
    # Set when the expected_state above is an assumption rather than a
    # confirmed policy. Surfaced by `review_needed()` so it gets a human pass
    # instead of silently becoming ground truth — the exact failure mode that
    # rotted the old hand-written ontology seed list.
    needs_review: bool = False
    notes: Optional[str] = None
    depends_on: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# The registry.
#
# Built 2026-08-07 from OBSERVED live state (`docker ps -a`, `systemctl --user
# list-units`, `systemctl is-active`), not from documentation — the ontology
# doc's hand-written service list had already gone stale, and reading it into
# code would have laundered the same errors into a new place.
#
# `expected_state` is conservative on purpose. Where the policy was not
# obvious from the service's role, it is `on_demand` + `needs_review=True`:
# a missing page on a side service is recoverable, whereas a wrong
# `always_up` pages the Hermes triage cron every 10 minutes forever, which is
# how an alert channel gets ignored.
# ---------------------------------------------------------------------------
REGISTRY: tuple[ServiceSpec, ...] = (
    # --- Core data plane. ARIA cannot function without these. ---
    ServiceSpec(
        slug="shared-mongod",
        description="MongoDB 8.2, replica set rs0 — every ARIA collection.",
        expected_state="always_up",
        kind="datastore",
        container_name="shared-mongod",
        darwin_label="com.ben.devbox.lima-mongot",
        darwin_lima_instance="mongot",
        darwin_lima_container="devbox-mongod",
        port=27017,
        compose_file="infrastructure/docker-compose.yml",
        service_name="mongod",
        manageable=False,
        notes="Bound 127.0.0.1 only. Shared with AgentBenchPlatform — stopping "
        "it breaks both projects. The Lima VM is shared with mongot, but this "
        "row probes the devbox-mongod container itself.",
    ),
    ServiceSpec(
        slug="shared-mongot",
        description="MongoDB search sidecar (vector + BM25) behind mongod.",
        expected_state="always_up",
        kind="datastore",
        container_name="shared-mongot",
        darwin_label="com.ben.devbox.lima-mongot",
        darwin_lima_instance="mongot",
        darwin_lima_container="devbox-mongot",
        compose_file="infrastructure/docker-compose.yml",
        service_name="mongot",
        depends_on=("shared-mongod",),
        notes="No host port — reachable only through mongod. Probed via "
        "$listSearchIndexes, not a socket. ARIA's use of it is gated by the "
        "`search` retrieval capability (memory/capabilities.py): with that "
        "switch off, recall degrades to a mongod-native scan and health stops "
        "probing this row, so a stopped container is not an incident. "
        "SWITCHED OFF 2026-09-03 and the devbox-mongot container is stopped. "
        "The Lima VM stays up because devbox-mongod shares it. See "
        "docs/ops/RETRIEVAL_CAPABILITIES.md.",
    ),
    ServiceSpec(
        slug="shared-embeddings",
        description="voyage-4-nano (1024-dim MRL) via sentence-transformers, CPU.",
        expected_state="always_up",
        kind="service",
        container_name="shared-embeddings",
        darwin_label="com.ben.devbox.embeddings",
        port=8001,
        health_path="/health",
        compose_file="infrastructure/docker-compose.yml",
        service_name="embeddings",
        notes="Memory store/recall no longer BLOCK on this (they did before "
        "2026-08-15): with the `embeddings` retrieval capability off, writes "
        "land flagged embedding_pending and are re-embedded by the backfill "
        "worker on re-enable, while recall degrades to lexical/fallback. "
        "STOPPED + SWITCHED OFF 2026-08-15. expected_state stays always_up "
        "because that is still the normal policy — health consults the "
        "capability switch, not this field, to tell 'off on purpose' apart "
        "from 'down'. See docs/ops/RETRIEVAL_CAPABILITIES.md.",
    ),
    # --- ARIA itself. ---
    ServiceSpec(
        slug="aria-api",
        description="ARIA FastAPI backend on :8200 (launchd on the Mac control plane).",
        expected_state="always_up",
        kind="app",
        user_unit="aria-api.service",
        darwin_label="com.ben.devbox.aria-api",
        port=8200,
        health_path="/health",
        manageable=False,
        notes="Not manageable from here — it would be restarting itself from "
        "inside its own request handler. Use the host service manager.",
    ),
    ServiceSpec(
        slug="aria-tmux",
        description="Owns the tmux server hosting every watched claude-* session.",
        expected_state="always_up",
        kind="app",
        user_unit="aria-tmux.service",
        darwin_process="tmux",
        manageable=False,
        notes="LOAD-BEARING: if this dies and aria-api respawns the tmux "
        "server, the server lands in aria-api's cgroup and the next "
        "`systemctl restart aria-api` kills every watched session. Reports "
        "'active (exited)' by design — it is a oneshot that leaves the server "
        "behind. Never start/stop from here.",
    ),
    ServiceSpec(
        slug="aria-ui",
        description="Next.js web UI on :3000 (cockpit, operate, health pages).",
        expected_state="always_up",
        kind="app",
        container_name="aria-ui",
        darwin_label="com.ben.devbox.aria-ui",
        port=3000,
        compose_file="ProjectAria/docker-compose.yml",
        service_name="ui",
    ),
    ServiceSpec(
        slug="shared-tts",
        description="Qwen3-TTS 0.6B speech synthesis (CPU) on :8002.",
        expected_state="always_up",
        kind="service",
        container_name="shared-tts",
        darwin_label="com.ben.devbox.tts",
        port=8002,
        health_path="/health",
        compose_file="ProjectAria/docker-compose.yml",
        service_name="tts",
    ),
    ServiceSpec(
        slug="aria-stt",
        description="whisper-large-v3-turbo transcription (CPU, int8) on :8003.",
        expected_state="on_demand",
        kind="service",
        container_name="aria-stt",
        port=8003,
        health_path="/health",
        compose_file="ProjectAria/docker-compose.yml",
        service_name="stt",
        needs_review=True,
        notes="Observed EXITED for 7 days as of 2026-08-07 while health.py "
        "probed it unconditionally — i.e. it has been quietly counted "
        "unhealthy for a week. Classified on_demand so that stops being noise; "
        "flip to always_up if transcription is meant to be continuously "
        "available.",
    ),
    # --- Hermes: the conversational front door. ---
    ServiceSpec(
        slug="hermes-gateway",
        description="Hermes agent gateway — the sole conversational path to ARIA.",
        expected_state="always_up",
        kind="app",
        user_unit="hermes-gateway.service",
        darwin_label="com.ben.devbox.hermes-gateway",
        depends_on=("aria-api",),
        notes="Hosts the aria MCP connection. Restart after editing mcp/server.py.",
    ),
    ServiceSpec(
        slug="hermes-webui",
        description="Hermes browser/mobile UI backend (hermex app).",
        # on_demand since 2026-08-15: the unit is `disabled` AND `inactive`, and
        # has been for weeks — Ben reaches Hermes over Signal, not this UI. While
        # it was declared always_up, every health tick counted it as an incident,
        # which is precisely the noise that trains a human to ignore the alert
        # queue. Declaring the truth is the fix; start it and flip this back if
        # the browser UI ever becomes part of the workflow.
        expected_state="on_demand",
        kind="app",
        user_unit="hermes-webui.service",
        depends_on=("hermes-gateway",),
    ),
    ServiceSpec(
        slug="signal-cli",
        description="signal-cli JSON-RPC daemon on 127.0.0.1:8090 — Hermes's Signal transport.",
        expected_state="always_up",
        kind="integration",
        user_unit="signal-cli.service",
        darwin_label="com.ben.devbox.signal-cli",
        port=8090,
        notes="Alert triage and the Signal→Linear capture path both die quietly "
        "without this.",
    ),
    # --- Proxies to other machines. ---
    ServiceSpec(
        slug="ridge-llama-proxy",
        description="Mac → Ridge LLM proxy on :8092 (WoL wake + OpenAI passthrough).",
        expected_state="always_up",
        kind="proxy",
        user_unit="ridge-llama-proxy.service",
        darwin_label="com.ben.devbox.ridge-proxy",
        port=8092,
        notes="Bound on the TAILNET IP ONLY — localhost:8092 is "
        "connection-refused even though `ss` shows a listener. Repeatedly "
        "misdiagnosed; do not 'fix'. The `ridge` backend depends on it.",
    ),
    ServiceSpec(
        slug="red-proxy",
        description="Mac → RED inference proxy, game-facing OpenAI endpoint on :8094.",
        expected_state="on_demand",
        kind="proxy",
        user_unit="red-proxy.service",
        darwin_label="com.ben.devbox.red-proxy",
        port=8094,
        needs_review=True,
        notes="Running as of 2026-08-07, but whether it is meant to be "
        "continuously up is unconfirmed.",
    ),
    ServiceSpec(
        slug="ridge-waker",
        description="Ridge TTS waker shim (WoL proxy for Railway over Tailscale).",
        expected_state="on_demand",
        kind="proxy",
        user_unit="ridge-waker.service",
        darwin_label="com.ben.devbox.wake-relay",
        needs_review=True,
    ),
    # --- Integrations and side services. ---
    ServiceSpec(
        slug="obsidian-livesync-bridge",
        description="Obsidian LiveSync bridge — propagates vault writes to every device.",
        # RECLASSIFIED 2026-08-19: on_demand -> always_up.
        #
        # The 2026-08-07 note said "C6 writes land in the vault regardless;
        # this only syncs them outward", which was true then. It stopped being
        # true on 2026-08-15, when the vault became the APPROVAL SURFACE (D10):
        # `approval:` on STEWARD_PLAN.md and `autonomy:` on CHARTER.md are
        # control inputs that reach ARIA only if this bridge is moving bytes.
        # A control channel whose failure "must never page" is not a control
        # channel.
        expected_state="always_up",
        kind="integration",
        container_name="obsidian-livesync-bridge-bridge-1",
        darwin_label="com.ben.devbox.obsidian-bridge",
        needs_review=False,
        notes="Carries Ben's approval/autonomy edits back to ARIA (D10), so a "
        "stop is an incident. ⚠️ Container state is NOT sufficient evidence "
        "this works: on 2026-08-17 the container stayed up while its "
        "`corsair-files` peer had died at startup on an EACCES, and sync was "
        "dead for two days with every check green. The functional check is the "
        "`vault` probe in shells/selfcheck.py, which tests the cause (a vault "
        "file the bridge's uid cannot read) rather than the symptom.",
    ),
    ServiceSpec(
        slug="samba",
        description="Samba file sharing (smbd) — LAN/NAS shares.",
        expected_state="always_up",
        kind="service",
        system_unit="smbd.service",
        manageable=False,
        notes="System-level unit; ARIA has no root, so it reports state only.",
    ),
    ServiceSpec(
        slug="war-audio-game",
        description="War Audio Game API (local Qwen models).",
        expected_state="on_demand",
        kind="app",
        user_unit="war-audio-game.service",
        needs_review=True,
        notes="A project service, not ARIA infrastructure. Running as of "
        "2026-08-07.",
    ),
    ServiceSpec(
        slug="war-audio-model-download",
        description="War Audio Game model download server.",
        expected_state="on_demand",
        kind="app",
        user_unit="war-audio-model-download.service",
        needs_review=True,
    ),
    ServiceSpec(
        slug="ts-drop-capture",
        description="Tailscale SSH drop capture (passive path + session state).",
        expected_state="on_demand",
        kind="integration",
        user_unit="ts-drop-capture.service",
        needs_review=True,
    ),
)

_BY_SLUG: dict[str, ServiceSpec] = {spec.slug: spec for spec in REGISTRY}


def get_spec(slug: str) -> ServiceSpec:
    spec = _BY_SLUG.get(slug)
    if spec is None:
        raise ServiceNotFound(f"Unknown service: {slug}")
    return spec


def review_needed() -> list[ServiceSpec]:
    """Specs whose `expected_state` is an assumption awaiting a human call."""
    return [s for s in REGISTRY if s.needs_review]


# ---------------------------------------------------------------------------
# Process probes
# ---------------------------------------------------------------------------


async def _run(*args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a command; never raise on a missing binary or a hang.

    Deliberately total: this module is consulted by the health endpoint, and a
    probe that raises would take the whole health page down with it.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        return 127, "", str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"{args[0]} timed out after {timeout}s"
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _unit_state(unit: str, *, user: bool) -> str:
    """Normalised state for a systemd unit, in docker's vocabulary.

    `active (exited)` maps to running: a oneshot like aria-tmux has done its
    job and left the tmux server behind, so calling it 'exited' would report a
    healthy, load-bearing unit as down.
    """
    scope = ("--user",) if user else ()
    rc, out, _ = await _run("systemctl", *scope, "list-unit-files", unit)
    if rc != 0 or unit not in out:
        return "not_created"
    _, out, _ = await _run("systemctl", *scope, "is-active", unit)
    state = out.strip()
    if state == "active":
        return "running"
    if state == "failed":
        return "failed"
    if state in ("activating", "reloading", "deactivating"):
        return "restarting"
    return "stopped"


async def _container_state(name: str) -> str:
    rc, out, err = await _run(
        "docker", "inspect", "--format", "{{.State.Status}}", name
    )
    if rc != 0:
        lowered = (err or out).lower()
        if "no such object" in lowered or "no such container" in lowered:
            return "not_created"
        # Daemon down: report it rather than conflating with "not created",
        # which would make every container look deliberately absent.
        return "unknown"
    status = out.strip()
    return "running" if status == "running" else status or "unknown"


async def _launchd_state(label: str) -> str:
    """Return a launchd system job's state in the registry vocabulary."""
    rc, out, err = await _run("launchctl", "print", f"system/{label}")
    if rc != 0:
        lowered = (err or out).lower()
        if "could not find service" in lowered or "not found" in lowered:
            return "not_created"
        return "unknown"

    for raw in out.splitlines():
        line = raw.strip()
        if not line.startswith("state ="):
            continue
        state = line.split("=", 1)[1].strip()
        if state == "running":
            return "running"
        if state in ("waiting", "exited"):
            return "stopped"
        return state or "unknown"
    # A loaded launchd job without a state line is present but not executing.
    return "stopped"


async def _process_state(name: str) -> str:
    rc, out, _ = await _run("pgrep", "-x", name)
    if rc == 0 and out.strip():
        return "running"
    if rc == 1:
        return "stopped"
    return "unknown"


async def _lima_container_state(instance: str, name: str) -> str:
    """Return one container's state without conflating it with its Lima VM."""
    limactl = "/opt/homebrew/bin/limactl"
    rc, out, _ = await _run(limactl, "list", instance, "--format", "{{.Status}}")
    if rc != 0:
        return "unknown"
    if out.strip().lower() != "running":
        return "stopped"
    rc, out, err = await _run(
        limactl,
        "shell",
        instance,
        "docker",
        "inspect",
        "--format",
        "{{.State.Status}}",
        name,
    )
    if rc != 0:
        lowered = (err or out).lower()
        if "no such object" in lowered or "no such container" in lowered:
            return "not_created"
        return "unknown"
    status = out.strip()
    return "running" if status == "running" else status or "unknown"


async def _darwin_fleet_states(specs: tuple[ServiceSpec, ...]) -> dict[str, str]:
    """Read launchd once and each Lima guest once for the full Mac roster."""
    states = {
        spec.slug: "not_applicable"
        for spec in specs
        if not (spec.darwin_label or spec.darwin_process)
    }

    launchd_specs = [
        spec for spec in specs
        if spec.darwin_label and not spec.darwin_lima_container
    ]
    labels = {spec.darwin_label for spec in launchd_specs}
    rc, out, _ = await _run("launchctl", "print", "system")
    if rc == 0:
        by_label: dict[str, str] = {}
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[-1] in labels and fields[0].isdigit():
                by_label[fields[-1]] = "running" if int(fields[0]) > 0 else "stopped"
        for spec in launchd_specs:
            states[spec.slug] = by_label.get(spec.darwin_label or "", "not_created")
    else:
        fallback = await asyncio.gather(
            *(_launchd_state(spec.darwin_label or "") for spec in launchd_specs)
        )
        states.update({spec.slug: state for spec, state in zip(launchd_specs, fallback)})

    process_specs = [spec for spec in specs if spec.darwin_process]
    process_states = await asyncio.gather(
        *(_process_state(spec.darwin_process or "") for spec in process_specs)
    )
    states.update({spec.slug: state for spec, state in zip(process_specs, process_states)})

    lima_groups: dict[str, list[ServiceSpec]] = {}
    for spec in specs:
        if spec.darwin_lima_instance and spec.darwin_lima_container:
            lima_groups.setdefault(spec.darwin_lima_instance, []).append(spec)
    for instance, group in lima_groups.items():
        limactl = "/opt/homebrew/bin/limactl"
        rc, out, _ = await _run(limactl, "list", instance, "--format", "{{.Status}}")
        if rc != 0:
            states.update({spec.slug: "unknown" for spec in group})
            continue
        if out.strip().lower() != "running":
            states.update({spec.slug: "stopped" for spec in group})
            continue
        names = [spec.darwin_lima_container or "" for spec in group]
        rc, out, err = await _run(
            limactl, "shell", instance, "docker", "inspect", "--format",
            "{{.Name}} {{.State.Status}}", *names,
        )
        by_name: dict[str, str] = {}
        for line in out.splitlines():
            name, _, state = line.strip().partition(" ")
            if name and state:
                by_name[name.removeprefix("/")] = state
        for spec in group:
            name = spec.darwin_lima_container or ""
            if name in by_name:
                state = by_name[name]
                states[spec.slug] = "running" if state == "running" else state
            else:
                lowered = (err or "").lower()
                states[spec.slug] = (
                    "not_created" if rc != 0 and "no such" in lowered else "unknown"
                )
    return states


async def _state_of(spec: ServiceSpec) -> str:
    if sys.platform == "darwin":
        if spec.darwin_lima_instance and spec.darwin_lima_container:
            return await _lima_container_state(
                spec.darwin_lima_instance, spec.darwin_lima_container
            )
        if spec.darwin_label:
            return await _launchd_state(spec.darwin_label)
        if spec.darwin_process:
            return await _process_state(spec.darwin_process)
        # Linux-only compatibility and historical project services are not
        # incidents on the Mac control plane.
        return "not_applicable"
    if spec.user_unit:
        return await _unit_state(spec.user_unit, user=True)
    if spec.system_unit:
        return await _unit_state(spec.system_unit, user=False)
    if spec.container_name:
        return await _container_state(spec.container_name)
    return "unwired"


def is_healthy(state: str, expected: ExpectedState) -> bool:
    """The whole point of the module, in one function.

    An `always_up` service is healthy only when live. An `on_demand` one is
    healthy unless it has actively failed — being stopped is its normal
    resting state and must never page.
    """
    if state in _LIVE_STATES:
        return True
    if state == "not_applicable":
        return True
    if expected == "on_demand":
        return state != "failed"
    return False


def _effective_expected_state(spec: ServiceSpec) -> ExpectedState:
    """Let retrieval service expectations follow their runtime switches."""
    if spec.slug not in {"shared-mongot", "shared-embeddings"}:
        return spec.expected_state
    # Lazy import avoids making the infrastructure registry part of memory's
    # import graph. The singleton is loaded from Mongo during API startup.
    from aria.memory.capabilities import retrieval_capabilities

    enabled = (
        retrieval_capabilities.search_enabled
        if spec.slug == "shared-mongot"
        else retrieval_capabilities.embeddings_enabled
    )
    return "always_up" if enabled else "on_demand"


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


def _row_for(spec: ServiceSpec, state: str) -> dict:
    """One service's status row. Shared by the full status() sweep and the
    single-spec get() so the two views cannot drift."""
    darwin = sys.platform == "darwin"
    if darwin:
        if spec.darwin_lima_instance and spec.darwin_lima_container:
            handle = f"lima:{spec.darwin_lima_instance}/{spec.darwin_lima_container}"
        else:
            handle = spec.darwin_label or (
                f"process:{spec.darwin_process}" if spec.darwin_process else None
            )
    else:
        handle = spec.user_unit or spec.system_unit

    expected_state = _effective_expected_state(spec)
    return {
        "slug": spec.slug,
        "description": spec.description,
        "kind": spec.kind,
        "state": state,
        "expected_state": expected_state,
        "healthy": is_healthy(state, expected_state),
        "port": spec.port,
        # Plain launchd mutation is intentionally not implemented here. Lima
        # guest containers have a precise non-root lifecycle handle.
        "manageable": spec.manageable and (
            not darwin or bool(spec.darwin_lima_instance and spec.darwin_lima_container)
        ),
        "needs_review": spec.needs_review,
        "notes": spec.notes,
        "depends_on": list(spec.depends_on),
        "unit": handle,
        "container": None if darwin else spec.container_name,
        "compose_file": None if darwin else spec.compose_file,
        "service_name": None if darwin else spec.service_name,
    }


class ServiceManager:
    """Status/start/stop for the non-LLM services.

    No exclusivity gate, no RAM projection, no agent binding — those exist in
    the model-server registry because GPU memory is a scarce shared resource.
    A database and a file share have none of those constraints, and importing
    the machinery would be the first step back toward one merged registry.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def status(self, db: Optional[AsyncIOMotorDatabase] = None) -> list[dict]:
        """Every registered service with its live state and health verdict."""
        if sys.platform == "darwin":
            state_by_slug = await _darwin_fleet_states(REGISTRY)
            states = [state_by_slug[spec.slug] for spec in REGISTRY]
        else:
            states = await asyncio.gather(*(_state_of(spec) for spec in REGISTRY))
        results = [_row_for(spec, state) for spec, state in zip(REGISTRY, states)]
        if db is not None:
            await self._record(db, results)
        return results

    async def _record(self, db: AsyncIOMotorDatabase, results: list[dict]) -> None:
        """Persist last-observed state so the ontology can project services
        without shelling out, and so a restart doesn't lose the roster."""
        now = datetime.now(timezone.utc)
        async def persist(entry: dict) -> None:
            try:
                await db[STATE_COLLECTION].update_one(
                    {"_id": entry["slug"]},
                    {
                        "$set": {
                            **{k: v for k, v in entry.items() if k != "slug"},
                            "last_seen_at": now,
                        },
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                )
            except Exception as exc:  # noqa: BLE001 — persistence is advisory
                logger.debug("service state persist failed for %s: %s", entry["slug"], exc)

        # Each row is independent and Mongo's pool is deliberately wider than
        # this registry. Serial upserts added one network round trip per service
        # to the most-polled dashboard endpoint (~5s on the Mac under load).
        await asyncio.gather(*(persist(entry) for entry in results))

    async def get(self, slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> dict:
        """One service's live state — probing ONLY that spec.

        The old implementation ran the full status() sweep (every service
        probed, every row upserted) to answer a question about one; a
        single-entity read must not pay the full-fleet cost.
        """
        spec = get_spec(slug)  # raises ServiceNotFound
        state = await _state_of(spec)
        entry = _row_for(spec, state)
        if db is not None:
            await self._record(db, [entry])
        return entry

    async def start(self, slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> dict:
        return await self._transition(slug, "start", db)

    async def stop(self, slug: str, db: Optional[AsyncIOMotorDatabase] = None) -> dict:
        return await self._transition(slug, "stop", db)

    async def _transition(
        self, slug: str, action: str, db: Optional[AsyncIOMotorDatabase]
    ) -> dict:
        spec = get_spec(slug)
        if not spec.manageable:
            raise ServiceNotManageable(
                f"{slug} is not manageable from ARIA: {spec.notes or 'see registry notes'}"
            )

        async with self._lock:
            state = await _state_of(spec)
            if action == "start" and state in _LIVE_STATES:
                return {"slug": slug, "state": state, "action": "noop"}
            if action == "stop" and state not in _LIVE_STATES:
                return {"slug": slug, "state": state, "action": "noop"}

            if sys.platform == "darwin" and (
                spec.darwin_lima_instance and spec.darwin_lima_container
            ):
                rc, out, err = await _run(
                    "/opt/homebrew/bin/limactl",
                    "shell",
                    spec.darwin_lima_instance,
                    "docker",
                    action,
                    spec.darwin_lima_container,
                )
            elif spec.user_unit:
                rc, out, err = await _run("systemctl", "--user", action, spec.user_unit)
            elif spec.container_name:
                rc, out, err = await _run("docker", action, spec.container_name)
            else:
                raise ServiceNotManageable(f"{slug} has no manageable handle.")

            if rc != 0:
                raise ServiceError(f"{action} {slug} failed: {(err or out).strip()[:300]}")

            new_state = await _state_of(spec)
            result = {
                "slug": slug,
                "state": new_state,
                "action": action + "ed",
                "output": (out + err)[-2000:],
            }
            if db is not None:
                await self._record(db, await self.status(None))
            return result


_manager: Optional[ServiceManager] = None


def get_service_manager() -> ServiceManager:
    global _manager
    if _manager is None:
        _manager = ServiceManager()
    return _manager
