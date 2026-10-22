"""What each mode will and will not answer, and what the API request looks like.

The two rules this layer exists to enforce in `claude` and `ollama` mode:

* a volume call that cannot reach the API is answered by the local model, and
  the audit record says it was;
* a judgment call that cannot reach Sonnet raises `JudgmentUnavailable`, in
  every mode and for every reason, because the caller's answer is always the
  same - take the rules path.

The API request shape is checked here too, against a stand-in for the SDK
client. Prompt caching is a property of the bytes we send, so it can be
tested exactly, and only here.
"""

from __future__ import annotations

import re

import anthropic
import httpx
import ollama
import pytest
from ollama import ChatResponse

from fixtures.llm_calls import (
    FLOOD_PAYLOAD,
    FakeAnthropic,
    FakeBlock,
    FakeResponse,
    FloodFields,
    FusionRationale,
    RecordingBus,
    reply,
)
from fixtures.llm_calls import truncated as truncated_reply
from headsup.llm import (
    ClaudeBackend,
    JudgmentUnavailable,
    LlmClient,
    LlmError,
    LlmParseError,
    LlmUnavailable,
    Message,
    MockBackend,
    Mode,
    OllamaBackend,
    Request,
    ResponseCache,
    SpendMeter,
    Tier,
    TokenUse,
    cost_usd,
    data_block,
    effort_for,
    model_for,
    system_blocks,
)

RULES = "You fill fields. You do not decide anything."
PLAYBOOK = "flood: max_area_km2 12.0, allowed NO_ENTRY"
ALERT = "Flash Flood Warning for Shoal Creek."


def claude_client(*replies, bus: RecordingBus | None = None, fallback=None) -> LlmClient:
    """A client in `claude` mode whose HTTP client is a scripted double."""
    fake = FakeAnthropic(*replies)
    backends = {
        tier: ClaudeBackend(model_for(tier), effort=effort_for(tier), client=fake) for tier in Tier
    }
    client = LlmClient(
        mode=Mode.CLAUDE,
        backends=backends,
        cache=ResponseCache(enabled=False),
        meter=SpendMeter(bus=bus),
        fallback=fallback,
    )
    client.fake = fake  # type: ignore[attr-defined]
    return client


async def flood(client: LlmClient, tier: Tier = Tier.VOLUME):
    return await client.complete(
        purpose="sentinel_flood_fields",
        tier=tier,
        schema=FloodFields,
        system=(RULES, PLAYBOOK),
        messages=data_block("nws_description", ALERT),
    )


def request(**kwargs) -> Request:
    defaults = {
        "purpose": "sentinel_flood_fields",
        "tier": Tier.VOLUME,
        "schema": FloodFields,
        "system": (RULES, PLAYBOOK),
        "messages": (Message("user", "q"),),
    }
    return Request(**{**defaults, **kwargs})


# --- the API request --------------------------------------------------------


def test_the_cache_breakpoint_sits_after_the_last_fixed_block() -> None:
    """One breakpoint, at the end of the prefix every call shares."""
    blocks = system_blocks(request())
    assert [block["text"] for block in blocks] == [RULES, PLAYBOOK]
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}


def test_a_request_with_no_fixed_prompt_has_nothing_to_cache() -> None:
    assert system_blocks(request(system=())) == []


async def test_the_request_carries_the_schema_the_effort_and_the_cached_prefix() -> None:
    client = claude_client(reply(FLOOD_PAYLOAD, input_tokens=1200, output_tokens=40))
    await flood(client)
    (payload,) = client.fake.messages.calls

    assert payload["model"] == model_for(Tier.VOLUME)
    assert payload["output_config"]["effort"] == "low"
    output_format = payload["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert "severity_hint" in output_format["schema"]["properties"]
    assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"] == [
        {"role": "user", "content": data_block("nws_description", ALERT)}
    ]


