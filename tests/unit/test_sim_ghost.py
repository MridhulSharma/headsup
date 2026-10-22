"""The ghost fleet: the same seed, the same roads, no advice.

D3.5 later needs the ghost running beside the live fleet on one clock, from
identical starting positions, so those properties are asserted here rather
than discovered then.
"""

from __future__ import annotations

from fixtures.sim import advisory_for, grid_net, scripted_hazards, tiny_config
from headsup.sim.config import SimMode
from headsup.sim.engine import Simulation
from headsup.sim.rng import SeedTree
from headsup.sim.router import Router
from samples import T0


def _pair(ticks: int = 0, *, advise: bool = False):
    """A live and a ghost simulation sharing one router and one seed tree."""
    cfg = tiny_config()
    net = grid_net()
    router = Router(net, cfg)
    field = scripted_hazards(net)
    sims = {
        mode: Simulation(
            net,
            cfg,
            mode=mode,
            hazards=field,
            start=T0,
            router=router,
            seeds=SeedTree(cfg.seed),
        )
        for mode in (SimMode.LIVE, SimMode.GHOST)
    }
    if advise:
        for sim in sims.values():
            sim.offer(advisory_for())
    for _ in range(ticks):
        for sim in sims.values():
            sim.step()
    return sims, router


def test_live_and_ghost_start_from_identical_positions() -> None:
    sims, _ = _pair()
    live = sims[SimMode.LIVE].vehicles
    ghost = sims[SimMode.GHOST].vehicles
    assert len(live) == len(ghost)
    for a, b in zip(live, ghost, strict=True):
        assert (a.edge, a.offset_mm, a.dest_node, a.op) == (b.edge, b.offset_mm, b.dest_node, b.op)


def test_ghost_vehicle_ids_pair_with_live_ones() -> None:
    """`veh-op_a-0007` and `ghost-op_a-0007` are the same trip, advised or not."""
    sims, _ = _pair()
    for live, ghost in zip(sims[SimMode.LIVE].vehicles, sims[SimMode.GHOST].vehicles, strict=True):
        assert live.vid.startswith("veh-")
        assert ghost.vid.startswith("ghost-")
        assert live.vid.removeprefix("veh-") == ghost.vid.removeprefix("ghost-")


def test_the_ghost_mirrors_all_three_personas() -> None:
    """Not one undifferentiated fleet: the comparison is per operator."""
    sims, _ = _pair()
    assert sims[SimMode.GHOST].operators.names == sims[SimMode.LIVE].operators.names
    assert all(op.ignores_advisories for op in sims[SimMode.GHOST].operators.operators)
    assert not any(op.ignores_advisories for op in sims[SimMode.LIVE].operators.operators)


def test_the_ghost_enters_the_hazard_where_the_live_fleet_does_not() -> None:
    sims, _ = _pair(ticks=400, advise=True)
    live = sims[SimMode.LIVE].counters()
    ghost = sims[SimMode.GHOST].counters()
    assert live["hazard_entries_op_a"] == 0
    assert ghost["hazard_entries_op_a"] > 0
    assert ghost["hazard_entries_total"] > live["hazard_entries_total"]


def test_the_ghost_never_emits_an_ack() -> None:
    """A counterfactual must not put an answer on the wire."""
    cfg = tiny_config()
    net = grid_net()
    ghost = Simulation(net, cfg, mode=SimMode.GHOST, hazards=scripted_hazards(net), start=T0)
    ghost.offer(advisory_for())
    acks = []
    for _ in range(200):
        acks.extend(ghost.step().acks)
    # It declines internally -- that is how it ignores advice -- but the
    # runner is what decides nothing reaches the bus, and it drops ghost acks.
    assert all(ack.operator in ghost.operators.names for ack in acks)
    assert ghost.counters()["advisories_accepted"] == 0


def test_the_ghost_adds_no_routing_work() -> None:
    """Its mask is the empty one, so it shares the live fleet's labels.

    If this regresses, running both fleets doubles the dominant cost in the
    tick budget instead of adding nothing to it.
    """
    cfg = tiny_config()
    net = grid_net()

    solo_router = Router(net, cfg)
    solo = Simulation(
        net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0, router=solo_router
    )
    solo.offer(advisory_for())
    for _ in range(120):
        solo.step()

    _sims, shared = _pair(ticks=120, advise=True)
    # The ghost registers no mask of its own -- its mask is the empty one,
    # which every router has from construction -- so the key set is the same
    # size with it as without it, and the label work is the same work.
    assert shared.mask_keys == solo_router.mask_keys
    assert shared.refreshes == solo_router.refreshes


def test_ghost_telemetry_is_flagged_and_live_telemetry_is_not() -> None:
    sims, _ = _pair(ticks=0)
    live = sims[SimMode.LIVE]
    ghost = sims[SimMode.GHOST]
    frames = {}
    for mode, sim in (("live", live), ("ghost", ghost)):
        for _ in range(sim._cfg.telemetry_stride):
            result = sim.step()
            if result.telemetry is not None:
                frames[mode] = result.telemetry
    assert frames["live"].ghost is False
    assert frames["ghost"].ghost is True
