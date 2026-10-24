"""Bus entries turned into viewer frames, with the ids that link them up.

The rail in the God's Eye View draws one row per pipeline stage and a line
from each row to the one it came from. Nothing on the bus carries a full
parent chain -- a `CriticVerdict` knows its proposal, an `Advisory` knows its
hazard, and neither knows the candidate the whole thing started from -- so the
links are assembled here, in `RailIndex`, as the entries go past.

Three of the four links are already in the contracts: a hazard lists the
candidates it fused, a proposal carries its hazard whole, and a verdict names
its proposal. The fourth is derived: `advisory_id_for` makes an advisory id out
of its proposal id, so `RailIndex` inverts that rather than guessing.

Every frame is a plain dict with a `type`, which is what `viz/mock_stream.py`
already speaks and what `gev/src/layers/headsup/source.js` already reads. Feed
and model text travels in `notes` and `reasons` fields as data: the viewer
shows it, nothing here acts on it.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from headsup.agents.compiler.compiler import advisory_id_for
from headsup.bus import (
    STREAM_ACKS,
    STREAM_ADVISORIES,
    STREAM_ASSIST,
    STREAM_CANDIDATES,
    STREAM_HAZARDS,
    STREAM_PROPOSALS,
    STREAM_VERDICTS,
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
from headsup.sim import counters

#: Frame types the viewer socket sends. `telemetry` and `advisory` are the two
#: `viz/mock_stream.py` established; the rest are new in this step.
FRAME_SCENARIO_STATE = "scenario_state"
FRAME_TELEMETRY = "telemetry"
FRAME_GHOST_TELEMETRY = "ghost_telemetry"
FRAME_CANDIDATE = "candidate"
FRAME_HAZARD = "hazard"
FRAME_PROPOSAL = "proposal"
FRAME_VERDICT = "verdict"
FRAME_ADVISORY = "advisory"
FRAME_ACK = "ack"
FRAME_ASSIST = "assist"
FRAME_COUNTERS = "counters"

#: The pipeline stages, in rail order. A viewer that wants to lay out rows
#: before anything has arrived reads this.
RAIL_STAGES: tuple[str, ...] = (
    FRAME_CANDIDATE,
    FRAME_HAZARD,
    FRAME_PROPOSAL,
    FRAME_VERDICT,
    FRAME_ADVISORY,
    FRAME_ACK,
)

#: The streams the viewer feed follows, and the frame each one becomes.
STREAM_FRAMES: dict[str, str] = {
    STREAM_CANDIDATES: FRAME_CANDIDATE,
    STREAM_HAZARDS: FRAME_HAZARD,
    STREAM_PROPOSALS: FRAME_PROPOSAL,
    STREAM_VERDICTS: FRAME_VERDICT,
    STREAM_ADVISORIES: FRAME_ADVISORY,
    STREAM_ACKS: FRAME_ACK,
    STREAM_ASSIST: FRAME_ASSIST,
}

#: How many parent links are remembered. A scenario run produces a few hundred
#: entries; past that the oldest rail rows have scrolled away anyway.
INDEX_LIMIT = 2000


def _event_ids(evidence: list[Any]) -> list[str]:
    return [item.event_id for item in evidence]


class RailIndex:
    """Remembers which parent each pipeline id came from, newest first.

    Bounded and ordered rather than a plain dict: a server that runs scenario
    after scenario must not grow a link table forever, and the links that
    matter to a rail are the recent ones.
    """

    def __init__(self, limit: int = INDEX_LIMIT) -> None:
        self.limit = limit
        #: candidate id -> the event ids it cites.
        self._candidate_events: OrderedDict[str, list[str]] = OrderedDict()
        #: hazard id -> the candidate ids fused into it.
        self._hazard_candidates: OrderedDict[str, list[str]] = OrderedDict()
        #: proposal id -> its hazard id.
        self._proposal_hazard: OrderedDict[str, str] = OrderedDict()
        #: advisory id -> its proposal id, inverted from `advisory_id_for`.
        self._advisory_proposal: OrderedDict[str, str] = OrderedDict()

    def clear(self) -> None:
        """Forget everything. A new scenario run shares no ids with the last."""
        self._candidate_events.clear()
        self._hazard_candidates.clear()
        self._proposal_hazard.clear()
        self._advisory_proposal.clear()

    def _remember(self, table: OrderedDict[str, Any], key: str, value: Any) -> None:
        table[key] = value
        table.move_to_end(key)
        while len(table) > self.limit:
            table.popitem(last=False)

    # --- learning as entries go past ---------------------------------------

    def note_candidate(self, candidate: CandidateHazard) -> None:
        self._remember(self._candidate_events, candidate.id, _event_ids(candidate.evidence))

    def note_hazard(self, hazard: Hazard) -> None:
        self._remember(self._hazard_candidates, hazard.id, list(hazard.candidates))

    def note_proposal(self, proposal: ProposedAdvisory) -> None:
        self._remember(self._proposal_hazard, proposal.id, proposal.hazard.id)
        self._remember(self._advisory_proposal, advisory_id_for(proposal.id), proposal.id)

    # --- answering the rail's questions ------------------------------------

    def candidates_of(self, hazard_id: str) -> list[str]:
        return list(self._hazard_candidates.get(hazard_id, ()))

    def hazard_of_proposal(self, proposal_id: str) -> str | None:
        return self._proposal_hazard.get(proposal_id)

    def proposal_of_advisory(self, advisory_id: str) -> str | None:
        """The proposal an advisory compiled from.

        Falls back to undoing `advisory_id_for`'s `adv_`/`prop_` swap, so an
        advisory that reached the socket before its proposal did -- or one
        published by a compiler in another process -- still links up.
        """
        known = self._advisory_proposal.get(advisory_id)
        if known is not None:
            return known
        if advisory_id.startswith("adv_"):
            return f"prop_{advisory_id[len('adv_') :]}"
        return None


def candidate_frame(candidate: CandidateHazard, index: RailIndex) -> dict[str, Any]:
    """A sentinel's reading, linked back to the observations it cites."""
    index.note_candidate(candidate)
    return {
        "type": FRAME_CANDIDATE,
        "id": candidate.id,
        "class": candidate.hazard_class.value,
        "severity": candidate.severity,
        "confidence": candidate.confidence,
        "detected_by": candidate.detected_by,
        "detected_at": candidate.detected_at.isoformat(),
        "valid_until": candidate.valid_until.isoformat(),
        "event_ids": _event_ids(candidate.evidence),
        "sources": sorted({item.source.value for item in candidate.evidence}),
        "geometry": candidate.geometry.to_wire(),
        "notes": candidate.notes,
    }


