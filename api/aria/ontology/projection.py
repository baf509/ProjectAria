"""
ARIA - Ontology projection

Phase: Ontology Memory Map · Phase 2 (§4a) + Phase 5a
Purpose: Derive `project` / `service` / `machine` entities from the collections
that already own those facts, so the graph cannot go stale independently.

Related Spec Sections:
- ONTOLOGY_MEMORY_DESIGN.md §4a (projected entities), §4b (durable seed),
  §4c (the non-LLM service registry this reads)

THE RULE: project what churns, hand-author what doesn't.

  project  <- db.projects                    (C4 ProjectHarvestWorker owns it)
  service  <- infrastructure/services.py     (non-LLM, Phase 0)
             + infrastructure/model_servers.py (the LLM control plane)
  machine  <- db.nodes                       (remote aria-node registrations)

Nothing here invents a fact. If a service disappears from its registry, its
entity goes `stale` (never deleted, per S3) on the next run instead of sitting
in the graph asserting something false.

`db.nodes` holds only REMOTE nodes — corsair-ai itself is not in it — so the
local host comes from the durable seed (§4b), not from here.
"""

from __future__ import annotations

import logging
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.ontology.models import entity_slug, project_entity_slug, project_roots
from aria.ontology.seed import SEED_ENTITIES, SEED_RELATIONS
from aria.ontology.store import OntologyStore
from aria.shared.review import add_review_item

logger = logging.getLogger(__name__)

LOCAL_HOST_SLUG = "machine:corsair-ai"

# Actor labels double as provenance in `source.<field>.actor` and as the
# ownership key for mark_missing_stale.
ACTOR_SEED = "ontology-seed"
ACTOR_PROJECT = "projection:projects"
ACTOR_SERVICE = "projection:services"
ACTOR_MODEL_SERVER = "projection:model-servers"
ACTOR_NODE = "projection:nodes"


