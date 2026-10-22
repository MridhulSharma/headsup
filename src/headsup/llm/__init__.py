"""Tiered Claude client with prompt-hash cache, Ollama fallback and spend meter.

One client, two tiers, three modes. Import from this package rather than from
the submodules, so the surface the agents use stays one list:

    from headsup.llm import LlmClient, Tier, data_block

`judgment` (claude-sonnet-5-5) writes prose and reconciles conflicting
sources; `volume` (claude-haiku-5-5) fills fixed schema fields from feed text
at feed rate. `HU_LLM_MODE` picks what answers: `mock` serves checked-in
fixtures and is what the unit run uses, `ollama` serves volume calls from a
local model and refuses judgment calls, `claude` calls the API.

Every call returns a validated Pydantic model or raises, writes one `LlmCall`
to the `audit` stream, and stops the run when `HU_LLM_BUDGET_USD` is gone.
"""

from __future__ import annotations

from headsup.llm.backends import (
    DEFAULT_OLLAMA_MODEL,
    Backend,
    ClaudeBackend,
    MockBackend,
    OllamaBackend,
    json_schema_for,
    parse_payload,
    system_blocks,
)
from headsup.llm.base import (
    DEFAULT_MAX_TOKENS,
    ENV_BUDGET,
    ENV_CACHE_DIR,
    ENV_FIXTURE_DIR,
    ENV_MODE,
    ENV_OLLAMA_MODEL,
    BudgetExceeded,
    JudgmentUnavailable,
    LlmError,
    LlmParseError,
    LlmUnavailable,
    Message,
    Mode,
    Reply,
    Request,
    Tier,
    TokenUse,
    data_block,
    mode_from_env,
    schema_correction,
)
from headsup.llm.cache import DEFAULT_CACHE_DIR, ResponseCache, fingerprint
from headsup.llm.client import MAX_ATTEMPTS, LlmClient, LlmResult
from headsup.llm.fixtures import DEFAULT_FIXTURE_DIR, FixtureStore
from headsup.llm.meter import DEFAULT_BUDGET_USD, LlmCall, SpendMeter, budget_from_env
from headsup.llm.pricing import (
    EFFORT_BY_TIER,
    MODEL_BY_TIER,
    PRICES,
    Price,
    cost_usd,
    effort_for,
    model_for,
    price_for,
)

__all__ = [
    "DEFAULT_BUDGET_USD",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_FIXTURE_DIR",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_OLLAMA_MODEL",
    "EFFORT_BY_TIER",
    "ENV_BUDGET",
    "ENV_CACHE_DIR",
    "ENV_FIXTURE_DIR",
    "ENV_MODE",
    "ENV_OLLAMA_MODEL",
    "MAX_ATTEMPTS",
    "MODEL_BY_TIER",
    "PRICES",
    "Backend",
    "BudgetExceeded",
    "ClaudeBackend",
    "FixtureStore",
    "JudgmentUnavailable",
    "LlmCall",
    "LlmClient",
    "LlmError",
    "LlmParseError",
    "LlmResult",
    "LlmUnavailable",
    "Message",
    "MockBackend",
    "Mode",
    "OllamaBackend",
    "Price",
    "Reply",
    "Request",
    "ResponseCache",
    "SpendMeter",
    "Tier",
    "TokenUse",
    "budget_from_env",
    "cost_usd",
    "data_block",
    "effort_for",
    "fingerprint",
    "json_schema_for",
    "mode_from_env",
    "model_for",
    "parse_payload",
    "price_for",
    "schema_correction",
    "system_blocks",
]
