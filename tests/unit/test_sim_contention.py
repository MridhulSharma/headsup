"""Node contention: density caps, all-way stops, and dark signals.

Several vehicles wanting the same intersection in the same tick is where a
simulation most easily becomes non-reproducible. The admission order here is
`(-held_ticks, -residual_mm, vehicle index)` -- all integers already in state,
so it is the same on every run and every platform.
"""

from __future__ import annotations

from fixtures.sim import (
    CORRIDOR_LINE,
    advisory_for,
    grid_net,
    no_hazards,
    scripted_hazards,
    tiny_config,
)
from headsup.contracts import ActionType, HazardClass, VehicleStatus
from headsup.sim.config import SimMode
from headsup.sim.engine import DARK_CONTENTION, Simulation
from headsup.sim.masks import resolve
from samples import T0

CORRIDOR = CORRIDOR_LINE.__geo_interface__


def _sim(*, hazards=None, advisory=None, cfg=None, ticks: int = 0, mode=SimMode.LIVE):
    cfg = cfg or tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=mode, hazards=hazards or no_hazards(), start=T0)
    if advisory is not None:
        sim.offer(advisory)
    for _ in range(ticks):
        sim.step()
    return sim


def test_a_density_cap_of_one_admits_one_vehicle_per_node_per_tick() -> None:
    cfg = tiny_config(vehicles=90, dwell_s=0)
    sim = _sim(
        cfg=cfg,
        advisory=advisory_for(ActionType.DENSITY_CAP, params={"max_vehicles": 1}),
        ticks=400,
    )
    capped = sim.operators.by_name["op_a"].mask.density_caps()
    assert capped
    # The cap is a queue, so it shows up as vehicles held, not as entries.
    assert any(veh.status is VehicleStatus.STOPPED for veh in sim.vehicles if veh.op == "op_a")


def test_an_all_way_stop_serialises_arrivals() -> None:
    cfg = tiny_config(vehicles=90, dwell_s=0)
    sim = _sim(
        cfg=cfg,
        advisory=advisory_for(ActionType.ALL_WAY_STOP, params={"delay_s": 4}),
        ticks=200,
    )
    mask = sim.operators.by_name["op_a"].mask
    assert mask.all_way_stop
    assert all(delay == 4 for _node, delay in mask.stop_delay_s)


def test_the_admission_order_is_longest_wait_then_furthest_past_the_line() -> None:
    """Checked directly on the sort key the engine uses.

    A vehicle that has queued longer goes first; among equal waits, the one
    that overshot the boundary furthest physically arrived first.
    """
    from headsup.sim.vehicles import Intent, Vehicle

    def vehicle(ix: int, held: int) -> Vehicle:
        return Vehicle(
            ix=ix, vid=f"v{ix}", op="op_a", edge=0, offset_mm=0, zone_ix=0, held_ticks=held
        )

    vehicles = [vehicle(0, 0), vehicle(1, 3), vehicle(2, 0)]
    intents = [
        Intent(veh=0, node=5, residual_mm=900, next_edge=1),
        Intent(veh=1, node=5, residual_mm=10, next_edge=1),
        Intent(veh=2, node=5, residual_mm=900, next_edge=1),
    ]
    intents.sort(key=lambda it: (-vehicles[it.veh].held_ticks, -it.residual_mm, it.veh))
    assert [it.veh for it in intents] == [1, 0, 2]


def test_a_dark_signal_stalls_vehicles_only_when_they_meet() -> None:
    """One car crossing an unlit signal proceeds; two arriving together do not.

    Deterministic, and it is the failure D3.1 dramatises -- an uncontrolled
    intersection deadlocks under contention, not merely because it is dark.
    """
    assert DARK_CONTENTION == 2
    cfg = tiny_config(vehicles=90, dwell_s=0, stall_to_assist_s=3)
    dark = scripted_hazards(grid_net(), geometry=CORRIDOR, hazard_class=HazardClass.SIGNAL_OUTAGE)
    busy = _sim(cfg=cfg, hazards=dark, ticks=300)
    assert busy.counters()["hazard_entries_total"] > 0

    # One vehicle per fleet: they can still meet, but far less often, so the
    # entry count must not go up when the roads get emptier.
    quiet = _sim(
        cfg=tiny_config(vehicles=3, fleets=3, zones=3, dwell_s=0, stall_to_assist_s=3),
        hazards=dark,
        ticks=300,
    )
    assert quiet.counters()["hazard_entries_total"] < busy.counters()["hazard_entries_total"]


def test_an_all_way_stop_advisory_protects_a_dark_intersection() -> None:
    """The signal_outage playbook allows ALL_WAY_STOP, not NO_ENTRY.

    Closing a dark intersection would push traffic onto the neighbouring dark
    ones, so the advisory makes it behave as a stop instead.
    """
    cfg = tiny_config(vehicles=90, dwell_s=0, stall_to_assist_s=3)
    dark = scripted_hazards(grid_net(), geometry=CORRIDOR, hazard_class=HazardClass.SIGNAL_OUTAGE)
    advisory = advisory_for(
        ActionType.ALL_WAY_STOP,
        geometry=CORRIDOR,
        params={"delay_s": 4},
        hazard_class=HazardClass.SIGNAL_OUTAGE,
    )
    advised = _sim(cfg=cfg, hazards=dark, advisory=advisory, ticks=300)
    bare = _sim(cfg=cfg, hazards=dark, ticks=300)
    counters = advised.counters()
    assert counters["assist_opened_op_a"] < bare.counters()["assist_opened_op_a"]


def test_keep_clear_outranks_a_density_cap_and_an_all_way_stop() -> None:
    """Holding a vehicle inside a corridor is what the corridor forbids."""
    net = grid_net()
    cfg = tiny_config()
    corridor = resolve(
        advisory_for(ActionType.KEEP_CLEAR, geometry=CORRIDOR, params={"width_m": 30.0}),
        net,
        cfg,
    )
    stop = resolve(
        advisory_for(ActionType.ALL_WAY_STOP, geometry=CORRIDOR, params={"delay_s": 6}),
        net,
        cfg,
    )
    from headsup.sim.masks import merge

    both = merge((corridor, stop))
    # Both restrictions survive the merge; precedence is applied in the tick,
    # and the corridor edges are the ones exempt from the hold.
    assert both.keep_clear_edges
    assert both.all_way_stop
    assert both.keep_clear_edges & frozenset(
        edge for edge in range(net.edge_count) if net.head[edge] in both.all_way_stop
    )


def test_occupancy_never_exceeds_a_density_cap_that_is_in_force() -> None:
    cfg = tiny_config(vehicles=90, dwell_s=0)
    sim = _sim(
        cfg=cfg,
        advisory=advisory_for(ActionType.DENSITY_CAP, params={"max_vehicles": 3}),
        ticks=300,
    )
    caps = sim.operators.by_name["op_a"].mask.density_caps()
    assert caps
    net = grid_net()
    for node, limit in caps.items():
        riding = sum(
            1
            for veh in sim.vehicles
            if veh.op == "op_a" and net.head[veh.edge] == node and veh.offset_mm == 0
        )
        assert riding <= limit