def hazard_frame(hazard: Hazard, index: RailIndex) -> dict[str, Any]:
    """A fused hazard, linked back to its candidates."""
    index.note_hazard(hazard)
    return {
        "type": FRAME_HAZARD,
        "id": hazard.id,
        "class": hazard.hazard_class.value,
        "severity": hazard.severity,
        "confidence": hazard.confidence,
        "candidate_ids": list(hazard.candidates),
        "event_ids": _event_ids(hazard.evidence),
        "sources": sorted({item.source.value for item in hazard.evidence}),
        "updated_at": hazard.updated_at.isoformat(),
        "valid_until": hazard.valid_until.isoformat(),
        "geometry": hazard.geometry.to_wire(),
        "rationale": hazard.rationale,
    }


def proposal_frame(proposal: ProposedAdvisory, index: RailIndex) -> dict[str, Any]:
    """A proposed action, linked back to its hazard and that hazard's candidates."""
    index.note_proposal(proposal)
    return {
        "type": FRAME_PROPOSAL,
        "id": proposal.id,
        "hazard_id": proposal.hazard.id,
        "candidate_ids": index.candidates_of(proposal.hazard.id),
        "action": proposal.action.type.value,
        "params": dict(proposal.action.params),
        "policy_rule": proposal.policy_rule,
        "proposed_by": proposal.proposed_by,
        "valid_from": proposal.valid_from.isoformat(),
        "valid_until": proposal.valid_until.isoformat(),
        "geometry": proposal.geometry.to_wire(),
        "rationale": proposal.rationale,
    }


def verdict_frame(verdict: CriticVerdict, index: RailIndex) -> dict[str, Any]:
    """The critic's answer, linked back to the proposal and its hazard."""
    return {
        "type": FRAME_VERDICT,
        "proposal_id": verdict.proposal_id,
        "hazard_id": index.hazard_of_proposal(verdict.proposal_id),
        "verdict": verdict.verdict.value,
        "reasons": list(verdict.reasons),
        "reviewed_by": verdict.reviewed_by,
        "revised": verdict.revised,
        "created_at": verdict.created_at.isoformat(),
    }


