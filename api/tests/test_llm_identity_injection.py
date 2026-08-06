"""Tests for the identified-proxy identity injection.

Context: consumers configure the synthetic model id `aria-resident`, which is a
routing alias, not a model. Asked what they are, they answer from config —
"aria-resident on a custom provider" — naming nothing. The identified route puts
the real model in the context so the answer is available without a tool call.

The invariant that matters most here is the negative one: /llm/v1 must stay
verbatim, because it is LLAMACPP_URL and feeds benchmark targets.
"""

import pytest

from aria.api.routes.llm_proxy import _identity_line, _inject_identity


class _Route:
    def __init__(self, slug):
        self.slug = slug


def test_identity_line_names_the_backend_model():
    line = _identity_line(_Route("Ling-3.0-flash-Q5_K_M"), "Ling-3.0-flash-Q5_K_M.gguf")
    assert "Ling-3.0-flash-Q5_K_M.gguf" in line
    # It must also disarm the alias, or the model may still volunteer it.
    assert "aria-resident" in line


def test_identity_line_falls_back_to_slug_when_backend_is_silent():
    line = _identity_line(_Route("Ling-3.0-flash-Q5_K_M"), None)
    assert "Ling-3.0-flash-Q5_K_M" in line


def test_injects_when_no_system_message_present():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = _inject_identity(body, "IDENTITY")
    assert out["messages"][0] == {"role": "system", "content": "IDENTITY"}
    assert out["messages"][1]["role"] == "user"


def test_merges_into_an_existing_leading_system_message():
    """Some backends and chat templates honour only the FIRST system turn."""
    body = {"messages": [
        {"role": "system", "content": "You are Hermes."},
        {"role": "user", "content": "hi"},
    ]}
    out = _inject_identity(body, "IDENTITY")
    assert len(out["messages"]) == 2, "must not add a second system turn"
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][0]["content"] == "IDENTITY\n\nYou are Hermes."


def test_structured_system_content_is_not_spliced():
    """Multipart content is prepended to, never string-concatenated into."""
    body = {"messages": [
        {"role": "system", "content": [{"type": "text", "text": "You are Hermes."}]},
        {"role": "user", "content": "hi"},
    ]}
    out = _inject_identity(body, "IDENTITY")
    assert out["messages"][0] == {"role": "system", "content": "IDENTITY"}
    assert out["messages"][1]["content"] == [{"type": "text", "text": "You are Hermes."}]


def test_raw_completions_body_is_left_alone():
    """/completions has no message list — mangling the prompt string is worse
    than skipping injection."""
    body = {"prompt": "once upon a time", "max_tokens": 5}
    out = _inject_identity(dict(body), "IDENTITY")
    assert out == body


def test_does_not_mutate_the_callers_message_dicts():
    original = {"role": "system", "content": "You are Hermes."}
    body = {"messages": [original, {"role": "user", "content": "hi"}]}
    _inject_identity(body, "IDENTITY")
    assert original["content"] == "You are Hermes.", "caller's dict was mutated in place"


def test_verbatim_route_and_identified_route_are_distinct_prefixes():
    """The whole point of the split: LLAMACPP_URL must not gain a system line."""
    from aria.api.routes.llm_proxy import identified_router, router

    assert router.prefix == "/llm/v1"
    assert identified_router.prefix == "/llm/v1-identified"
    assert router.prefix != identified_router.prefix
