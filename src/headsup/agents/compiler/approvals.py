"""The second key: a human approval token for the advisories that need one.

Two classes of advisory never publish on the agents' word alone: anything at
severity 4 or above, and anything covering more ground than its playbook's
`two_key_area_km2`. Those wait for a token a person issued. D3.2 puts the real
endpoints on it (`POST /approvals/{proposal_id}`, `GET /approvals`); this
module is the store behind them, and the shape the compiler checks.

`PendingCompiles` is the other half of the same rule: the proposals the gate
has already refused for want of a token, kept so that issuing one can put them
back through it.

A token is deliberately boring: an opaque string bound to one proposal id,
with who issued it and when it stops counting. It is not a capability the
holder can spend elsewhere, because the compiler only ever looks up the
approval for the proposal in front of it.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, model_validator

from headsup.contracts import (
    CriticVerdict,
    FreeText,
    HeadsUpModel,
    Identifier,
    ProposedAdvisory,
    UtcDatetime,
    utcnow,
)

#: An approval token: opaque, URL-safe, long enough not to be guessed.
Token = Annotated[
    str, StringConstraints(min_length=16, max_length=256, pattern=r"^[A-Za-z0-9_\-]+$")
]

#: How long a fresh approval counts for unless the caller says otherwise. An
#: approval is permission to publish now, not a standing waiver.
DEFAULT_TTL = timedelta(minutes=30)

TOKEN_BYTES = 24


def new_token() -> str:
    """A fresh approval token."""
    return token_urlsafe(TOKEN_BYTES)


class Approval(HeadsUpModel):
    """One person's recorded decision to let one proposal publish."""

    proposal_id: Identifier
    token: Token
    #: Who approved. An operator or city user id, never an agent name.
    approved_by: Identifier
    approved_at: UtcDatetime = Field(default_factory=utcnow)
    expires_at: UtcDatetime | None = None
    note: FreeText | None = None

    @model_validator(mode="after")
    def _expiry_is_after_approval(self) -> Approval:
        if self.expires_at is not None and self.expires_at <= self.approved_at:
            raise ValueError(
                f"expires_at {self.expires_at.isoformat()} is not after "
                f"approved_at {self.approved_at.isoformat()}"
            )
        return self

    def usable_at(self, moment: datetime) -> bool:
        """True if this approval still counts at `moment`."""
        if moment < self.approved_at:
            return False
        return self.expires_at is None or moment < self.expires_at

    @property
    def fingerprint(self) -> str:
        """The first bytes of the token, for the audit record.

        The audit log is read by people who are not entitled to replay the
        approval, so it records enough to match a token to its record and not
        enough to reuse it.
        """
        return f"{self.token[:6]}..."


@runtime_checkable
class ApprovalStore(Protocol):
    """Where the compiler looks for the second key."""

    def approval_for(self, proposal_id: str) -> Approval | None:  # pragma: no cover - protocol
        """The approval recorded for this proposal, or None."""
        ...


class NoApprovals:
    """An empty store: nothing is approved.

    The compiler's default. A run with no approval route rejects every
    two-key advisory rather than publishing it unapproved.
    """

    def approval_for(self, proposal_id: str) -> Approval | None:
        return None


