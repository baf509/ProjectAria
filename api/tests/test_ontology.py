"""
ARIA - Ontology Memory Map tests

Purpose: lock the rules that keep the graph trustworthy rather than merely
populated. Each test here corresponds to a mistake that was actually made
while building this, not a hypothetical:

  - plain prefix matching attributed ~/Development/ProjectAria to the parent
    project (the C4 PathIndex lesson, re-learned here);
  - slug derivation drifting between the projection and the cross-link would
    point entities[] at entities that do not exist;
  - bulk `mentions` edges buried every structural edge in the neighborhood
    view (500 of 500 incoming edges on project:aria);
  - a reasoning model spent its whole token budget thinking and returned no
    JSON, silently labelling every memory with zero entities;
  - the extractor proposed the WRONG QUANT of the right model server, which is
    the most dangerous kind of wrong because it reads as correct.
"""

import pytest

from aria.ontology.crosslink import (
    BULK_SOURCE_TYPES,
    CURATED_SOURCE_TYPES,
    PathProjectIndex,
    _norm_path,
    looks_like_path,
    parse_json_object,
    verify_slug,
)
from aria.ontology.models import (
    ENTITY_TYPES,
    INVERSE,
    PREDICATES,
    PROTECTED_FIELDS,
    WORKER_FIELDS,
    entity_slug,
    is_valid_predicate,
    is_valid_type,
    normalize_name,
    project_entity_slug,
    project_roots,
    split_slug,
)
from aria.ontology.seed import SEED_ENTITIES, SEED_RELATIONS


# --------------------------------------------------------------------------
# Slugs and vocabulary
# --------------------------------------------------------------------------


def test_entity_slug_is_type_prefixed_and_normalized():
    assert entity_slug("machine", "corsair-ai") == "machine:corsair-ai"
    assert entity_slug("machine", "Corsair AI") == "machine:corsair-ai"
    assert entity_slug("project", "ARIA") == "project:aria"


def test_entity_slug_keeps_meaningful_punctuation():
    """`@` and `.` are real in these names (restic-repo@nas); collapsing them
    would make slugs unreadable and collide distinct entities."""
    assert entity_slug("datastore", "restic-repo@nas") == "datastore:restic-repo@nas"
    assert "3.0" in entity_slug("service", "ling-3.0-flash")


def test_entity_slug_rejects_unknown_type():
    with pytest.raises(ValueError):
        entity_slug("nonsense", "x")


def test_split_slug_roundtrips():
    assert split_slug("machine:corsair-ai") == ("machine", "corsair-ai")


def test_normalize_name_never_empty():
    assert normalize_name("") == "unnamed"
    assert normalize_name("!!!") == "unnamed"


def test_vocabulary_is_closed():
    assert is_valid_type("machine") and not is_valid_type("server")
    assert is_valid_predicate("runs_on") and not is_valid_predicate("uses")


def test_inverse_pairs_are_symmetric():
    for a, b in INVERSE.items():
        assert INVERSE[b] == a, f"{a}/{b} inverse is not symmetric"
        assert a in PREDICATES and b in PREDICATES


def test_worker_and_protected_fields_are_disjoint():
    """The S3 ownership rule only works if a field has exactly one owner. An
    overlap would let a projection overwrite curated prose."""
    assert not set(WORKER_FIELDS) & set(PROTECTED_FIELDS)


def test_protected_fields_are_the_human_authored_ones():
    assert set(PROTECTED_FIELDS) == {"aliases", "summary", "tags"}


# --------------------------------------------------------------------------
# project slug derivation — one source of truth
# --------------------------------------------------------------------------


def test_project_entity_slug_prefers_slug_over_name():
    """`name` is a display string that can be edited; `slug` is identity.
    Deriving from name would mint a second entity on any rename."""
    doc = {"slug": "aria", "name": "ARIA"}
    assert project_entity_slug(doc) == "project:aria"
    renamed = {"slug": "aria", "name": "ARIA v2"}
    assert project_entity_slug(renamed) == project_entity_slug(doc)


def test_project_entity_slug_falls_back_to_name():
    assert project_entity_slug({"name": "emu_fleet_monitor"}) == "project:emu_fleet_monitor"


