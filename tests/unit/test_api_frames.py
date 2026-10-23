"""The rail's parent links, which nothing on the bus carries end to end.

A viewer draws a line from an ack back to the advisory, the proposal, the
hazard and the candidate it came from. Three of those links are in the
contracts and the fourth is derived from the advisory id, so these tests pin
both the derivation and the fallback that covers an advisory arriving before
its proposal did.
"""

from __future__ import annotations

from headsup.api.frames import (
    FRAME_ACK,
    FRAME_ADVISORY,
    FRAME_CANDIDATE,
    FRAME_HAZARD,
    FRAME_PROPOSAL,
    FRAME_VERDICT,
    RailIndex,
    ack_frame,
    advisory_frame,
    candidate_frame,
    hazard_frame,
    proposal_frame,
    verdict_frame,
)
from samples import ACK, ADVISORY, CANDIDATE, HAZARD, PROPOSAL, VERDICT


def test_the_chain_links_every_stage_to_its_parent() -> None:
    index = RailIndex()
    candidate = candidate_frame(CANDIDATE, index)
    hazard = hazard_frame(HAZARD, index)
    proposal = proposal_frame(PROPOSAL, index)
    verdict = verdict_frame(VERDICT, index)
    advisory = advisory_frame(ADVISORY, index, signature_valid=False)
    ack = ack_frame(ACK, index)

    assert candidate["type"] == FRAME_CANDIDATE
    assert candidate["event_ids"] == [item.event_id for item in CANDIDATE.evidence]
    assert hazard["type"] == FRAME_HAZARD
    assert candidate["id"] in hazard["candidate_ids"]
    assert proposal["type"] == FRAME_PROPOSAL
    assert proposal["hazard_id"] == hazard["id"]
    assert proposal["candidate_ids"] == hazard["candidate_ids"]
    assert verdict["type"] == FRAME_VERDICT
    assert verdict["proposal_id"] == proposal["id"]
    assert verdict["hazard_id"] == hazard["id"]
    assert advisory["type"] == FRAME_ADVISORY
    assert advisory["proposal_id"] == proposal["id"]
    assert advisory["hazard_id"] == hazard["id"]
    assert ack["type"] == FRAME_ACK
    assert ack["advisory_id"] == advisory["id"]
    assert ack["proposal_id"] == proposal["id"]


def test_an_advisory_frame_carries_the_whole_signed_feature() -> None:
    """The layer that draws it reads the contract, not a summary of it."""
    frame = advisory_frame(ADVISORY, RailIndex(), signature_valid=True)
    assert frame["feature"] == ADVISORY.to_wire()
    assert frame["signature_valid"] is True


def test_an_advisory_links_back_without_having_seen_its_proposal() -> None:
    """A compiler in another process publishes an advisory this server links anyway."""
    index = RailIndex()
    frame = advisory_frame(ADVISORY, index, signature_valid=True)
    assert frame["proposal_id"] == PROPOSAL.id


def test_an_advisory_with_an_unreadable_id_links_to_nothing() -> None:
    index = RailIndex()
    assert index.proposal_of_advisory("not-an-advisory-id") is None


def test_the_index_forgets_the_oldest_links_rather_than_growing() -> None:
    """A server that runs scenario after scenario must not grow a link table."""
    index = RailIndex(limit=2)
    for number in range(3):
        index.note_proposal(PROPOSAL.model_copy(update={"id": f"prop_{number:012d}"}))
    assert index.hazard_of_proposal("prop_000000000000") is None
    assert index.hazard_of_proposal("prop_000000000002") == HAZARD.id


def test_clear_drops_every_link() -> None:
    """A new scenario run shares no ids with the last one."""
    index = RailIndex()
    hazard_frame(HAZARD, index)
    proposal_frame(PROPOSAL, index)
    index.clear()
    assert index.candidates_of(HAZARD.id) == []
    assert index.hazard_of_proposal(PROPOSAL.id) is None
