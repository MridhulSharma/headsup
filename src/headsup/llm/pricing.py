"""Which model answers each tier, and what a call costs in dollars.

The rates are the published first-party API rates as of October 2026, per
million tokens. They are here, in one table, rather than spread across call
sites, because the ablation reports "LLM dollars per simulated hour" and that
number is only worth printing if every call priced itself the same way.

    cost_usd("claude-haiku-5-5", TokenUse(input_tokens=1_000, output_tokens=200))

A model with no entry in `PRICES` costs nothing: that is a local Ollama model
or the mock backend, and `test_pricing` pins that every model a tier can pick
in `claude` mode does have a rate, so a new tier cannot quietly bill $0.
"""

from __future__ import annotations

from dataclasses import dataclass

from headsup.llm.base import Tier, TokenUse

#: Prompt-cache reads are a tenth of the input rate, writes a quarter above it.
#: Both hold across the current models, so they are factors rather than rows.
CACHE_READ_FACTOR = 0.1
CACHE_WRITE_FACTOR = 1.25

A_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class Price:
    """One model's rates, in dollars per million tokens."""

    input_usd: float
    output_usd: float
    #: Haiku charges more above a prompt length; None means one flat rate.
    long_input_usd: float | None = None
    long_output_usd: float | None = None
    long_context_tokens: int = 100_000

    def rates_for(self, input_tokens: int) -> tuple[float, float]:
        """Input and output rates that apply to a prompt of this length."""
        if self.long_input_usd is None or input_tokens <= self.long_context_tokens:
            return self.input_usd, self.output_usd
        return self.long_input_usd, self.long_output_usd or self.output_usd


#: The models behind the two tiers in `claude` mode.
MODEL_BY_TIER: dict[Tier, str] = {
    Tier.JUDGMENT: "claude-sonnet-5-5",
    Tier.VOLUME: "claude-haiku-5-5",
}

#: Effort is the cost lever on both tiers: thinking is adaptive and on by
#: default on these models and cannot be budgeted in tokens any more. Volume
#: calls copy fields out of an alert, which needs none of it; judgment calls
#: run a step below Sonnet's default rather than at it.
EFFORT_BY_TIER: dict[Tier, str] = {
    Tier.JUDGMENT: "medium",
    Tier.VOLUME: "low",
}

PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(input_usd=2.00, output_usd=10.00),
    "claude-haiku-5-5": Price(
        input_usd=0.10, output_usd=0.50, long_input_usd=0.50, long_output_usd=2.50
    ),
}


def model_for(tier: Tier) -> str:
    """The Claude model this tier calls."""
    return MODEL_BY_TIER[tier]


def effort_for(tier: Tier) -> str:
    """The `output_config.effort` this tier runs at."""
    return EFFORT_BY_TIER[tier]


def price_for(model: str) -> Price | None:
    """The rates for a model, or None if it is free to call (local, or mock)."""
    return PRICES.get(model)


def cost_usd(model: str, use: TokenUse) -> float:
    """Dollars for one call's tokens. Zero for a local or mocked model."""
    price = price_for(model)
    if price is None:
        return 0.0
    input_rate, output_rate = price.rates_for(use.total_input_tokens)
    dollars = (
        use.input_tokens * input_rate
        + use.cache_read_tokens * input_rate * CACHE_READ_FACTOR
        + use.cache_write_tokens * input_rate * CACHE_WRITE_FACTOR
        + use.output_tokens * output_rate
    )
    return dollars / A_MILLION
