"""
ARIA - Ontology durable seed

Phase: Ontology Memory Map · Phase 2 (§4b)
Purpose: The ~14 entities that have no authoritative collection and change on
a scale of months. Everything else is PROJECTED (see projection.py).

Related Spec Sections:
- ONTOLOGY_MEMORY_DESIGN.md §4b (hand-authored durable entities), §4 (the rule)

WHY THIS FILE IS SHORT
======================
The original plan hand-seeded ~40 entities including every service. Three
weeks later that list named two retired qwen containers, a dead Fireworks
account, and a slot topology that no longer existed — while missing chadrock,
gemma-aux, and the model-server registry entirely. Hand-written service lists
rot; physical machines do not.

So this file holds only what genuinely cannot be observed from a collection:
Ben's machines, his handhelds, the networks, the backup target, and Ben. If
you are tempted to add a service here, add it to
`aria/infrastructure/services.py` instead and let projection.py derive it.
"""

from __future__ import annotations

from typing import TypedDict


class SeedEntity(TypedDict, total=False):
    slug: str
    type: str
    name: str
    summary: str
    aliases: list[str]
    tags: list[str]
    attributes: dict


class SeedRelation(TypedDict, total=False):
    subject: str
    predicate: str
    object: str
    attributes: dict


# --- §4b durable entities --------------------------------------------------
SEED_ENTITIES: tuple[SeedEntity, ...] = (
    {
        "slug": "machine:corsair-ai",
        "type": "machine",
        "name": "corsair-ai",
        "summary": (
            "The always-on Linux GPU box. Runs ARIA, Hermes, the shared "
            "infrastructure, and every local model server. Unified-memory AMD "
            "box: GPU allocations come out of the GTT pool, which is why "
            "docker mem_limit does not constrain the model servers."
        ),
        "aliases": ["corsair", "corsair-ai.local"],
        "tags": ["primary", "gpu", "always-on"],
        "attributes": {"os": "Linux", "role": "primary-host"},
    },
    {
        "slug": "machine:nas",
        "type": "machine",
        "name": "nas",
        "summary": "Network-attached storage; restic backup target and media store.",
        "tags": ["storage"],
        "attributes": {"role": "storage"},
    },
    {
        "slug": "machine:red",
        "type": "machine",
        "name": "red",
        "summary": "Windows machine; reached over the tailnet. Inference target behind red-proxy.",
        "tags": ["windows"],
        "attributes": {"os": "Windows", "role": "inference"},
    },
    {
        "slug": "machine:ridge",
        "type": "machine",
        "name": "ridge",
        "summary": (
            "Windows gaming PC with an RTX 3090, used as an off-box inference "
            "node. Sleeps when idle and is woken by Wake-on-LAN through "
            "ridge-llama-proxy, so it is deliberately never health-probed."
        ),
        "tags": ["windows", "gpu", "sleeps"],
        "attributes": {"os": "Windows", "gpu": "RTX 3090", "role": "inference"},
    },
    {
        "slug": "device:odin2-sm8550",
        "type": "device",
        "name": "odin2-sm8550",
        "summary": "AYN Odin 2 Android handheld (SM8550).",
        "tags": ["handheld", "android"],
        "attributes": {"os": "Android", "role": "handheld"},
    },
    {
        "slug": "device:odin3",
        "type": "device",
        "name": "odin3",
        "summary": "AYN Odin 3 Android handheld.",
        "tags": ["handheld", "android"],
        "attributes": {"os": "Android", "role": "handheld"},
    },
    {
        "slug": "device:retroid-pocket-classic",
        "type": "device",
        "name": "retroid-pocket-classic",
        "summary": "Retroid Pocket Classic handheld.",
        "tags": ["handheld", "android"],
        "attributes": {"os": "Android", "role": "handheld"},
    },
    {
        "slug": "device:steamdeck",
        "type": "device",
        "name": "steamdeck",
        "summary": "Valve Steam Deck.",
        "tags": ["handheld", "linux"],
        "attributes": {"os": "SteamOS", "role": "handheld"},
    },
    {
        "slug": "datastore:aria-db",
        "type": "datastore",
        "name": "aria-db",
        "summary": (
            "The `aria` MongoDB database: memories, conversations, projects, "
            "tasks, shells, alerts, and the ontology graph itself."
        ),
        "tags": ["mongodb"],
        "attributes": {"kind": "mongodb", "database": "aria", "host": "corsair-ai"},
    },
    {
        "slug": "datastore:restic-repo-nas",
        "type": "datastore",
        "name": "restic-repo@nas",
        "summary": "Restic backup repository living on the NAS.",
        "aliases": ["restic-repo@nas", "restic"],
        "tags": ["backup"],
        "attributes": {"kind": "restic", "host": "nas"},
    },
    {
        "slug": "datastore:syncthing",
        "type": "datastore",
        "name": "syncthing",
        "summary": "Peer-to-peer file sync across Ben's machines and devices.",
        "tags": ["sync"],
        "attributes": {"kind": "syncthing"},
    },
    {
        "slug": "network:tailnet",
        "type": "network",
        "name": "tailnet",
        "summary": (
            "The closed Tailscale network every machine joins. ARIA's security "
            "posture depends on it: Mongo and :8200 bind 0.0.0.0 with no "
            "per-service auth because nothing outside the tailnet can reach "
            "them."
        ),
        "aliases": ["tailscale"],
        "tags": ["private"],
        "attributes": {"kind": "tailscale"},
    },
    {
        "slug": "network:lan",
        "type": "network",
        "name": "lan",
        "summary": "The local home network.",
        "tags": ["local"],
        "attributes": {"kind": "ethernet/wifi"},
    },
    {
        "slug": "person:ben",
        "type": "person",
        "name": "Ben",
        "summary": "The single user this whole system exists for.",
        "aliases": ["benjamin"],
        "tags": ["owner"],
        "attributes": {"role": "owner"},
    },
)


# --- §4b durable relations -------------------------------------------------
# ONLY edges between hand-authored entities. Every `hosts` / `depends_on` edge
# touching a projected service is DERIVED in projection.py — those are exactly
# the ones that rotted last time (`aria-api --depends_on--> qwen-agentic` was
# already false when the plan was written down).
SEED_RELATIONS: tuple[SeedRelation, ...] = (
    {"subject": "machine:corsair-ai", "predicate": "member_of", "object": "network:tailnet"},
    {"subject": "machine:nas", "predicate": "member_of", "object": "network:tailnet"},
    {"subject": "machine:red", "predicate": "member_of", "object": "network:tailnet"},
    {"subject": "machine:ridge", "predicate": "member_of", "object": "network:tailnet"},
    {"subject": "machine:corsair-ai", "predicate": "member_of", "object": "network:lan"},
    {"subject": "machine:nas", "predicate": "member_of", "object": "network:lan"},
    {"subject": "machine:corsair-ai", "predicate": "backs_up_to", "object": "datastore:restic-repo-nas"},
    {"subject": "datastore:restic-repo-nas", "predicate": "runs_on", "object": "machine:nas"},
    {"subject": "datastore:aria-db", "predicate": "runs_on", "object": "machine:corsair-ai"},
    {"subject": "person:ben", "predicate": "client_of", "object": "machine:corsair-ai"},
    {"subject": "device:steamdeck", "predicate": "member_of", "object": "network:lan"},
)
