"""Replay a recording onto the bus at 1x to 60x, on an injectable clock.

The replayer never parses anything itself. It reads the recorded `RawPayload`s
in fetch order and hands each one to the same adapter `parse` that live mode
uses, so a replayed `CivicEvent` is the event live mode produced: same id,
same `raw_ref`, same `ingested_at`.

Timing is measured against the first payload in the recording rather than
accumulated per gap, so a slow consumer makes the replay late but never makes
it drift.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime

from headsup.bus import Bus
from headsup.contracts import CivicEvent, Source
from headsup.ingest.base import Adapter, Clock, IngestError, RawPayload, SystemClock
from headsup.ingest.recorder import Recording
from headsup.ingest.registry import parsers

#: The speed factors the CLI accepts. 1x is real time; 60x turns the ten and a
#: half hours of S1 into ten and a half minutes, which is the length of the demo.
MIN_SPEED = 1.0
MAX_SPEED = 60.0


def check_speed(speed: float) -> float:
    """Validate a speed factor, with the allowed range in the error."""
    if not MIN_SPEED <= speed <= MAX_SPEED:
        raise ValueError(f"speed {speed} is outside {MIN_SPEED:g}x to {MAX_SPEED:g}x")
    return float(speed)


@dataclass(frozen=True, slots=True)
class Emission:
    """One replayed event, with where it sat in the recording and in the run."""

    event: CivicEvent
    source: Source
    #: When the feed was polled during the original recording.
    feed_time: datetime
    #: Seconds from the start of the recording, in recorded time.
    feed_offset: float
    #: Seconds from the start of the replay, on the replay clock.
    replay_offset: float
    #: Entry id on the bus, or None when the replay did not publish.
    entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """What a finished replay did."""

    scenario: str
    speed: float
    payloads: int
    events: int
    by_source: dict[Source, int]
    feed_span: float
    replay_span: float


class Replayer:
    """Emit a recording's events onto the bus with the recorded spacing."""

    def __init__(
        self,
        recording: Recording,
        *,
        bus: Bus | None = None,
        clock: Clock | None = None,
        speed: float = 1.0,
        adapters: dict[Source, Adapter] | None = None,
    ) -> None:
        self.recording = recording
        self.bus = bus
        self.clock: Clock = clock or SystemClock()
        self.speed = check_speed(speed)
        self.adapters = adapters if adapters is not None else parsers()
        self._payloads: list[RawPayload] | None = None

    def payloads(self) -> list[RawPayload]:
        """The recording, read from disk once and kept for the run."""
        if self._payloads is None:
            self._payloads = self.recording.payloads()
        return self._payloads

    def _parse(self, payload: RawPayload) -> list[CivicEvent]:
        adapter = self.adapters.get(payload.source)
        if adapter is None:
            raise IngestError(
                f"recording contains {payload.source.value} payloads but no adapter parses them"
            )
        return adapter.parse(payload)

    async def run(self, *, limit: int | None = None) -> AsyncIterator[Emission]:
        """Yield every recorded event, waiting out the gaps between them."""
        payloads = self.payloads()
        if not payloads:
            raise IngestError(f"recording at {self.recording.directory.as_posix()} is empty")

        origin = payloads[0].fetched_at
        started = self.clock.now()
        emitted = 0

        for payload in payloads:
            feed_offset = (payload.fetched_at - origin).total_seconds()
            target = feed_offset / self.speed
            elapsed = (self.clock.now() - started).total_seconds()
            await self.clock.sleep(max(0.0, target - elapsed))
            replay_offset = (self.clock.now() - started).total_seconds()

            for event in self._parse(payload):
                entry_id = await self.bus.publish(event) if self.bus is not None else None
                yield Emission(
                    event=event,
                    source=payload.source,
                    feed_time=payload.fetched_at,
                    feed_offset=feed_offset,
                    replay_offset=replay_offset,
                    entry_id=entry_id,
                )
                emitted += 1
                if limit is not None and emitted >= limit:
                    return

    async def drain(self, *, limit: int | None = None) -> tuple[list[Emission], ReplayReport]:
        """Run to the end and keep every emission. For tests and short demos."""
        emissions = [item async for item in self.run(limit=limit)]
        by_source: dict[Source, int] = {}
        for item in emissions:
            by_source[item.source] = by_source.get(item.source, 0) + 1
        report = ReplayReport(
            scenario=self.recording.scenario,
            speed=self.speed,
            payloads=len(self.payloads()),
            events=len(emissions),
            by_source=by_source,
            feed_span=emissions[-1].feed_offset if emissions else 0.0,
            replay_span=emissions[-1].replay_offset if emissions else 0.0,
        )
        return emissions, report
