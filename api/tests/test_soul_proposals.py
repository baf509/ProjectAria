"""Tests for applying dream soul proposals to SOUL.md.

Regression coverage for the 2026-08-05 incident: approving a four-month backlog
of proposals in one pass appended a fresh section per proposal whose `current`
snapshot had gone stale, leaving SOUL.md with six "## Values" headings, four
"## Core Identity", three "## Continuity", and two footers.
"""

import re

import pytest

from aria.core.soul import (
    StaleProposalError,
    apply_proposal,
    find_section_span,
    preview_proposals,
)

SOUL = """\
# ARIA - Who I Am

## Core Identity

I am ARIA.

## Values

**Have opinions.** Disagree when warranted.

### Sub-value

A nested note.

## Continuity

Each session, I wake up fresh.

---

_This file is mine to evolve._
"""


def headings(text):
    return re.findall(r"^##\s+(.*)$", text, re.MULTILINE)


def dup_headings(text):
    hs = headings(text)
    return {h for h in hs if hs.count(h) > 1}


# --- section lookup ---------------------------------------------------------

def test_finds_section_body():
    start, end = find_section_span(SOUL, "Core Identity")
    assert SOUL[start:end].strip() == "I am ARIA."


def test_section_body_includes_subsections():
    start, end = find_section_span(SOUL, "Values")
    body = SOUL[start:end]
    assert "### Sub-value" in body
    assert "Continuity" not in body


def test_last_section_stops_at_footer():
    start, end = find_section_span(SOUL, "Continuity")
    assert "---" not in SOUL[start:end]


@pytest.mark.parametrize("label", [
    "Values",
    "Values — 'Be resourceful before asking'",
    "Values: something",
    "Values - something",
    "values",
])
def test_qualified_section_labels_resolve_to_the_base_heading(label):
    """Dreams qualify sections; those must not mint a new heading."""
    assert find_section_span(SOUL, label) == find_section_span(SOUL, "Values")


def test_unknown_section_has_no_span():
    assert find_section_span(SOUL, "Nonexistent") is None


# --- applying ---------------------------------------------------------------

def test_matching_current_is_replaced_in_place():
    out, mode = apply_proposal(
        SOUL, {"section": "Core Identity", "current": "I am ARIA.", "proposed": "I am ARIA, v2."}
    )
    assert mode == "replaced"
    assert "I am ARIA, v2." in out
    assert not dup_headings(out)


def test_stale_current_is_refused_not_appended():
    """The core bug: a missing `current` used to append a duplicate section."""
    with pytest.raises(StaleProposalError) as e:
        apply_proposal(
            SOUL, {"section": "Values", "current": "text that is long gone", "proposed": "X"}
        )
    assert e.value.section == "Values"


def test_force_merges_stale_proposal_without_duplicating_the_heading():
    out, mode = apply_proposal(
        SOUL,
        {"section": "Values", "current": "long gone", "proposed": "**Be careful.**"},
        force=True,
    )
    assert mode == "merged"
    assert headings(out).count("Values") == 1
    assert "**Be careful.**" in out


def test_pure_addition_merges_into_existing_section():
    out, mode = apply_proposal(SOUL, {"section": "Values", "proposed": "**Be kind.**"})
    assert mode == "merged"
    assert headings(out).count("Values") == 1
    assert "**Be kind.**" in out


def test_genuinely_new_section_is_created_above_the_footer():
    out, mode = apply_proposal(SOUL, {"section": "Curiosity", "proposed": "Ask why."})
    assert mode == "created"
    assert out.index("## Curiosity") < out.index("\n---\n")
    assert out.count("\n---\n") == 1


def test_proposed_text_carrying_a_footer_does_not_duplicate_it():
    """Some proposals quote the closing block as part of the section they rewrite."""
    out, _ = apply_proposal(
        SOUL,
        {
            "section": "Continuity",
            "current": "Each session, I wake up fresh.",
            "proposed": "I persist.\n\n---\n\n_This file is mine to evolve._",
        },
    )
    assert out.count("\n---\n") == 1


def test_empty_proposed_is_treated_as_stale():
    with pytest.raises(StaleProposalError):
        apply_proposal(SOUL, {"section": "Values", "proposed": ""})


# --- batch behaviour --------------------------------------------------------

def test_backlog_of_stale_proposals_never_duplicates_headings():
    """Regression: the exact shape that produced six '## Values' headings."""
    backlog = [
        {"section": "Values", "current": "stale snapshot A", "proposed": "**Trust the territory.**"},
        {"section": "Values — 'Be resourceful'", "current": "stale B", "proposed": "**Verify first.**"},
        {"section": "Core Identity", "current": "stale C", "proposed": "I am ARIA, revised."},
        {"section": "Continuity", "current": "stale D", "proposed": "I also dream."},
    ]
    soul = SOUL
    for prop in backlog:
        soul, _ = apply_proposal(soul, prop, force=True)

    assert not dup_headings(soul)
    assert soul.count("\n---\n") == 1
    for prop in backlog:
        assert prop["proposed"] in soul


def test_preview_flags_exactly_the_stale_sections():
    stale = preview_proposals(SOUL, [
        {"section": "Core Identity", "current": "I am ARIA.", "proposed": "fresh"},
        {"section": "Values", "current": "long gone", "proposed": "stale one"},
    ])
    assert stale == ["Values"]


def test_preview_is_side_effect_free():
    before = SOUL
    preview_proposals(SOUL, [{"section": "Values", "current": "gone", "proposed": "x"}])
    assert SOUL == before
