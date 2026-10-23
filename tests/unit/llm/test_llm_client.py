"""The client end to end in mock mode: replies, the retry, and the audit trail.

Nothing here touches a network or Redis. Mock mode is what the whole default
test run uses, so these tests are also the contract the later agents rely on:
a validated object or an exception, and one audit record either way.
"""

from __future__ import annotations

import pytest

from fixtures.llm_calls import (
    FLOOD_PAYLOAD,
    FloodFields,
    FusionRationale,
    ProposedAction,
    RecordingBus,
)
from headsup.contracts import ActionType, Source
from headsup.llm import (
    LlmClient,
    LlmError,
    LlmParseError,
    Message,
    MockBackend,
    Mode,
    ResponseCache,
    SpendMeter,
    Tier,
    data_block,
)

ALERT = "Flash Flood Warning for Shoal Creek. Turn around, don't drown."


@pytest.fixture
def backend() -> MockBackend:
    return MockBackend()


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def client(backend, bus) -> LlmClient:
    return LlmClient.mock(backend=backend, meter=SpendMeter(bus=bus))


async def flood(client: LlmClient, **kwargs):
    return await client.complete(
        purpose="sentinel_flood_fields",
        tier=Tier.VOLUME,
        schema=FloodFields,
        system=("You fill fields. You do not decide anything.",),
        messages=data_block("nws_description", ALERT),
        **kwargs,
    )


# --- fixtures in, contracts out ---------------------------------------------


async def test_a_volume_call_comes_back_as_a_validated_model(client) -> None:
    result = await flood(client)
    assert isinstance(result.value, FloodFields)
    assert result.value.waterway == "Shoal Creek"
    assert result.value.severity_hint == 4


async def test_a_variant_picks_a_second_fixture_for_the_same_call_site(client) -> None:
    quiet = await flood(client, variant="quiet")
    assert quiet.value.waterway is None
    assert quiet.value.severity_hint == 2


async def test_a_variant_with_no_file_falls_back_to_the_plain_fixture(client) -> None:
    result = await flood(client, variant="nonexistent")
    assert result.value.waterway == "Shoal Creek"


async def test_a_judgment_call_runs_on_the_same_mock(client) -> None:
    result = await client.complete(
        purpose="fusion_rationale",
        tier=Tier.JUDGMENT,
        schema=FusionRationale,
        system=("You explain. You do not publish.",),
        messages="two sources agree",
    )
    assert result.value.primary_source is Source.NWS
    assert result.value.conflicts == []
    assert result.call.tier is Tier.JUDGMENT


async def test_the_proposer_fixture_still_matches_its_contract(client) -> None:
    result = await client.complete(
        purpose="proposer_action",
        tier=Tier.JUDGMENT,
        schema=ProposedAction,
        messages="choose an action",
    )
    assert result.value.action is ActionType.NO_ENTRY
    assert 0.0 <= result.value.confidence <= 1.0


async def test_a_missing_fixture_names_the_file_it_wanted(client) -> None:
    with pytest.raises(LlmError, match=r"has no reply for purpose"):
        await client.complete(
            purpose="critic_verdict",
            tier=Tier.VOLUME,
            schema=FloodFields,
            messages="anything",
        )


# --- one retry on a parse failure -------------------------------------------


async def test_a_reply_that_does_not_validate_is_asked_once_more(client, backend) -> None:
    backend.script("sentinel_flood_fields", {"severity_hint": "high"}, FLOOD_PAYLOAD)
    result = await flood(client)
    assert result.value.severity_hint == 4
    assert result.call.attempts == 2
    assert result.call.ok


async def test_the_retry_states_the_schema_violation_and_keeps_the_question(
    client, backend
) -> None:
    backend.script("sentinel_flood_fields", {"severity_hint": "high"}, FLOOD_PAYLOAD)
    await flood(client)
    first, second = backend.seen
    assert len(second.messages) == len(first.messages) + 1
    assert second.messages[: len(first.messages)] == first.messages
    assert second.system == first.system
    correction = second.messages[-1]
    assert correction.role == "user"
    assert "FloodFields" in correction.content
    assert "severity_hint" in correction.content
    assert "high" not in correction.content


async def test_invalid_json_is_a_parse_failure_like_any_other(client, backend) -> None:
    backend.script("sentinel_flood_fields", "{not json at all", FLOOD_PAYLOAD)
    result = await flood(client)
    assert result.call.attempts == 2