async def test_a_judgment_call_runs_at_a_higher_effort_than_a_volume_call() -> None:
    client = claude_client(reply({"rationale": "x", "primary_source": "nws", "conflicts": []}))
    await client.complete(
        purpose="fusion_rationale",
        tier=Tier.JUDGMENT,
        schema=FusionRationale,
        system=(RULES,),
        messages="why",
    )
    (payload,) = client.fake.messages.calls
    assert payload["output_config"]["effort"] == "medium"


async def test_tokens_and_dollars_come_from_the_usage_the_api_reported() -> None:
    bus = RecordingBus()
    client = claude_client(
        reply(
            FLOOD_PAYLOAD,
            input_tokens=300,
            output_tokens=60,
            cache_read_input_tokens=1100,
            cache_creation_input_tokens=0,
        ),
        bus=bus,
    )
    result = await flood(client)
    call = result.call
    assert call.input_tokens == 300
    assert call.output_tokens == 60
    assert call.cache_read_tokens == 1100, "a cached prefix has to be visible in the log"
    assert call.model == model_for(Tier.VOLUME)
    billed = TokenUse(input_tokens=300, output_tokens=60, cache_read_tokens=1100)
    assert call.usd == pytest.approx(cost_usd(call.model, billed), abs=1e-9)
    assert call.usd > 0, "a real call has to cost something or the budget means nothing"
    assert call.spent_usd == client.meter.spent_usd
    assert bus.on("audit") == [call]


async def test_a_truncated_reply_is_retried_and_both_attempts_are_billed() -> None:
    """The realistic parse failure with constrained decoding: max_tokens."""
    client = claude_client(
        truncated_reply(input_tokens=200, output_tokens=4096),
        reply(FLOOD_PAYLOAD, input_tokens=260, output_tokens=50),
    )
    result = await flood(client)
    assert result.call.attempts == 2
    assert result.call.output_tokens == 4096 + 50
    assert result.call.input_tokens == 200 + 260


async def test_a_declined_request_is_reported_not_retried() -> None:
    class Details:
        category = "cyber"

    declined = FakeResponse(
        content=[FakeBlock(text="{}")], stop_reason="refusal", stop_details=Details()
    )
    client = claude_client(declined)
    with pytest.raises(LlmError, match="declined the request"):
        await flood(client)
    assert len(client.fake.messages.calls) == 1


async def test_a_bad_request_is_the_callers_problem_not_a_fallback() -> None:
    """A 400 means our prompt or schema is wrong; a local model would not help."""
    stand_in = MockBackend(model="ollama:llama3.1:8b")
    client = claude_client(_status_error(400, "max_tokens too large"), fallback=stand_in)
    with pytest.raises(LlmError, match="refused the request"):
        await flood(client)
    assert stand_in.seen == []


# --- the fallback -----------------------------------------------------------


async def test_a_volume_call_falls_back_to_the_local_model_and_says_so() -> None:
    bus = RecordingBus()
    stand_in = MockBackend(model="ollama:llama3.1:8b")
    client = claude_client(_status_error(529, "overloaded"), bus=bus, fallback=stand_in)
    result = await flood(client)

    assert result.value.waterway == "Shoal Creek"
    assert result.call.model == "ollama:llama3.1:8b"
    assert result.call.fallback == "ollama:llama3.1:8b"
    assert result.call.usd == 0.0
    assert len(stand_in.seen) == 1
    assert "fallback" in result.call.summary()


async def test_a_rate_limit_is_a_fallback_not_a_failure() -> None:
    stand_in = MockBackend(model="ollama:llama3.1:8b")
    client = claude_client(_status_error(429, "slow down"), fallback=stand_in)
    assert (await flood(client)).call.fallback == "ollama:llama3.1:8b"


async def test_a_connection_failure_is_a_fallback_too() -> None:
    stand_in = MockBackend(model="ollama:llama3.1:8b")
    unreachable = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api"))
    client = claude_client(unreachable, fallback=stand_in)
    assert (await flood(client)).call.fallback == "ollama:llama3.1:8b"


