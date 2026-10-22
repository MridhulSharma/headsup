"""The worker: a sentinel's feeds in, candidates onto `candidates`, acked.

No Redis here. `FakeBus` yields prepared entries and keeps what was published
and acked, which is all the worker's contract is: one entry read, zero or more
candidates written, then the ack - and never an exception out of `run`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import orjson

from fixtures.ingest import POLYGON_ALERT, nws_body
from headsup.agents.sentinels import FloodSentinel, Sentinel, SentinelWorker
from headsup.bus import STREAM_CANDIDATES, BusMessage
from headsup.contracts import CandidateHazard, CivicEvent, Source
from headsup.ingest import NwsAdapter, RawPayload, make_ref

T0 = datetime(2026, 4, 20, 18, 55, tzinfo=UTC)


@dataclass(slots=True)
class FakeBus:
    """Entries in, publishes and acks recorded. Enough bus for one worker."""

    entries: list[BusMessage] = field(default_factory=list)
    published: list[tuple[str, Any]] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)
    asked: list[tuple[tuple[str, ...], str, str]] = field(default_factory=list)

    async def consume(
        self,
        streams: str | Sequence[str],
        group: str,
        consumer: str,
        **kwargs: Any,
    ) -> AsyncIterator[BusMessage]:
        names = (streams,) if isinstance(streams, str) else tuple(streams)
        self.asked.append((names, group, consumer))
        limit = kwargs.get("limit")
        for index, message in enumerate(self.entries):
            if limit is not None and index >= limit:
                return
            yield message

    async def publish(self, message: Any, *, stream: str | None = None) -> str:
        self.published.append((stream or "", message))
        return f"0-{len(self.published)}"

    async def ack(self, message: BusMessage) -> int:
        self.acked.append(message.id)
        return 1

    def on(self, stream: str) -> list[Any]:
        return [message for name, message in self.published if name == stream]


class ExplodingSentinel(Sentinel):
    """A sentinel that raises on every event, as a malformed record would."""

    name = "sentinel.exploding"
    sources = (Source.NWS,)

    async def read(self, event: CivicEvent) -> list[CandidateHazard]:
        raise ValueError(f"cannot read {event.id}")


def entry(event: CivicEvent, entry_id: str = "1-0") -> BusMessage:
    return BusMessage(
        stream="events.nws",
        id=entry_id,
        type=type(event).__name__,
        data=orjson.loads(event.to_json()),
        group="sentinel.flood",
    )


def flood_event() -> CivicEvent:
    payload = RawPayload(
        source=Source.NWS,
        ref=make_ref(Source.NWS, T0, 0),
        url="https://api.weather.gov/alerts/active?area=TX",
        fetched_at=T0,
        body=nws_body(POLYGON_ALERT),
    )
    return NwsAdapter().parse(payload)[0]


# --- the happy path ---------------------------------------------------------


async def test_an_event_becomes_a_candidate_on_the_candidates_stream() -> None:
    bus = FakeBus(entries=[entry(flood_event())])
    worker = SentinelWorker(FloodSentinel(), bus)  # type: ignore[arg-type]
    report = await worker.run(once=True)

    assert report.events == 1
    assert report.candidates == 1
    assert report.skipped == 0
    (stream, candidate) = bus.published[0]
    assert stream == STREAM_CANDIDATES
    assert isinstance(candidate, CandidateHazard)
    assert bus.acked == ["1-0"]


async def test_a_worker_reads_its_sentinels_own_feeds_through_its_own_group() -> None:
    bus = FakeBus()
    sentinel = FloodSentinel()
    async with SentinelWorker(sentinel, bus) as worker:  # type: ignore[arg-type]
        await worker.run(once=True)

    (streams, group, consumer) = bus.asked[0]
    assert streams == ("events.nws", "events.usgs")
    assert group == sentinel.name == "sentinel.flood"
    assert consumer == "worker-1"


async def test_the_candidate_is_published_before_the_entry_is_acked() -> None:
    """An ack means the event is handled, so nothing may be lost after it."""
    order: list[str] = []

    class Watching(FakeBus):
        async def publish(self, message: Any, *, stream: str | None = None) -> str:
            order.append("publish")
            return await super().publish(message, stream=stream)

        async def ack(self, message: BusMessage) -> int:
            order.append("ack")
            return await super().ack(message)

    bus = Watching(entries=[entry(flood_event())])
    await SentinelWorker(FloodSentinel(), bus).run(once=True)  # type: ignore[arg-type]
    assert order == ["publish", "ack"]


async def test_events_caps_how_many_entries_a_run_reads() -> None:
    event = flood_event()
    bus = FakeBus(entries=[entry(event, "1-0"), entry(event, "2-0"), entry(event, "3-0")])
    report = await SentinelWorker(FloodSentinel(), bus).run(events=2)  # type: ignore[arg-type]
    assert report.events == 2


# --- nothing ends the run ---------------------------------------------------


async def test_an_entry_that_is_not_an_event_is_skipped_acked_and_counted() -> None:
    """A poison entry must not become a loop that blocks the stream."""
    rubbish = BusMessage(
        stream="events.nws", id="1-0", type="CivicEvent", data={"nope": True}, group="g"
    )
    bus = FakeBus(entries=[rubbish, entry(flood_event(), "2-0")])
    report = await SentinelWorker(FloodSentinel(), bus).run(once=True)  # type: ignore[arg-type]

    assert report.skipped == 1
    assert report.events == 1
    assert report.candidates == 1
    assert bus.acked == ["1-0", "2-0"]
    assert report.errors and "CivicEvent" in report.errors[0]


async def test_a_sentinel_that_raises_does_not_end_the_run() -> None:
    event = flood_event()
    bus = FakeBus(entries=[entry(event, "1-0"), entry(event, "2-0")])
    report = await SentinelWorker(ExplodingSentinel(), bus).run(once=True)  # type: ignore[arg-type]

    assert report.events == 2
    assert report.skipped == 2
    assert report.candidates == 0
    assert bus.acked == ["1-0", "2-0"]
    assert "cannot read" in report.errors[0]


def test_the_report_reads_as_one_line() -> None:
    bus = FakeBus()
    worker = SentinelWorker(FloodSentinel(), bus)  # type: ignore[arg-type]
    worker.report.events = 140
    worker.report.candidates = 9
    assert worker.report.summary() == "sentinel.flood: 140 events, 9 candidates"
    worker.report.note("one bad record")
    assert "1 skipped" in worker.report.summary()


def test_the_group_is_the_sentinels_name_unless_one_is_given() -> None:
    """Two workers in one group share a feed; two groups each read it all."""
    bus = FakeBus()
    assert SentinelWorker(FloodSentinel(), bus).group == "sentinel.flood"  # type: ignore[arg-type]
    shared = SentinelWorker(FloodSentinel(), bus, group="shared")  # type: ignore[arg-type]
    assert shared.group == "shared"
