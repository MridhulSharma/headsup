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
    CORRIDOR_LINE,
    advisory_for,
    corridor_edges,
    grid_net,
    no_hazards,
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
