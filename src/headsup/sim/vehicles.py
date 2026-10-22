"""Mutable per-tick vehicle state.

Plain slotted dataclasses, not Pydantic: `HeadsUpModel` sets
`validate_assignment=True`, so every field write on a contract model would run
a validator. At 900 vehicles and ten ticks a wall-second that is nine thousand
validations a second for state nobody outside this package ever sees.
`VehicleState` is built only when a telemetry frame is published.

Position is `offset_mm: int`, an exact integer. Nothing float accumulates
across ticks, so two runs of one seed agree exactly rather than approximately.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from headsup.contracts import VehicleStatus

#: Hashing and telemetry need a stable integer per status. `StrEnum` ordering
#: would depend on declaration order in another module; this table is local
#: and explicit, so appending a status later cannot renumber the existing ones.
STATUS_ORD: dict[VehicleStatus, int] = {
    VehicleStatus.MOVING: 0,
    VehicleStatus.STOPPED: 1,
    VehicleStatus.STALLED: 2,
    VehicleStatus.AWAITING_ASSIST: 3,
    VehicleStatus.OUT_OF_SERVICE: 4,
}

#: Statuses that mean the vehicle is not making progress. A vehicle in one of
#: these inside a KEEP_CLEAR corridor is accruing blocked vehicle-seconds.
HELD_STATUSES: frozenset[VehicleStatus] = frozenset(
    {
        VehicleStatus.STOPPED,
        VehicleStatus.STALLED,
        VehicleStatus.AWAITING_ASSIST,
        VehicleStatus.OUT_OF_SERVICE,
    }
)


@dataclass(slots=True)
class Vehicle:
    """One simulated vehicle between two intersections."""

    ix: int
    vid: str
    op: str

    #: Current edge id and how far along it the vehicle is, in millimetres.
    edge: int
    offset_mm: int

    #: Destination zone index, and which trip this is for this vehicle. The
    #: pair `(ix, trip_seq)` keys every trip-level random draw.
    zone_ix: int
    #: The vertex this trip ends at. In zone mode it is `zones[zone_ix]`;
    #: in `any_vertex_destinations` mode `zone_ix` is -1 and this is any
    #: core vertex, routed to per vehicle.
    dest_node: int = -1
    trip_seq: int = 0

    #: Explicit edge path, used only in `any_vertex_destinations` mode.
    #: Zone mode stores no path at all, which is why an advisory landing
    #: costs it no per-vehicle work.
    route: tuple[int, ...] = ()
    route_pos: int = 0
    #: Mask key the stored route was planned under; a change replans.
    route_key: str = ""

    status: VehicleStatus = VehicleStatus.MOVING
    #: Seconds left of a passenger dwell, or of a hold at an intersection.
    dwell_s: int = 0
    #: Consecutive seconds stalled; drives the stall-to-assist threshold.
    stalled_s: int = 0
    #: Consecutive seconds held at an intersection queue. Vehicles that have
    #: waited longest are admitted first, so a queue is fair and not a
    #: function of vehicle index alone.
    held_ticks: int = 0
    #: Seconds since the condition that stalled this vehicle cleared.
    recovering_s: int = 0
    #: Edges traversed on this trip, against `SimConfig.hop_budget`.
    hops: int = 0

    assist_open: bool = False
    #: Ground hazard already counted as an entry for this vehicle, so a
    #: vehicle sitting in a flood zone counts one entry, not one per tick.
    hazard_seen: str = ""

    #: Why this vehicle stalled, and where. A flooded edge blocks the *edge*
    #: the vehicle is on; a dark signal blocks the *node* it is waiting at, so
    #: "is it still blocked?" cannot be answered from the edge alone.
    stall_node: int = -1
    stall_reason: str = ""
    #: The `GroundHazard` that caused the stall, so the assist request can name
    #: it without having to guess from the vehicle's position.
    stall_cause: object | None = None

    @property
    def held(self) -> bool:
        """True when the vehicle is not moving along its edge this tick."""
        return self.status in HELD_STATUSES or self.dwell_s > 0

    @property
    def trip_id(self) -> str:
        """Stable per-trip identifier, derived rather than drawn."""
        return f"trip-{self.vid}-{self.trip_seq:04d}"


@dataclass(slots=True)
class Intent:
    """A vehicle that reached the end of its edge and wants to cross a node.

    Phase 1 of the tick produces these without touching shared state; phase 2
    admits them node by node. `residual_mm` is how far past the edge end the
    vehicle would have travelled, which is an exact integer measure of who
    arrived first -- the tie-break that makes contention reproducible.
    """

    veh: int
    node: int
    residual_mm: int
    next_edge: int


@dataclass(slots=True)
class NodeState:
    """Per-node admission state, for DENSITY_CAP and ALL_WAY_STOP.

    `admitted` counts vehicles let through *this tick* and is reset whenever
    the node is processed. A mesoscopic model has no room inside an
    intersection to hold vehicles in, so "limit vehicles per node" is
    implemented as a limit on how many may cross it per second -- a flow cap.
    The visible effect is the same one the advisory is asking for: a queue
    forms on the approaches instead of a scrum in the junction.
    """

    #: Vehicles admitted through this node during the current tick.
    admitted: int = 0
    #: The tick `admitted` refers to, so a stale count is never trusted.
    admitted_tick: int = -1
    #: Earliest tick at which an all-way stop may admit the next vehicle.
    next_free_tick: int = 0
    #: Vehicles contending for this node this tick, for dark-signal deadlock.
    contenders: int = 0
    pending: list[Intent] = field(default_factory=list)
