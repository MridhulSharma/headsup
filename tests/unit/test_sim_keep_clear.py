"""KEEP_CLEAR: a corridor vehicles may travel through but not stop in.

The corridor here is deliberately placed away from the barrier used by the
other tests, so blocked-corridor time measures *stopping in the corridor* and
not vehicles stalled on a flood that happens to be underneath it.

The counter is integer vehicle-seconds because `Telemetry.counters` is
`dict[str, int]`; minutes are derived by floor division, so the published
minute figure never loses the seconds beneath it.
"""

from __future__ import annotations

from shapely.geometry import LineString

from fixtures.sim import (
    BARRIER,
    CORRIDOR_LINE,
    advisory_for,
    corridor_edges,
    grid_net,
    no_hazards,
    scripted_hazards,
    tiny_config,
)
from headsup.contracts import ActionType, Telemetry
from headsup.sim import counters as C
from headsup.sim.config import SimMode
from headsup.sim.engine import Simulation
from headsup.sim.masks import corridor_polygon, resolve
from samples import T0

CORRIDOR = CORRIDOR_LINE.__geo_interface__
TICKS = 400


def _run(mode: SimMode, *, advise: bool, confidence: float = 0.9, ticks: int = TICKS):
    cfg = tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=mode, hazards=no_hazards(), start=T0)
    if advise:
        sim.offer(
            advisory_for(
                ActionType.KEEP_CLEAR,
                geometry=CORRIDOR,
                confidence=confidence,
                params={"width_m": 30.0},
            )
        )
    for _ in range(ticks):
        sim.step()
    return sim


def test_the_corridor_covers_part_of_the_graph() -> None:
    """Guard the fixture, as for the barrier."""
    assert 0 < len(corridor_edges()) < grid_net().edge_count


def test_a_line_corridor_and_an_equivalent_polygon_resolve_alike() -> None:
    """A centre line plus a width is the same restriction as the area itself."""
    net = grid_net()
    cfg = tiny_config()
    area = advisory_for(ActionType.KEEP_CLEAR, geometry=CORRIDOR, params={"width_m": 30.0})
    polygon_mask = resolve(area, net, cfg)
    assert polygon_mask.keep_clear_edges == frozenset(
        net.world.graph.edge_ids_in_polygon(CORRIDOR_LINE)
    )


def test_a_line_string_corridor_is_widened_in_metres_not_degrees() -> None:
    """A single degree-space radius cannot be right in both axes at once.

    At the fixture grid's latitude a degree of longitude is about 0.86 of a
    degree of latitude, so a naive buffer would be ~16% too wide east-west.
    """
    line = LineString([(-97.74, 30.266), (-97.74, 30.270)])
    widened = corridor_polygon(line, 100.0)
    minx, miny, maxx, maxy = widened.bounds
    import math

    lat0 = (miny + maxy) / 2.0
    width_m = (maxx - minx) * 111_320.0 * math.cos(math.radians(lat0))
    assert 95.0 < width_m < 105.0


def test_an_accepting_operator_accrues_no_blocked_corridor_time() -> None:
    sim = _run(SimMode.LIVE, advise=True)
    counters = sim.counters()
    assert counters[C.key(C.BLOCKED_SECONDS, "op_a")] == 0


def test_a_declining_operator_and_the_ghost_do_accrue_blocked_time() -> None:
    """op_c declines below 0.7, so it keeps idling in the corridor."""
    declining = _run(SimMode.LIVE, advise=True, confidence=0.6).counters()
    ghost = _run(SimMode.GHOST, advise=True).counters()
    assert declining[C.key(C.BLOCKED_SECONDS, "op_c")] > 0
    assert ghost[C.key(C.BLOCKED_SECONDS)] > 0
    assert declining[C.key(C.BLOCKED_SECONDS, "op_a")] == 0


