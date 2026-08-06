"""
ARIA - Local LLM route selection tests

Purpose: lock the precedence (request model > operator pin > largest resident)
and, more importantly, the edge behaviours that decide whether a consumer ends
up silently talking to the wrong model:
  - naming a stopped server must FAIL, not downgrade to a different model;
  - a stale pin must DEGRADE to auto, not take every consumer down.
"""

import pytest

from aria.infrastructure.llm_route import (
    base_url_for,
    is_servable,
    match_requested,
    rank_resident,
    select,
)


def server(slug, *, gib=10.0, running=True, port=8100, onbox=True, model_file=None,
           endpoints=None):
    return {
        "slug": slug,
        "state": "running" if running else "exited",
        "port": port,
        "onbox": onbox,
        "resident_gib_estimate": gib,
        "model_file": model_file,
        "endpoints": endpoints if endpoints is not None else {
            "local": f"http://localhost:{port}/v1",
            "tailnet": f"http://100.123.245.84:{port}/v1",
        },
    }


DS4 = server("DS4-0731-ROCMFPX-affine-128k", gib=86.5, port=8107,
             model_file="models/llm/DS4-0731-ROCMFPX-affine.gguf")
GEMMA = server("gemma-4-e4b-Q4", gib=8.0, port=8104)
CHADROCK = server("Chadrock-ROCmFP6-qwen3.6-27b", gib=30.0, port=8105, running=False)


class TestAuto:
    def test_largest_resident_wins_when_several_loaded(self):
        # The live case that motivated this: DS4 and gemma are both up.
        chosen, reason, unavailable = select([GEMMA, DS4])
        assert chosen["slug"] == DS4["slug"]
        assert not unavailable
        assert "largest resident" in reason

    def test_small_model_serves_when_nothing_large_is_resident(self):
        chosen, _, _ = select([GEMMA, CHADROCK])
        assert chosen["slug"] == GEMMA["slug"]

    def test_no_running_server_yields_nothing(self):
        chosen, _, unavailable = select([CHADROCK])
        assert chosen is None
        assert not unavailable  # "nothing is up", not "you asked for the wrong one"

    def test_offbox_and_portless_servers_are_never_chosen(self):
        offbox = server("Ridge-Qwen3.6-35B-A3B", gib=99.0, onbox=False)
        portless = server("no-port", gib=99.0, port=None, endpoints={})
        chosen, _, _ = select([offbox, portless, GEMMA])
        assert chosen["slug"] == GEMMA["slug"]


class TestRequestedModel:
    def test_request_model_selects_a_loaded_server_by_slug(self):
        chosen, reason, _ = select([DS4, GEMMA], requested="gemma-4-e4b-Q4")
        assert chosen["slug"] == GEMMA["slug"]
        assert "requested by caller" in reason

    def test_match_is_case_and_separator_insensitive(self):
        chosen, _, _ = select([DS4, GEMMA], requested="GEMMA_4_E4B_Q4")
        assert chosen["slug"] == GEMMA["slug"]

    def test_gguf_filename_also_names_the_server(self):
        for name in ("DS4-0731-ROCMFPX-affine.gguf", "DS4-0731-ROCMFPX-affine"):
            chosen, _, _ = select([DS4, GEMMA], requested=name)
            assert chosen["slug"] == DS4["slug"], name

    @pytest.mark.parametrize("alias", ["auto", "aria-resident", "aria", "", None])
    def test_auto_aliases_defer_to_ranking(self, alias):
        chosen, reason, _ = select([GEMMA, DS4], requested=alias)
        assert chosen["slug"] == DS4["slug"]
        assert "largest resident" in reason

    def test_unknown_model_string_falls_through_to_auto(self):
        # OpenAI clients send arbitrary model ids; that must not be fatal.
        chosen, _, unavailable = select([GEMMA, DS4], requested="gpt-4")
        assert chosen["slug"] == DS4["slug"]
        assert not unavailable

    def test_naming_a_stopped_server_is_an_error_not_a_downgrade(self):
        chosen, reason, unavailable = select([DS4, CHADROCK], requested=CHADROCK["slug"])
        assert chosen is None
        assert unavailable
        assert "not running" in reason


class TestPin:
    def test_pin_overrides_the_ranking(self):
        chosen, reason, _ = select([GEMMA, DS4], pin=GEMMA["slug"])
        assert chosen["slug"] == GEMMA["slug"]
        assert "pinned" in reason

    def test_request_model_beats_the_pin(self):
        chosen, _, _ = select([GEMMA, DS4], requested=DS4["slug"], pin=GEMMA["slug"])
        assert chosen["slug"] == DS4["slug"]

    def test_stale_pin_degrades_to_auto_rather_than_failing(self):
        # A forgotten pin must never take every consumer offline.
        chosen, reason, unavailable = select([GEMMA, DS4], pin=CHADROCK["slug"])
        assert chosen["slug"] == DS4["slug"]
        assert not unavailable
        assert "fell back" in reason

    def test_stale_pin_with_nothing_running_still_reports_no_server(self):
        chosen, _, unavailable = select([CHADROCK], pin=CHADROCK["slug"])
        assert chosen is None
        assert not unavailable


class TestEndpoints:
    def test_tailnet_only_server_is_reachable(self):
        # DS4 binds the tailnet IP only; localhost:8107 is refused. A server
        # with no `local` endpoint must still be servable.
        tailnet_only = server("ds4", endpoints={"tailnet": "http://100.123.245.84:8107/v1"})
        assert is_servable(tailnet_only)
        assert base_url_for(tailnet_only) == "http://100.123.245.84:8107/v1"

    def test_loopback_preferred_when_both_exist(self):
        assert base_url_for(GEMMA) == "http://localhost:8104/v1"

    def test_trailing_slash_is_stripped(self):
        s = server("x", endpoints={"local": "http://localhost:8104/v1/"})
        assert base_url_for(s) == "http://localhost:8104/v1"

    def test_server_without_endpoints_is_not_servable(self):
        assert not is_servable(server("x", port=8104, endpoints={}))


def test_rank_resident_handles_missing_footprint():
    unmeasured = server("unmeasured", gib=None)
    assert rank_resident([unmeasured])["slug"] == "unmeasured"
    assert rank_resident([unmeasured, GEMMA])["slug"] == GEMMA["slug"]


def test_match_requested_reports_stopped_only_for_known_names():
    assert match_requested([CHADROCK], "nonexistent") == (None, False)
    assert match_requested([CHADROCK], CHADROCK["slug"]) == (None, True)
