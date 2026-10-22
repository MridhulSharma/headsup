"""The vocabulary of the LLM layer: modes, tiers, requests, replies and errors.

Two tiers, named for what the call is for rather than which model answers it:
`judgment` writes prose and reconciles conflicting sources, `volume` fills
fixed schema fields from feed text thousands of times a run. A caller asks for
a tier; `HU_LLM_MODE` decides what is behind it.

Three rules are structural here rather than left to each caller:

* A reply is a Pydantic model or it is an error. Nothing downstream ever sees
  model prose it has to interpret.
* `system` blocks are *fixed* text - prompts from the codebase, playbook caps.
  Anything that varies per call, and in particular anything that came off a
  feed, belongs in `messages`, wrapped by `data_block`. That is the hard rule
  in CLAUDE.md made hard to break: there is no instruction slot to put feed
  text in.
* The retry after a parse failure never quotes the model's reply back at it.
  It states which fields were wrong, in our words. Model text is data too.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeVar

from pydantic import BaseModel

#: mock | ollama | claude. Unit tests run with mock and spend nothing.
ENV_MODE = "HU_LLM_MODE"
#: Dollars this process may spend before every further call aborts. 0 = no cap.
ENV_BUDGET = "HU_LLM_BUDGET_USD"
#: Where the disk response cache lives. Defaults to `data/cache/llm`.
ENV_CACHE_DIR = "HU_LLM_CACHE"
#: Where mock mode reads replies from. Defaults to `tests/fixtures/llm`.
ENV_FIXTURE_DIR = "HU_LLM_FIXTURES"
#: The local model Ollama serves for volume calls and for the offline demo.
ENV_OLLAMA_MODEL = "HU_OLLAMA_MODEL"

DEFAULT_MAX_TOKENS = 4096

#: A purpose names the call site (`fusion_rationale`), picks the mock fixture
#: file, and is the field the eval harness groups spend by. Narrow on purpose:
#: it is a filename and an audit key, so no caller can smuggle text through it.
PURPOSE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

M = TypeVar("M", bound=BaseModel)


class Mode(StrEnum):
    """Which backend answers, from `HU_LLM_MODE`."""

    #: Replies come from `tests/fixtures/llm/`. No network, no spend.
    MOCK = "mock"
    #: A local model over Ollama. Volume calls only; judgment has no stand-in.
    OLLAMA = "ollama"
    #: The real API, both tiers.
    CLAUDE = "claude"


class Tier(StrEnum):
    """What a call is for, which is what picks the model."""

    #: Prose, reconciliation, policy choices. Sonnet.
    JUDGMENT = "judgment"
    #: Fixed-field fills over feed text, at the rate feeds arrive. Haiku.
    VOLUME = "volume"


class LlmError(RuntimeError):
    """Base class for every failure this layer reports."""


class LlmParseError(LlmError):
    """A reply did not validate against the requested model.

    Raised by a backend for one attempt and re-raised by the client once the
    single retry has also failed. `detail` lists the offending fields and is
    safe to log: it holds the validator's own wording, never the reply's text.
    `use` carries what the rejected attempt cost, because the provider billed
    for it and the meter has to as well.
    """

    def __init__(
        self, detail: str, *, use: TokenUse | None = None, model: str | None = None
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.use = use
        self.model = model


class LlmUnavailable(LlmError):
    """The backend could not be reached, or refused the request."""


class JudgmentUnavailable(LlmUnavailable):
    """No judgment-grade model is available in this mode.

    Deliberately its own class: every judgment call site has a rules-only path
    (variant V1 runs on nothing else), so this is the signal to take that path
    rather than an error to report upward.
    """


class BudgetExceeded(LlmError):
    """`HU_LLM_BUDGET_USD` is spent. Raised before any further call is made."""

    def __init__(self, spent_usd: float, budget_usd: float) -> None:
        super().__init__(
            f"LLM budget spent: ${spent_usd:.4f} of ${budget_usd:.2f} "
            f"({ENV_BUDGET}); no further calls will be made"
        )
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd


@dataclass(frozen=True, slots=True)
class Message:
    """One turn. `content` may carry feed text, as long as `data_block` wrapped it."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class TokenUse:
    """What one attempt cost in tokens, as the provider reported it."""

    input_tokens: int = 0
    output_tokens: int = 0
    #: Prefix served from the provider's prompt cache, at a tenth of the price.
    cache_read_tokens: int = 0
    #: Prefix written to it, at a quarter above the price.
    cache_write_tokens: int = 0

    def __add__(self, other: TokenUse) -> TokenUse:
        """Sum two attempts: a retry is billed on top of the attempt it follows."""
        return TokenUse(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def total_input_tokens(self) -> int:
        """Every input token, cached or not: what the long-context rate is judged on."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True, slots=True)
class Request[M: BaseModel]:
    """One call, before a model has been chosen for it.

    This is the object the cache key is computed from, so everything that
    changes the answer is in it and nothing that does not - no timestamps, no
    request ids, no run id.
    """

    purpose: str
    tier: Tier
    schema: type[M]
    #: Fixed system blocks, in order. The provider's cache breakpoint goes
    #: after the last one, so these must not vary call to call.
    system: tuple[str, ...]
    messages: tuple[Message, ...]
    max_tokens: int = DEFAULT_MAX_TOKENS
    #: Picks `<purpose>.<variant>.json` in mock mode. Ignored elsewhere.
    variant: str | None = None

    def __post_init__(self) -> None:
        if not PURPOSE_PATTERN.fullmatch(self.purpose):
            raise LlmError(
                f"{self.purpose!r} is not a usable purpose: lowercase letters, "
                "digits and underscores, starting with a letter"
            )
        if not self.messages:
            raise LlmError(f"{self.purpose}: a request needs at least one message")
        if self.messages[0].role != "user":
            raise LlmError(f"{self.purpose}: the first message must be the user's")

    def with_messages(self, messages: Sequence[Message]) -> Request[M]:
        """The same request with a different turn list, for the retry."""
        return Request(
            purpose=self.purpose,
            tier=self.tier,
            schema=self.schema,
            system=self.system,
            messages=tuple(messages),
            max_tokens=self.max_tokens,
            variant=self.variant,
        )


@dataclass(frozen=True, slots=True)
class Reply[M: BaseModel]:
    """One successful attempt: a validated model and what it cost."""

    value: M
    use: TokenUse
    model: str


def mode_from_env() -> Mode:
    """`HU_LLM_MODE`, defaulting to `mock`.

    Mock is the default rather than `claude` on purpose: a process that was
    meant to spend money and did not is a confusing demo, while a process that
    spent money nobody meant it to spend is a bad afternoon.
    """
    raw = os.environ.get(ENV_MODE, "").strip().lower()
    if not raw:
        return Mode.MOCK
    try:
        return Mode(raw)
    except ValueError:
        allowed = "|".join(mode.value for mode in Mode)
        raise LlmError(f"{ENV_MODE}={raw!r} is not one of {allowed}") from None


def data_block(name: str, text: str, *, limit: int = 2000) -> str:
    """Wrap untrusted text - feed payloads, model output - as data for a turn.

    The angle brackets and ampersands in `text` are escaped, so nothing inside
    can close the block and start giving instructions, and the result is capped
    so one verbose alert cannot push the real prompt out of the context window.
    """
    if not PURPOSE_PATTERN.fullmatch(name):
        raise LlmError(f"{name!r} is not a usable data block name")
    body = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(body) > limit:
        body = body[:limit] + "\n[truncated]"
    return f'<data name="{name}">\n{body}\n</data>'


def schema_correction(schema: type[BaseModel], detail: str) -> str:
    """The user turn that follows an unparseable reply.

    It names the model and the fields the validator rejected and asks again.
    It does not include the reply: feeding a model's own text back into an
    instruction is the same mistake as feeding it a feed's.
    """
    return (
        f"That reply did not validate as {schema.__name__}. "
        f"The validator rejected: {detail}. "
        "Answer again with a single JSON object that satisfies the schema, "
        "and no other text."
    )


def validation_detail(error: Exception) -> str:
    """Field-by-field reason a reply failed, with no values from the reply in it."""
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return str(error)[:500]
    parts = [
        f"{'.'.join(str(part) for part in item.get('loc', ())) or '(root)'}: "
        f"{item.get('msg', 'invalid')}"
        for item in errors()
    ]
    return "; ".join(parts[:10])[:500] or "no fields reported"
