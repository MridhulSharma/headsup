"""The auditor: every stream into one hash-chained file, and nothing out.

    async with Auditor(AuditLog(run_id), bus) as auditor:
        report = await auditor.run(once=True)

One consumer group (`agent.auditor`) across every stream the system has - the
feed streams, every pipeline stage, the advisories, the acks, the telemetry, the
assist queue and the `audit` stream the compiler and the LLM meter write to. It
reads all of them in one worker because the record it keeps is of the run, not
of a stage: the question the log exists to answer is "what did this system see,
decide and publish, in what order", and a chain per stream could not answer the
"in what order" part.

**The auditor writes to no stream.** It is the one agent on the bus that only
reads. That is not an accident of this implementation: a recorder that
publishes is a recorder that can be fed its own output, and the single-writer
rule (`CLAUDE.md`: only the compiler writes `advisories`) is easier to trust
when the thing recording it cannot write anywhere at all.

**Order on the chain is arrival order, not publish order.** One worker reading
several streams interleaves them by when Redis handed them over, so two entries
published a millisecond apart on different streams can be chained either way
round. Each line carries the publisher's own `published_at`, which is what the
report sorts a timeline by; the chain's own order is only the order the
recording happened in. Making it publish order would mean buffering and a
watermark, and a record that waits is a record that loses the entries a crash
interrupts.

**A line is written before its entry is acked.** `AuditLog.append` flushes
before it returns, and only then does the worker ack, so an entry is only ever
dropped from the group's backlog once the chain has it. An entry whose payload
cannot be recorded is counted, acked and left behind, like everywhere else on
this bus: a poison entry must not become a loop that blocks every later entry
from the record.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Protocol

from headsup.audit.chain import AuditLog, AuditRecord, ChainError
from headsup.bus import ALL_STREAMS, Bus, BusError, BusMessage

log = logging.getLogger(__name__)

AUDITOR_NAME = "agent.auditor"

#: How long a follow loop blocks on an empty read before looking again.
BLOCK_MS = 1000

#: Errors kept per run. The chain is the record; this is a window on the worker.
MAX_ERRORS = 50


class OnRecord(Protocol):
    """How the CLI prints a line as it is sealed."""

    def __call__(self, record: AuditRecord) -> None: ...


@dataclass(slots=True)
class AuditorReport:
    """What one auditor did while it ran."""

    run_id: str
    path: str
    entries: int = 0
    #: Entries that could not be appended. Acked and skipped, never retried.
    skipped: int = 0
    #: The chain head after the last line: what a verifier should arrive at.
    head: str = ""
    by_stream: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    def note(self, problem: str) -> None:
        self.skipped += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(problem)

    def summary(self) -> str:
        line = f"{self.run_id}: {self.entries} entries from {len(self.by_stream)} streams"
        if self.skipped:
            line += f", {self.skipped} skipped"
        return line + f", head {self.head[:12]}"


class Auditor:
    """Every stream in through one consumer group, one chained line out each."""

    name = AUDITOR_NAME

    def __init__(
        self,
        audit_log: AuditLog,
        bus: Bus,
        *,
        streams: Sequence[str] | None = None,
        consumer: str = "worker-1",
        group: str = AUDITOR_NAME,
        on_record: OnRecord | None = None,
    ) -> None:
        self.log = audit_log
        self.bus = bus
        #: Every stream by default. A caller that names fewer is debugging:
        #: a chain over some of the streams is not a record of the run.
        self.streams = tuple(streams) if streams is not None else tuple(ALL_STREAMS)
        self.consumer = consumer
        self.group = group
        self.on_record = on_record
        self.report = AuditorReport(run_id=audit_log.run_id, path=audit_log.path.as_posix())

    async def __aenter__(self) -> Auditor:
        self.log.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.log.close()

    async def run(
        self,
        *,
        events: int | None = None,
        once: bool = False,
        block_ms: int = BLOCK_MS,
    ) -> AuditorReport:
        """Record until the streams are idle (`once`) or `events` are in.

        With neither, this runs until the task is cancelled, which is what
        `hu audit record` does.
        """
        self.log.open()
        async for message in self.bus.consume(
            self.streams,
            self.group,
            self.consumer,
            limit=events,
            block_ms=block_ms,
            stop_when_idle=once,
        ):
            await self._handle(message)
        self.report.head = self.log.head
        return self.report

    async def _handle(self, message: BusMessage) -> None:
        """Seal one entry, then ack it. Nothing here may end the run."""
        try:
            record = self.log.append(
                stream=message.stream,
                entry_id=message.id,
                type=message.type,
                data=message.data,
                published_at=message.published_at,
            )
        except ChainError as exc:
            # The file itself is the problem - a full disk, a revoked
            # permission. Counted and acked: retrying it on this entry would
            # only stop the auditor reading the next one, and the chain is
            # already short either way.
            self.report.note(f"{message.stream} {message.id}: {exc}")
            log.error("%s: cannot append %s %s: %s", self.name, message.stream, message.id, exc)
        else:
            self.report.entries += 1
            self.report.by_stream[message.stream] += 1
            self.report.head = record.hash
            if self.on_record is not None:
                self.on_record(record)
        try:
            await self.bus.ack(message)
        except BusError as exc:
            # The line is written; a failed ack only means it may be recorded
            # twice on restart, which the chain shows as two lines rather than
            # as a gap. Worth a log and nothing more.
            log.warning("%s: ack failed for %s %s: %s", self.name, message.stream, message.id, exc)
