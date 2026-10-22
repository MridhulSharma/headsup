"""A sentinel on the bus: one consumer group, one process, one worker each.

    async with SentinelWorker(FloodSentinel(llm=client), bus) as worker:
        report = await worker.run(once=True)

Each sentinel reads its own feeds through its own consumer group named after
itself, so running the flood sentinel twice shares the work rather than
doubling it, and a worker that dies mid-event picks that event up again when
it restarts (`Bus.consume` replays its own unacked backlog first).

Nothing a sentinel can do to one event is allowed to end the run. A payload
that will not parse, or a sentinel that raises on a malformed feed record, is
counted, acked and left behind: a poison entry must not become a loop that
blocks every later event on the stream. The counts are in the report, and the
worker never acks an entry before the candidates it produced are published.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol

from headsup.agents.sentinels.base import Sentinel
from headsup.bus import STREAM_CANDIDATES, Bus, BusError, BusMessage
from headsup.contracts import CandidateHazard, CivicEvent

log = logging.getLogger(__name__)

#: How long a follow loop blocks on an empty read before looking again.
BLOCK_MS = 1000

#: Errors kept per worker. The audit stream is the record; this is a window.
MAX_ERRORS = 50


class OnCandidate(Protocol):
    """How the CLI prints a candidate as it is published."""

    def __call__(self, sentinel: str, candidate: CandidateHazard) -> None: ...


@dataclass(slots=True)
class WorkerReport:
    """What one worker did while it ran."""

    sentinel: str
    events: int = 0
    candidates: int = 0
    #: Entries that did not parse as a `CivicEvent`, or that the sentinel
    #: raised on. Acked and skipped, never retried in a loop.
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def note(self, problem: str) -> None:
        self.skipped += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(problem)

    def summary(self) -> str:
        line = f"{self.sentinel}: {self.events} events, {self.candidates} candidates"
        return line + (f", {self.skipped} skipped" if self.skipped else "")


class SentinelWorker:
    """Feeds in through a consumer group, candidates out onto `candidates`."""

    def __init__(
        self,
        sentinel: Sentinel,
        bus: Bus,
        *,
        consumer: str = "worker-1",
        group: str | None = None,
        candidates_stream: str = STREAM_CANDIDATES,
        on_candidate: OnCandidate | None = None,
    ) -> None:
        self.sentinel = sentinel
        self.bus = bus
        self.consumer = consumer
        self.group = group or sentinel.name
        self.candidates_stream = candidates_stream
        self.on_candidate = on_candidate
        self.report = WorkerReport(sentinel=sentinel.name)

    async def __aenter__(self) -> SentinelWorker:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(
        self,
        *,
        events: int | None = None,
        once: bool = False,
        block_ms: int = BLOCK_MS,
    ) -> WorkerReport:
        """Consume until the stream is idle (`once`) or `events` have been read.

        With neither, this runs until the task is cancelled, which is what
        `hu agents` does.
        """
        async for message in self.bus.consume(
            self.sentinel.streams,
            self.group,
            self.consumer,
            limit=events,
            block_ms=block_ms,
            stop_when_idle=once,
        ):
            await self._handle(message)
        return self.report

    async def _handle(self, message: BusMessage) -> None:
        """One entry: parse, read, publish, ack. Never raise."""
        try:
            event = message.parse(CivicEvent)
        except BusError as exc:
            self.report.note(str(exc))
            log.warning("%s: %s", self.sentinel.name, exc)
            await self._ack(message)
            return

        self.report.events += 1
        try:
            candidates = await self.sentinel.on_event(event)
        except Exception as exc:  # one bad record must not end the run
            self.report.note(f"{event.id}: {exc}")
            log.warning("%s: %s on %s", self.sentinel.name, exc, event.id, exc_info=True)
            await self._ack(message)
            return

        for candidate in candidates:
            await self.bus.publish(candidate, stream=self.candidates_stream)
            self.report.candidates += 1
            if self.on_candidate is not None:
                self.on_candidate(self.sentinel.name, candidate)
        await self._ack(message)

    async def _ack(self, message: BusMessage) -> None:
        try:
            await self.bus.ack(message)
        except BusError as exc:  # pragma: no cover - a dead Redis ends the run anyway
            log.warning("%s: ack failed: %s", self.sentinel.name, exc)
