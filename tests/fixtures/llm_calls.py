"""What the LLM tests share: the schemas the fixtures answer, and two doubles.

The reply schemas live here rather than in `src` because the agents that will
ask these questions arrive in D2.4 and D3.1. They are the shapes
`tests/fixtures/llm/*.json` was written against, so a fixture that drifts out
of contract fails the unit run.

`FakeAnthropic` is a stand-in for the SDK client: it records the request body
it was handed, which is the only way to assert on prompt-cache placement
without spending money, and replies with whatever the test queued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import orjson
from pydantic import Field

from headsup.contracts import (
    ActionType,
    Confidence,
    FreeText,
    HeadsUpModel,
    Severity,
    Source,
)

# --- the shapes the fixtures answer ----------------------------------------


class FloodFields(HeadsUpModel):
    """What the flood sentinel lets a volume call take from NWS free text.

    Every field is a fact to be looked up, never an instruction: the sentinel
    decides what to do with them, the model only fills them in.
    """

    waterway: FreeText | None = None
    severity_hint: Severity
    evacuation_mentioned: bool = False
    roads_named: list[FreeText] = Field(default_factory=list, max_length=10)


class FusionRationale(HeadsUpModel):
    """The paragraph fusion attaches to a hazard, and which source led."""

    rationale: FreeText
    primary_source: Source
    conflicts: list[FreeText] = Field(default_factory=list, max_length=5)


class ProposedAction(HeadsUpModel):
    """The proposer's choice, which the playbook then has to allow."""

    action: ActionType
    reason: FreeText
    confidence: Confidence


# --- doubles ---------------------------------------------------------------


@dataclass(slots=True)
class RecordingBus:
    """An audit sink that keeps what it was given. No Redis in a unit test."""

    published: list[tuple[str, Any]] = field(default_factory=list)

    async def publish(self, message: Any, *, stream: str = "audit") -> str:
        self.published.append((stream, message))
        return f"0-{len(self.published)}"

    def on(self, stream: str) -> list[Any]:
        return [message for name, message in self.published if name == stream]


@dataclass(frozen=True, slots=True)
class FakeBlock:
    text: str
    type: str = "text"


@dataclass(frozen=True, slots=True)
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class FakeResponse:
    content: list[FakeBlock]
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_reason: str = "end_turn"
    stop_details: Any = None


@dataclass(slots=True)
class FakeMessages:
    replies: list[Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **payload: Any) -> Any:
        self.calls.append(payload)
        if not self.replies:
            raise AssertionError("the fake Claude client was called more times than scripted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeAnthropic:
    """Scripted replies, and the request bodies that asked for them."""

    def __init__(self, *replies: Any) -> None:
        self.messages = FakeMessages(list(replies))
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def reply(payload: dict[str, Any] | str, **usage: int) -> FakeResponse:
    """One scripted reply: a payload (or raw text) and its token counts."""
    text = payload if isinstance(payload, str) else orjson.dumps(payload).decode()
    return FakeResponse(content=[FakeBlock(text=text)], usage=FakeUsage(**usage))


def truncated(**usage: int) -> FakeResponse:
    """A reply the model ran out of room to finish."""
    return FakeResponse(
        content=[FakeBlock(text='{"severity_hint": 4, "roads_nam')],
        usage=FakeUsage(**usage),
        stop_reason="max_tokens",
    )


#: A plausible flood reply, for tests that do not care what is in it.
FLOOD_PAYLOAD: dict[str, Any] = {
    "waterway": "Shoal Creek",
    "severity_hint": 4,
    "evacuation_mentioned": False,
    "roads_named": ["Lamar Blvd at 15th St"],
}
