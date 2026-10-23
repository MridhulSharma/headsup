"""A proposal refused for want of a second key, and what issuing one does.

A compile is synchronous: the proposer hands the gate a proposal and the gate
answers at once. Proposal ids are drawn at random, so nobody can approve one
before it exists, and until D3.2 a proposal refused on `two_key_approved` was
simply gone. `PendingCompiles` keeps it and `CompilerService.recompile` puts it
back through the same rules.

What these pin is that the retry is a retry and not a bypass: every rule runs
again, both trips leave an audit record, and an approval for the wrong
proposal, an expired one or none at all is refused exactly as it was the first
time.

Nothing here needs Redis. The bus is a list that remembers what was written to
it, because what is under test is which of the two streams got what.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from fixtures.compiler import NOW, approve, clock, poi_checker, proposal
from headsup.agents.compiler import (
    Compiler,
    Decision,
    InMemoryApprovals,
    PendingCompiles,
    RuleCode,
)
from headsup.agents.compiler.service import CompilerService
from headsup.bus import STREAM_ADVISORIES, STREAM_AUDIT
from headsup.contracts import HeadsUpModel

#: Severity 4 is at `playbooks/flood.yaml` two_key_severity, so this one needs
#: a human token and fails nothing else.
TWO_KEY = dict(severity=4, area_km2=0.05, lifetime_minutes=90.0)


class FakeBus:
    """Everything `CompilerService` asks of a bus, with a memory."""

    def __init__(self) -> None:
        self.written: list[tuple[str, HeadsUpModel]] = []
        self.closed = False

    async def publish(self, message: HeadsUpModel, *, stream: str) -> str:
        self.written.append((stream, message))
        return f"{len(self.written)}-0"

    async def close(self) -> None:  # pragma: no cover - the service owns no bus here
        self.closed = True

    def on(self, stream: str) -> list[Any]:
        return [message for name, message in self.written if name == stream]


@pytest.fixture
def approvals() -> InMemoryApprovals:
    return InMemoryApprovals()


@pytest.fixture
def service(approvals: InMemoryApprovals) -> CompilerService:
    """A real gate on a fake bus, with a stub POI checker and a fixed clock."""
    compiler = Compiler(
        poi=poi_checker(),
        approvals=approvals,
        issuer="headsup/austin",
        clock=clock(),
    )
    return CompilerService(FakeBus(), compiler, pending=PendingCompiles())


def bus_of(service: CompilerService) -> FakeBus:
    assert isinstance(service.bus, FakeBus)
    return service.bus


# --- what gets remembered ----------------------------------------------------


async def test_a_proposal_refused_only_for_a_second_key_is_kept(
    service: CompilerService,
) -> None:
    asked = proposal(**TWO_KEY)
    outcome = await service.publish(asked, approve(asked))

    assert outcome.ok is False
    assert outcome.result.codes == (RuleCode.TWO_KEY_APPROVED,)
    assert outcome.result.needs_approval is True
    assert len(service.pending) == 1
    waiting = service.pending.waiting()[0]
    assert waiting.proposal_id == asked.id
    assert waiting.verdict.proposal_id == asked.id
    # The rejection is already on the audit stream; nothing was published.
    assert bus_of(service).on(STREAM_ADVISORIES) == []
    assert len(bus_of(service).on(STREAM_AUDIT)) == 1


async def test_a_proposal_refused_for_anything_else_is_not_kept(
    service: CompilerService,
) -> None:
    """Only a missing key is worth keeping: nothing else a token could fix."""
    over_cap = proposal(severity=4, area_km2=40.0)
    outcome = await service.publish(over_cap, approve(over_cap))

    assert outcome.ok is False
    assert RuleCode.AREA_UNDER_CAP in outcome.result.codes
    assert len(service.pending) == 0


async def test_a_proposal_that_published_is_not_kept(service: CompilerService) -> None:
    fine = proposal(severity=3)
    outcome = await service.publish(fine, approve(fine))

    assert outcome.ok is True
    assert len(service.pending) == 0


async def test_the_store_is_bounded_and_drops_the_oldest(
    service: CompilerService,
) -> None:
    """A long run reaches proposals nobody will approve; it must not grow."""
    service.pending.limit = 2
    kept = []
    for index in range(4):
        asked = proposal(proposal_id=f"prop_pending_{index}", **TWO_KEY)
        await service.publish(asked, approve(asked))
        kept.append(asked.id)
    assert [item.proposal_id for item in service.pending.waiting()] == kept[-2:]


# --- what issuing the key does -----------------------------------------------


async def test_recompiling_with_a_token_publishes_and_audits_twice(
    service: CompilerService, approvals: InMemoryApprovals
) -> None:
    asked = proposal(**TWO_KEY)
    first = await service.publish(asked, approve(asked))
    assert first.ok is False

    approvals.grant(asked.id, "op_dispatch", note="creek is over the crossing", now=NOW)
    second = await service.recompile(asked.id)

    assert second is not None and second.ok is True
    assert second.result.advisory.properties.hazard.severity == 4
    # The rule did not go away: it passed, and says who passed it.
    two_key = next(
        check for check in second.result.checks if check.code is RuleCode.TWO_KEY_APPROVED
    )
    assert two_key.ok is True
    assert "op_dispatch" in two_key.detail

    bus = bus_of(service)
    assert len(bus.on(STREAM_ADVISORIES)) == 1
    records = bus.on(STREAM_AUDIT)
    assert [record.decision for record in records] == [Decision.REJECTED, Decision.PUBLISHED]
    assert records[-1].approved_by == "op_dispatch"
    assert records[-1].two_key_required is True
    # Published, so it is no longer waiting on anyone.
    assert len(service.pending) == 0


async def test_recompiling_without_a_token_is_refused_again(
    service: CompilerService,
) -> None:
    """The retry is a full trip through the gate, not a pass."""
    asked = proposal(**TWO_KEY)
    await service.publish(asked, approve(asked))
    again = await service.recompile(asked.id)

    assert again is not None and again.ok is False
    assert again.result.codes == (RuleCode.TWO_KEY_APPROVED,)
    assert bus_of(service).on(STREAM_ADVISORIES) == []
    # Still waiting: a refused retry does not consume the proposal.
    assert len(service.pending) == 1


async def test_a_token_for_another_proposal_does_not_unlock_this_one(
    service: CompilerService, approvals: InMemoryApprovals
) -> None:
    asked = proposal(**TWO_KEY)
    await service.publish(asked, approve(asked))
    approvals.grant("prop_somebody_else", "op_dispatch", now=NOW)

    again = await service.recompile(asked.id)
    assert again is not None and again.ok is False
    assert again.result.codes == (RuleCode.TWO_KEY_APPROVED,)


async def test_an_expired_token_does_not_unlock_it_either(
    service: CompilerService, approvals: InMemoryApprovals
) -> None:
    asked = proposal(**TWO_KEY)
    await service.publish(asked, approve(asked))
    approvals.grant(asked.id, "op_dispatch", ttl=timedelta(minutes=5), now=NOW - timedelta(hours=1))

    again = await service.recompile(asked.id)
    assert again is not None and again.ok is False
    failure = again.result.failures[0]
    assert failure.code is RuleCode.TWO_KEY_APPROVED
    assert "not usable at" in failure.detail


async def test_recompiling_something_nobody_is_waiting_on_is_none(
    service: CompilerService,
) -> None:
    assert await service.recompile("prop_never_seen") is None
    assert bus_of(service).written == []
