"""Polling the live feeds, with the recorder in the same path.

`hu ingest live` and `hu ingest record` are the same loop: each feed is polled
on its own interval, every raw payload is handed to the recorder if there is
one, and the events parsed out of it go on the bus. Recording is therefore
never a separate code path that can drift from live.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from headsup.bus import Bus
from headsup.contracts import CivicEvent, Source
from headsup.ingest.austin_incidents import AustinIncidentsAdapter
from headsup.ingest.austin_signals import AustinSignalsAdapter
from headsup.ingest.base import Adapter, Clock, IngestError, SystemClock
from headsup.ingest.cities import City
from headsup.ingest.freshness import (
    INCIDENTS_INTERVAL,
    NWS_INTERVAL,
    SIGNALS_INTERVAL,
    USGS_INTERVAL,
)
from headsup.ingest.nws import NwsAdapter
from headsup.ingest.recorder import Recorder
from headsup.ingest.usgs import UsgsAdapter

#: Seconds between polls live on `ingest.freshness` with the staleness
#: breaker that measures against them, so a cadence change cannot leave the
#: breaker tripping on the old one.
__all__ = [
    "INCIDENTS_INTERVAL",
    "NWS_INTERVAL",
    "SIGNALS_INTERVAL",
    "USGS_INTERVAL",
    "FeedRun",
    "LiveIngest",
    "city_adapters",
]


@dataclass(slots=True)
class FeedRun:
    """What one feed did during a live session."""

    source: Source
    polls: int = 0
    payloads: int = 0
    events: int = 0
    errors: list[str] = field(default_factory=list)


def city_adapters(city: City, **kwargs: object) -> list[tuple[Adapter, float]]:
    """The feeds Heads Up polls for one city, with their intervals.

    A city only gets the signal and incident feeds if it publishes them, so
    San Antonio polls two feeds and Austin four.
    """
    feeds: list[tuple[Adapter, float]] = [
        (NwsAdapter(area=city.nws_area, **kwargs), NWS_INTERVAL),  # type: ignore[arg-type]
        (UsgsAdapter.for_city(city, **kwargs), USGS_INTERVAL),  # type: ignore[arg-type]
    ]
    if city.signal_dataset:
        feeds.append(
            (
                AustinSignalsAdapter(dataset=city.signal_dataset, **kwargs),  # type: ignore[arg-type]
                SIGNALS_INTERVAL,
            )
        )
    if city.incident_dataset:
        feeds.append(
            (
                AustinIncidentsAdapter(dataset=city.incident_dataset, **kwargs),  # type: ignore[arg-type]
                INCIDENTS_INTERVAL,
            )
        )
    return feeds


class LiveIngest:
    """Poll a set of adapters until the time budget runs out.

    `on_event` is how the CLI prints; it is called for every parsed event
    whether or not there is a bus to publish to.
    """

    def __init__(
        self,
        feeds: list[tuple[Adapter, float]],
        *,
        bus: Bus | None = None,
        recorder: Recorder | None = None,
        clock: Clock | None = None,
        on_event: Callable[[CivicEvent], None] | None = None,
        on_error: Callable[[Source, Exception], None] | None = None,
    ) -> None:
        if not feeds:
            raise IngestError("live ingest needs at least one feed")
        self.feeds = feeds
        self.bus = bus
        self.recorder = recorder
        self.clock: Clock = clock or SystemClock()
        self.on_event = on_event
        self.on_error = on_error
        self.runs: dict[Source, FeedRun] = {
            adapter.source: FeedRun(source=adapter.source) for adapter, _ in feeds
        }

    async def run(self, *, duration_s: float | None = None, polls: int | None = None) -> None:
        """Poll every feed concurrently until the budget is spent.

        `duration_s` is the wall-clock budget (`--minutes`); `polls` caps the
        number of rounds per feed, which is what a test wants instead.
        """
        deadline = None if duration_s is None else self.clock.now().timestamp() + duration_s
        await asyncio.gather(
            *(
                self._poll_feed(adapter, interval, deadline, polls)
                for adapter, interval in self.feeds
            )
        )

    async def _poll_feed(
        self,
        adapter: Adapter,
        interval: float,
        deadline: float | None,
        polls: int | None,
    ) -> None:
        run = self.runs[adapter.source]
        while True:
            if deadline is not None and self.clock.now().timestamp() >= deadline:
                return
            if polls is not None and run.polls >= polls:
                return
            run.polls += 1
            await self._one_poll(adapter, run)

            if polls is not None and run.polls >= polls:
                return
            wait = interval
            if deadline is not None:
                wait = min(wait, deadline - self.clock.now().timestamp())
                if wait <= 0:
                    return
            await self.clock.sleep(wait)

    async def _one_poll(self, adapter: Adapter, run: FeedRun) -> None:
        """One fetch, recorded then parsed. A feed error never stops the rest."""
        try:
            payloads, events = await adapter.poll()
        except Exception as exc:  # one bad feed must not end the session
            run.errors.append(str(exc))
            if self.on_error is not None:
                self.on_error(adapter.source, exc)
            return

        run.payloads += len(payloads)
        if self.recorder is not None:
            self.recorder.write_many(payloads)

        for event in events:
            run.events += 1
            if self.bus is not None:
                await self.bus.publish(event)
            if self.on_event is not None:
                self.on_event(event)

    async def aclose(self) -> None:
        for adapter, _ in self.feeds:
            await adapter.aclose()

    @property
    def total_events(self) -> int:
        return sum(run.events for run in self.runs.values())

    @property
    def total_payloads(self) -> int:
        return sum(run.payloads for run in self.runs.values())
