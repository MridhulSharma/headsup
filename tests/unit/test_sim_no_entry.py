"""A NO_ENTRY advisory must keep an accepting fleet out, and the ghost in.

These two assertions are the demo. If the first fails, Heads Up does not
work; if the second fails, the comparison proves nothing because the baseline
was never exposed to the hazard.
"""

from __future__ import annotations

from fixtures.sim import (
    advisory_for,
    barrier_edges,
    grid_net,
    scripted_hazards,
    tiny_config,
)
from headsup.contracts import ActionType, VehicleStatus
from headsup.sim.config import SimMode
from headsup.sim.engine import Simulation
from samples import T0

TICKS = 400


def _run(mode: SimMode, *, advise: bool, confidence: float = 0.9, ticks: int = TICKS):
    cfg = tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=mode, hazards=scripted_hazards(net), start=T0)
    if advise:
        sim.offer(advisory_for(ActionType.NO_ENTRY, confidence=confidence))
    for _ in range(ticks):
        sim.step()
    return sim


def test_the_barrier_actually_covers_part_of_the_graph() -> None:
    """Guard the fixture: a hazard over nothing would pass every test below."""
    edges = barrier_edges()
    assert 0 < len(edges) < grid_net().edge_count


def test_a_no_entry_advisory_yields_zero_hazard_entries_for_op_a() -> None:
    counters = _run(SimMode.LIVE, advise=True).counters()
    assert counters["hazard_entries_op_a"] == 0


def test_ghost_mode_yields_hazard_entries_on_the_same_seed() -> None:
    counters = _run(SimMode.GHOST, advise=True).counters()
    assert counters["hazard_entries_total"] > 0


def test_the_live_fleet_beats_the_ghost_fleet_on_the_same_hazard() -> None:
    live = _run(SimMode.LIVE, advise=True).counters()
    ghost = _run(SimMode.GHOST, advise=True).counters()
    assert live["hazard_entries_total"] < ghost["hazard_entries_total"]
    assert live["assist_opened_total"] < ghost["assist_opened_total"]


def test_op_b_enters_during_its_delay_and_stops_afterwards() -> None:
    """op_b accepts after 30-90 s, so a few early entries are expected.

    What must not happen is entries continuing once it has accepted, which
    would mean the mask was not applied.
    """
    cfg = tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0)
    sim.offer(advisory_for(ActionType.NO_ENTRY))

    for _ in range(120):  # past the 90 s worst case
        sim.step()
    settled = sim.counters()["hazard_entries_op_b"]
    for _ in range(400):
        sim.step()
    assert sim.counters()["hazard_entries_op_b"] == settled


def test_op_c_declines_low_confidence_and_then_enters_the_hazard() -> None:
    """Below 0.7 op_c declines, so it is unprotected and behaves like a ghost."""
    sim = _run(SimMode.LIVE, advise=True, confidence=0.6)
    counters = sim.counters()
    assert counters["advisories_declined"] >= 1
    assert counters["hazard_entries_op_c"] > 0
    assert counters["hazard_entries_op_a"] == 0


def test_no_vehicle_of_an_accepting_operator_ever_enters_a_closed_edge() -> None:
    """The strong form of the guarantee, checked on every edge transition.

    Phrased as *entering* rather than *occupying*: a vehicle already part way
    along an edge when the advisory lands finishes that edge, by design --
    teleporting it off mid-block would be a worse lie than the detour.
    """
    cfg = tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0)
    sim.offer(advisory_for(ActionType.NO_ENTRY))
    for _ in range(120):  # let every operator accept
        sim.step()

    closed = sim.operators.by_name["op_a"].mask.no_entry
    assert closed, "op_a should be driving under a non-empty mask by now"

    where = {veh.ix: veh.edge for veh in sim.vehicles}
    for _ in range(300):
        sim.step()
        for veh in sim.vehicles:
            moved = veh.edge != where[veh.ix]
            where[veh.ix] = veh.edge
            if veh.op == "op_a" and moved:
                assert veh.edge not in closed


def test_vehicles_never_start_a_run_inside_a_hazard() -> None:
    """A fleet begins its shift on passable roads.

    A vehicle spawned on a flooded edge would be stuck for the whole run and
    would never be counted as having entered the hazard, which would quietly
    understate the baseline.
    """
    net = grid_net()
    sim = Simulation(
        net, tiny_config(), mode=SimMode.GHOST, hazards=scripted_hazards(net), start=T0
    )
    flooded = barrier_edges()
    assert all(veh.edge not in flooded for veh in sim.vehicles)


def test_a_fleet_with_no_advice_stalls_and_asks_for_help() -> None:
    """The failure mode the advisory prevents, confirmed to exist."""
    sim = _run(SimMode.LIVE, advise=False)
    counters = sim.counters()
    assert counters["hazard_entries_total"] > 0
    assert counters["assist_opened_total"] > 0
    assert any(
        veh.status in (VehicleStatus.STALLED, VehicleStatus.AWAITING_ASSIST) for veh in sim.vehicles
    )