async def test_the_local_model_gets_its_own_retry() -> None:
    stand_in = MockBackend(model="ollama:llama3.1:8b")
    stand_in.script("sentinel_flood_fields", "{", FLOOD_PAYLOAD)
    client = claude_client(_status_error(500, "boom"), fallback=stand_in)
    result = await flood(client)
    assert result.call.attempts == 2
    assert result.call.ok


async def test_a_judgment_call_does_not_fall_back_to_the_local_model() -> None:
    """Sonnet's job is judgment; a 7B model's answer would read the same and not be."""
    bus = RecordingBus()
    stand_in = MockBackend(model="ollama:llama3.1:8b")
    client = claude_client(_status_error(503, "down"), bus=bus, fallback=stand_in)
    with pytest.raises(JudgmentUnavailable, match="no judgment model answered"):
        await flood(client, tier=Tier.JUDGMENT)
    assert stand_in.seen == []
    (call,) = bus.on("audit")
    assert call.ok is False
    assert call.tier is Tier.JUDGMENT


async def test_a_volume_call_with_nowhere_left_to_go_raises() -> None:
    client = claude_client(_status_error(503, "down"))
    with pytest.raises(LlmUnavailable, match="503"):
        await flood(client)


# --- ollama mode ------------------------------------------------------------


async def test_ollama_mode_refuses_judgment_before_it_spends_anything() -> None:
    """`HU_LLM_MODE=ollama uv run hu demo s1`: fusion and policy use rules."""
    bus = RecordingBus()
    client = LlmClient(
        mode=Mode.OLLAMA,
        backends={Tier.JUDGMENT: None, Tier.VOLUME: MockBackend(model="ollama:test")},
        cache=ResponseCache(enabled=False),
        meter=SpendMeter(bus=bus),
    )
    with pytest.raises(JudgmentUnavailable, match="rules path"):
        await flood(client, tier=Tier.JUDGMENT)
    assert bus.published == []

    result = await flood(client)
    assert result.call.model == "ollama:test"
    assert result.call.usd == 0.0


async def test_the_local_backend_asks_for_the_schema_and_reports_its_tokens() -> None:
    sent: dict = {}

    class FakeOllama:
        host = "http://localhost:11434"

        async def chat(self, **kwargs):
            sent.update(kwargs)
            return ChatResponse(
                model=kwargs["model"],
                message=ollama.Message(role="assistant", content='{"severity_hint": 3}'),
                prompt_eval_count=412,
                eval_count=17,
                done=True,
            )

    backend = OllamaBackend("llama3.1:8b", client=FakeOllama())
    result = await backend.send(request())

    assert backend.model == "ollama:llama3.1:8b", "the cache key must not confuse the two"
    assert sent["model"] == "llama3.1:8b"
    assert sent["messages"][0] == {"role": "system", "content": f"{RULES}\n\n{PLAYBOOK}"}
    assert "severity_hint" in sent["format"]["properties"]
    assert result.value.severity_hint == 3
    assert result.use.input_tokens == 412
    assert result.use.output_tokens == 17


async def test_an_unreachable_ollama_says_where_it_looked() -> None:
    class Down:
        host = "http://localhost:11434"

        async def chat(self, **kwargs):
            raise ollama.ResponseError("connection refused")

    backend = OllamaBackend("llama3.1:8b", client=Down())
    with pytest.raises(LlmUnavailable, match=re.escape("llama3.1:8b")):
        await backend.send(request())


async def test_a_local_reply_that_misses_the_schema_is_a_parse_failure() -> None:
    class Loose:
        async def chat(self, **kwargs):
            return ChatResponse(
                model="llama3.1:8b",
                message=ollama.Message(role="assistant", content="sure, here you go"),
                done=True,
            )

    backend = OllamaBackend("llama3.1:8b", client=Loose())
    with pytest.raises(LlmParseError):
        await backend.send(request())


def _status_error(status: int, message: str) -> anthropic.APIStatusError:
    response = httpx.Response(
        status_code=status,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        json={"error": {"type": "error", "message": message}},
    )
    return anthropic.APIStatusError(message, response=response, body=None)