async def test_two_bad_replies_raise_and_leave_a_record_saying_so(client, backend, bus) -> None:
    """The signal to a caller that it has to use its rules path."""
    backend.script("sentinel_flood_fields", {"severity_hint": "high"}, {"waterway": "x"})
    with pytest.raises(LlmParseError):
        await flood(client)
    (call,) = bus.on("audit")
    assert call.ok is False
    assert call.attempts == 2
    assert "severity_hint" in (call.error or "")
    assert len(backend.seen) == 2


async def test_there_is_no_second_retry(client, backend) -> None:
    backend.script("sentinel_flood_fields", "{", "{", FLOOD_PAYLOAD)
    with pytest.raises(LlmParseError):
        await flood(client)
    assert len(backend.seen) == 2


# --- the audit record -------------------------------------------------------


async def test_every_call_writes_one_record_to_the_audit_stream(client, bus) -> None:
    await flood(client)
    await flood(client, variant="quiet")
    assert len(bus.on("audit")) == 2
    assert {stream for stream, _ in bus.published} == {"audit"}


async def test_the_record_says_what_answered_and_what_it_cost(client, bus) -> None:
    result = await flood(client)
    call = result.call
    assert call.purpose == "sentinel_flood_fields"
    assert call.tier is Tier.VOLUME
    assert call.mode is Mode.MOCK
    assert call.model == "mock"
    assert call.attempts == 1
    assert call.input_tokens > 0
    assert call.output_tokens > 0
    assert call.usd == 0.0, "a mocked reply must never cost anything"
    assert call.latency_ms >= 0.0
    assert call.fallback is None
    assert bus.on("audit") == [call]


async def test_the_record_is_a_contract_and_goes_over_the_wire(client) -> None:
    result = await flood(client)
    wire = result.call.to_wire()
    assert wire["tier"] == "volume"
    assert wire["mode"] == "mock"
    assert wire["purpose"] == "sentinel_flood_fields"
    assert "mock" in result.call.summary()


async def test_a_meter_without_a_bus_still_keeps_the_records(backend) -> None:
    """A bare `hu` invocation has no Redis and still has to show what it spent."""
    client = LlmClient.mock(backend=backend)
    await flood(client)
    assert len(client.meter.records) == 1
    assert client.meter.spent_usd == 0.0


# --- the cache, through the client ------------------------------------------


async def test_a_second_identical_call_is_a_cache_hit(tmp_path, backend, bus) -> None:
    """The Done-when check: the model is asked once, the answer serves twice."""
    client = LlmClient.mock(
        backend=backend, cache=ResponseCache(tmp_path), meter=SpendMeter(bus=bus)
    )
    first = await flood(client)
    second = await flood(client)

    assert len(backend.seen) == 1, "the second call reached the model"
    assert first.cached is False
    assert second.cached is True
    assert second.value == first.value
    assert second.call.usd == 0.0
    assert second.call.input_tokens == 0
    assert [call.cached for call in bus.on("audit")] == [False, True]


async def test_a_changed_prompt_is_asked_again(tmp_path, backend) -> None:
    client = LlmClient.mock(backend=backend, cache=ResponseCache(tmp_path))
    await flood(client)
    await client.complete(
        purpose="sentinel_flood_fields",
        tier=Tier.VOLUME,
        schema=FloodFields,
        system=("You fill fields. You do not decide anything. Ever.",),
        messages=data_block("nws_description", ALERT),
    )
    assert len(backend.seen) == 2


async def test_an_entry_that_no_longer_fits_its_contract_is_a_miss(tmp_path, backend) -> None:
    cache = ResponseCache(tmp_path)
    client = LlmClient.mock(backend=backend, cache=cache)
    result = await flood(client)
    key = cache.key("mock", backend.seen[0])
    cache.put(key, {"severity_hint": 99}, model="mock", purpose="sentinel_flood_fields")
    again = await flood(client)
    assert again.cached is False
    assert again.value == result.value
    assert len(backend.seen) == 2


# --- what the caller asked for ----------------------------------------------


async def test_a_bare_string_is_the_user_turn_and_a_bare_block_is_one_block(
    client, backend
) -> None:
    await client.complete(
        purpose="sentinel_flood_fields",
        tier=Tier.VOLUME,
        schema=FloodFields,
        system="one fixed block",
        messages="one question",
    )
    seen = backend.seen[0]
    assert seen.system == ("one fixed block",)
    assert seen.messages == (Message("user", "one question"),)


async def test_empty_system_blocks_are_dropped(client, backend) -> None:
    await flood(client)
    await client.complete(
        purpose="sentinel_flood_fields",
        tier=Tier.VOLUME,
        schema=FloodFields,
        system=("", "kept", ""),
        messages="q",
    )
    assert backend.seen[-1].system == ("kept",)