def test_the_corridor_is_measured_even_for_an_operator_that_declined() -> None:
    """Measurement is separate from behaviour.

    op_c's own mask is empty, so if the counter were driven off the operator's
    mask there would be nothing to compare the advised fleet against.
    """
    sim = _run(SimMode.LIVE, advise=True, confidence=0.6)
    assert sim.operators.by_name["op_c"].mask.empty
    assert sim.counters()[C.key(C.BLOCKED_SECONDS, "op_c")] > 0


def test_blocked_minutes_are_floor_divided_seconds() -> None:
    counters = _run(SimMode.GHOST, advise=True).counters()
    for op in ("op_a", "op_b", "op_c"):
        seconds = counters[C.key(C.BLOCKED_SECONDS, op)]
        assert counters[C.key(C.BLOCKED_MINUTES, op)] == seconds // 60


def test_every_counter_is_an_integer_and_the_telemetry_validates() -> None:
    """`Telemetry.counters` is `dict[str, int]` and would reject a float."""
    sim = _run(SimMode.GHOST, advise=True, ticks=60)
    counters = sim.counters()
    assert all(isinstance(value, int) for value in counters.values())
    frame = Telemetry(tick=1, sim_time=T0, vehicles=[], ghost=True, counters=counters)
    assert frame.counters == counters


def test_the_expected_counter_keys_are_all_present() -> None:
    sim = _run(SimMode.LIVE, advise=True, ticks=30)
    counters = sim.counters()
    for key in C.expected_keys(("op_a", "op_b", "op_c")):
        assert key in counters, key


def test_a_vehicle_is_never_trapped_inside_the_corridor() -> None:
    """Through travel stays legal: KEEP_CLEAR touches no edge weight."""
    net = grid_net()
    cfg = tiny_config()
    advisory = advisory_for(ActionType.KEEP_CLEAR, geometry=CORRIDOR, params={"width_m": 30.0})
    mask = resolve(advisory, net, cfg)
    assert mask.keep_clear_edges
    assert not mask.no_entry

    from headsup.sim.masks import weights_for

    weights = weights_for(mask, net)
    assert all(weight != float("inf") for weight in weights)


def test_an_accepting_operator_does_not_dwell_at_a_corridor_node() -> None:
    sim = _run(SimMode.LIVE, advise=True, ticks=200)
    banned = sim.operators.by_name["op_a"].mask.keep_clear_nodes
    assert banned, "the corridor should cover at least one intersection"
    for veh in sim.vehicles:
        if veh.op == "op_a" and veh.dest_node in banned:
            raise AssertionError(f"{veh.vid} is bound for a corridor node")


def test_a_corridor_moves_routing_decisions_off_the_street() -> None:
    """The point of the detour penalty, measured where routing is decided.

    End-to-end corridor occupancy is a weak instrument on a small grid: some
    trips begin inside the corridor and some end there, and those vehicles
    have to be on it whatever the weights say. What the penalty controls is
    the CHOICE -- which edge a vehicle takes out of a node when it has an
    alternative -- so that is what this asserts.
    """
    from dataclasses import replace as _replace

    from headsup.sim.router import Router

    advisory = advisory_for(
        ActionType.KEEP_CLEAR, geometry=CORRIDOR, confidence=0.9, params={"width_m": 30.0}
    )

    def hops_into_corridor(detour_s: float) -> tuple[int, int]:
        cfg = _replace(tiny_config(), keep_clear_detour_s=detour_s)
        net = grid_net()
        router = Router(net, cfg)
        mask = resolve(advisory, net, cfg)
        router.ensure_mask(mask)
        inside = 0
        total = 0
        for zone in range(len(net.zones)):
            for node in range(len(net.out_edges)):
                edge = router.next_hop(mask.key, zone, node)
                total += 1
                if edge in mask.keep_clear_edges:
                    inside += 1
        return inside, total

    free, _ = hops_into_corridor(0.0)
    costly, _ = hops_into_corridor(90.0)
    assert free > 0, "the fixture corridor must be on some shortest path"
    assert costly < free, "a priced corridor must pull routing off it"


