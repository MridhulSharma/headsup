"""The invariants that make the simulator fast, asserted on the fixture graph.

A wall-clock throughput test can only run where a city is built and is only
meaningful on a quiet machine, so it lives in `tests/perf` and is skipped
almost everywhere. These assertions are the part that can run anywhere,
including CI, and they check the *reasons* the thing is fast rather than the
symptom -- which is also what will actually regress.

The three costs that would each blow the budget on their own:

* shapely in the tick loop (a per-vehicle point-in-polygon is ~45 ms a tick,
  4.5x over budget by itself),
* unbounded label refreshes (per-vehicle destinations are ~432 ms a mask),
* `RoadGraph.nearest_node`, which is O(V) with a geodesic call per vertex.
"""

from __future__ import annotations

from fixtures.sim import advisory_for, grid_net, scripted_hazards, tiny_config
from headsup.contracts import ActionType
from headsup.sim.config import SimMode
from headsup.sim.engine import Simulation
from samples import T0

TICKS = 200


def _run(cfg=None, *, advisories=()):
    cfg = cfg or tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0)
    for advisory in advisories:
        sim.offer(advisory)
    return sim, [sim.step() for _ in range(TICKS)]


def test_shapely_runs_once_per_advisory_version_and_never_again() -> None:
    """Geometry is resolved at the edge of the system, not in the loop."""
    advisories = (
        advisory_for(ActionType.NO_ENTRY, advisory_id="adv-1"),
        advisory_for(ActionType.KEEP_CLEAR, advisory_id="adv-2", params={"width_m": 30.0}),
    )
    _sim, results = _run(advisories=advisories)
    assert sum(result.shapely_calls for result in results) == len(advisories)
    # And the resolution all happens on the tick the advisories are drained.
    assert results[0].shapely_calls == len(advisories)
    assert all(result.shapely_calls == 0 for result in results[1:])


def test_re_offering_the_same_advisory_version_resolves_no_geometry() -> None:
    cfg = tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0)
    advisory = advisory_for()
    sim.offer(advisory)
    first = sim.step()
    assert first.shapely_calls == 1
    for _ in range(5):
        sim.offer(advisory)
        assert sim.step().shapely_calls == 0


def test_label_refreshes_never_exceed_the_per_tick_budget() -> None:
    cfg = tiny_config(label_budget=2)
    _sim, results = _run(cfg, advisories=(advisory_for(),))
    assert any(result.label_refreshes > 0 for result in results)
    for result in results:
        assert result.label_refreshes <= cfg.label_budget


def test_the_label_cache_settles_instead_of_thrashing() -> None:
    """Once the masks stop changing, routing work must stop too.

    This is the regression that matters most: a shared router evicting another
    owner's mask every tick would recompute its labels every tick, turning the
    cheapest part of the design into the most expensive.
    """
    _sim, results = _run(advisories=(advisory_for(),))
    tail = results[-50:]
    assert sum(result.label_refreshes for result in tail) == 0


def test_models_are_built_only_on_a_telemetry_tick() -> None:
    cfg = tiny_config(speed=10.0, telemetry_hz=2.0)
    _sim, results = _run(cfg)
    for result in results:
        if result.telemetry is None:
            assert result.models_built == 0
        else:
            assert result.models_built == cfg.vehicles + 1


def test_the_tick_loop_never_calls_the_linear_nearest_node_scan() -> None:
    """O(V) with a geodesic call per vertex: ~3 ms, so 900 calls is 2.6 s.

    Scenario load may use it. The loop may not.
    """
    from headsup.world.graph import RoadGraph

    original = RoadGraph.nearest_node
    calls = 0

    def counting(self, lon, lat):
        nonlocal calls
        calls += 1
        return original(self, lon, lat)

    RoadGraph.nearest_node = counting  # type: ignore[method-assign]
    try:
        _run(advisories=(advisory_for(),))
    finally:
        RoadGraph.nearest_node = original  # type: ignore[method-assign]
    assert calls == 0


def test_the_tick_loop_never_touches_shapely_through_the_network() -> None:
    """Position interpolation is pure Python over precomputed tuples."""
    net = grid_net()
    for edge in range(net.edge_count):
        lon, lat, heading = net.interpolate(edge, net.length_mm[edge] // 3)
        assert isinstance(lon, float)
        assert isinstance(lat, float)
        assert isinstance(heading, float)


def test_the_ghost_fleet_registers_no_mask_of_its_own() -> None:
    """Its mask is the empty one, which every router already has."""
    cfg = tiny_config()
    net = grid_net()
    ghost = Simulation(net, cfg, mode=SimMode.GHOST, hazards=scripted_hazards(net), start=T0)
    before = ghost.router.mask_keys
    ghost.offer(advisory_for())
    for _ in range(50):
        ghost.step()
    assert ghost.router.mask_keys == before


def test_a_corridor_check_is_a_set_lookup_not_a_geometry_test() -> None:
    """The KEEP_CLEAR cost the plan promised: membership, nothing more."""
    cfg = tiny_config()
    net = grid_net()
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0)
    sim.offer(advisory_for(ActionType.KEEP_CLEAR, params={"width_m": 30.0}))
    results = [sim.step() for _ in range(TICKS)]
    assert sum(result.shapely_calls for result in results) == 1
    corridor = sim.operators.by_name["op_a"].mask.keep_clear_edges
    assert isinstance(corridor, frozenset)
