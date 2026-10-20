"""The bus against the Docker Redis: publish, consume with groups, ack, replay.

Needs `uv run hu infra up`. Every test runs inside its own stream namespace, so
these never touch the streams a demo is using.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import redis.exceptions

from headsup.bus import (
    ALL_STREAMS,
    STREAM_ADVISORIES,
    STREAM_HAZARDS,
    STREAM_PROPOSALS,
    Bus,
    BusError,
)
from headsup.contracts import Advisory, CivicEvent, Hazard, Telemetry
from samples import ACK, ADVISORY, HAZARD, NWS_EVENT, PROPOSAL, TELEMETRY

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
HINT = "redis unreachable - run `uv run hu infra up`"


@pytest.fixture
async def bus() -> AsyncIterator[Bus]:
    """A bus in a throwaway namespace, cleaned up afterwards."""
    instance = Bus(REDIS_URL, namespace=f"test_{uuid4().hex[:8]}")
    try:
        await instance.ping()
    except redis.exceptions.ConnectionError as exc:
        await instance.close()
        pytest.fail(f"{HINT}: {REDIS_URL} ({exc})")
    try:
        yield instance
    finally:
        await instance.delete_streams(ALL_STREAMS)
        await instance.close()


@pytest.fixture
async def compiler_bus(bus: Bus) -> AsyncIterator[Bus]:
    """A second bus on the same namespace, allowed to write advisories."""
    instance = Bus(REDIS_URL, namespace=bus.namespace, writer="compiler", client=bus.client)
    yield instance
    await instance.close()


async def test_publish_returns_a_stream_id(bus: Bus) -> None:
    entry_id = await bus.publish(HAZARD)
    assert "-" in entry_id
    assert await bus.length(STREAM_HAZARDS) == 1
    assert await bus.last_id(STREAM_HAZARDS) == entry_id


async def test_publish_routes_an_event_to_its_feed_stream(bus: Bus) -> None:
    await bus.publish(NWS_EVENT)
    assert await bus.length("events.nws") == 1
    assert await bus.length("events.usgs") == 0


async def test_consume_delivers_the_message_typed(bus: Bus) -> None:
    await bus.publish(HAZARD)
    received = [
        message async for message in bus.consume(STREAM_HAZARDS, "fusion", "worker-1", limit=1)
    ]
    assert len(received) == 1
    assert received[0].type == "Hazard"
    assert received[0].parse(Hazard) == HAZARD


async def test_an_entry_parses_without_being_told_its_type(bus: Bus) -> None:
    """The `type` field on the entry is enough for a mixed-stream consumer."""
    await bus.publish(NWS_EVENT)
    message = (await bus.read_once("events.nws", "ingest", "worker-1", count=1))[0]
    parsed = message.parse()
    assert isinstance(parsed, CivicEvent)
    assert parsed == NWS_EVENT


async def test_consume_preserves_publish_order(bus: Bus) -> None:
    for index in range(5):
        await bus.publish(HAZARD.model_copy(update={"id": f"haz_{index}"}))
    received = [
        message.parse(Hazard).id
        async for message in bus.consume(STREAM_HAZARDS, "fusion", "worker-1", limit=5)
    ]
    assert received == [f"haz_{index}" for index in range(5)]


async def test_two_consumers_in_one_group_split_the_work(bus: Bus) -> None:
    for index in range(4):
        await bus.publish(HAZARD.model_copy(update={"id": f"haz_{index}"}))

    first = await bus.read_once(STREAM_HAZARDS, "fusion", "worker-1", count=2)
    second = await bus.read_once(STREAM_HAZARDS, "fusion", "worker-2", count=2)
    assert {message.id for message in first}.isdisjoint({message.id for message in second})
    assert len(first) == len(second) == 2


async def test_ack_clears_the_pending_entry(bus: Bus) -> None:
    await bus.publish(HAZARD)
    message = (await bus.read_once(STREAM_HAZARDS, "fusion", "worker-1"))[0]
    assert await bus.pending_count(STREAM_HAZARDS, "fusion") == 1
    assert await bus.ack(message) == 1
    assert await bus.pending_count(STREAM_HAZARDS, "fusion") == 0


async def test_auto_ack_leaves_nothing_pending(bus: Bus) -> None:
    await bus.publish(PROPOSAL)
    async for _ in bus.consume(STREAM_PROPOSALS, "critic", "worker-1", limit=1, auto_ack=True):
        pass
    assert await bus.pending_count(STREAM_PROPOSALS, "critic") == 0


async def test_an_unacked_entry_comes_back_to_the_same_consumer(bus: Bus) -> None:
    """A worker that died mid-message must see it again on restart."""
    await bus.publish(HAZARD)
    first = await bus.read_once(STREAM_HAZARDS, "fusion", "worker-1")
    assert len(first) == 1

    redelivered = [
        message
        async for message in bus.consume(
            STREAM_HAZARDS, "fusion", "worker-1", limit=1, claim_pending=True
        )
    ]
    assert [message.id for message in redelivered] == [first[0].id]


async def test_consume_stops_when_idle(bus: Bus) -> None:
    received = [
        message
        async for message in bus.consume(
            STREAM_HAZARDS, "fusion", "worker-1", block_ms=50, stop_when_idle=True
        )
    ]
    assert received == []


async def test_a_new_group_reads_the_backlog(bus: Bus) -> None:
    """Groups default to the start of the stream, so a late agent catches up."""
    await bus.publish(HAZARD)
    received = [
        message async for message in bus.consume(STREAM_HAZARDS, "late-agent", "w1", limit=1)
    ]
    assert len(received) == 1


async def test_replay_reads_the_whole_stream_in_order(bus: Bus) -> None:
    for index in range(3):
        await bus.publish(HAZARD.model_copy(update={"id": f"haz_{index}"}))
    ids = [message.parse(Hazard).id async for message in bus.replay(STREAM_HAZARDS)]
    assert ids == ["haz_0", "haz_1", "haz_2"]


async def test_replay_from_a_stream_id(bus: Bus) -> None:
    entry_ids = [
        await bus.publish(HAZARD.model_copy(update={"id": f"haz_{index}"})) for index in range(4)
    ]

    inclusive = [
        message.parse(Hazard).id async for message in bus.replay(STREAM_HAZARDS, start=entry_ids[2])
    ]
    assert inclusive == ["haz_2", "haz_3"]

    exclusive = [
        message.parse(Hazard).id
        async for message in bus.replay(STREAM_HAZARDS, start=entry_ids[2], exclusive=True)
    ]
    assert exclusive == ["haz_3"]


async def test_replay_pages_and_honours_a_limit(bus: Bus) -> None:
    for index in range(7):
        await bus.publish(HAZARD.model_copy(update={"id": f"haz_{index}"}))
    ids = [
        message.parse(Hazard).id async for message in bus.replay(STREAM_HAZARDS, batch=2, limit=5)
    ]
    assert ids == [f"haz_{index}" for index in range(5)]


async def test_replay_does_not_consume(bus: Bus) -> None:
    """Replay is for the auditor and the eval harness; it leaves groups alone."""
    await bus.publish(HAZARD)
    assert len([message async for message in bus.replay(STREAM_HAZARDS)]) == 1
    assert len([message async for message in bus.replay(STREAM_HAZARDS)]) == 1
    assert await bus.pending_count(STREAM_HAZARDS, "fusion") == 0


async def test_replay_of_an_absent_stream_is_empty(bus: Bus) -> None:
    assert [message async for message in bus.replay("events.bexar")] == []
    assert await bus.last_id("events.bexar") is None


async def test_only_the_compiler_may_publish_advisories(bus: Bus, compiler_bus: Bus) -> None:
    """CLAUDE.md hard rule, enforced client-side; D1.3 adds the Redis ACL."""
    with pytest.raises(BusError, match="only compiler may publish"):
        await bus.publish(ADVISORY)
    assert await bus.length(STREAM_ADVISORIES) == 0

    entry_id = await compiler_bus.publish(ADVISORY)
    assert entry_id
    assert await bus.length(STREAM_ADVISORIES) == 1

    message = (await bus.read_once(STREAM_ADVISORIES, "operators", "op_a"))[0]
    assert message.parse(Advisory) == ADVISORY


async def test_telemetry_can_be_capped(bus: Bus) -> None:
    """Telemetry at 2 Hz for a long run must not grow without a bound."""
    for tick in range(20):
        await bus.publish(TELEMETRY.model_copy(update={"tick": tick}), maxlen=5, approximate=False)
    assert await bus.length("telemetry") == 5
    ticks = [message.parse(Telemetry).tick async for message in bus.replay("telemetry")]
    assert ticks == [15, 16, 17, 18, 19]


async def test_acks_round_trip(bus: Bus) -> None:
    await bus.publish(ACK)
    message = (await bus.read_once("acks", "coordinator", "worker-1"))[0]
    assert message.parse().to_wire() == ACK.to_wire()


async def test_publishing_a_mapping_needs_a_stream(bus: Bus) -> None:
    with pytest.raises(BusError, match="explicit stream"):
        await bus.publish({"type": "audit", "note": "compile rejected"})
    entry_id = await bus.publish({"type": "audit", "note": "compile rejected"}, stream="audit")
    assert entry_id
    message = (await bus.read_once("audit", "auditor", "worker-1"))[0]
    assert message.data["note"] == "compile rejected"


async def test_an_entry_that_does_not_match_its_model_is_an_error(bus: Bus) -> None:
    await bus.publish({"type": "Hazard", "severity": 99}, stream=STREAM_HAZARDS)
    message = (await bus.read_once(STREAM_HAZARDS, "fusion", "worker-1"))[0]
    with pytest.raises(BusError, match="does not match Hazard"):
        message.parse()


async def test_namespaces_isolate_two_runs(bus: Bus) -> None:
    other = Bus(REDIS_URL, namespace=f"test_{uuid4().hex[:8]}", client=bus.client)
    try:
        await bus.publish(HAZARD)
        assert await other.length(STREAM_HAZARDS) == 0
    finally:
        await other.delete_streams(ALL_STREAMS)
        await other.close()


async def test_ensure_group_is_idempotent(bus: Bus) -> None:
    assert await bus.ensure_group(STREAM_HAZARDS, "fusion") is True
    assert await bus.ensure_group(STREAM_HAZARDS, "fusion") is False
