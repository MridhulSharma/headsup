"""The parse path, and the one piece of the SDK this layer reaches inside for.

`json_schema_for` goes through `anthropic.lib._parse._transform` because the
public entry point, `messages.parse`, raises before handing back the raw reply
and its token counts - and a reply that failed to validate was still billed.
That is a private module, so this test is the tripwire: if an SDK upgrade
moves it, the unit run fails here rather than the demo failing in front of
people.
"""

from __future__ import annotations

import pytest

from fixtures.llm_calls import FloodFields, FusionRationale
from headsup.llm import LlmParseError, MockBackend, TokenUse, json_schema_for, parse_payload
from headsup.llm.backends import DEFAULT_OLLAMA_MODEL, SDK_SCHEMA_TRANSFORM, _ollama_model
from headsup.llm.base import ENV_OLLAMA_MODEL


def test_the_sdk_schema_transform_is_still_where_we_import_it_from() -> None:
    assert SDK_SCHEMA_TRANSFORM is not None, (
        "anthropic.lib._parse._transform.transform_schema has moved; "
        "find it in the installed SDK and fix the import in headsup/llm/backends.py"
    )


def test_the_schema_the_api_is_given_is_strict() -> None:
    """Constrained decoding needs a closed object, or it will invent fields."""
    schema = json_schema_for(FloodFields)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "waterway",
        "severity_hint",
        "evacuation_mentioned",
        "roads_named",
    }


def test_a_nested_contract_still_produces_a_schema() -> None:
    """Enums and lists are what these replies are made of."""
    schema = json_schema_for(FusionRationale)
    assert "primary_source" in schema["properties"]


# --- parsing ----------------------------------------------------------------


def test_a_good_reply_validates() -> None:
    value = parse_payload(FloodFields, '{"severity_hint": 3}')
    assert value.severity_hint == 3
    assert value.roads_named == []


def test_a_rejected_reply_carries_what_it_cost() -> None:
    """The provider billed for it, so the meter has to see the tokens."""
    use = TokenUse(input_tokens=120, output_tokens=8)
    with pytest.raises(LlmParseError) as caught:
        parse_payload(FloodFields, "not json", use=use, model="claude-haiku-5-5")
    assert caught.value.use == use
    assert caught.value.model == "claude-haiku-5-5"
    assert caught.value.detail


def test_a_field_the_contract_does_not_have_is_a_parse_failure() -> None:
    """`extra="forbid"` on the contracts means a hallucinated field is caught."""
    with pytest.raises(LlmParseError, match="invented_field"):
        parse_payload(FloodFields, '{"severity_hint": 3, "invented_field": true}')


# --- the mock backend -------------------------------------------------------


async def test_scripted_replies_are_consumed_before_the_fixture() -> None:
    from headsup.llm import Message, Request, Tier

    backend = MockBackend()
    backend.script("sentinel_flood_fields", {"severity_hint": 1})
    request = Request(
        purpose="sentinel_flood_fields",
        tier=Tier.VOLUME,
        schema=FloodFields,
        system=(),
        messages=(Message("user", "q"),),
    )
    assert (await backend.send(request)).value.severity_hint == 1
    assert (await backend.send(request)).value.severity_hint == 4, "the fixture takes over"


def test_the_local_model_name_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.delenv(ENV_OLLAMA_MODEL, raising=False)
    assert _ollama_model() == DEFAULT_OLLAMA_MODEL
    monkeypatch.setenv(ENV_OLLAMA_MODEL, "mistral:7b")
    assert _ollama_model() == "mistral:7b"
