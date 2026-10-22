"""The deterministic core: one `Simulation`, stepped one sim-second at a time.

No Redis, no asyncio, no wall clock. Everything that varies between two runs
of the same seed is either an integer or derived from one, which is why
`trajectory_hash` can be compared byte for byte rather than approximately.

The tick is two-phase, and that is what makes contention reproducible:

* **Phase 1, advance.** Every vehicle moves along its edge, reading shared
  state but writing none of it. A vehicle that reaches its edge's end emits an
  `Intent` instead of crossing.
* **Phase 2, commit.** Intents are grouped by node, nodes are processed in
  ascending vertex id, and the intents at one node are admitted in a fixed
  order. Nothing in phase 2 depends on the order phase 1 happened to run in.

Advisories only ever *restrict*; ground truth decides what is physically
possible. A vehicle stalls because the road is flooded, not because nobody
told it -- which is what lets the ghost fleet fail on the same roads the
advised fleet avoids.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from headsup.contracts import (
    Ack,
    AckStatus,
    Advisory,
    AssistReason,
    AssistRequest,
    HazardClass,
    Point,
    Telemetry,
    VehicleState,
    VehicleStatus,
)
from headsup.sim import counters as C
from headsup.sim.config import PERSONAS, SimConfig, SimMode
from headsup.sim.counters import Counters
from headsup.sim.ground_truth import EmptyHazardField, HazardField, HazardSnapshot
from headsup.sim.hashing import TrajectoryHash
from headsup.sim.masks import EMPTY_MASK, Mask, resolve
from headsup.sim.network import DriveNetwork
from headsup.sim.operators import (
    PERSONA_BY_NAME,
    MaskChange,
    OperatorSet,
    ghost_persona,
)
from headsup.sim.rng import SeedTree
from headsup.sim.router import Router
from headsup.sim.vehicles import Intent, NodeState, Vehicle

#: Reasons a hazard class gives when a vehicle stalls in it.
_ASSIST_REASON: dict[HazardClass, AssistReason] = {
    HazardClass.FLOOD: AssistReason.FLOODED_EDGE,
    HazardClass.SIGNAL_OUTAGE: AssistReason.DARK_SIGNAL,
    HazardClass.EMERGENCY_SCENE: AssistReason.EMERGENCY_SCENE,
    HazardClass.WORK_ZONE: AssistReason.BLOCKED_ROUTE,
}

#: Dark intersections only deadlock when vehicles actually meet. One car
#: crossing an unlit signal proceeds; two arriving together is the failure D3.1
#: dramatises. Deterministic, and it keeps the assist queue meaningful.
DARK_CONTENTION = 2

MM_PER_M = 1000.0
MPH_PER_MPS = 2.236936


@dataclass(frozen=True, slots=True)
class TickResult:
    """Everything one tick produced, including its own cost invariants."""

    tick: int
    sim_time: datetime
    telemetry: Telemetry | None
    acks: tuple[Ack, ...] = ()
    assists: tuple[AssistRequest, ...] = ()
    #: Labels the router computed this tick. Bounded by `label_budget`.
    label_refreshes: int = 0
    #: Mask resolutions, each of which runs shapely. Must be zero on any tick
    #: where no new advisory version arrived -- the invariant that keeps
    #: geometry out of the hot loop.
    shapely_calls: int = 0
    #: Pydantic models built. Zero except on a telemetry tick.
    models_built: int = 0


@dataclass(slots=True)
class SimReport:
    """What a whole run produced, per mode."""

    ticks: int = 0
    sim_span_s: float = 0.0
    wall_span_s: float = 0.0
    counters_by_mode: dict[str, dict[str, int]] = field(default_factory=dict)
    hashes_by_mode: dict[str, str] = field(default_factory=dict)
    blocked_minutes_by_mode: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def speedup(self) -> float:
        """Sim seconds per wall second: the 10x figure."""
        if self.wall_span_s <= 0:
            return float("inf")
        return self.sim_span_s / self.wall_span_s


class Simulation:
    """One fleet set -- live or ghost -- on one city, stepped by the second."""

    __slots__ = (
        "_assist_seq",
        "_cfg",
        "_corridor",
        "_counters",
        "_hash",
        "_hazards",
        "_mask_cache",
        "_mode",
        "_net",
        "_nodes",
        "_operators",
        "_pending",
        "_router",
        "_seeds",
        "_shapely_calls",
        "_start",
        "_tick",
        "_vehicles",
        "_zone_set",
    )

    def __init__(
        self,
        net: DriveNetwork,
        cfg: SimConfig,
        *,
        mode: SimMode = SimMode.LIVE,
        hazards: HazardField | None = None,
        start: datetime,
        router: Router | None = None,
        seeds: SeedTree | None = None,
    ) -> None:
        cfg.validate()
        self._net = net
        self._cfg = cfg
        self._mode = mode
        self._hazards: HazardField = hazards or EmptyHazardField()
        self._start = start
        self._tick = 0
        self._seeds = seeds or SeedTree(cfg.seed)
        # The router is shared between a live and a ghost simulation on
        # purpose: labels are keyed by mask content, and the ghost's mask is
        # the empty one, so the ghost costs no routing work at all.
        self._router = router or Router(net, cfg)

        personas = tuple(PERSONA_BY_NAME[name] for name in cfg.personas)
        if mode is SimMode.GHOST:
            personas = tuple(ghost_persona(name) for name in cfg.personas)
        self._operators = OperatorSet.build(personas, self._seeds)
        self._counters = Counters(self._operators.names)
        self._hash = TrajectoryHash()
        self._nodes: list[NodeState] = [NodeState() for _ in range(net.node_count)]
        self._mask_cache: dict[tuple[str, int], Mask] = {}
        self._pending: list[Advisory] = []
        self._shapely_calls = 0
        self._assist_seq = 0
        self._zone_set = frozenset(net.zones)
        # Measurement is deliberately separate from behaviour: a declining
        # operator's blocked-corridor time has to be counted against the
        # corridor even though its own mask is empty, or the comparison that
        # justifies KEEP_CLEAR cannot be made.
        self._corridor: Mask = EMPTY_MASK
        self._vehicles = self._spawn()

    # --- construction -------------------------------------------------------

    def _spawn(self) -> list[Vehicle]:
        """Place every vehicle, identically for live and ghost.

        Draws come from `("spawn",)` keyed by vehicle index, not by fleet, so a
        ghost run of the same seed starts from byte-identical positions. D3.5
        depends on that and asserts it.
        """
        net = self._net
        cfg = self._cfg
        prefix = "ghost" if self._mode is SimMode.GHOST else "veh"
        # A fleet does not begin its shift parked in a flood. Without this a
        # vehicle spawned on an impassable edge is stuck for the whole run --
        # it never "entered" the hazard so it is never counted, and it never
        # recovers because the edge it is sitting on is the blocked one.
        start_snapshot = self._hazards.snapshot(self._start)
        spawnable = (
            tuple(edge for edge in net.core_edges if edge not in start_snapshot.impassable)
            or net.core_edges
        )
        vehicles: list[Vehicle] = []
        ix = 0
        for op in self._operators.names:
            for _ in range(cfg.per_fleet):
                edge = self._seeds.draw_choice("spawn", ix, "edge", options=spawnable)
                length = net.length_mm[edge]
                offset = self._seeds.draw_int("spawn", ix, "offset", lo=0, hi=max(0, length - 1))
                zone_ix, dest_node = self._pick_destination(ix, 0, exclude=())
                vehicles.append(
                    Vehicle(
                        ix=ix,
                        vid=f"{prefix}-{op}-{ix:04d}",
                        op=op,
                        edge=edge,
                        offset_mm=offset,
                        zone_ix=zone_ix,
                        dest_node=dest_node,
                    )
                )
                ix += 1
        return vehicles

    def _pick_destination(
        self, veh_ix: int, trip_seq: int, *, exclude: Sequence[int]
    ) -> tuple[int, int]:
        """`(zone index, destination vertex)` for one trip.

        Keyed by `(vehicle, trip)` rather than drawn sequentially, so
        excluding a destination -- which a KEEP_CLEAR corridor does -- shifts
        no other vehicle's draw.
        """
        net = self._net
        if self._cfg.any_vertex_destinations:
            banned = frozenset(exclude)
            pool = tuple(node for node in net.core if node not in banned) or net.core
            node = self._seeds.draw_choice("trip", veh_ix, trip_seq, "node", options=pool)
            return -1, node
        allowed = tuple(ix for ix in range(len(net.zones)) if net.zones[ix] not in exclude)
        if not allowed:
            allowed = tuple(range(len(net.zones)))
        zone_ix = self._seeds.draw_choice("trip", veh_ix, trip_seq, "zone", options=allowed)
        return zone_ix, net.zones[zone_ix]

    def _next_edge(self, veh: Vehicle, mask: Mask, node: int) -> int:
        """The edge to leave `node` by, under this operator's mask.

        Zone mode reads it off a reverse label, so there is no stored path and
        nothing to invalidate when a mask changes. Any-vertex mode plans a
        path per vehicle and replans when the mask key moves -- the expensive
        route the zone design exists to avoid, kept for evaluation.
        """
        if veh.zone_ix >= 0:
            return self._router.next_hop(mask.key, veh.zone_ix, node)

        if veh.route_key != mask.key or veh.route_pos >= len(veh.route):
            veh.route = self._router.route_to_node(mask.key, node, veh.dest_node)
            veh.route_pos = 0
            veh.route_key = mask.key
        if veh.route_pos >= len(veh.route):
            return -1
        edge = veh.route[veh.route_pos]
        if self._net.tail[edge] != node or edge in mask.no_entry:
            veh.route = self._router.route_to_node(mask.key, node, veh.dest_node)
            veh.route_pos = 0
            if not veh.route:
                return -1
            edge = veh.route[0]
            if self._net.tail[edge] != node:
                return -1
        return edge

    def note_rejected_advisory(self) -> None:
        """An advisory failed verification upstream and was never applied."""
        self._counters.bump_run(C.ADVISORIES_REJECTED)

    # --- inspection ---------------------------------------------------------

    @property
    def tick(self) -> int:
        return self._tick

    @property
    def mode(self) -> SimMode:
        return self._mode

    @property
    def sim_time(self) -> datetime:
        return self._start + timedelta(seconds=self._tick * self._cfg.tick_s)

    @property
    def vehicles(self) -> tuple[Vehicle, ...]:
        return tuple(self._vehicles)

    @property
    def operators(self) -> OperatorSet:
        return self._operators

    @property
    def router(self) -> Router:
        return self._router

    @property
    def network(self) -> DriveNetwork:
        return self._net

    def counters(self) -> dict[str, int]:
        return self._counters.snapshot()

    def blocked_minutes(self) -> dict[str, float]:
        return {op: self._counters.blocked_minutes(op) for op in self._operators.names}

    def trajectory_hash(self) -> str:
        return self._hash.digest()

    def checkpoints(self) -> tuple[tuple[int, str], ...]:
        return self._hash.checkpoints()

    # --- input --------------------------------------------------------------

    def offer(self, advisory: Advisory) -> None:
        """Queue an advisory. It is answered at the start of the next step."""
        self._pending.append(advisory)

    # --- the tick -----------------------------------------------------------

    def step(self) -> TickResult:
        """Advance exactly one simulated second."""
        self._tick += 1
        now = self.sim_time
        self._shapely_calls = 0

        acks: list[Ack] = []
        self._drain_offers(now, acks)

        changes = {op.name: op.tick(now) for op in self._operators.operators}
        self._router.forget_unused(self._operators.live_mask_keys(), owner=id(self))
        for op in self._operators.operators:
            self._router.ensure_mask(op.mask)
        refreshes = self._router.refresh()

        snapshot = self._hazards.snapshot(now)
        assists: list[AssistRequest] = []

        intents = self._advance(snapshot)
        self._commit(intents, snapshot, now, assists)
        self._account(snapshot, now, assists)
        self._emit_acks(changes, acks)

        self._hash.fold(self._tick, self._vehicles)

        telemetry = None
        models = 0
        if self._tick % self._cfg.telemetry_stride == 0:
            telemetry, models = self._telemetry(now)

        return TickResult(
            tick=self._tick,
            sim_time=now,
            telemetry=telemetry,
            acks=tuple(acks),
            assists=tuple(assists),
            label_refreshes=refreshes,
            shapely_calls=self._shapely_calls,
            models_built=models,
        )

    def _drain_offers(self, now: datetime, acks: list[Ack]) -> None:
        """Answer queued advisories, resolving each mask at most once."""
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        for advisory in pending:
            key = (advisory.properties.id, advisory.properties.version)
            mask = self._mask_cache.get(key)
            if mask is None:
                mask = resolve(advisory, self._net, self._cfg)
                self._mask_cache[key] = mask
                self._shapely_calls += 1
            self._counters.bump_run(C.ADVISORIES_SEEN)
            if mask.keep_clear_edges:
                # Registered for measurement regardless of who accepts it.
                from headsup.sim.masks import merge as merge_masks

                self._corridor = merge_masks((self._corridor, mask))
            for op in self._operators.operators:
                decision = op.offer(advisory, mask, now)
                if not decision.accepted:
                    self._counters.bump_run(C.ADVISORIES_DECLINED)
            acks.extend(self._collect_acks())

    def _collect_acks(self) -> list[Ack]:
        out: list[Ack] = []
        for op in self._operators.operators:
            out.extend(op.drain_acks())
        return out

    def _emit_acks(self, changes: dict[str, MaskChange], acks: list[Ack]) -> None:
        """Ack an acceptance at the tick it takes effect, with its reroute count."""
        for op in self._operators.operators:
            change = changes.get(op.name)
            if change is None or not change.newly_effective:
                continue
            mask = op.mask
            affected = self._count_affected(op.name, mask)
            if affected:
                self._counters.bump(C.VEHICLES_REROUTED, op.name, affected)
            for advisory in change.newly_effective:
                self._counters.bump_run(C.ADVISORIES_ACCEPTED)
                acks.append(
                    Ack(
                        advisory_id=advisory.properties.id,
                        advisory_version=advisory.properties.version,
                        operator=op.name,
                        status=AckStatus.ACCEPTED,
                        vehicles_rerouted=affected,
                        acknowledged_at=self.sim_time,
                    )
                )
        acks.extend(self._collect_acks())

    def _count_affected(self, op_name: str, mask: Mask) -> int:
        """Vehicles whose current edge or destination a new mask disturbs.

        O(N) and no routing: there are no stored paths to invalidate, which is
        the whole reason an advisory landing is cheap here.
        """
        if mask.empty:
            return 0
        blocked = mask.no_entry
        banned = mask.keep_clear_nodes
        count = 0
        for veh in self._vehicles:
            if veh.op != op_name:
                continue
            if veh.edge in blocked:
                count += 1
                continue
            if banned and veh.dest_node in banned:
                count += 1
                continue
            if veh.zone_ix >= 0 and not self._router.reachable(
                mask.key, veh.zone_ix, self._net.head[veh.edge]
            ):
                count += 1
        return count

    def _advance(self, snapshot: HazardSnapshot) -> list[Intent]:
        """Phase 1: move every vehicle, collecting node-crossing intents."""
        net = self._net
        cfg = self._cfg
        length_mm = net.length_mm
        base_step = net.base_step_mm
        head = net.head
        intents: list[Intent] = []

        for veh in self._vehicles:
            if veh.status is VehicleStatus.OUT_OF_SERVICE:
                continue

            if veh.dwell_s > 0:
                veh.dwell_s -= 1
                if veh.dwell_s == 0 and veh.status is VehicleStatus.STOPPED:
                    veh.status = VehicleStatus.MOVING
                continue

            if veh.status in (VehicleStatus.STALLED, VehicleStatus.AWAITING_ASSIST):
                self._tend_stalled(veh, snapshot)
                continue

            op = self._operators.by_name[veh.op]
            mask = op.mask
            edge = veh.edge
            limit = length_mm[edge]

            if veh.offset_mm >= limit:
                # Waiting at the end of the edge for a node to admit it.
                veh.held_ticks += 1
                residual = 0
            else:
                step = base_step[edge]
                overrides = mask.speed_cap_mm_s
                if overrides:
                    capped = mask.speed_overrides().get(edge)
                    if capped is not None and capped < step:
                        step = capped
                veh.offset_mm += step
                if veh.offset_mm < limit:
                    veh.status = VehicleStatus.MOVING
                    veh.held_ticks = 0
                    continue
                residual = veh.offset_mm - limit
                veh.offset_mm = limit

            node = head[edge]
            if veh.hops > cfg.hop_budget:
                self._stall(veh, None, AssistReason.BLOCKED_ROUTE)
                continue
            next_edge = self._next_edge(veh, mask, node)
            intents.append(Intent(veh=veh.ix, node=node, residual_mm=residual, next_edge=next_edge))
        return intents

    def _tend_stalled(self, veh: Vehicle, snapshot: HazardSnapshot) -> None:
        """A stalled vehicle: count the wait, or recover once the road clears."""
        cfg = self._cfg
        still_blocked = veh.edge in snapshot.impassable or (
            veh.stall_node >= 0 and veh.stall_node in snapshot.dark
        )
        if still_blocked:
            veh.stalled_s += 1
            veh.recovering_s = 0
            return
        veh.recovering_s += 1
        if veh.recovering_s >= cfg.recovery_s:
            if veh.assist_open:
                veh.assist_open = False
            veh.status = VehicleStatus.MOVING
            veh.stalled_s = 0
            veh.recovering_s = 0
            veh.stall_node = -1
            veh.stall_reason = ""
            veh.stall_cause = None

    def _commit(
        self,
        intents: Sequence[Intent],
        snapshot: HazardSnapshot,
        now: datetime,
        assists: list[AssistRequest],
    ) -> None:
        """Phase 2: admit intents node by node, in a fixed order."""
        if not intents:
            return
        by_node: dict[int, list[Intent]] = {}
        for intent in intents:
            by_node.setdefault(intent.node, []).append(intent)

        net = self._net
        for node in sorted(by_node):
            group = by_node[node]
            # Longest wait first, then furthest past the boundary, then index.
            # Every key is an integer already in state, so the order is the
            # same on every run and on every platform.
            group.sort(key=lambda it: (-self._vehicles[it.veh].held_ticks, -it.residual_mm, it.veh))
            state = self._nodes[node]
            state.contenders = len(group)
            if state.admitted_tick != self._tick:
                state.admitted = 0
                state.admitted_tick = self._tick
            dark_hazard = snapshot.dark.get(node)

            for intent in group:
                veh = self._vehicles[intent.veh]
                op = self._operators.by_name[veh.op]
                mask = op.mask
                in_corridor = veh.edge in self._corridor.keep_clear_edges
                # KEEP_CLEAR outranks every other hold: making a vehicle queue
                # inside a corridor is precisely what the advisory forbids.
                corridor_exempt = in_corridor and veh.edge in mask.keep_clear_edges

                # An unlit signal only deadlocks when cars actually meet: one
                # vehicle crossing proceeds, two arriving together do not.
                if (
                    dark_hazard is not None
                    and node not in mask.all_way_stop
                    and len(group) >= DARK_CONTENTION
                ):
                    self._count_hazard_entry(veh, dark_hazard)
                    self._stall(veh, dark_hazard, AssistReason.DARK_SIGNAL, node=node)
                    continue

                if not corridor_exempt:
                    if node in mask.all_way_stop and self._tick < state.next_free_tick:
                        self._hold(veh)
                        continue
                    cap = mask.density_caps().get(node)
                    if cap is not None and state.admitted >= cap:
                        self._hold(veh)
                        continue

                if node in mask.all_way_stop:
                    state.next_free_tick = self._tick + max(
                        1, mask.stop_delays().get(node, self._cfg.all_way_stop_delay_s)
                    )

                state.admitted += 1

                if node == veh.dest_node:
                    self._complete_trip(veh, node, mask)
                    continue

                if intent.next_edge < 0:
                    self._stall(veh, None, AssistReason.BLOCKED_ROUTE)
                    continue

                hazard = snapshot.impassable.get(intent.next_edge)
                if hazard is not None and intent.next_edge not in mask.no_entry:
                    # The road is physically impassable and nothing told this
                    # operator to avoid it. This is the measurement.
                    veh.edge = intent.next_edge
                    veh.offset_mm = 0
                    veh.hops += 1
                    self._count_hazard_entry(veh, hazard)
                    self._stall(veh, hazard, _ASSIST_REASON.get(hazard.hazard_class))
                    continue

                veh.edge = intent.next_edge
                veh.offset_mm = min(intent.residual_mm, net.length_mm[intent.next_edge] - 1)
                veh.hops += 1
                if veh.zone_ix < 0 and veh.route_pos < len(veh.route):
                    veh.route_pos += 1
                veh.held_ticks = 0
                veh.status = VehicleStatus.MOVING

        del assists, now  # assists are emitted in `_account`, which sees timers

    def _hold(self, veh: Vehicle) -> None:
        veh.status = VehicleStatus.STOPPED
        veh.held_ticks += 1

    def _complete_trip(self, veh: Vehicle, node: int, mask: Mask) -> None:
        """Arrive, dwell unless the corridor forbids it, then pick a new zone."""
        self._counters.bump(C.RIDES_COMPLETED, veh.op)
        veh.trip_seq += 1
        veh.hops = 0
        veh.held_ticks = 0
        # No pick-up, drop-off or idling inside a corridor the operator
        # accepted: continue straight to the next trip instead of dwelling.
        # The edge matters as much as the node -- a vehicle parked at the end
        # of a corridor edge is sitting in the corridor even when the
        # intersection itself is outside it.
        in_corridor = node in mask.keep_clear_nodes or veh.edge in mask.keep_clear_edges
        if in_corridor:
            veh.dwell_s = 0
            veh.status = VehicleStatus.MOVING
        else:
            veh.dwell_s = self._cfg.dwell_s
            veh.status = VehicleStatus.STOPPED
        banned = tuple(sorted(mask.keep_clear_nodes)) if mask.keep_clear_nodes else ()
        veh.zone_ix, veh.dest_node = self._pick_destination(veh.ix, veh.trip_seq, exclude=banned)
        veh.route = ()
        veh.route_pos = 0
        veh.route_key = ""

    def _count_hazard_entry(self, veh: Vehicle, hazard: object) -> None:
        """Count one entry per vehicle per hazard, not one per tick."""
        hazard_id = str(getattr(hazard, "id", ""))
        if veh.hazard_seen == hazard_id:
            return
        veh.hazard_seen = hazard_id
        self._counters.bump(C.HAZARD_ENTRIES, veh.op)

    def _stall(
        self,
        veh: Vehicle,
        hazard: object,
        reason: AssistReason | None,
        *,
        node: int = -1,
    ) -> None:
        """Stop a vehicle, remembering why -- `_account` needs the cause.

        The cause is recorded rather than re-derived: a dark-signal stall
        happens at a node, and by the time the assist request is built the
        vehicle is sitting on an edge that is perfectly passable.
        """
        if veh.status is not VehicleStatus.STALLED:
            self._counters.bump(C.VEHICLES_STALLED, veh.op)
        veh.status = VehicleStatus.STALLED
        veh.stalled_s = max(veh.stalled_s, 1)
        veh.recovering_s = 0
        veh.held_ticks = 0
        veh.stall_node = node
        veh.stall_reason = (reason or AssistReason.OTHER).value
        veh.stall_cause = hazard
        veh.hazard_seen = veh.hazard_seen or str(getattr(hazard, "id", ""))

    def _account(
        self, snapshot: HazardSnapshot, now: datetime, assists: list[AssistRequest]
    ) -> None:
        """Counters, corridor time, and the assist queue."""
        cfg = self._cfg
        corridor_edges = self._corridor.keep_clear_edges
        open_by_op: dict[str, int] = dict.fromkeys(self._operators.names, 0)

        for veh in self._vehicles:
            if corridor_edges and veh.edge in corridor_edges and veh.held:
                self._counters.bump(C.BLOCKED_SECONDS, veh.op)
                if veh.held_ticks == 1 or veh.dwell_s == cfg.dwell_s:
                    self._counters.bump(C.CORRIDOR_ENTRIES, veh.op)

            if veh.status in (VehicleStatus.STALLED, VehicleStatus.AWAITING_ASSIST):
                if not veh.assist_open and veh.stalled_s >= cfg.stall_to_assist_s:
                    veh.assist_open = True
                    veh.status = VehicleStatus.AWAITING_ASSIST
                    assists.append(self._assist_for(veh, snapshot, now))
                    self._counters.bump(C.ASSIST_OPENED, veh.op)
                if veh.assist_open:
                    open_by_op[veh.op] += 1

        for op_name, depth in open_by_op.items():
            self._counters.set_gauge(C.ASSIST_OPEN, op_name, depth)

    def _assist_for(self, veh: Vehicle, snapshot: HazardSnapshot, now: datetime) -> AssistRequest:
        hazard = veh.stall_cause or snapshot.impassable.get(veh.edge)
        reason = AssistReason(veh.stall_reason) if veh.stall_reason else AssistReason.BLOCKED_ROUTE
        hazard_class = None
        hazard_id = None
        severity = 3
        if hazard is not None:
            hazard_class = hazard.hazard_class
            hazard_id = hazard.id
            severity = hazard.severity
        lon, lat, _heading = self._net.interpolate(veh.edge, veh.offset_mm)
        self._assist_seq += 1
        return AssistRequest(
            id=f"assist-{self._mode.value}-{self._tick:06d}-{self._assist_seq:04d}",
            vehicle_id=veh.vid,
            operator=veh.op,
            location=Point(type="Point", coordinates=[lon, lat]),
            reason=reason,
            hazard_id=hazard_id,
            hazard_class=hazard_class,
            priority=max(1, min(5, 6 - severity)),
            requested_at=now,
            ghost=self._mode is SimMode.GHOST,
        )

    def _telemetry(self, now: datetime) -> tuple[Telemetry, int]:
        """One frame: every vehicle, in the viewer's own field names."""
        net = self._net
        states: list[VehicleState] = []
        for veh in self._vehicles:
            lon, lat, heading = net.interpolate(veh.edge, veh.offset_mm)
            speed_mm_s = 0 if veh.held else net.base_step_mm[veh.edge]
            states.append(
                VehicleState(
                    id=veh.vid,
                    op=veh.op,
                    lon=lon,
                    lat=lat,
                    heading=heading,
                    speed_mph=min(120.0, speed_mm_s / MM_PER_M * MPH_PER_MPS),
                    status=veh.status,
                    trip_id=veh.trip_id,
                )
            )
        frame = Telemetry(
            tick=self._tick,
            sim_time=now,
            vehicles=states,
            ghost=self._mode is SimMode.GHOST,
            counters=self._counters.snapshot(),
        )
        return frame, len(states) + 1


def personas_for(cfg: SimConfig) -> tuple[str, ...]:
    """The persona names a config will run. Handy for tests and reports."""
    return PERSONAS[: cfg.fleets]