def test_project_roots_includes_relevant_paths():
    """The harvested 'ARIA' row carries ONLY relevant_paths — a path-only read
    missed it entirely, leaving the project with no location and no host edge."""
    doc = {"relevant_paths": ["/home/ben/Development/ProjectAria"]}
    assert project_roots(doc) == ["/home/ben/Development/ProjectAria"]


def test_project_roots_puts_path_first_and_dedupes():
    doc = {"path": "/a", "relevant_paths": ["/a", "/b"]}
    assert project_roots(doc) == ["/a", "/b"]


# --------------------------------------------------------------------------
# Path attribution — most-specific-root wins
# --------------------------------------------------------------------------


def test_most_specific_root_wins():
    """THE regression to prevent. Plain prefix matching lets the coarse
    ~/Development row swallow every child project."""
    idx = PathProjectIndex(
        [("/home/ben/Development", "project:development"),
         ("/home/ben/Development/ProjectAria", "project:aria")]
    )
    assert idx.owner("~/Development/ProjectAria") == "project:aria"
    assert idx.owner("~/Development") == "project:development"


def test_deeper_path_inherits_nearest_project():
    idx = PathProjectIndex([("/home/ben/Development/ProjectAria", "project:aria")])
    assert idx.owner("~/Development/ProjectAria/api/aria") == "project:aria"


def test_unrelated_path_matches_nothing():
    """`~/` must not be forced onto some project — 570 memories carry it, and
    attributing them all to a 'home' project would be noise, not signal."""
    idx = PathProjectIndex([("/home/ben/Development/ProjectAria", "project:aria")])
    assert idx.owner("~/") is None
    assert idx.owner("/etc") is None


def test_sibling_prefix_does_not_match():
    """/home/ben/Dev must not match a /home/ben/Development root."""
    idx = PathProjectIndex([("/home/ben/Development", "project:development")])
    assert idx.owner("/home/ben/Dev") is None


def test_equal_length_roots_tie_break_deterministically():
    """Two projects genuinely claim ~/Development/ProjectAria ('ARIA' and
    'ProjectAria'). Without a stable secondary sort the winner depends on
    Mongo's iteration order, silently reassigning thousands of memories."""
    entries = [("/x/y", "project:zeta"), ("/x/y", "project:alpha")]
    assert PathProjectIndex(entries).owner("/x/y") == "project:alpha"
    assert PathProjectIndex(list(reversed(entries))).owner("/x/y") == "project:alpha"


def test_norm_path_canonicalizes_home_across_nodes():
    assert _norm_path("~/Development") == "~/Development"
    assert _norm_path("/Users/ben/Development") == "~/Development"
    assert _norm_path("/home/ben/Development") == "~/Development"


def test_norm_path_strips_trailing_slash():
    assert _norm_path("/a/b/") == "/a/b"


def test_looks_like_path_only_matches_paths():
    assert looks_like_path("~/Development/ProjectAria")
    assert looks_like_path("/etc/hosts")
    # Topical categories are tags, not entities — they must not be forced in.
    assert not looks_like_path("infrastructure")
    assert not looks_like_path("llm")
    assert not looks_like_path("")


# --------------------------------------------------------------------------
# LLM response parsing — the resident model is not fixed
# --------------------------------------------------------------------------


def test_parse_plain_json():
    assert parse_json_object('{"entities": ["a"]}') == {"entities": ["a"]}


def test_parse_strips_reasoning_block():
    """DS4 (the resident server) emits <think>...</think> before its answer;
    strict json.loads returned zero entities for every single memory."""
    raw = '<think>The user wants JSON.\nLet me think.</think>{"entities": ["machine:corsair-ai"]}'
    assert parse_json_object(raw) == {"entities": ["machine:corsair-ai"]}


def test_parse_strips_markdown_fence():
    raw = '```json\n{"entities": []}\n```'
    assert parse_json_object(raw) == {"entities": []}


def test_parse_recovers_object_from_prose():
    raw = 'Here you go:\n{"entities": ["x"]}\nHope that helps!'
    assert parse_json_object(raw) == {"entities": ["x"]}