def test_an_advisory_names_itself_on_the_vehicles_it_deflects() -> None:
    """`rerouted_by` is what the viewer draws a deflection trail from.

    Without it the viewer can show that an advisory exists but not that any
    vehicle acted on it, which leaves the rerouted readout at 0 in every
    scenario and the trail unable to draw at all.
    """
    cfg = tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=no_hazards(), start=T0)
    advisory = advisory_for(
        ActionType.KEEP_CLEAR, geometry=CORRIDOR, confidence=0.9, params={"width_m": 30.0}
    )
    assert all(not veh.rerouted_by for veh in sim._vehicles), "nothing is deflected yet"
    sim.offer(advisory)
    # Collected across the run rather than sampled at the end: an operator
    # with a delayed persona accepts late, and a tag clears when the vehicle
    # it is on starts a new trip, so one snapshot can easily miss both.
    advisory_id = advisory.properties.id
    seen: set[str] = set()
    on_the_wire: set[str] = set()
    for _ in range(TICKS):
        sim.step()
        for veh in sim._vehicles:
            if veh.rerouted_by:
                seen.add(veh.rerouted_by)
        frame, _ = sim._telemetry(sim.sim_time)
        on_the_wire.update(row.rerouted_by for row in frame.vehicles if row.rerouted_by)
    assert seen == {advisory_id}, (
        "a deflection must name the advisory that caused it, and name no other"
    )
    assert on_the_wire == {advisory_id}, (
        "the deflection has to reach the wire, not just the engine"
    )


def test_a_corridor_keeps_vehicles_out_of_the_scene_underneath_it() -> None:
    """A KEEP_CLEAR advisory has to protect, not merely annotate.

    The movement gate used to honour only `no_entry` when deciding whether an
    operator had been told to stay off a physically impassable edge. A
    KEEP_CLEAR corridor populates `keep_clear_edges` and never `no_entry`, so
    it gave no protection at all: vehicles drove onto the scene, counted a
    hazard entry and stalled in it. Measured on this fixture the advisory
    changed hazard entries by exactly nothing, which is the definition of a
    metric that cannot move.

    Routing cost alone cannot fix that. A vehicle whose only remaining path
    is through the corridor still takes it, and then drives into the scene.
    The gate is where the guarantee has to live.
    """
    from headsup.contracts import HazardClass

    scene = BARRIER.__geo_interface__

    def entries(advise: bool) -> tuple[int, int]:
        cfg = tiny_config()
        net = grid_net()
        sim = Simulation(
            net,
            cfg,
            mode=SimMode.LIVE,
            hazards=scripted_hazards(
                net, geometry=scene, hazard_class=HazardClass.EMERGENCY_SCENE
            ),
            start=T0,
        )
        if advise:
            sim.offer(
                advisory_for(
                    ActionType.KEEP_CLEAR,
                    geometry=scene,
                    confidence=0.9,
                    params={"width_m": 30.0},
                )
            )
        for _ in range(TICKS):
            sim.step()
        snap = sim._counters.snapshot()
        return (
            sum(v for k, v in snap.items() if k.startswith(C.HAZARD_ENTRIES)),
            sum(v for k, v in snap.items() if k.startswith(C.RIDES_COMPLETED)),
        )

    unadvised, unadvised_rides = entries(advise=False)
    advised, advised_rides = entries(advise=True)
    assert unadvised > 0, "the fixture must send vehicles into the scene unadvised"
    assert advised < unadvised, "a corridor over a scene must keep vehicles out of it"
    # Not zero, and that is honest: a vehicle already inside the corridor, or
    # with no other way out of its node, still has to move. The advisory
    # protects everyone it can reach rather than pretending to reach everyone.
    assert advised_rides >= unadvised_rides * 0.9, (
        "protection must not be bought by stopping the fleet from working"
    )
