"""The S1 fixture replays onto the real Redis streams.

Needs `uv run hu infra up`. Each run uses its own stream namespace, so this
never disturbs the streams a demo is using.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import redis.exceptions

from headsup.bus import ALL_STREAMS, Bus, events_stream
from headsup.contracts import CivicEvent, Source
from headsup.ingest import FakeClock, Recording, Replayer
from headsup.ingest.recorder import PROJECT_ROOT

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
HINT = "redis unreachable - run `uv run hu infra up`"
FIXTURE_DIR = PROJECT_ROOT / "data" / "recordings" / "s1_flood" / "fixture"


@pytest.fixture
async def bus() -> AsyncIterator[Bus]:
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


async def test_replay_puts_each_feed_on_its_own_events_stream(bus: Bus) -> None:
    recording = Recording.at(FIXTURE_DIR, scenario="s1_flood")
    replayer = Replayer(
        recording,
        bus=bus,
        clock=FakeClock(datetime(2026, 10, 20, tzinfo=UTC)),
        speed=60.0,
    )
    emissions, report = await replayer.drain()

    assert all(item.entry_id for item in emissions), "every event should carry its entry id"
    assert await bus.length(events_stream(Source.NWS)) == report.by_source[Source.NWS]
    assert await bus.length(events_stream(Source.USGS)) == report.by_source[Source.USGS]


async def test_what_comes_back_off_the_stream_is_what_went_on(bus: Bus) -> None:
    recording = Recording.at(FIXTURE_DIR, scenario="s1_flood")
    emissions, _ = await Replayer(
        recording,
        bus=bus,
        clock=FakeClock(datetime(2026, 10, 20, tzinfo=UTC)),
        speed=60.0,
    ).drain()
    published = [item.event for item in emissions if item.source is Source.NWS]

    replayed = [
        message.parse(CivicEvent) async for message in bus.replay(events_stream(Source.NWS))
    ]
    assert [event.to_wire() for event in replayed] == [event.to_wire() for event in published]
    assert replayed[0].kind == "flash_flood_warning"
    assert replayed[0].geometry.type == "Polygon"


async def test_a_consumer_group_reads_the_flood_warnings_in_order(bus: Bus) -> None:
    recording = Recording.at(FIXTURE_DIR, scenario="s1_flood")
    await Replayer(
        recording,
        bus=bus,
        clock=FakeClock(datetime(2026, 10, 20, tzinfo=UTC)),
        speed=60.0,
    ).drain()

    stream = events_stream(Source.NWS)
    events = [
        message.parse(CivicEvent)
        async for message in bus.consume(
            stream, "flood_sentinel", "worker-1", auto_ack=True, limit=9
        )
    ]
    assert len(events) == 9
    observed = [event.observed_at for event in events]
    assert observed == sorted(observed)
    assert await bus.pending_count(stream, "flood_sentinel") == 0
