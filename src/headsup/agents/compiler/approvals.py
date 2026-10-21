"""The second key: a human approval token for the advisories that need one.

Two classes of advisory never publish on the agents' word alone: anything at
severity 4 or above, and anything covering more ground than its playbook's
`two_key_area_km2`. Those wait for a token a person issued. D3.2 puts the real
endpoints on it (`POST /approvals/{proposal_id}`, `GET /approvals`); this
module is the store behind them, and the shape the compiler checks.

A token is deliberately boring: an opaque string bound to one proposal id,
with who issued it and when it stops counting. It is not a capability the
holder can spend elsewhere, because the compiler only ever looks up the
approval for the proposal in front of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from secrets import token_urlsafe
from typing import Annotated, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, model_validator

from headsup.contracts import FreeText, HeadsUpModel, Identifier, UtcDatetime, utcnow

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
