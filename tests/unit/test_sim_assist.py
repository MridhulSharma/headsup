"""Stalls and assist requests: the queue the demo measures."""

from __future__ import annotations

from fixtures.sim import (
    CORRIDOR_LINE,
    advisory_for,
    grid_net,
    no_hazards,
    scripted_hazards,
    tiny_config,
)
from headsup.contracts import ActionType, AssistReason, HazardClass, VehicleStatus
from headsup.sim.config import SimMode
from headsup.sim.engine import Simulation
from headsup.sim.ground_truth import HazardRegion, ScriptedHazardField
from samples import T0

CORRIDOR = CORRIDOR_LINE.__geo_interface__


def _run(*, hazards=None, advisory=None, ticks: int = 300, mode=SimMode.LIVE, cfg=None):
    cfg = cfg or tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=mode, hazards=hazards or no_hazards(), start=T0)
    if advisory is not None:
        sim.offer(advisory)
    assists = []
    for _ in range(ticks):
        assists.extend(sim.step().assists)
    return sim, assists


def test_a_flooded_edge_gives_a_flooded_edge_request() -> None:
    net = grid_net()
    _sim, assists = _run(hazards=scripted_hazards(net))
    assert assists
    assert any(request.reason is AssistReason.FLOODED_EDGE for request in assists)


def test_a_dark_signal_gives_a_dark_signal_request() -> None:
    net = grid_net()
    dark = scripted_hazards(net, geometry=CORRIDOR, hazard_class=HazardClass.SIGNAL_OUTAGE)
    _sim, assists = _run(hazards=dark, cfg=tiny_config(vehicles=90, dwell_s=0, stall_to_assist_s=3))
    assert any(request.reason is AssistReason.DARK_SIGNAL for request in assists)


def test_one_request_per_stall_not_one_per_tick() -> None:
    """A stalled vehicle asks for help once. Otherwise the queue is noise."""
    net = grid_net()
    sim, assists = _run(hazards=scripted_hazards(net), ticks=400)
    by_vehicle: dict[str, int] = {}
    for request in assists:
        by_vehicle[request.vehicle_id] = by_vehicle.get(request.vehicle_id, 0) + 1
    assert by_vehicle
    assert max(by_vehicle.values()) == 1
    assert sim.counters()["assist_opened_total"] == len(assists)


def test_the_request_carries_the_hazard_it_was_caused_by() -> None:
    net = grid_net()
    _sim, assists = _run(hazards=scripted_hazards(net, hazard_id="gt-flood-1"))
    flooded = [r for r in assists if r.reason is AssistReason.FLOODED_EDGE]
    assert flooded
    assert flooded[0].hazard_id == "gt-flood-1"
    assert flooded[0].hazard_class is HazardClass.FLOOD


def test_the_request_validates_against_the_contract() -> None:
    """Priority 1-5 and a real GeoJSON Point, or the model would have raised."""
    net = grid_net()
    _sim, assists = _run(hazards=scripted_hazards(net, severity=4))
    assert assists
    for request in assists:
        assert 1 <= request.priority <= 5
        assert request.location.type == "Point"
        lon, lat = request.location.coordinates
        assert -180.0 <= lon <= 180.0
        assert -90.0 <= lat <= 90.0


def test_a_ghost_request_is_flagged_as_one() -> None:
    net = grid_net()
    _sim, live = _run(hazards=scripted_hazards(net))
    _ghost_sim, ghost = _run(hazards=scripted_hazards(net), mode=SimMode.GHOST)
    assert live and ghost
    assert all(not request.ghost for request in live)
    assert all(request.ghost for request in ghost)


def test_an_accepted_no_entry_prevents_the_stall_entirely() -> None:
    net = grid_net()
    _sim, with_advice = _run(
        hazards=scripted_hazards(net), advisory=advisory_for(ActionType.NO_ENTRY)
    )
    _bare_sim, without = _run(hazards=scripted_hazards(net))
    op_a_advised = [r for r in with_advice if r.operator == "op_a"]
    op_a_bare = [r for r in without if r.operator == "op_a"]
    assert not op_a_advised
    assert op_a_bare


def test_a_vehicle_recovers_once_the_hazard_expires() -> None:
    """A flood that drains lets the fleet resume, and clears the queue gauge."""
    from datetime import timedelta

    net = grid_net()
    cfg = tiny_config(stall_to_assist_s=3, recovery_s=5)
    window = ScriptedHazardField.from_regions(
        [
            HazardRegion(
                id="gt-brief",
                hazard_class=HazardClass.FLOOD,
                geometry=CORRIDOR,
                valid_from=T0,
                valid_until=T0 + timedelta(seconds=60),
                severity=4,
            )
        ],
        net,
    )
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=window, start=T0)
    for _ in range(90):
        sim.step()
    peak = sim.counters()["assist_open_total"]
    for _ in range(120):
        sim.step()
    assert sim.counters()["assist_open_total"] <= peak
    assert all(
        veh.status is not VehicleStatus.STALLED
        for veh in sim.vehicles
        if veh.edge not in window.snapshot(sim.sim_time).impassable
    )


def test_assist_open_is_a_gauge_and_assist_opened_is_cumulative() -> None:
    net = grid_net()
    sim, assists = _run(hazards=scripted_hazards(net), ticks=300)
    counters = sim.counters()
    assert counters["assist_opened_total"] == len(assists)
    assert counters["assist_open_total"] <= counters["assist_opened_total"]
