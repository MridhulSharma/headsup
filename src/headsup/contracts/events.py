"""What comes off a feed, and what a sentinel makes of it.

`CivicEvent` is the raw observation, `Evidence` is a citation back to one, and
`CandidateHazard` is a single sentinel's guess before fusion weighs it.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from headsup.contracts.base import (
    Confidence,
    FreeText,
    HeadsUpModel,
    Identifier,
    Kind,
    RawRef,
    Severity,
    UtcDatetime,
    new_id,
    utcnow,
)
from headsup.contracts.enums import AUTHORITATIVE_SOURCES, HazardClass, Source
from headsup.contracts.geometry import Geometry


class CivicEvent(HeadsUpModel):
    """One observation from one public feed, as ingested.

    Everything a feed says in prose stays inside `payload`: it is data for the
    sentinels to read, never text that reaches an instruction slot.
    """

    id: Identifier = Field(default_factory=lambda: new_id("ev"))
    source: Source
    kind: Kind
    geometry: Geometry
    observed_at: UtcDatetime
    raw_ref: RawRef
    payload: dict[str, Any] = Field(default_factory=dict)
    ingested_at: UtcDatetime = Field(default_factory=utcnow)


class Evidence(HeadsUpModel):
    """A citation: which observation supports a hazard or an advisory."""

    event_id: Identifier
    source: Source
    kind: Kind
    observed_at: UtcDatetime
    raw_ref: RawRef
    #: Where the observation was taken, when a later agent has to reason about
    #: it geometrically. A stream gauge is the case this exists for: fusion
    #: narrows a flood hazard to the reach its gauges cover, and a citation
    #: that said only "USGS 08178593 at 21:15Z" could not tell it where that
    #: is. Off by default, because an areal product's own area is already the
    #: candidate's geometry and copying it into every citation is bus traffic
    #: for nothing. `raw_ref` is still the way back to the whole observation.
    geometry: Geometry | None = None

    @property
    def authoritative(self) -> bool:
        """True for NWS, USGS and city dispatch (D2.5 evidence threshold)."""
        return self.source in AUTHORITATIVE_SOURCES

    @classmethod
    def from_event(cls, event: CivicEvent, *, with_geometry: bool = False) -> Evidence:
        """Cite a CivicEvent without copying its payload.

        `with_geometry` carries the event's own geometry into the citation.
        Asked for at the call site rather than inferred, so a sentinel that
        wants a downstream agent to be able to place an observation says so.
        """
        return cls(
            event_id=event.id,
            source=event.source,
            kind=event.kind,
            observed_at=event.observed_at,
            raw_ref=event.raw_ref,
            geometry=event.geometry if with_geometry else None,
        )


class CandidateHazard(HeadsUpModel):
    """One sentinel's single-source reading, on its way to fusion."""

    id: Identifier = Field(default_factory=lambda: new_id("cand"))
    hazard_class: HazardClass = Field(alias="class")
    severity: Severity
    confidence: Confidence
    geometry: Geometry
    evidence: list[Evidence] = Field(min_length=1)
    detected_by: Identifier
    detected_at: UtcDatetime = Field(default_factory=utcnow)
    valid_until: UtcDatetime
    notes: FreeText | None = None

    @model_validator(mode="after")
    def _expiry_is_in_the_future(self) -> CandidateHazard:
        if self.valid_until <= self.detected_at:
            raise ValueError(
                f"valid_until {self.valid_until.isoformat()} is not after "
                f"detected_at {self.detected_at.isoformat()}"
            )
        return self


class GeometryNarrowing(HeadsUpModel):
    """Why a fused hazard's area is, or is not, narrower than its feeds' own.

    A sentinel publishes the area a feed drew. Fusion may cut that down to the
    part its corroborating observations actually cover - an NWS flood warning
    polygon clipped to the reach of the gauges above their action stage under
    it. Either way the decision is recorded here and travels with the hazard,
    so an audit record carries both the geometry the feed published and the
    geometry a fleet was told about, and the reason they differ or do not.

    Never a widening: `area_km2` is always at or below `original_area_km2`.
    """

    #: False when the original geometry travelled on unchanged; `reason` then
    #: says what stopped the narrowing.
    narrowed: bool
    reason: FreeText
    #: The geometry before the clip: what the feeds themselves published.
    original_geometry: Geometry
    original_area_km2: float = Field(ge=0.0)
    #: The geometry the hazard carries, measured the way the compiler measures.
    area_km2: float = Field(ge=0.0)
    #: The reach radius each corroborating observation was buffered by.
    reach_radius_m: float = Field(ge=0.0)
    #: The citations the reach was measured from, by event id, so two runs of
    #: the same recording record them in the same order.
    corroborated_by: list[Identifier] = Field(default_factory=list)
    #: Citations whose reach was deliberately left out of the geometry, with
    #: `reason` saying why. A hazard that corroborates two reaches too far
    #: apart to advise on as one area keeps the one already in force and
    #: excludes the rest; they are still cited as evidence for the hazard,
    #: they just do not shape what a fleet was told to avoid.
    excluded: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def _narrowing_never_widens(self) -> GeometryNarrowing:
        if self.area_km2 > self.original_area_km2:
            raise ValueError(
                f"narrowed area {self.area_km2} km2 is larger than the original "
                f"{self.original_area_km2} km2; narrowing may never widen a hazard"
            )
        return self


class Hazard(HeadsUpModel):
    """A fused hazard: what Heads Up believes, with every citation attached."""

    id: Identifier = Field(default_factory=lambda: new_id("haz"))
    hazard_class: HazardClass = Field(alias="class")
    severity: Severity
    confidence: Confidence
    geometry: Geometry
    evidence: list[Evidence] = Field(min_length=1)
    valid_until: UtcDatetime
    first_seen: UtcDatetime = Field(default_factory=utcnow)
    updated_at: UtcDatetime = Field(default_factory=utcnow)
    candidates: list[Identifier] = Field(default_factory=list)
    rationale: FreeText | None = None
    #: How `geometry` relates to what the feeds published, when fusion
    #: considered narrowing it. None means no narrowing rule applies to this
    #: hazard class.
    narrowing: GeometryNarrowing | None = None

    @property
    def authoritative_sources(self) -> set[Source]:
        """The authoritative feeds among this hazard's citations."""
        return {item.source for item in self.evidence if item.authoritative}

    @property
    def sources(self) -> set[Source]:
        return {item.source for item in self.evidence}

    @model_validator(mode="after")
    def _expiry_is_in_the_future(self) -> Hazard:
        if self.valid_until <= self.first_seen:
            raise ValueError(
                f"valid_until {self.valid_until.isoformat()} is not after "
                f"first_seen {self.first_seen.isoformat()}"
            )
        return self
