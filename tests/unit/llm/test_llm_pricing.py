"""The price table: the ablation's dollar figures are only as good as this.

`uv run hu eval` reports LLM dollars per simulated hour by adding up audit
records, so these tests are about the arithmetic being right and about no
model a tier can pick being silently free.
"""

from __future__ import annotations

import pytest

from headsup.llm import MODEL_BY_TIER, PRICES, Tier, TokenUse, cost_usd, effort_for, model_for
from headsup.llm.pricing import CACHE_READ_FACTOR, CACHE_WRITE_FACTOR


def test_the_tiers_are_the_models_claude_md_names() -> None:
    assert model_for(Tier.JUDGMENT) == "claude-sonnet-5-5"
    assert model_for(Tier.VOLUME) == "claude-haiku-5-5"


def test_every_tier_model_has_a_price_so_a_new_tier_cannot_bill_zero() -> None:
    """An unpriced model costs $0, which is right for Ollama and a lie for the API."""
    for tier, model in MODEL_BY_TIER.items():
        assert model in PRICES, f"the {tier.value} tier calls {model} at no recorded price"


def test_volume_runs_at_lower_effort_than_judgment() -> None:
    """Effort is the only cost lever left: thinking cannot be budgeted any more."""
    assert effort_for(Tier.VOLUME) == "low"
    assert effort_for(Tier.JUDGMENT) == "medium"


def test_a_haiku_call_is_priced_at_the_published_rate() -> None:
    """$0.10 in and $0.50 out per million, under the long-context threshold."""
    use = TokenUse(input_tokens=100_000, output_tokens=100_000)
    assert cost_usd("claude-haiku-5-5", use) == pytest.approx(0.01 + 0.05)


def test_a_sonnet_call_is_priced_at_the_published_rate() -> None:
    use = TokenUse(input_tokens=500_000, output_tokens=100_000)
    assert cost_usd("claude-sonnet-5-5", use) == pytest.approx(1.0 + 1.0)


def test_a_cached_prefix_costs_a_tenth_and_writing_it_costs_a_quarter_more() -> None:
    read = cost_usd("claude-haiku-5-5", TokenUse(cache_read_tokens=1_000_000))
    write = cost_usd("claude-haiku-5-5", TokenUse(cache_write_tokens=1_000_000))
    plain = cost_usd("claude-haiku-5-5", TokenUse(input_tokens=1_000_000))
    assert read == pytest.approx(plain * CACHE_READ_FACTOR)
    assert write == pytest.approx(plain * CACHE_WRITE_FACTOR)


def test_a_long_haiku_prompt_is_billed_at_the_long_context_rate() -> None:
    """A sentinel handed a whole alert bulletin crosses 100K and costs 5x more."""
    short = TokenUse(input_tokens=100_000, output_tokens=1_000)
    long = TokenUse(input_tokens=100_001, output_tokens=1_000)
    assert cost_usd("claude-haiku-5-5", long) > 4 * cost_usd("claude-haiku-5-5", short)


def test_the_threshold_counts_cached_input_too() -> None:
    """A cached prefix is still prompt length, and the provider prices it that way."""
    use = TokenUse(input_tokens=1_000, cache_read_tokens=120_000)
    rate_applied = cost_usd("claude-haiku-5-5", use) / (1_000 + 120_000 * CACHE_READ_FACTOR)
    assert rate_applied == pytest.approx(0.50 / 1_000_000)


def test_a_local_or_mocked_model_is_free() -> None:
    use = TokenUse(input_tokens=50_000, output_tokens=5_000)
    assert cost_usd("ollama:llama3.1:8b", use) == 0.0
    assert cost_usd("mock", use) == 0.0
