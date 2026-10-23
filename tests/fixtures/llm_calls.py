"""What the LLM tests share: the schemas the fixtures answer, and two doubles.

Every schema here now belongs to the agent that asks the question -
`FloodFields` to the flood sentinel, `FusionRationale` to fusion,
`ProposedAction` to the proposer - and all three are re-exported so the LLM
tests keep asserting against the *real* schemas: a reply shape that drifts out
of an agent's contract fails the unit run rather than the demo.

`FakeAnthropic` is a stand-in for the SDK client: it records the request body
it was handed, which is the only way to assert on prompt-cache placement
without spending money, and replies with whatever the test queued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import orjson

from headsup.agents.fusion import FusionRationale
from headsup.agents.proposer import ProposedAction
from headsup.agents.sentinels import FloodFields

# --- the shapes the fixtures answer ----------------------------------------

#: Each agent's own schema, re-exported for the LLM tests.
__all__ = [
    "FLOOD_PAYLOAD",
    "FakeAnthropic",
    "FakeBlock",
    "FakeMessages",
    "FakeResponse",
    "FakeUsage",
    "FloodFields",
    "FusionRationale",
    "ProposedAction",
    "RecordingBus",
    "reply",
    "truncated",
]


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
