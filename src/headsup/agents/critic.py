"""The critic, as a pass-through stub until D3.2.

The real critic reads a proposal independently and may APPROVE, REVISE or
ESCALATE it. Until it exists, the compiler still needs a verdict for every
proposal, so this module supplies one: APPROVE, with a reason that says in
writing that nothing reviewed it.

It is deliberately one small module rather than a default inside the compiler.
A compiler that invented its own approval when none was supplied would be a
gate with a hole in it; a caller that has to ask for `stub_verdict` by name
leaves the fact in the audit record, where `reviewed_by` reads `critic_stub`
and the reason names the step that replaces it.
"""

from __future__ import annotations

from headsup.contracts import CriticVerdict, ProposedAdvisory, Verdict

#: Written into `CriticVerdict.reasons`, so an audit record of a run from
#: before D3.2 says which critic looked at the proposal: none.
STUB_REASON = "pass-through critic stub (D1.3); the real critic arrives in D3.2"

#: Written into `CriticVerdict.reviewed_by`. Never `critic`: a reader filtering
#: the audit stream for what the real critic approved must not match this.
STUB_NAME = "critic_stub"


def stub_verdict(proposal: ProposedAdvisory | str) -> CriticVerdict:
    """An APPROVE verdict for one proposal, reviewed by nothing."""
    return CriticVerdict(
        proposal_id=proposal if isinstance(proposal, str) else proposal.id,
        verdict=Verdict.APPROVE,
        reasons=[STUB_REASON],
        reviewed_by=STUB_NAME,
    )