def test_parse_raises_when_no_object():
    """A truncated reasoning dump must RAISE, not return {}. Returning empty
    would look identical to 'the model found no entities' and hide the
    truncation for the entire backfill."""
    with pytest.raises(ValueError):
        parse_json_object("I was thinking about the entities and then ran out")


# --------------------------------------------------------------------------
# Extraction verification gate
# --------------------------------------------------------------------------


def test_verify_accepts_exact_mention():
    assert verify_slug("service:qwen3.6-27b-q8", "container 'qwen3.6-27b-Q8' stopped on corsair-ai.")


def test_verify_rejects_wrong_quant_of_right_family():
    """The most dangerous false positive observed: the memory names the q5km
    build, the model proposed the MXFP4 one. Both are real servers, so nothing
    downstream would flag it."""
    content = "service 'ling-3.0-flash-q5km.service' stopped on corsair-ai."
    assert not verify_slug("service:ling-3.0-flash-mxfp4", content)


def test_verify_rejects_entity_absent_from_text():
    content = "ARIA's model-server registry must not cover non-LLM services."
    assert not verify_slug("machine:red", content)
    assert not verify_slug("service:hermes-webui", content)


def test_verify_accepts_via_alias():
    assert verify_slug(
        "datastore:restic-repo@nas", "backed up with restic today", aliases=["restic"]
    )


def test_verify_is_case_and_punctuation_insensitive():
    assert verify_slug("machine:corsair-ai", "Sizing rule for CORSAIR_AI changed")


# --------------------------------------------------------------------------
# Extraction scope — phase 5e is closed, not forgotten
# --------------------------------------------------------------------------


def test_bulk_sources_are_excluded_from_llm_extraction():
    """13,671 machine-generated memories get no LLM pass, by decision
    (2026-08-07). If someone widens this it should be deliberate."""
    assert set(BULK_SOURCE_TYPES) == {"shell_extraction", "claude_session_digest"}


def test_curated_and_bulk_sources_do_not_overlap():
    assert not set(CURATED_SOURCE_TYPES) & set(BULK_SOURCE_TYPES)


# --------------------------------------------------------------------------
# Seed content — §4b, the durable half only
# --------------------------------------------------------------------------


def test_seed_contains_no_services():
    """§4 rule: services are PROJECTED from the registries. Hand-seeding them
    is what produced a list naming two retired qwen containers and a dead
    Fireworks account within three weeks."""
    types = {e["type"] for e in SEED_ENTITIES}
    assert "service" not in types
    assert "external_service" not in types


def test_seed_only_holds_durable_types():
    allowed = {"machine", "device", "datastore", "network", "person"}
    assert {e["type"] for e in SEED_ENTITIES} <= allowed


def test_seed_slugs_are_valid_and_unique():
    slugs = [e["slug"] for e in SEED_ENTITIES]
    assert len(slugs) == len(set(slugs))
    for entity in SEED_ENTITIES:
        assert entity["slug"].startswith(entity["type"] + ":")
        assert is_valid_type(entity["type"])


def test_seed_includes_the_local_host():
    """corsair-ai is deliberately absent from db.nodes (which registers only
    REMOTE nodes), so if the seed drops it nothing else supplies it."""
    assert "machine:corsair-ai" in {e["slug"] for e in SEED_ENTITIES}


def test_seed_relations_reference_seeded_entities_only():
    """Hand-authored edges may only connect hand-authored entities. Edges to
    projected services are derived — those are the ones that rotted."""
    known = {e["slug"] for e in SEED_ENTITIES}
    for rel in SEED_RELATIONS:
        assert rel["subject"] in known, rel
        assert rel["object"] in known, rel
        assert is_valid_predicate(rel["predicate"]), rel


def test_all_entity_types_are_reachable():
    """Every declared type should be produced by either the seed or a
    projection; an unreachable type is dead vocabulary."""
    seeded = {e["type"] for e in SEED_ENTITIES}
    projected = {"project", "service", "machine"}
    unreachable = set(ENTITY_TYPES) - seeded - projected
    assert unreachable == {"external_service"}, (
        f"unexpected unreachable types: {unreachable}"
    )
