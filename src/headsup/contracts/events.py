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

    @property
    def authoritative(self) -> bool:
        """True for NWS, USGS and city dispatch (D2.5 evidence threshold)."""
        return self.source in AUTHORITATIVE_SOURCES

    @classmethod
    def from_event(cls, event: CivicEvent) -> Evidence:
        """Cite a CivicEvent without copying its payload."""
        return cls(
            event_id=event.id,
            source=event.source,
            kind=event.kind,
            observed_at=event.observed_at,
            raw_ref=event.raw_ref,
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
