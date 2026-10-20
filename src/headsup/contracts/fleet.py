"""What the fleets and the simulator send back.

`Telemetry` is shaped the way the viewer socket already speaks it
(`{type: "telemetry", vehicles: [{id, op, lon, lat, heading}]}`) so the God's
Eye View layer can consume it without a translation step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from headsup.contracts.base import (
    FreeText,
    HeadsUpModel,
    Identifier,
    Latitude,
    Longitude,
    UtcDatetime,
    new_id,
    utcnow,
)
from headsup.contracts.enums import AckStatus, AssistReason, HazardClass, VehicleStatus
from headsup.contracts.geometry import Corridor, Geometry, Point

#: Compass heading in degrees, 0 = north, clockwise.
Heading = Annotated[float, Field(ge=0.0, lt=360.0)]


class Ack(HeadsUpModel):
    """One operator's answer to one advisory version."""

    id: Identifier = Field(default_factory=lambda: new_id("ack"))
    advisory_id: Identifier
    advisory_version: int = Field(default=1, ge=1)
    operator: Identifier
    status: AckStatus
    reason: FreeText | None = None
    vehicles_rerouted: int = Field(default=0, ge=0)
    acknowledged_at: UtcDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _anything_short_of_acceptance_needs_a_reason(self) -> Ack:
        if self.status is not AckStatus.ACCEPTED and not self.reason:
            raise ValueError(f"{self.status} needs a reason")
        return self


class VehicleState(HeadsUpModel):
    """One simulated vehicle at one tick, in the viewer's field names."""

    id: Identifier
    op: Identifier
    lon: Longitude
    lat: Latitude
    heading: Heading = 0.0
    speed_mph: float = Field(default=0.0, ge=0.0, le=120.0)
    status: VehicleStatus = VehicleStatus.MOVING
    trip_id: Identifier | None = None


class Telemetry(HeadsUpModel):
    """One simulator tick: every vehicle plus the counters the console shows."""

    type: Literal["telemetry"] = "telemetry"
    tick: int = Field(ge=0)
    sim_time: UtcDatetime
    vehicles: list[VehicleState] = Field(default_factory=list)
    ghost: bool = False
    counters: dict[str, int] = Field(default_factory=dict)


class AssistRequest(HeadsUpModel):
    """A vehicle that stopped and needs a human: the queue the demo measures."""

    id: Identifier = Field(default_factory=lambda: new_id("assist"))
    vehicle_id: Identifier
    operator: Identifier
    location: Point
    reason: AssistReason
    hazard_id: Identifier | None = None
    hazard_class: HazardClass | None = None
    cluster_id: Identifier | None = None
    priority: int = Field(default=3, ge=1, le=5)
    requested_at: UtcDatetime = Field(default_factory=utcnow)
    ghost: bool = False
    notes: FreeText | None = None


class MoveOrder(HeadsUpModel):
    """A verified responder asking vehicles out of an area.

    `token_fingerprint` is the SHA-256 of the responder token the liaison
    checked; an order without one never reaches the compiler (attack A5).
    """

    id: Identifier = Field(default_factory=lambda: new_id("move"))
    responder_id: Identifier
    agency: Identifier
    geometry: Geometry
    vehicle_ids: list[Identifier] = Field(default_factory=list)
    reason: FreeText | None = None
    token_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    issued_at: UtcDatetime = Field(default_factory=utcnow)
    valid_until: UtcDatetime
    signature: str | None = None

    @model_validator(mode="after")
    def _window_is_ordered(self) -> MoveOrder:
        _check_expiry(self.issued_at, self.valid_until)
        return self


class KeepClear(HeadsUpModel):
    """A corridor that must stay clear: the geometry behind a KEEP_CLEAR action."""

    id: Identifier = Field(default_factory=lambda: new_id("kc"))
    geometry: Corridor
    width_m: float | None = Field(default=None, gt=0.0, le=200.0)
    hazard_id: Identifier | None = None
    advisory_id: Identifier | None = None
    reason: FreeText | None = None
    valid_from: UtcDatetime = Field(default_factory=utcnow)
    valid_until: UtcDatetime
    priority: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def _a_centre_line_needs_a_width(self) -> KeepClear:
        if self.geometry.type == "LineString" and self.width_m is None:
            raise ValueError("a LineString corridor needs width_m to become an area")
        _check_expiry(self.valid_from, self.valid_until)
        return self


def _check_expiry(start: datetime, valid_until: datetime) -> None:
    if valid_until <= start:
        raise ValueError(f"valid_until {valid_until.isoformat()} is not after {start.isoformat()}")