class OntologyProjector:
    """Rebuilds the derived half of the graph from live sources."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.store = OntologyStore(db)

    async def run_all(self, *, embed: bool = True) -> dict:
        """Full projection pass. Idempotent — safe to run on every boot and on
        every scan tick."""
        await self.store.ensure_indexes()
        result = {
            "seed": await self.project_seed(embed=embed),
            "projects": await self.project_projects(embed=embed),
            "services": await self.project_services(embed=embed),
            "model_servers": await self.project_model_servers(embed=embed),
            "nodes": await self.project_nodes(embed=embed),
        }
        result["counts"] = await self.store.counts()
        return result

    # -- §4b durable seed ---------------------------------------------------

    async def project_seed(self, *, embed: bool = True) -> dict:
        """Upsert the hand-authored entities.

        Written with `worker=False` so the prose in seed.py lands in the
        protected fields on first run — but only ONCE per field: because
        upsert_entity records provenance, a later hand-edit through the API or
        `kg` is not clobbered by the next boot. The seed is a starting point,
        not a recurring overwrite.
        """
        written, skipped = [], []
        for spec in SEED_ENTITIES:
            existing = await self.store.get_entity(spec["slug"])
            if existing and (existing.get("source") or {}).get("summary", {}).get(
                "actor"
            ) not in (None, ACTOR_SEED):
                # A human or agent has since curated this entity — leave it be.
                skipped.append(spec["slug"])
                continue
            await self.store.upsert_entity(
                spec["slug"],
                entity_type=spec["type"],
                name=spec.get("name"),
                summary=spec.get("summary"),
                aliases=list(spec.get("aliases") or []),
                tags=list(spec.get("tags") or []),
                attributes=dict(spec.get("attributes") or {}),
                actor=ACTOR_SEED,
                worker=False,
                embed=embed,
            )
            written.append(spec["slug"])

        edges = 0
        for rel in SEED_RELATIONS:
            await self.store.upsert_relation(
                rel["subject"],
                rel["predicate"],
                rel["object"],
                attributes=rel.get("attributes"),
                actor=ACTOR_SEED,
            )
            edges += 1
        return {"entities": len(written), "skipped_curated": len(skipped), "relations": edges}

    # -- §4a projects -------------------------------------------------------

    async def project_projects(self, *, embed: bool = False) -> dict:
        """db.projects -> `project` entities.

        The C4 harvester already owns path/activity_status/language and keeps
        them current; re-entering any of that by hand would fork an
        authoritative source. Note `summary` is NOT written here — a project's
        prose is Ben's to write, and the harvester has none.
        """
        seen: set[str] = set()
        edges = 0
        roots_seen: dict[str, str] = {}
        async for doc in self.db.projects.find({}):
            if not (doc.get("slug") or doc.get("name")):
                continue
            slug = project_entity_slug(doc)
            roots = project_roots(doc)
            attributes = {
                k: v
                for k, v in {
                    # `path` falls back to the first relevant_path: the
                    # harvested "ARIA" row has no `path` at all, and a
                    # path-only read left it with no location and no host edge.
                    "path": doc.get("path") or (roots[0] if roots else None),
                    "relevant_paths": roots or None,
                    "language": doc.get("language"),
                    "activity_status": doc.get("activity_status"),
                    "status": doc.get("status"),
                    "git_remote": doc.get("git_remote"),
                    "deploy_target": doc.get("deploy_target"),
                }.items()
                if v is not None
            }
            await self.store.upsert_entity(
                slug,
                entity_type="project",
                name=doc.get("name") or slug,
                attributes=attributes,
                actor=ACTOR_PROJECT,
                worker=True,
                embed=embed,
            )
            seen.add(slug)
            # A project with any local root lives on this box.
            if roots:
                await self.store.upsert_relation(
                    slug, "runs_on", LOCAL_HOST_SLUG, actor=ACTOR_PROJECT
                )
                edges += 1

            # Two projects claiming the same root is a real condition in this
            # data (`ARIA` and `ProjectAria` both own ~/Development/ProjectAria),
            # and it silently splits a project's memories across two entities.
            # Surface it rather than picking a winner behind Ben's back.
            for root in roots:
                if root in roots_seen and roots_seen[root] != slug:
                    await add_review_item(
                        self.db,
                        kind="conflict",
                        subject=root,
                        detail=(
                            f"projects '{roots_seen[root]}' and '{slug}' both claim "
                            f"{root}; memories about it split between them."
                        ),
                    )
                else:
                    roots_seen[root] = slug

        stale = await self.store.mark_missing_stale("project", seen, actor=ACTOR_PROJECT)
        return {"entities": len(seen), "relations": edges, "stale": len(stale)}

    # -- §4a services (non-LLM, Phase 0 registry) ---------------------------

    async def project_services(self, *, embed: bool = False) -> dict:
        """infrastructure/services.py -> `service` entities.

        Reads the registry rather than the `services` collection so a projection
        works even before the first ServiceManager.status() call has persisted
        anything.
        """
        from aria.infrastructure.services import REGISTRY, ServiceManager

        try:
            live = {s["slug"]: s for s in await ServiceManager().status(None)}
        except Exception as exc:  # noqa: BLE001 — live state is a bonus, not required
            logger.debug("service projection: live state unavailable: %s", exc)
            live = {}

        seen: set[str] = set()
        edges = 0
        for spec in REGISTRY:
            slug = entity_slug("service", spec.slug)
            observed = live.get(spec.slug, {})
            attributes = {
                k: v
                for k, v in {
                    "port": spec.port,
                    "unit": spec.user_unit or spec.system_unit,
                    "container": spec.container_name,
                    "compose_file": spec.compose_file,
                    "service_name": spec.service_name,
                    "kind": spec.kind,
                    "expected_state": spec.expected_state,
                    "manageable": spec.manageable,
                    "lifecycle": observed.get("state"),
                    "healthy": observed.get("healthy"),
                }.items()
                if v is not None
            }
            await self.store.upsert_entity(
                slug,
                entity_type="service",
                name=spec.slug,
                attributes=attributes,
                actor=ACTOR_SERVICE,
                worker=True,
                embed=embed,
            )
            seen.add(slug)

            await self.store.upsert_relation(
                slug, "runs_on", LOCAL_HOST_SLUG, actor=ACTOR_SERVICE
            )
            edges += 1
            for dep in spec.depends_on:
                await self.store.upsert_relation(
                    slug, "depends_on", entity_slug("service", dep), actor=ACTOR_SERVICE
                )
                edges += 1

        # The data plane every ARIA service actually sits on. Derived, not
        # typed by hand — the old seed's `aria-api --depends_on--> qwen-agentic`
        # was false by the time it was written.
        await self.store.upsert_relation(
            entity_slug("service", "shared-mongod"),
            "stores_in",
            "datastore:aria-db",
            actor=ACTOR_SERVICE,
        )
        edges += 1

        stale = await self.store.mark_missing_stale("service", seen, actor=ACTOR_SERVICE)
        return {"entities": len(seen), "relations": edges, "stale": len(stale)}

    # -- §4a services (LLM control plane) -----------------------------------

    async def project_model_servers(self, *, embed: bool = False) -> dict:
        """model_servers.REGISTRY -> `service` entities.

        Kept in the same `service` type but under a distinct actor, so the two
        registries stay separately owned (and separately stale-able) while a
        `kg map --type service` still shows the whole picture.
        """
        from aria.infrastructure.model_servers import REGISTRY as MODEL_SERVERS

        seen: set[str] = set()
        edges = 0
        for spec in MODEL_SERVERS:
            slug = entity_slug("service", spec.slug)
            attributes = {
                k: v
                for k, v in {
                    "port": spec.port,
                    "kind": "model-server",
                    "backend_device": spec.backend_device,
                    "model_file": spec.model_file,
                    "resident_gib": spec.resident_gib,
                    "onbox": spec.onbox,
                    "startable": spec.startable,
                    "container": spec.container_name,
                    "unit": spec.systemd_unit,
                    "exclusive_with": list(spec.exclusive_with) or None,
                }.items()
                if v is not None
            }
            await self.store.upsert_entity(
                slug,
                entity_type="service",
                name=spec.slug,
                attributes=attributes,
                actor=ACTOR_MODEL_SERVER,
                worker=True,
                embed=embed,
            )
            seen.add(slug)
            # Off-box servers run somewhere else — saying they run on corsair
            # would be exactly the kind of false edge this rewrite is meant to
            # prevent. But hardcoding `machine:ridge` for every off-box server
            # was the same mistake in the other direction: once RED was declared
            # (2026-08-15) the graph asserted that RED's model server runs on
            # Ridge. The host is now declared per spec, and an off-box entry
            # that forgets to declare one gets NO edge rather than a wrong one —
            # a missing edge is visibly incomplete, a wrong edge reads as fact.
            host = LOCAL_HOST_SLUG if spec.onbox else spec.host_machine
            if not host:
                logger.warning(
                    "ontology: %s is off-box with no host_machine declared; "
                    "skipping its runs_on edge rather than guessing",
                    spec.slug,
                )
                continue
            await self.store.upsert_relation(slug, "runs_on", host, actor=ACTOR_MODEL_SERVER)
            edges += 1

        stale = await self.store.mark_missing_stale(
            "service", seen, actor=ACTOR_MODEL_SERVER
        )
        return {"entities": len(seen), "relations": edges, "stale": len(stale)}

    # -- §4a machines (remote nodes only) -----------------------------------

    async def project_nodes(self, *, embed: bool = False) -> dict:
        """db.nodes -> `machine` entities for REMOTE nodes.

        corsair-ai is deliberately absent from db.nodes (it is the local host,
        identified by settings.local_node_id), so it comes from the durable
        seed instead. Projecting it from here would produce nothing.
        """
        seen: set[str] = set()
        edges = 0
        async for doc in self.db.nodes.find({}):
            node_id = doc.get("_id") or doc.get("node_id")
            if not node_id:
                continue
            slug = entity_slug("machine", str(node_id))
            attributes = {
                k: v
                for k, v in {
                    "hostname": doc.get("hostname"),
                    "os": doc.get("os"),
                    "arch": doc.get("arch"),
                    "tailscale_ip": doc.get("tailscale_ip"),
                    "last_seen": doc.get("last_seen"),
                    "status": doc.get("status"),
                }.items()
                if v is not None
            }
            await self.store.upsert_entity(
                slug,
                entity_type="machine",
                name=str(node_id),
                attributes=attributes,
                actor=ACTOR_NODE,
                worker=True,
                embed=embed,
            )
            seen.add(slug)
            await self.store.upsert_relation(
                slug, "member_of", "network:tailnet", actor=ACTOR_NODE
            )
            await self.store.upsert_relation(
                slug, "client_of", LOCAL_HOST_SLUG, actor=ACTOR_NODE
            )
            edges += 2

        stale = await self.store.mark_missing_stale("machine", seen, actor=ACTOR_NODE)
        return {"entities": len(seen), "relations": edges, "stale": len(stale)}


async def run_projection(
    db: AsyncIOMotorDatabase, *, embed: bool = True
) -> dict:
    """Module-level entry point used by the API, the CLI and the scan emitter."""
    return await OntologyProjector(db).run_all(embed=embed)
