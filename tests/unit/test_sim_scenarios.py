"""The scenario hazard areas must sit where the traffic actually goes.

A hazard off to one side of the map would make every comparison in this suite
look good for the wrong reason: the advised fleet would avoid something nobody
was driving through. So the exposure is measured, and held to at least 20% of
seeded trips for each scenario.

These tests need a built city, which `data/cache/` holds and `.gitignore`
keeps out of the repo, so they skip rather than fail on a machine (or a CI
runner) that has not run `hu world build`.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from headsup.agents.compiler.geo import area_km2
from headsup.agents.compiler.playbooks import PlaybookSet
from headsup.contracts import HazardClass
from headsup.scenario.blackout import CLEAR_MIN, ONSET_MIN
from headsup.sim.config import SimConfig
from headsup.sim.ground_truth import ScriptedHazardField
from headsup.sim.masks import EMPTY_KEY
from headsup.sim.network import DriveNetwork
from headsup.sim.rng import SeedTree
from headsup.sim.router import Router
from headsup.sim.scenarios import (
    SCENARIO_CITY,
    s1_flood_regions,
    s2_blackout_regions,
    salado_creek_corridor,
)
from headsup.world.graph import as_shapely
from headsup.world.model import is_built, load_world

#: The bar the exposure has to clear, for each scenario.
MIN_EXPOSURE = 0.20

#: Trips to sample. Enough that the fraction is stable, few enough to be quick.
TRIPS = 900

pytestmark = pytest.mark.filterwarnings("ignore::RuntimeWarning")


def _requires(city: str) -> None:
    if not is_built(city):
        pytest.skip(f"{city} is not built - run `hu world build --city {city}`")


def _exposure(city: str, regions, *, use_nodes: bool) -> float:
    """Share of seeded trips whose route touches the hazard."""
    world = load_world(city)
    cfg = SimConfig(city=city, seed=42, vehicles=TRIPS, fleets=3, zones=48)
    cfg.validate()
    net = DriveNetwork.build(world, cfg, anchors=[region.geometry for region in regions])
    router = Router(net, cfg)
    field = ScriptedHazardField.from_regions(regions, net)

    hazard_edges: set[int] = set()
    hazard_nodes: set[int] = set()
    for hazard in field.hazards:
        hazard_edges |= set(hazard.edges)
        hazard_nodes |= set(hazard.nodes)

    seeds = SeedTree(cfg.seed)
    routable = 0
    exposed = 0
    for ix in range(TRIPS):
        origin = seeds.draw_choice("trip", ix, 0, "origin", options=net.core)
        zone_ix = seeds.draw_int("trip", ix, 0, "zone", lo=0, hi=len(net.zones) - 1)
        if not router.reachable(EMPTY_KEY, zone_ix, origin):
            continue
        routable += 1
        node = origin
        target = net.zones[zone_ix]
        touched = False
        hops = 0
        while node != target and hops < 2000:
            edge = router.next_hop(EMPTY_KEY, zone_ix, node)
            if edge < 0:
                break
            if edge in hazard_edges:
                touched = True
            node = net.head[edge]
            if use_nodes and node in hazard_nodes:
                touched = True
            hops += 1
        exposed += int(touched)

    assert routable > TRIPS * 0.9, f"only {routable}/{TRIPS} trips were routable"
    return exposed / routable


def test_s1_is_set_in_san_antonio_and_s2_in_austin() -> None:
    assert SCENARIO_CITY == {
        "s1_flood": "san_antonio",
        "s2_blackout": "austin",
        "s3_incident": "austin",
    }


def test_at_least_a_fifth_of_s1_trips_cross_the_salado_creek_reach() -> None:
    _requires("san_antonio")
    from samples import T0

    regions = s1_flood_regions(valid_from=T0, valid_until=T0.replace(year=T0.year + 1))
    exposure = _exposure("san_antonio", regions, use_nodes=False)
    assert exposure >= MIN_EXPOSURE, f"only {exposure:.1%} of trips cross the creek"


def test_at_least_a_fifth_of_s2_trips_pass_through_the_signal_cluster() -> None:
    _requires("austin")
    from samples import T0

    regions = s2_blackout_regions(valid_from=T0, valid_until=T0.replace(year=T0.year + 1))
    exposure = _exposure("austin", regions, use_nodes=True)
    assert exposure >= MIN_EXPOSURE, f"only {exposure:.1%} of trips cross the cluster"


def test_the_s1_corridor_stays_inside_the_flood_playbooks_area_cap() -> None:
    """A hazard the compiler could never legally advise on proves nothing.

    `playbooks/flood.yaml` caps a flood advisory's area, so the corridor the
    simulator floods has to be something an advisory could actually cover.
    """
    book = PlaybookSet.load().require(HazardClass.FLOOD)
    area = area_km2(salado_creek_corridor())
    assert area <= book.max_area_km2, f"{area:.2f} km2 exceeds the {book.max_area_km2} km2 cap"


def test_the_s2_cluster_stays_inside_the_signal_outage_area_cap() -> None:
    from samples import T0

    book = PlaybookSet.load().require(HazardClass.SIGNAL_OUTAGE)
    regions = s2_blackout_regions(valid_from=T0, valid_until=T0.replace(year=T0.year + 1))
    area = area_km2(as_shapely(regions[0].geometry))
    assert area <= book.max_area_km2, f"{area:.2f} km2 exceeds the {book.max_area_km2} km2 cap"


def test_the_s2_hazard_is_the_descriptors_own_and_runs_on_the_feeds_clock() -> None:
    """The geometry lives in `scenarios/s2_blackout.yaml`, and so does the timing.

    D2.2 kept a hand-placed box here; the descriptor now carries the hull of
    the signals the recording actually darkens, and the minutes into the run
    at which they drop out and come back. The run's *start* is still the
    caller's -- a run starts when it is started and the recording's own
    timestamps are months old -- but how the outage sits inside it is the
    feed's, because a simulator that darkened the signals from the first tick
    would be charging the fleet for an outage no feed had reported yet.
    """
    from samples import T0

    regions = s2_blackout_regions(valid_from=T0, valid_until=T0 + timedelta(hours=24))
    assert len(regions) == 1
    region = regions[0]
    assert region.id == "s2-downtown-signals"
    assert region.hazard_class is HazardClass.SIGNAL_OUTAGE
    assert region.severity == 3
    assert as_shapely(region.geometry).geom_type == "Polygon"

    # `scenarios/s2_blackout.yaml` phases: the first signals drop out at
    # minute 10 and the grid is back at 85.
    assert region.valid_from == T0 + timedelta(minutes=ONSET_MIN)
    assert region.valid_until == T0 + timedelta(minutes=CLEAR_MIN)
    # Nothing is dark before the feed says so, or after it says the grid is back.
    assert not region.active_at(T0)
    assert region.active_at(T0 + timedelta(minutes=(ONSET_MIN + CLEAR_MIN) / 2))
    assert not region.active_at(T0 + timedelta(minutes=CLEAR_MIN))


def test_the_s1_corridor_covers_the_two_gauges_that_crossed_their_action_stage() -> None:
    """Wilderness Rd and Blanco Rd crossed; Loop 13 did not and is downstream.

    Coordinates come from `data/recordings/s1_flood/fixture/usgs.jsonl`, and
    `scenarios/s1_flood.yaml` records which gauges crossed.
    """
    from shapely.geometry import Point

    from headsup.sim.scenarios import SALADO_BLANCO_RD, SALADO_WILDERNESS_RD

    corridor = salado_creek_corridor()
    for gauge in (SALADO_WILDERNESS_RD, SALADO_BLANCO_RD):
        assert corridor.distance(Point(*gauge)) < 1e-3


def test_the_s1_corridor_closes_real_roads() -> None:
    _requires("san_antonio")
    from samples import T0

    world = load_world("san_antonio")
    cfg = SimConfig(city="san_antonio", seed=42, vehicles=900, fleets=3, zones=48)
    net = DriveNetwork.build(world, cfg)
    regions = s1_flood_regions(valid_from=T0, valid_until=T0.replace(year=T0.year + 1))
    field = ScriptedHazardField.from_regions(regions, net)
    edges = field.hazards[0].edges
    assert len(edges) > 20, "a flooded creek reach should close more than a handful of edges"
    assert len(edges) < net.edge_count // 4, "but it is a crossing, not a district"


def test_the_s2_cluster_darkens_real_intersections() -> None:
    _requires("austin")
    from samples import T0

    world = load_world("austin")
    cfg = SimConfig(city="austin", seed=42, vehicles=900, fleets=3, zones=48)
    net = DriveNetwork.build(world, cfg)
    regions = s2_blackout_regions(valid_from=T0, valid_until=T0.replace(year=T0.year + 1))
    field = ScriptedHazardField.from_regions(regions, net)
    hazard = field.hazards[0]
    assert hazard.hazard_class is HazardClass.SIGNAL_OUTAGE
    assert len(hazard.nodes) > 10
    # Signals only: a stop-sign corner inside the hull has none to lose.
    assert hazard.nodes <= net.world.graph.signalized_nodes
    # A signal outage darkens intersections; it does not close roads.
    assert hazard.edges == frozenset()
