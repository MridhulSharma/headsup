"""The demo's side-by-side run: one clock, two fleets, four figures.

`hu demo s1|s2` runs the unadvised baseline beside the advised fleet on the
same seed, and the viewer draws the two sets of counters against each other.
Three things have to hold for that comparison to mean anything, and they are
what is asserted here:

* the two fleets **start from identical vehicle positions**, so a later
  difference is the advice and not the seed;
* they stay **in lockstep on one clock** -- one `SimRunner`, one tick loop --
  so the two counter sets are read off the same simulated instant;
* the counters frame names the **four demo figures** the panel shows.

No Redis and no city: the runner takes the fixture grid's network directly
and a `FakeClock`, so the pacing is asserted rather than waited out.
"""

from __future__ import annotations

from fixtures.sim import advisory_for, grid_net, scripted_hazards, tiny_config
from fixtures.world import grid_world
from headsup.api.frames import FRAME_COUNTERS, HEADLINE_BLOCKED, counters_frame
from headsup.api.runner import StartOptions, modes_for_run
from headsup.ingest import FakeClock
from headsup.sim.config import SimMode
from headsup.sim.runner import SimRunner
from samples import T0

#: What `hu demo` runs: the advised fleet first, the baseline beside it.
DEMO_MODES = (SimMode.LIVE, SimMode.GHOST)


def demo_runner(**overrides: object) -> SimRunner:
    """A `SimRunner` built the way a demo run builds one, on the fixture grid."""
    cfg = tiny_config(**overrides)
    net = grid_net()
    return SimRunner(
        grid_world(),
        cfg,
        modes=DEMO_MODES,
        hazards=scripted_hazards(net),
        start=T0,
        net=net,
        clock=FakeClock(T0),
    )


def test_the_demo_runs_both_fleets_from_one_runner() -> None:
    """Two runners would be two clocks; the comparison rests on there being one."""
    assert tuple(demo_runner().simulations) == DEMO_MODES


def test_both_fleets_start_from_identical_vehicle_positions() -> None:
    """Same seed, same roads, same starting grid: the run begins as one state.

    Edge, offset, destination and operator, vehicle by vehicle. If any of them
    differed at tick 0, every counter the demo puts side by side would be
    measuring the seed as well as the advice.
    """
    sims = demo_runner().simulations
    live = sims[SimMode.LIVE].vehicles
    ghost = sims[SimMode.GHOST].vehicles

    assert len(live) == len(ghost) > 0
    for advised, baseline in zip(live, ghost, strict=True):
        assert (advised.edge, advised.offset_mm, advised.dest_node, advised.op) == (
            baseline.edge,
            baseline.offset_mm,
            baseline.dest_node,
            baseline.op,
        )


def test_identical_positions_hold_for_every_seed() -> None:
    """Not a property of 42: the pairing is the shared seed tree, not the number."""
    for seed in (1, 7, 99):
        sims = demo_runner(seed=seed).simulations
        live = [(v.edge, v.offset_mm, v.dest_node) for v in sims[SimMode.LIVE].vehicles]
        ghost = [(v.edge, v.offset_mm, v.dest_node) for v in sims[SimMode.GHOST].vehicles]
        assert live == ghost, f"seed {seed} started the two fleets apart"


async def test_the_two_fleets_stay_in_lockstep_on_one_clock() -> None:
    """Same tick count and the same simulated instant, advised or not."""
    runner = demo_runner()
    runner.offer(advisory_for())
    report = await runner.run(ticks=30)

    live = runner.simulations[SimMode.LIVE]
    ghost = runner.simulations[SimMode.GHOST]
    assert live.tick == ghost.tick == report.ticks == 30
    assert live.sim_time == ghost.sim_time


async def test_a_run_reports_both_fleets_counters() -> None:
    """The side-by-side figures come off one run, not two."""
    runner = demo_runner()
    runner.offer(advisory_for())
    report = await runner.run(ticks=30)
    assert set(report.counters_by_mode) == {"live", "ghost"}
    assert set(report.blocked_minutes_by_mode) == {"live", "ghost"}


def test_the_counters_frame_names_the_four_demo_figures() -> None:
    """Hazard entries, assist queue depth, blocked corridor minutes, rides."""
    by_mode = {
        "live": {"hazard_entries_total": 0, "assist_open_total": 1, "rides_completed_total": 9},
        "ghost": {"hazard_entries_total": 4, "assist_open_total": 3, "rides_completed_total": 7},
    }
    blocked = {"live": {"op_a": 0.5, "op_b": 0.25}, "ghost": {"op_a": 12.0, "op_b": 3.5}}

    frame = counters_frame(by_mode, blocked)

    assert frame["type"] == FRAME_COUNTERS
    assert frame["headline"]["live"] == {
        "hazard_entries": 0.0,
        "assist_queue_depth": 1.0,
        "rides_completed": 9.0,
        HEADLINE_BLOCKED: 0.75,
    }
    assert frame["headline"]["ghost"] == {
        "hazard_entries": 4.0,
        "assist_queue_depth": 3.0,
        "rides_completed": 7.0,
        HEADLINE_BLOCKED: 15.5,
    }
    # The full snapshots stay beside the headline: the panel reads four
    # figures, the console still reads everything.
    assert frame["counters"] == by_mode
    assert frame["blocked_minutes"] == blocked


def test_an_idle_server_has_a_headline_with_no_columns() -> None:
    """Nothing running is an empty comparison, not a missing field."""
    assert counters_frame({}, {})["headline"] == {}


def test_the_ghost_flag_decides_whether_the_baseline_runs() -> None:
    """`--ghost` is the default; `--no-ghost` leaves the advised fleet alone."""
    assert StartOptions().ghost is True
    assert modes_for_run(StartOptions()) == DEMO_MODES
    assert modes_for_run(StartOptions(ghost=False)) == (SimMode.LIVE,)