def advisory_frame(
    advisory: Advisory, index: RailIndex, *, signature_valid: bool
) -> dict[str, Any]:
    """A published advisory: the whole Feature, plus its place in the chain.

    `feature` is the advisory exactly as the compiler signed it, so the layer
    that draws it is reading the contract and not a summary of it.
    `signature_valid` is this server's own verification, stated rather than
    assumed: an advisory that does not verify still reaches the viewer, marked.
    """
    proposal_id = index.proposal_of_advisory(advisory.properties.id)
    return {
        "type": FRAME_ADVISORY,
        "id": advisory.properties.id,
        "version": advisory.properties.version,
        "proposal_id": proposal_id,
        "hazard_id": advisory.hazard.id,
        "candidate_ids": index.candidates_of(advisory.hazard.id),
        "action": advisory.action.type.value,
        "policy_rule": advisory.properties.policy_rule,
        "issuer": advisory.properties.issuer,
        "valid_from": advisory.properties.valid_from.isoformat(),
        "valid_until": advisory.properties.valid_until.isoformat(),
        "signature_valid": signature_valid,
        "feature": advisory.to_wire(),
    }


def ack_frame(ack: Ack, index: RailIndex) -> dict[str, Any]:
    """One operator's answer, linked back to the advisory it answers."""
    return {
        "type": FRAME_ACK,
        "id": ack.id,
        "advisory_id": ack.advisory_id,
        "advisory_version": ack.advisory_version,
        "proposal_id": index.proposal_of_advisory(ack.advisory_id),
        "operator": ack.operator,
        "status": ack.status.value,
        "reason": ack.reason,
        "vehicles_rerouted": ack.vehicles_rerouted,
        "acknowledged_at": ack.acknowledged_at.isoformat(),
    }


def assist_frame(assist: AssistRequest) -> dict[str, Any]:
    """A vehicle waiting for a human: the queue the demo measures."""
    return {
        "type": FRAME_ASSIST,
        "id": assist.id,
        "vehicle_id": assist.vehicle_id,
        "operator": assist.operator,
        "reason": assist.reason.value,
        "hazard_id": assist.hazard_id,
        "class": assist.hazard_class.value if assist.hazard_class else None,
        "priority": assist.priority,
        "ghost": assist.ghost,
        "location": assist.location.to_wire(),
        "requested_at": assist.requested_at.isoformat(),
    }


#: The four figures the demo's side-by-side panel reads, and the counter key
#: each one is taken from. Blocked corridor minutes is the exception and is
#: not in this map: it is summed from `blocked_minutes`, which carries the
#: vehicle-seconds undivided, rather than from the floor-divided integer in
#: the snapshot.
HEADLINE_KEYS: dict[str, str] = {
    "hazard_entries": counters.key(counters.HAZARD_ENTRIES),
    "assist_queue_depth": counters.key(counters.ASSIST_OPEN),
    "rides_completed": counters.key(counters.RIDES_COMPLETED),
}

#: The headline figure derived from `blocked_minutes` instead.
HEADLINE_BLOCKED = "blocked_corridor_minutes"


def headline(snapshot: dict[str, int], blocked: dict[str, float]) -> dict[str, float]:
    """One fleet set's four demo figures, named as the panel names them.

    The full snapshot stays in the frame beside this; the headline exists so
    the viewer reads the comparison off four agreed names rather than off
    counter keys it would have to know the spelling of.
    """
    out: dict[str, float] = {
        name: float(snapshot.get(key, 0)) for name, key in HEADLINE_KEYS.items()
    }
    out[HEADLINE_BLOCKED] = round(sum(blocked.values()), 2)
    return out


def counters_frame(
    by_mode: dict[str, dict[str, int]], blocked_minutes: dict[str, dict[str, float]]
) -> dict[str, Any]:
    """The live and ghost counter sets side by side: the comparison, as numbers.

    `headline` is the same two sets reduced to the four figures the demo
    panel shows -- hazard entries, assist queue depth, blocked corridor
    minutes, completed rides -- one entry per mode, so `headline["live"]` and
    `headline["ghost"]` are the two columns.
    """
    return {
        "type": FRAME_COUNTERS,
        "counters": by_mode,
        "blocked_minutes": blocked_minutes,
        "headline": {
            mode: headline(snapshot, blocked_minutes.get(mode, {}))
            for mode, snapshot in by_mode.items()
        },
    }
