"""The spend meter: what a run is allowed to cost, and where it stops.

The guarantee is narrow and worth stating exactly: the call that crosses
`HU_LLM_BUDGET_USD` is paid for, recorded, and then raises, and every call
after it raises before anything is sent. A budget is a backstop against a
loop, not a precise ledger - the Console limit is the one that cannot be
argued with.
"""

from __future__ import annotations

import pytest

from fixtures.llm_calls import FLOOD_PAYLOAD, FakeAnthropic, FloodFields, RecordingBus, reply
from headsup.llm import (
    ENV_BUDGET,
    BudgetExceeded,
    ClaudeBackend,
    LlmCall,
    LlmClient,
    Mode,
    ResponseCache,
    SpendMeter,
    Tier,
    budget_from_env,
    effort_for,
    model_for,
)
from headsup.llm.meter import DEFAULT_BUDGET_USD


def call(usd: float, **kwargs) -> LlmCall:
    fields = {
        "purpose": "sentinel_flood_fields",
        "tier": Tier.VOLUME,
        "mode": Mode.CLAUDE,
        "model": "claude-haiku-5-5",
        "usd": usd,
    }
    return LlmCall(**{**fields, **kwargs})


def spendy_client(replies: int, *, budget: float, bus: RecordingBus) -> LlmClient:
    """A `claude` mode client where every reply costs about a tenth of a cent."""
    fake = FakeAnthropic(
        *(reply(FLOOD_PAYLOAD, input_tokens=100_000, output_tokens=1_000) for _ in range(replies))
    )
    return LlmClient(
        mode=Mode.CLAUDE,
        backends={
            tier: ClaudeBackend(model_for(tier), effort=effort_for(tier), client=fake)
            for tier in Tier
        },
        cache=ResponseCache(enabled=False),
        meter=SpendMeter(budget_usd=budget, bus=bus),
    )


# --- reading the budget -----------------------------------------------------


def test_an_unset_budget_is_the_default_cap_not_an_open_account(monkeypatch) -> None:
    monkeypatch.delenv(ENV_BUDGET, raising=False)
    assert budget_from_env() == DEFAULT_BUDGET_USD


def test_zero_is_how_you_ask_for_no_cap(monkeypatch) -> None:
    monkeypatch.setenv(ENV_BUDGET, "0")
    assert budget_from_env() is None
    assert SpendMeter(budget_usd=None).over_budget is False


def test_a_budget_that_is_not_a_number_is_a_startup_error(monkeypatch) -> None:
    monkeypatch.setenv(ENV_BUDGET, "fifty dollars")
    with pytest.raises(ValueError, match=ENV_BUDGET):
        budget_from_env()
    monkeypatch.setenv(ENV_BUDGET, "-5")
    with pytest.raises(ValueError, match="negative"):
        budget_from_env()


def test_the_env_budget_reaches_the_meter(monkeypatch) -> None:
    monkeypatch.setenv(ENV_BUDGET, "2.50")
    assert SpendMeter.from_env().budget_usd == 2.50


# --- the meter --------------------------------------------------------------


async def test_the_meter_adds_up_what_it_was_told() -> None:
    meter = SpendMeter(budget_usd=10.0)
    await meter.record(call(0.25))
    await meter.record(call(0.75))
    assert meter.spent_usd == 1.0
    assert [record.spent_usd for record in meter.records] == [0.25, 1.0]
    assert meter.records[-1].budget_usd == 10.0


async def test_the_crossing_call_is_recorded_before_it_raises() -> None:
    """A run that stopped on budget still has to explain what it stopped on."""
    bus = RecordingBus()
    meter = SpendMeter(budget_usd=1.0, bus=bus)
    await meter.record(call(0.60))
    with pytest.raises(BudgetExceeded, match=r"\$1.0000 of \$1.00"):
        await meter.record(call(0.40))
    assert len(bus.on("audit")) == 2
    assert bus.on("audit")[-1].usd == 0.40


async def test_once_spent_nothing_else_is_sent() -> None:
    meter = SpendMeter(budget_usd=0.5)
    with pytest.raises(BudgetExceeded):
        await meter.record(call(0.5))
    for _ in range(3):
        with pytest.raises(BudgetExceeded):
            meter.check()
    assert meter.spent_usd == 0.5, "check() must not be able to spend anything"


async def test_a_meter_keeps_only_a_window_of_records_in_memory() -> None:
    """A 900-vehicle run must not grow a list until the laptop swaps."""
    meter = SpendMeter(budget_usd=None, keep=3)
    for index in range(10):
        await meter.record(call(0.0, purpose=f"call_{index}"))
    assert [record.purpose for record in meter.records] == ["call_7", "call_8", "call_9"]


# --- the client -------------------------------------------------------------


async def test_the_client_aborts_the_run_when_the_budget_is_gone() -> None:
    bus = RecordingBus()
    client = spendy_client(3, budget=0.015, bus=bus)

    first = await client.complete(
        purpose="sentinel_flood_fields",
        tier=Tier.VOLUME,
        schema=FloodFields,
        messages="first",
    )
    assert first.call.usd == pytest.approx(0.0105)

    with pytest.raises(BudgetExceeded):
        await client.complete(
            purpose="sentinel_flood_fields",
            tier=Tier.VOLUME,
            schema=FloodFields,
            messages="second",
        )

    with pytest.raises(BudgetExceeded):
        await client.complete(
            purpose="sentinel_flood_fields",
            tier=Tier.VOLUME,
            schema=FloodFields,
            messages="third",
        )

    audit = bus.on("audit")
    assert len(audit) == 2, "the third call must not reach the model at all"
    assert client.meter.spent_usd == pytest.approx(0.021)
