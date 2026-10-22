"""The spend meter: one audit record per call, and a hard stop at the budget.

Every call through this layer writes one `LlmCall` to the `audit` stream,
whether it reached a model, came back from the disk cache, fell back to Ollama
or failed to parse. Two things depend on that being exceptionless: the
ablation computes LLM dollars per simulated hour by reading the audit log and
nothing else, and the budget is only a guarantee if nothing can spend without
being counted.

    meter = SpendMeter.from_env(bus=bus)
    meter.check()                    # raises BudgetExceeded before spending
    call = await meter.record(call)  # charges, publishes, then raises if over

The budget is a backstop against a loop, not a precise ledger: the call that
crosses the line is paid for, recorded and then raises, and every call after
it aborts before spending anything. Set a hard limit in the Anthropic Console
as well - this process cannot enforce one on a process it does not run.
"""

from __future__ import annotations

import os
from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, StringConstraints

from headsup.bus import STREAM_AUDIT
from headsup.contracts import HeadsUpModel, Identifier, Kind, UtcDatetime, new_id, utcnow
from headsup.llm.base import ENV_BUDGET, BudgetExceeded, Mode, Tier

#: Spent before any call is made, when `HU_LLM_BUDGET_USD` is unset. A research
#: prototype should not be able to empty an account because someone forgot an
#: environment variable; `HU_LLM_BUDGET_USD=0` is how you ask for no cap.
DEFAULT_BUDGET_USD = 5.00

#: Error text and model names: short, because an audit record is read in bulk.
MeterText = Annotated[str, StringConstraints(max_length=500)]

#: How many records a meter keeps in memory for the CLI and the tests. The
#: audit stream is the real record; this is a window on the current process.
DEFAULT_KEEP = 2000


@runtime_checkable
class AuditSink(Protocol):
    """Whatever the records are published to. `Bus` satisfies this."""

    async def publish(self, message: HeadsUpModel, *, stream: str) -> str: ...


class LlmCall(HeadsUpModel):
    """What one call to a model cost, and whether it worked."""

    id: Identifier = Field(default_factory=lambda: new_id("llm"))
    #: The call site: `fusion_rationale`, `sentinel_flood_fields`.
    purpose: Kind
    tier: Tier
    mode: Mode
    #: The model that actually answered: `claude-haiku-5-5`, `ollama:llama3.1:8b`,
    #: or `mock`. Not derivable from the tier, which is the point of recording it.
    model: MeterText

    #: True when the reply came from the disk cache: no tokens, no dollars.
    cached: bool = False
    #: Set when a volume call could not reach the API and a local model
    #: answered instead, so a reader can see which runs were degraded.
    fallback: MeterText | None = None
    #: 1, or 2 when the first reply did not validate.
    attempts: int = 1
    ok: bool = True
    error: MeterText | None = None

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

    #: Dollars for this call alone.
    usd: float = Field(default=0.0, ge=0.0)
    #: Dollars this process has spent including this call.
    spent_usd: float = Field(default=0.0, ge=0.0)
    #: The cap in force, or None when there is none.
    budget_usd: float | None = None

    latency_ms: float = Field(default=0.0, ge=0.0)
    at: UtcDatetime = Field(default_factory=utcnow)

    def summary(self) -> str:
        """One line for a console or a log file."""
        where = "cache" if self.cached else self.model
        head = f"{self.purpose} {self.tier.value} via {where}"
        if self.fallback:
            head += f" (fallback {self.fallback})"
        if not self.ok:
            return f"{head}: FAILED {self.error or ''}".rstrip()
        tokens = f"{self.input_tokens}in/{self.output_tokens}out"
        if self.cache_read_tokens:
            tokens += f"/{self.cache_read_tokens}cached"
        return f"{head}: {tokens} ${self.usd:.6f} (${self.spent_usd:.4f} spent)"


class SpendMeter:
    """Running spend for one process, with the audit write and the budget.

    `bus=None` keeps the records in memory only, which is what a unit test and
    a bare `hu` invocation want: neither has Redis, and neither should need it
    to find out what a call cost.
    """

    def __init__(
        self,
        *,
        budget_usd: float | None = DEFAULT_BUDGET_USD,
        bus: AuditSink | None = None,
        stream: str = STREAM_AUDIT,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        self.budget_usd = budget_usd if budget_usd else None
        self.bus = bus
        self.stream = stream
        self.keep = keep
        self.records: list[LlmCall] = []
        self._spent = 0.0

    @classmethod
    def from_env(cls, *, bus: AuditSink | None = None, stream: str = STREAM_AUDIT) -> SpendMeter:
        """A meter with the budget from `HU_LLM_BUDGET_USD`."""
        return cls(budget_usd=budget_from_env(), bus=bus, stream=stream)

    @property
    def spent_usd(self) -> float:
        """Dollars spent through this meter so far."""
        return round(self._spent, 6)

    @property
    def over_budget(self) -> bool:
        return self.budget_usd is not None and self._spent >= self.budget_usd

    def check(self) -> None:
        """Raise `BudgetExceeded` if there is nothing left to spend."""
        if self.over_budget:
            raise BudgetExceeded(self.spent_usd, float(self.budget_usd or 0.0))

    async def record(self, call: LlmCall) -> LlmCall:
        """Charge a call, publish it to the audit stream, and stop if over budget.

        The record is written before the budget is enforced, so the call that
        crossed the line is in the log like every other one.
        """
        self._spent += call.usd
        call.spent_usd = self.spent_usd
        call.budget_usd = self.budget_usd
        self.records.append(call)
        if len(self.records) > self.keep:
            del self.records[: len(self.records) - self.keep]
        if self.bus is not None:
            await self.bus.publish(call, stream=self.stream)
        self.check()
        return call


def budget_from_env() -> float | None:
    """`HU_LLM_BUDGET_USD` as a cap: unset means the default, 0 means no cap."""
    raw = os.environ.get(ENV_BUDGET, "").strip()
    if not raw:
        return DEFAULT_BUDGET_USD
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{ENV_BUDGET}={raw!r} is not a number of dollars") from exc
    if value < 0:
        raise ValueError(f"{ENV_BUDGET}={raw!r} is negative; use 0 for no cap")
    return value or None
