"""900 vehicles must sustain 10x real time on this laptop.

Marked `perf` and excluded from CI, because a wall-clock throughput number
measured on a shared runner says nothing about anything. It also skips unless
the city is built, and `data/cache/` is gitignored, so a runner physically
cannot run it either way.

Measurement discipline, because this is a 15 W part whose P-cores down-clock
under sustained load:

* warm up first, so igraph, the label cache and the Pydantic classes are not
  being paid for inside the measured window,
* three windows, and assert on the **median**, so one scheduling hiccup
  cannot fail the build,
* measure the core, with no clock, no bus and no telemetry serialisation, so
  "the simulator is fast enough" is not confused with "Redis was quick today".

The invariants that make it fast are asserted separately, in
`tests/unit/test_sim_perf_invariants.py`, which does run everywhere.
"""

from __future__ import annotations

import os
import time

import pytest

from headsup.contracts import (
    Action,
    ActionType,
    Advisory,
    AdvisoryProperties,
    AppliesTo,
    Evidence,
    Hazard,
    HazardClass,
    Source,
)
from headsup.sim.config import SimConfig, SimMode
from headsup.sim.engine import Simulation
from headsup.sim.ground_truth import ScriptedHazardField
from headsup.sim.network import DriveNetwork
from headsup.sim.router import Router
from headsup.sim.scenarios import s1_flood_regions, salado_creek_corridor
from headsup.world.model import is_built, load_world

pytestmark = [
    pytest.mark.perf,
    pytest.mark.filterwarnings("ignore::RuntimeWarning"),
]

CITY = "san_antonio"
VEHICLES = 900
WARMUP_TICKS = 60
WINDOW_TICKS = 200
WINDOWS = 3

#: The target from `docs/build-packet.md:434`. Overridable for a slower box
#: rather than edited, so a local workaround never lands in the repo.
TARGET_X = float(os.environ.get("HU_SIM_PERF_MIN_X", "10.0"))

pytest_skip = pytest.mark.skipif(
    not is_built(CITY),
    reason=f"{CITY} is not built - run `hu world build --city {CITY}`",
)


def _advisory(valid_from, valid_until) -> Advisory:
    """The S1 advisory, so the measured window includes a mask change."""
    geometry = salado_creek_corridor().__geo_interface__
    evidence = [
        Evidence(
            event_id="ev-perf",
            source=Source.NWS,
            kind="flood_warning",
            observed_at=valid_from,
            raw_ref="data/recordings/s1_flood/fixture/nws.jsonl",
        )
    ]
    hazard = Hazard(
        id="haz-perf",
        **{"class": HazardClass.FLOOD},
        severity=4,
        confidence=0.91,
        geometry=geometry,
        evidence=evidence,
        valid_until=valid_until,
        first_seen=valid_from,
        updated_at=valid_from,
    )
    return Advisory(
        geometry=geometry,
        properties=AdvisoryProperties(
            id="adv-perf",
            version=1,
            hazard=hazard,
            action=Action(type=ActionType.NO_ENTRY, applies_to=AppliesTo()),
            valid_from=valid_from,
            valid_until=valid_until,
            evidence=evidence,
            policy_rule="flood.no_entry.v1",
            issuer=f"headsup/{CITY}",
        ),
    )


def _setup(vehicles: int = VEHICLES):
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 4, 20, 18, 50, tzinfo=UTC)
    until = start + timedelta(hours=9)
    world = load_world(CITY)
    cfg = SimConfig(city=CITY, seed=42, vehicles=vehicles, fleets=3, zones=48, speed=0.0)
    cfg.validate()
    regions = s1_flood_regions(valid_from=start, valid_until=until)
    net = DriveNetwork.build(world, cfg, anchors=[r.geometry for r in regions])
    field = ScriptedHazardField.from_regions(regions, net)
    router = Router(net, cfg)
    return cfg, net, field, router, start, until


def _window(cfg, net, field, router, start, until, ticks: int) -> float:
    """Sim seconds per wall second over one window, advisory included."""
    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=field, start=start, router=router)
    sim.offer(_advisory(start, until))
    begin = time.perf_counter()
    for _ in range(ticks):
        sim.step()
    return ticks / (time.perf_counter() - begin)


@pytest_skip
def test_nine_hundred_vehicles_sustain_ten_times_real_time() -> None:
    cfg, net, field, router, start, until = _setup()

    warm = Simulation(net, cfg, mode=SimMode.LIVE, hazards=field, start=start, router=router)
    warm.offer(_advisory(start, until))
    for _ in range(WARMUP_TICKS):
        warm.step()

    rates = sorted(
        _window(cfg, net, field, router, start, until, WINDOW_TICKS) for _ in range(WINDOWS)
    )
    median = rates[len(rates) // 2]
    print(
        f"\n{VEHICLES} vehicles, {CITY}: windows "
        f"{', '.join(f'{rate:.0f}x' for rate in rates)}; median {median:.0f}x "
        f"({1000.0 / median:.2f} ms per tick); target {TARGET_X:.0f}x"
    )
    assert median >= TARGET_X, (
        f"median {median:.1f}x is under the {TARGET_X:.0f}x target; "
        "set HU_SIM_PERF_MIN_X to measure on a slower machine"
    )


@pytest_skip
def test_running_the_ghost_fleet_alongside_still_clears_the_target() -> None:
    """D3.5 runs both fleets on one clock, which is the demo configuration."""
    cfg, net, field, router, start, until = _setup()
    sims = [
        Simulation(net, cfg, mode=mode, hazards=field, start=start, router=router)
        for mode in (SimMode.LIVE, SimMode.GHOST)
    ]
    for sim in sims:
        sim.offer(_advisory(start, until))
    for _ in range(WARMUP_TICKS):
        for sim in sims:
            sim.step()

    begin = time.perf_counter()
    for _ in range(WINDOW_TICKS):
        for sim in sims:
            sim.step()
    rate = WINDOW_TICKS / (time.perf_counter() - begin)
    print(f"\nlive + ghost ({2 * VEHICLES} vehicles): {rate:.0f}x")
    assert rate >= TARGET_X


@pytest_skip
def test_the_advisory_tick_does_not_stall_the_run() -> None:
    """The tick an advisory lands on must not cost a multiple of the others.

    A per-vehicle routing design would pay ~313 ms here, three times the whole
    budget at 10x. Reverse labels make it a bounded refresh instead, so the
    worst tick should stay in the same order of magnitude as the median.
    """
    cfg, net, field, router, start, until = _setup()
    warm = Simulation(net, cfg, mode=SimMode.LIVE, hazards=field, start=start, router=router)
    for _ in range(WARMUP_TICKS):
        warm.step()

    sim = Simulation(net, cfg, mode=SimMode.LIVE, hazards=field, start=start, router=router)
    sim.offer(_advisory(start, until))
    costs = []
    for _ in range(60):
        begin = time.perf_counter()
        sim.step()
        costs.append(time.perf_counter() - begin)

    worst = max(costs)
    median = sorted(costs)[len(costs) // 2]
    print(f"\nworst tick {worst * 1000:.1f} ms, median {median * 1000:.1f} ms")
    # Generous: this is about ruling out a multi-second stall, not about
    # policing jitter on an ultrabook.
    assert worst < 0.5, f"a single tick took {worst * 1000:.0f} ms"
