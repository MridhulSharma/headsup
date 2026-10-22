"""The same seed must produce the same trajectory, exactly.

This is the property the whole evaluation rests on: if two runs of one seed can
drift, then a difference between variants cannot be attributed to the variant.
The hash is folded over integer state only, so these assertions are exact
rather than approximate.
"""

from __future__ import annotations

from fixtures.sim import advisory_for, grid_net, scripted_hazards, tiny_config
from headsup.sim.config import SimMode
from headsup.sim.engine import Simulation
from headsup.sim.hashing import first_divergence
from samples import T0


def _run(ticks: int = 300, *, seed: int = 42, speed: float = 10.0, advise: bool = True):
    cfg = tiny_config(seed=seed, speed=speed)
    net = grid_net()
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0)
    if advise:
        sim.offer(advisory_for())
    for _ in range(ticks):
        sim.step()
    return sim


def test_the_same_seed_gives_the_same_trajectory_hash() -> None:
    first, second = _run(), _run()
    assert first.trajectory_hash() == second.trajectory_hash()
    assert first_divergence(first.checkpoints(), second.checkpoints()) is None


def test_a_different_seed_gives_a_different_trajectory_hash() -> None:
    assert _run(seed=42).trajectory_hash() != _run(seed=7).trajectory_hash()


def test_the_hash_does_not_depend_on_the_pacing_speed() -> None:
    """`--speed` is wall-clock dressing; the simulation steps are identical.

    If this ever fails, something in the tick is reading the clock.
    """
    slow, fast = _run(speed=1.0), _run(speed=60.0)
    assert slow.trajectory_hash() == fast.trajectory_hash()


def test_the_hash_does_not_depend_on_advisory_arrival_order() -> None:
    """Two advisories offered in either order must leave the same state.

    op_b's delay is keyed by `(advisory id, version)` rather than by arrival
    count, which is what makes this hold.
    """
    net = grid_net()
    cfg = tiny_config()
    first = advisory_for(advisory_id="adv-one")
    second = advisory_for(advisory_id="adv-two", confidence=0.95)

    digests = []
    for order in ((first, second), (second, first)):
        sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0)
        for advisory in order:
            sim.offer(advisory)
        for _ in range(200):
            sim.step()
        digests.append(sim.trajectory_hash())
    assert digests[0] == digests[1]


def test_checkpoints_localise_a_divergence_to_one_minute() -> None:
    """A checkpoint per sim-minute, so a failure says *where*, not just *that*."""
    matched = _run(ticks=180)
    diverged = _run(ticks=180, seed=99)
    assert len(matched.checkpoints()) == 3
    assert [tick for tick, _ in matched.checkpoints()] == [60, 120, 180]
    assert first_divergence(matched.checkpoints(), diverged.checkpoints()) == 60


def test_a_run_without_advice_matches_the_ghost_of_the_same_seed() -> None:
    """With nothing advised, the live fleet *is* the baseline.

    A useful guard: if these ever differ, the live path is doing something the
    ghost path is not, independently of any advisory.
    """
    net = grid_net()
    cfg = tiny_config()
    live = Simulation(net, cfg, mode=SimMode.LIVE, hazards=scripted_hazards(net), start=T0)
    ghost = Simulation(net, cfg, mode=SimMode.GHOST, hazards=scripted_hazards(net), start=T0)
    for _ in range(200):
        live.step()
        ghost.step()
    assert live.trajectory_hash() == ghost.trajectory_hash()