class InMemoryApprovals:
    """The approvals one process knows about, newest per proposal.

    D3.2 hands this to the API so `POST /approvals/{proposal_id}` writes here
    and the compiler reads it. Single process and not durable, which is what a
    demo run needs and all it needs.
    """

    def __init__(self, approvals: Iterator[Approval] | list[Approval] | None = None) -> None:
        self._by_proposal: dict[str, Approval] = {}
        for approval in approvals or ():
            self.add(approval)

    def add(self, approval: Approval) -> Approval:
        """Record an approval someone else built."""
        self._by_proposal[approval.proposal_id] = approval
        return approval

    def grant(
        self,
        proposal_id: str,
        approved_by: str,
        *,
        token: str | None = None,
        ttl: timedelta | None = DEFAULT_TTL,
        note: str | None = None,
        now: datetime | None = None,
    ) -> Approval:
        """Approve a proposal and return the record, token included."""
        moment = now or utcnow()
        return self.add(
            Approval(
                proposal_id=proposal_id,
                token=token or new_token(),
                approved_by=approved_by,
                approved_at=moment,
                expires_at=moment + ttl if ttl is not None else None,
                note=note,
            )
        )

    def revoke(self, proposal_id: str) -> bool:
        """Drop an approval. True if there was one."""
        return self._by_proposal.pop(proposal_id, None) is not None

    def approval_for(self, proposal_id: str) -> Approval | None:
        return self._by_proposal.get(proposal_id)

    def pending(self, moment: datetime | None = None) -> tuple[Approval, ...]:
        """Every approval that still counts, oldest first."""
        at = moment or utcnow()
        usable = [item for item in self._by_proposal.values() if item.usable_at(at)]
        return tuple(sorted(usable, key=lambda item: item.approved_at))

    def __len__(self) -> int:
        return len(self._by_proposal)


@dataclass(frozen=True, slots=True)
class PendingCompile:
    """One proposal the gate refused only for want of a second key."""

    proposal: ProposedAdvisory
    verdict: CriticVerdict
    recorded_at: datetime

    @property
    def proposal_id(self) -> str:
        return self.proposal.id


class PendingCompiles:
    """The proposals waiting on a human, and what they were judged against.

    A compile is synchronous: the proposer hands the gate a proposal, the gate
    answers, and a proposal refused for want of an approval token would
    otherwise be gone before anyone could issue one. Proposal ids are drawn at
    random, so nobody can approve one in advance either.

    So the gate keeps it. `CompilerService.publish` records every rejection
    whose only failed rule was `two_key_approved`, with the verdict it was
    judged against, and `CompilerService.recompile` puts the pair back through
    the same rules once a token exists. Nothing here decides anything: the
    second trip through the gate is a full one, and an approval that arrived
    too late, for the wrong proposal or from nobody is refused again.

    Bounded, oldest dropped first. A long run can reach a lot of proposals
    nobody will ever approve, and a store that grew without limit would be a
    slow leak holding every hazard it ever saw.
    """

    def __init__(self, *, limit: int = 256) -> None:
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        self.limit = limit
        self._waiting: OrderedDict[str, PendingCompile] = OrderedDict()
        #: Proposals recorded and proposals put back through the gate: what a
        #: run reports about its own second key.
        self.recorded = 0
        self.recompiled = 0

    def remember(
        self,
        proposal: ProposedAdvisory,
        verdict: CriticVerdict,
        *,
        now: datetime | None = None,
    ) -> PendingCompile:
        """Record a proposal that needs a token, replacing any earlier one."""
        entry = PendingCompile(proposal=proposal, verdict=verdict, recorded_at=now or utcnow())
        self._waiting.pop(entry.proposal_id, None)
        self._waiting[entry.proposal_id] = entry
        while len(self._waiting) > self.limit:
            self._waiting.popitem(last=False)
        self.recorded += 1
        return entry

    def forget(self, proposal_id: str) -> bool:
        """Drop a proposal. True if there was one waiting."""
        return self._waiting.pop(proposal_id, None) is not None

    def get(self, proposal_id: str) -> PendingCompile | None:
        """The proposal waiting under this id, or None."""
        return self._waiting.get(proposal_id)

    def waiting(self) -> tuple[PendingCompile, ...]:
        """Every proposal waiting on a second key, oldest first."""
        return tuple(sorted(self._waiting.values(), key=lambda item: item.recorded_at))

    def clear(self) -> None:
        """Forget everything. A new scenario run starts with an empty store."""
        self._waiting.clear()

    def __len__(self) -> int:
        return len(self._waiting)
