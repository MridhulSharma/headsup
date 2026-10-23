"""Fusion and the proposer on the bus, and the hand-off into the compiler.

    async with FusionWorker(Fusion(llm=client), bus) as worker:
        report = await worker.run(once=True)

Two workers, built the same way `SentinelWorker` is and with the same contract:
one consumer group named after the agent, so running the same worker twice
shares the work rather than doubling it; parse, handle, publish, then ack, so a
worker that dies mid-message picks that message up again on restart; and
nothing one message can do ends the run. A payload that will not parse, or an
agent that raises on a malformed one, is counted, acked and left behind - a
poison entry must not become a loop that blocks every later hazard.

The pipeline the two of them complete:

    candidates --FusionWorker--> hazards --ProposerWorker--> proposals
                                                                 |
                                            stub_verdict + CompilerService
                                                                 v
                                                            advisories

`ProposerWorker` publishes its proposal to `proposals` and only then hands the
pair to the compiler, so the proposal is on the bus whatever the gate decides
and a rejection can be read against what was asked for. The critic between them
is `agents.critic.stub_verdict` until D3.2.

The compiler service is injected and optional. With one, this is S1 end to end;
without one the pipeline stops at `proposals`, which is what lets a process
with no signing key and no world build still run the agents. The single-writer
rule is untouched either way: `CompilerService` is still the only thing here
that writes to `advisories`.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Protocol

from headsup.agents.critic import stub_verdict
from headsup.agents.fusion import Fusion
from headsup.agents.proposer import Proposer
from headsup.agents.sentinels.worker import BLOCK_MS, WorkerReport
from headsup.bus import (
    STREAM_CANDIDATES,
    STREAM_HAZARDS,
    STREAM_PROPOSALS,
    STREAM_VERDICTS,
    Bus,
    BusError,
    BusMessage,
)
from headsup.contracts import CandidateHazard, Hazard, ProposedAdvisory

log = logging.getLogger(__name__)


class OnHazard(Protocol):
    """How the CLI prints a hazard as it is published."""

    def __call__(self, agent: str, hazard: Hazard) -> None: ...


class OnProposal(Protocol):
    """How the CLI prints a proposal as it is published."""

    def __call__(self, agent: str, proposal: ProposedAdvisory) -> None: ...


class Publisher(Protocol):
    """What `ProposerWorker` needs of the compiler: one call, one outcome.

    Narrow on purpose. `CompilerService.publish` satisfies it, and a test can
    satisfy it without a signing key, a world build or a Redis.
    """

    async def publish(self, proposal: object, verdict: object) -> object: ...


class _Worker:
    """What the two pipeline workers share: the loop, the acks, the report."""

    #: Consumer group and `WorkerReport.sentinel` name.
    name: str
    #: The stream this worker reads.
    source_stream: str

    def __init__(self, bus: Bus, *, consumer: str = "worker-1", group: str | None = None) -> None:
        self.bus = bus
        self.consumer = consumer
        self.group = group or self.name
        self.report = WorkerReport(sentinel=self.name)

    async def __aenter__(self) -> _Worker:
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
        """Consume until the stream is idle (`once`) or `events` have been read."""
        async for message in self.bus.consume(
            (self.source_stream,),
            self.group,
            self.consumer,
            limit=events,
            block_ms=block_ms,
            stop_when_idle=once,
        ):
            await self._handle(message)
        return self.report

    async def _handle(self, message: BusMessage) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def _ack(self, message: BusMessage) -> None:
        try:
            await self.bus.ack(message)
        except BusError as exc:  # pragma: no cover - a dead Redis ends the run anyway
            log.warning("%s: ack failed: %s", self.name, exc)


class FusionWorker(_Worker):
    """Candidates in through a consumer group, fused hazards out."""

    source_stream = STREAM_CANDIDATES

    def __init__(
        self,
        fusion: Fusion,
        bus: Bus,
        *,
        consumer: str = "worker-1",
        group: str | None = None,
        hazards_stream: str = STREAM_HAZARDS,
        on_hazard: OnHazard | None = None,
    ) -> None:
        self.name = fusion.name
        super().__init__(bus, consumer=consumer, group=group)
        self.fusion = fusion
        self.hazards_stream = hazards_stream
        self.on_hazard = on_hazard

    async def _handle(self, message: BusMessage) -> None:
        """One candidate: parse, fuse, publish, ack. Never raise."""
        try:
            candidate = message.parse(CandidateHazard)
        except BusError as exc:
            self.report.note(str(exc))
            log.warning("%s: %s", self.name, exc)
            await self._ack(message)
            return

        self.report.events += 1
        try:
            hazard = await self.fusion.on_candidate(candidate)
        except Exception as exc:  # one bad candidate must not end the run
            self.report.note(f"{candidate.id}: {exc}")
            log.warning("%s: %s on %s", self.name, exc, candidate.id, exc_info=True)
            await self._ack(message)
            return

        await self.bus.publish(hazard, stream=self.hazards_stream)
        self.report.candidates += 1
        if self.on_hazard is not None:
            self.on_hazard(self.name, hazard)
        await self._ack(message)


class ProposerWorker(_Worker):
    """Hazards in, proposals out, and into the compiler if one was given."""

    source_stream = STREAM_HAZARDS

    def __init__(
        self,
        proposer: Proposer,
        bus: Bus,
        *,
        consumer: str = "worker-1",
        group: str | None = None,
        proposals_stream: str = STREAM_PROPOSALS,
        verdicts_stream: str = STREAM_VERDICTS,
        compiler: Publisher | None = None,
        on_proposal: OnProposal | None = None,
    ) -> None:
        self.name = proposer.name
        super().__init__(bus, consumer=consumer, group=group)
        self.proposer = proposer
        self.proposals_stream = proposals_stream
        self.verdicts_stream = verdicts_stream
        self.compiler = compiler
        self.on_proposal = on_proposal
        #: Proposals handed to the compiler, and what it published.
        self.compiled = 0
        self.published = 0

    async def _handle(self, message: BusMessage) -> None:
        """One hazard: parse, propose, publish, compile, ack. Never raise."""
        try:
            hazard = message.parse(Hazard)
        except BusError as exc:
            self.report.note(str(exc))
            log.warning("%s: %s", self.name, exc)
            await self._ack(message)
            return

        self.report.events += 1
        try:
            proposal = await self.proposer.propose(hazard)
        except Exception as exc:  # one bad hazard must not end the run
            self.report.note(f"{hazard.id}: {exc}")
            log.warning("%s: %s on %s", self.name, exc, hazard.id, exc_info=True)
            await self._ack(message)
            return

        if proposal is None:
            # Nothing the playbook allows, or nothing left of the window.
            # Normal, and not an error the report should count as skipped.
            await self._ack(message)
            return

        await self.bus.publish(proposal, stream=self.proposals_stream)
        self.report.candidates += 1
        if self.on_proposal is not None:
            self.on_proposal(self.name, proposal)
        await self._compile(proposal)
        await self._ack(message)

    async def _compile(self, proposal: ProposedAdvisory) -> None:
        """Hand one proposal and the stub verdict to the compiler, if there is one.

        The verdict goes on the `verdicts` stream before the compiler sees it,
        for the same reason the proposal does: the gate acted on it, so a
        reader of the streams can see what it acted on. It is written only
        here, where a verdict is actually used -- a pipeline with no compiler
        attached reaches no verdict and publishes none.

        A rejection is the gate working and is already in the `audit` stream,
        so it is logged rather than counted as a skipped message. A `BusError`
        on the way out means the advisory was not written: that one is noted,
        because the proposal is on the bus with nothing to show for it.
        """
        if self.compiler is None:
            return
        self.compiled += 1
        verdict = stub_verdict(proposal)
        try:
            await self.bus.publish(verdict, stream=self.verdicts_stream)
            outcome = await self.compiler.publish(proposal, verdict)
        except BusError as exc:
            self.report.note(f"{proposal.id}: compiler publish failed: {exc}")
            log.warning("%s: %s on %s", self.name, exc, proposal.id)
            return
        if getattr(outcome, "ok", False):
            self.published += 1
        else:
            log.info("%s: compiler refused %s: %s", self.name, proposal.id, outcome)
