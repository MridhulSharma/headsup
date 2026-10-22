"""Properties that must hold for any seed, fleet size and set of advisories.

Profile and idioms follow `tests/property/test_compiler_properties.py`:
600 examples, no deadline, guard-and-return rather than `assume()`, and a
generator-honesty test at the bottom so a property cannot pass by only ever
seeing the uninteresting case.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fixtures.sim import (
    BARRIER,
    CORRIDOR_LINE,
    advisory_for,
    grid_net,
    no_hazards,
    scripted_hazards,
    tiny_config,
)
from headsup.contracts import ActionType, VehicleStatus
from headsup.sim import counters as C
from headsup.sim.config import SimMode
from headsup.sim.engine import Simulation
from samples import T0

PROFILE = settings(
    max_examples=600,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

#: Keeps an example quick: the invariants are per tick, not per run length.
TICKS = 40

ACTIONS = (
    ActionType.NO_ENTRY,
    ActionType.SPEED_CAP,
    ActionType.ALL_WAY_STOP,
    ActionType.DENSITY_CAP,
    ActionType.KEEP_CLEAR,
)

#: What each action needs in `params` to satisfy the contract.
_PARAMS = {
    ActionType.SPEED_CAP: {"speed_mph": 12.0},
    ActionType.DENSITY_CAP: {"max_vehicles": 2},
    ActionType.KEEP_CLEAR: {"width_m": 30.0},
    ActionType.ALL_WAY_STOP: {"delay_s": 4},
}

#: The tally the honesty test reads.
SEEN: dict[str, int] = {}


def _note(label: str) -> None:
    SEEN[label] = SEEN.get(label, 0) + 1


@st.composite
def runs(draw) -> dict:
    """A seeded run: a seed, a fleet size, and zero or more advisories."""
    seed = draw(st.integers(min_value=0, max_value=10_000))
    fleets = draw(st.integers(min_value=1, max_value=3))
    # At least two vehicles, so there are at least two zones to route between.
    per_fleet = draw(st.integers(min_value=2, max_value=8))
    mode = draw(st.sampled_from((SimMode.LIVE, SimMode.GHOST)))
    with_hazard = draw(st.booleans())
    actions = draw(st.lists(st.sampled_from(ACTIONS), min_size=0, max_size=2))
    confidence = draw(st.floats(min_value=0.3, max_value=1.0))
    geometry = draw(st.sampled_from((BARRIER, CORRIDOR_LINE)))
    return {
        "seed": seed,
        "fleets": fleets,
        "vehicles": fleets * per_fleet,
        "mode": mode,
        "with_hazard": with_hazard,
        "actions": actions,
        "confidence": confidence,
        "geometry": geometry,
    }


def _build(case: dict) -> Simulation:
    cfg = tiny_config(
        seed=case["seed"],
        fleets=case["fleets"],
        vehicles=case["vehicles"],
        zones=max(2, min(6, case["vehicles"])),
        stall_to_assist_s=3,
    )
    net = grid_net(zones=cfg.zones)
    hazards = scripted_hazards(net) if case["with_hazard"] else no_hazards()
    sim = Simulation(net, cfg, mode=case["mode"], hazards=hazards, start=T0)
    for index, action in enumerate(case["actions"]):
        sim.offer(
            advisory_for(
                action,
                advisory_id=f"adv-{index}",
                geometry=case["geometry"].__geo_interface__,
                confidence=case["confidence"],
                params=_PARAMS.get(action),
            )
        )
    return sim


@PROFILE
@given(runs())
def test_an_accepting_operator_never_enters_a_closed_edge(case: dict) -> None:
    sim = _build(case)
    where = {veh.ix: veh.edge for veh in sim.vehicles}
    for _ in range(TICKS):
        sim.step()
        for veh in sim.vehicles:
            moved = veh.edge != where[veh.ix]
            where[veh.ix] = veh.edge
            if not moved:
                continue
            closed = sim.operators.by_name[veh.op].mask.no_entry
            if closed:
                _note("drove under a closure")
                assert veh.edge not in closed


@PROFILE
@given(runs())
def test_a_vehicle_is_always_somewhere_on_exactly_one_edge(case: dict) -> None:
    sim = _build(case)
    net = sim.network
    for _ in range(TICKS):
        sim.step()
        for veh in sim.vehicles:
            assert 0 <= veh.edge < net.edge_count
            assert 0 <= veh.offset_mm <= net.length_mm[veh.edge]


@PROFILE
@given(runs())
def test_the_fleet_never_gains_or_loses_a_vehicle(case: dict) -> None:
    sim = _build(case)
    expected = case["vehicles"]
    ids = {veh.vid for veh in sim.vehicles}
    assert len(ids) == expected
    for _ in range(TICKS):
        sim.step()
        assert len(sim.vehicles) == expected
        assert {veh.vid for veh in sim.vehicles} == ids


@PROFILE
@given(runs())
def test_cumulative_counters_never_decrease(case: dict) -> None:
    """Gauges may fall; totals may not."""
    sim = _build(case)
    previous = sim.counters()
    for _ in range(TICKS):
        sim.step()
        current = sim.counters()
        for key, value in current.items():
            if key.startswith(C.ASSIST_OPEN) and not key.startswith(C.ASSIST_OPENED):
                continue
            assert value >= previous[key], key
        previous = current


@PROFILE
@given(runs())
def test_a_density_cap_in_force_is_never_exceeded(case: dict) -> None:
    """No more vehicles cross a capped node in one tick than the cap allows.

    Counted as admissions -- the node each vehicle just drove out of -- since
    that is what the cap actually restricts.
    """
    sim = _build(case)
    net = sim.network
    where = {veh.ix: veh.edge for veh in sim.vehicles}
    for _ in range(TICKS):
        sim.step()
        crossed: dict[tuple[str, int], int] = {}
        for veh in sim.vehicles:
            previous = where[veh.ix]
            where[veh.ix] = veh.edge
            if veh.edge == previous:
                continue
            node = net.tail[veh.edge]
            crossed[(veh.op, node)] = crossed.get((veh.op, node), 0) + 1

        for op_name in sim.operators.names:
            caps = sim.operators.by_name[op_name].mask.density_caps()
            if not caps:
                continue
            _note("a density cap was in force")
            for node, limit in caps.items():
                assert crossed.get((op_name, node), 0) <= limit


@PROFILE
@given(runs())
def test_the_ghost_fleet_never_acts_on_an_advisory(case: dict) -> None:
    case = {**case, "mode": SimMode.GHOST}
    sim = _build(case)
    for _ in range(TICKS):
        sim.step()
        for op in sim.operators.operators:
            assert op.mask.empty
    assert sim.counters()[C.ADVISORIES_ACCEPTED] == 0
    if case["actions"]:
        _note("a ghost run was offered advice")


@PROFILE
@given(runs())
def test_blocked_minutes_are_always_the_floor_of_blocked_seconds(case: dict) -> None:
    sim = _build(case)
    for _ in range(TICKS):
        sim.step()
    counters = sim.counters()
    for op in sim.operators.names:
        seconds = counters[C.key(C.BLOCKED_SECONDS, op)]
        if seconds:
            _note("corridor time accrued")
        assert counters[C.key(C.BLOCKED_MINUTES, op)] == seconds // 60


@PROFILE
@given(runs())
def test_a_stalled_vehicle_eventually_asks_for_help_or_recovers(case: dict) -> None:
    sim = _build(case)
    for _ in range(TICKS):
        sim.step()
    for veh in sim.vehicles:
        if veh.status is VehicleStatus.AWAITING_ASSIST:
            _note("a vehicle awaited assistance")
            assert veh.assist_open
        if veh.status is VehicleStatus.STALLED:
            _note("a vehicle stalled")
            assert veh.stalled_s >= 1


def test_the_generated_space_reached_every_interesting_case() -> None:
    """Honesty check: a property that only sees the dull case proves nothing.

    Runs last, reading the tally the properties above filled in.
    """
    for label in (
        "drove under a closure",
        "a density cap was in force",
        "a ghost run was offered advice",
        "corridor time accrued",
        "a vehicle stalled",
    ):
        assert SEEN.get(label), f"the generator never produced: {label}"
