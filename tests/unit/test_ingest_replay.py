"""Replay: order, relative timing, and that it reproduces live mode exactly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from fixtures.ingest import NWS_BODY, NWS_URL, USGS_BODY
from headsup.contracts import CivicEvent, Source
from headsup.ingest import (
    ALERTS_URL,
    MAX_SPEED,
    FakeClock,
    IngestError,
    NwsAdapter,
    RawPayload,
    Recorder,
    Recording,
    Replayer,
    UsgsAdapter,
    check_speed,
)
from headsup.ingest.usgs import COLLECTION_LATEST, collection_url

T0 = datetime(2026, 4, 20, 18, 0, tzinfo=UTC)
#: Somewhere else entirely: the replay clock has nothing to do with feed time.
REPLAY_START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def payload(source: Source, offset_s: int, seq: int = 0) -> RawPayload:
    at = T0 + timedelta(seconds=offset_s)
    return RawPayload(
        source=source,
        ref=f"{source.value}/{at:%Y%m%dT%H%M%SZ}/{seq}",
        url=NWS_URL,
        fetched_at=at,
        body=NWS_BODY if source is Source.NWS else USGS_BODY,
    )


def recording_of(tmp_path: Path, *payloads: RawPayload) -> Recording:
    with Recorder("s_test", root=tmp_path) as recorder:
        recorder.write_many(payloads)
    return Recording.locate("s_test", root=tmp_path)


# --- speed ------------------------------------------------------------------


@pytest.mark.parametrize("speed", [1.0, 30.0, 60.0])
def test_speeds_inside_the_range_are_accepted(speed: float) -> None:
    assert check_speed(speed) == speed


@pytest.mark.parametrize("speed", [0.0, 0.5, 60.1, 1000.0, -1.0])
def test_speeds_outside_one_to_sixty_are_refused(speed: float) -> None:
    with pytest.raises(ValueError, match="outside"):
        check_speed(speed)


def test_replayer_refuses_a_bad_speed_at_construction(tmp_path: Path) -> None:
    recording = recording_of(tmp_path, payload(Source.NWS, 0))
    with pytest.raises(ValueError, match="outside"):
        Replayer(recording, speed=120.0)


# --- order and timing -------------------------------------------------------


async def test_replay_at_60x_keeps_the_order_and_divides_the_gaps(tmp_path: Path) -> None:
    # Feed time: NWS at 0 s, USGS at 600 s, NWS again at 1800 s.
    recording = recording_of(
        tmp_path,
        payload(Source.NWS, 0),
        payload(Source.USGS, 600),
        payload(Source.NWS, 1800, seq=1),
    )
    clock = FakeClock(REPLAY_START)
    emissions, report = await Replayer(recording, clock=clock, speed=MAX_SPEED).drain()

    assert [item.source for item in emissions] == [
        Source.NWS,  # the one polygon alert
        Source.USGS,  # two readings in one payload
        Source.USGS,
        Source.NWS,
    ]
    assert [item.feed_offset for item in emissions] == [0.0, 600.0, 600.0, 1800.0]
    # 60x: a ten-minute gap becomes ten seconds, a thirty-minute one thirty.
    assert [item.replay_offset for item in emissions] == [0.0, 10.0, 10.0, 30.0]
    assert clock.slept == [0.0, 10.0, 20.0]
    assert report.events == 4
    assert report.payloads == 3
    assert report.feed_span == 1800.0
    assert report.replay_span == 30.0


async def test_replay_at_1x_waits_out_the_real_gaps(tmp_path: Path) -> None:
    recording = recording_of(tmp_path, payload(Source.NWS, 0), payload(Source.NWS, 120, seq=1))
    clock = FakeClock(REPLAY_START)
    emissions, _ = await Replayer(recording, clock=clock, speed=1.0).drain()
    assert [item.replay_offset for item in emissions] == [0.0, 120.0]
    assert clock.slept == [0.0, 120.0]


async def test_timing_is_measured_from_the_start_so_a_slow_step_does_not_drift(
    tmp_path: Path,
) -> None:
    recording = recording_of(
        tmp_path,
        payload(Source.NWS, 0),
        payload(Source.NWS, 600, seq=1),
        payload(Source.NWS, 1200, seq=2),
    )
    clock = FakeClock(REPLAY_START)
    replayer = Replayer(recording, clock=clock, speed=MAX_SPEED)

    offsets: list[float] = []
    async for emission in replayer.run():
        offsets.append(emission.replay_offset)
        # A consumer that takes four seconds over the first event.
        if len(offsets) == 1:
            clock.advance(4.0)

    # The lost time is absorbed, not added: the third event still lands on its
    # own 20 s mark rather than at 24 s.
    assert offsets == [0.0, 10.0, 20.0]


async def test_limit_stops_the_replay_early(tmp_path: Path) -> None:
    recording = recording_of(tmp_path, payload(Source.USGS, 0), payload(Source.USGS, 600, seq=1))
    emissions, _ = await Replayer(recording, clock=FakeClock(REPLAY_START), speed=MAX_SPEED).drain(
        limit=3
    )
    assert len(emissions) == 3


async def test_an_empty_recording_is_an_error(tmp_path: Path) -> None:
    directory = tmp_path / "s_test" / "raw"
    directory.mkdir(parents=True)
    directory.joinpath("nws.jsonl").write_bytes(b"\n")
    with pytest.raises(IngestError, match="is empty"):
        await Replayer(Recording.locate("s_test", root=tmp_path)).drain()


async def test_a_feed_with_no_adapter_is_reported(tmp_path: Path) -> None:
    recording = recording_of(tmp_path, payload(Source.WZDX, 0))
    with pytest.raises(IngestError, match="no adapter parses them"):
        await Replayer(recording, clock=FakeClock(REPLAY_START)).drain()


# --- live and replay agree --------------------------------------------------


@respx.mock
async def test_replay_reproduces_the_live_events_exactly(tmp_path: Path) -> None:
    """The point of the whole arrangement: one parse path, two ways in.

    What live mode published and what replay publishes must be the same
    `CivicEvent`s, field for field, including the ids, the `raw_ref`s and the
    `ingested_at` stamps that cite the recording.
    """
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    respx.get(collection_url(COLLECTION_LATEST)).mock(
        return_value=httpx.Response(200, json=USGS_BODY)
    )

    live_events: list[CivicEvent] = []
    with Recorder("s_test", root=tmp_path) as recorder:
        for adapter in (
            NwsAdapter(clock=FakeClock(T0)),
            UsgsAdapter(sites=("USGS-08178593",), clock=FakeClock(T0 + timedelta(seconds=30))),
        ):
            async with adapter:
                payloads, events = await adapter.poll()
            recorder.write_many(payloads)
            live_events.extend(events)

    replayed, _ = await Replayer(
        Recording.locate("s_test", root=tmp_path),
        clock=FakeClock(REPLAY_START),
        speed=MAX_SPEED,
    ).drain()

    assert [event.to_wire() for event in live_events] == [item.event.to_wire() for item in replayed]


@respx.mock
async def test_replaying_twice_gives_the_same_events(tmp_path: Path) -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    async with NwsAdapter(clock=FakeClock(T0)) as adapter:
        payloads = await adapter.fetch()
    recording = recording_of(tmp_path, *payloads)

    runs = []
    for start in (REPLAY_START, REPLAY_START + timedelta(days=3)):
        emissions, _ = await Replayer(recording, clock=FakeClock(start), speed=30.0).drain()
        runs.append([item.event.canonical_json() for item in emissions])
    assert runs[0] == runs[1]
