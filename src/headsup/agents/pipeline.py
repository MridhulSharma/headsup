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
                                               Critic.review --> verdicts
                                                                 |
                                                        CompilerService
                                                                 v
                                                            advisories

`ProposerWorker` publishes its proposal to `proposals` and only then hands the
pair to the compiler, so the proposal is on the bus whatever the gate decides
and a rejection can be read against what was asked for. One hazard may yield
several proposals -- see `Proposer.propose_all` -- and each goes through that
same sequence in turn.

The critic between them is `agents.critic.Critic`, reviewing one proposal at a
time on a context of its own: it is given the proposal and its evidence and
never the proposer's rationale, so its APPROVE, REVISE or ESCALATE is an
independent read rather than an agreement. It is deliberately not a bus worker
of its own. A worker on `proposals` would be a second consumer group on a
stream the compiler is already reacting to, and the compile would have to wait
on a verdict arriving back over Redis; here the review happens inline, between
the publish and the compile, so a proposal, its verdict and its audit record
are written in that order and one judgment call is spent per proposal.

A review that raises falls back to `Critic.review_offline`, the deterministic
rules path, rather than to an approval: the gate still gets a verdict from a
critic that measured the proposal, and the pipeline never publishes because
the reviewer broke.

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

from headsup.agents.critic import Critic
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
from headsup.contracts import CandidateHazard, CriticVerdict, Hazard, ProposedAdvisory

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
        critic: Critic | None = None,
        compiler: Publisher | None = None,
        on_proposal: OnProposal | None = None,
    ) -> None:
        self.name = proposer.name
        super().__init__(bus, consumer=consumer, group=group)
        self.proposer = proposer
        #: The independent second read. Built here if the caller did not pass
        #: one, with whatever client the proposer has: a pipeline wired with a
        #: model has a critic that can use it, and one without has a critic on
        #: its rules path, which is the same arrangement every other agent has.
        self.critic = critic if critic is not None else Critic(llm=proposer.llm)
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
            proposals = await self.proposer.propose_all(hazard)
        except Exception as exc:  # one bad hazard must not end the run
            self.report.note(f"{hazard.id}: {exc}")
            log.warning("%s: %s on %s", self.name, exc, hazard.id, exc_info=True)
            await self._ack(message)
            return

        if not proposals:
            # Nothing the playbook allows, or nothing left of the window.
            # Normal, and not an error the report should count as skipped.
            await self._ack(message)
            return

        # One hazard can be asked for more than one action -- a dark
        # intersection needs the stop rule, the queue cap and the approach cap
        # -- and each one is gated on its own, so a companion the compiler
        # refuses leaves the others standing. Published and compiled in order,
        # not concurrently: the compiler is the single writer to `advisories`
        # and the rail a viewer draws should follow the order asked.
        for proposal in proposals:
            await self.bus.publish(proposal, stream=self.proposals_stream)
            self.report.candidates += 1
            if self.on_proposal is not None:
                self.on_proposal(self.name, proposal)
            await self._compile(proposal)
        await self._ack(message)

    async def _review(self, proposal: ProposedAdvisory) -> CriticVerdict:
        """The critic's verdict on one proposal, never raising.

        A critic that blows up - a model backend misbehaving, a geometry it
        cannot measure - must not take the proposal with it, and must not be
        read as an approval either. The deterministic rules path answers
        instead, which is a real review with the same `reviewed_by` on it.
        """
        try:
            return await self.critic.review(proposal)
        except Exception as exc:
            self.report.note(f"{proposal.id}: critic failed: {exc}")
            log.warning("%s: critic failed on %s: %s", self.name, proposal.id, exc, exc_info=True)
            return self.critic.review_offline(proposal)

    async def _compile(self, proposal: ProposedAdvisory) -> None:
        """Review one proposal and hand the pair to the compiler, if there is one.

        The verdict goes on the `verdicts` stream before the compiler sees it,
        for the same reason the proposal does: the gate acted on it, so a
        reader of the streams can see what it acted on. It is written only
        here, where a verdict is actually used -- a pipeline with no compiler
        attached reaches no verdict, spends no judgment call reviewing a
        proposal nothing will gate, and publishes none.

        An ESCALATE is not a failure here. `two_key_reason` turns it into the
        same `two_key_approved` refusal a severity 4 advisory gets, so the
        proposal lands in `CompilerService.pending` and waits for a person,
        which is what `GET /approvals` lists and `POST /approvals/{id}`
        answers.

        A rejection is the gate working and is already in the `audit` stream,
        so it is logged rather than counted as a skipped message. A `BusError`
        on the way out means the advisory was not written: that one is noted,
        because the proposal is on the bus with nothing to show for it.
        """
        if self.compiler is None:
            return
        self.compiled += 1
        verdict = await self._review(proposal)
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
