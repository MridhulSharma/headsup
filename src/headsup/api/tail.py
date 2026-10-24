"""One reader of the pipeline streams, feeding the viewer and the operators.

The viewer's rail and the operator sockets both want the same thing: every
entry the pipeline writes, in order, as it is written. So one tailer follows
the streams through a consumer group and fans each entry out, rather than each
socket opening its own read.

It is long-lived, not per-run. A scenario reset deletes the streams underneath
it, which drops the group with them; `Bus.read_once` recreates a missing group
from the start of the stream and retries, so the next run is picked up from its
first entry with nothing missed and nothing replayed from the last.

Advisories go out twice over, and the difference matters. The viewer gets every
advisory with this server's verification result attached, marked rather than
withheld, because a viewer that silently omitted an unverifiable advisory would
be hiding exactly the failure worth seeing. An operator gets only what verifies
(`OperatorSession.offer`), because an operator is being asked to act.

Assist requests go out twice over too: to the viewer as a frame, and to the
triage desk through `on_assist`, which clusters them into decision cards. The
desk is the API's, not a run's, so the tailer hands it every request it reads
and the routes answer off whatever it holds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from headsup.agents.compiler.signing import verify
from headsup.api.frames import (
    RailIndex,
    ack_frame,
    advisory_frame,
    assist_frame,
    candidate_frame,
    hazard_frame,
    proposal_frame,
    verdict_frame,
)
from headsup.api.hub import ViewerHub
from headsup.bus import (
    STREAM_ACKS,
    STREAM_ADVISORIES,
    STREAM_ASSIST,
    STREAM_CANDIDATES,
    STREAM_HAZARDS,
    STREAM_PROPOSALS,
    STREAM_VERDICTS,
    Bus,
    BusError,
    BusMessage,
)
from headsup.contracts import (
    Ack,
    Advisory,
    AssistRequest,
    CandidateHazard,
    CriticVerdict,
    Hazard,
    ProposedAdvisory,
)

log = logging.getLogger(__name__)

#: The streams the rail is drawn from, in pipeline order, and the contract on
#: each. Telemetry is not among them: it comes off the simulator's own tap, at
#: the viewer's rate, rather than through a stream capped at 600 entries.
TAILED: dict[str, type] = {
    STREAM_CANDIDATES: CandidateHazard,
    STREAM_HAZARDS: Hazard,
    STREAM_PROPOSALS: ProposedAdvisory,
    STREAM_VERDICTS: CriticVerdict,
    STREAM_ADVISORIES: Advisory,
    STREAM_ACKS: Ack,
    STREAM_ASSIST: AssistRequest,
}

#: How long a read blocks before looking again.
BLOCK_MS = 500

#: Consumer group and consumer name. One server, one reader.
GROUP = "viewer"
CONSUMER = "viewer-1"

#: Seconds to wait out after a bus error before reading again. A dead Redis
#: must not become a busy loop in the server's log.
RETRY_S = 1.0


class StreamTailer:
    """Follows the pipeline streams and fans every entry out."""

    def __init__(
        self,
        bus: Bus,
        hub: ViewerHub,
        index: RailIndex,
        *,
        on_advisory: Callable[[Advisory], None] | None = None,
        on_assist: Callable[[AssistRequest], None] | None = None,
        group: str = GROUP,
        consumer: str = CONSUMER,
        block_ms: int = BLOCK_MS,
    ) -> None:
        self.bus = bus
        self.hub = hub
        self.index = index
        self.on_advisory = on_advisory
        self.on_assist = on_assist
        self.group = group
        self.consumer = consumer
        self.block_ms = block_ms
        #: Entries read, and entries that did not parse as their contract.
        self.read = 0
        self.skipped = 0

    async def run(self) -> None:
        """Read and fan out until cancelled."""
        while True:
            try:
                async for message in self.bus.consume(
                    tuple(TAILED),
                    self.group,
                    self.consumer,
                    block_ms=self.block_ms,
                    auto_ack=True,
                ):
                    self.handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Anything the bus can raise, including a Redis that is not
                # up yet: the tailer is the viewer's only feed and must come
                # back on its own rather than leave the socket silent.
                log.warning("viewer tailer: %s; retrying", exc)
                await asyncio.sleep(RETRY_S)

    def handle(self, message: BusMessage) -> None:
        """Turn one entry into frames. Never raises: one bad entry is not fatal."""
        model = TAILED.get(message.stream)
        if model is None:  # pragma: no cover - the group only covers TAILED
            return
        try:
            parsed = message.parse(model)
        except BusError as exc:
            self.skipped += 1
            log.warning("viewer tailer skipped %s %s: %s", message.stream, message.id, exc)
            return
        self.read += 1
        frame = self._frame(parsed)
        if frame is not None:
            self.hub.publish(frame)

    def _frame(self, parsed: Any) -> dict[str, Any] | None:
        if isinstance(parsed, CandidateHazard):
            return candidate_frame(parsed, self.index)
        if isinstance(parsed, Hazard):
            return hazard_frame(parsed, self.index)
        if isinstance(parsed, ProposedAdvisory):
            return proposal_frame(parsed, self.index)
        if isinstance(parsed, CriticVerdict):
            return verdict_frame(parsed, self.index)
        if isinstance(parsed, Advisory):
            return self._advisory(parsed)
        if isinstance(parsed, Ack):
            return ack_frame(parsed, self.index)
        if isinstance(parsed, AssistRequest):
            if self.on_assist is not None:
                self.on_assist(parsed)
            return assist_frame(parsed)
        return None  # pragma: no cover - TAILED has no other types

    def _advisory(self, advisory: Advisory) -> dict[str, Any]:
        """The viewer's advisory frame, and the operators' copy of it."""
        if self.on_advisory is not None:
            self.on_advisory(advisory)
        return advisory_frame(advisory, self.index, signature_valid=verify(advisory))
