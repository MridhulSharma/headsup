"""The committed grid graph, and the polygons the world tests query it with.

No test here touches the network. `tests/fixtures/world_grid.graphml` is a
4x4 grid of 100 m blocks written by `scripts/build_world_fixture.py`, read
back through the same `osmnx.load_graphml` that reads a downloaded city:

    n03 --- n07 --- n11 --- n15        y
     |       |       |       |         ^
    n02 --- n06 --- n10 --- n14        |
     |       |  H    |       |         +--> x
    n01 --- n05 --- n09 --- n13
     |       |       |       |
    n00 --- n04 --- n08 --- n12

`H` is a hospital in the middle of the block n05-n06-n09-n10, with a fire
station on the same block and a school two blocks north-west.

The two middle columns -- n04..n07 and n08..n11 -- are signalised and the
outer two are not, so an area drawn across the grid covers intersections of
both kinds. `SIGNALIZED` is that set as igraph vertex ids.

Loading is cached per process: the graphml parse and the H3 index are the
slowest things in these tests, and nothing in them mutates the graph.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from shapely.geometry import Polygon as ShapelyPolygon

from headsup.contracts import Polygon
from headsup.world import RoadGraph, World
from headsup.world.pois import load_pois

FIXTURE_DIR = Path(__file__).resolve().parent
GRAPH_FILE = FIXTURE_DIR / "world_grid.graphml"
POIS_FILE = FIXTURE_DIR / "world_grid_pois.json"

CITY = "fixture"

#: Must match `scripts/build_world_fixture.py`.
ORIGIN_LON, ORIGIN_LAT = -97.7400, 30.2660
BLOCK_M = 100.0
SIZE = 4
M_PER_DEG_LAT = 111_132.0

HOSPITAL = "poi_hospital_n900001"
FIRE_STATION = "poi_fire_station_n900002"
SCHOOL = "poi_school_n900003"

#: What the fixture is built to contain, asserted on so that a regenerated
#: fixture that changed shape fails here rather than somewhere subtler.
NODES = 16
EDGES = 48

#: The hospital's block has four corner intersections, each with two streets
#: leading out of it: eight streets, both directions.
HOSPITAL_ACCESS_EDGES = 16

#: The signal-controlled intersections, as igraph vertex ids: the two middle
#: columns, which are vertices 4..11 because node ids count up the columns.
#: Must match `SIGNALIZED_COLUMNS` in `scripts/build_world_fixture.py`.
SIGNALIZED = frozenset(range(4, 12))


@lru_cache(maxsize=1)
def grid_graph() -> RoadGraph:
    """The fixture graph, loaded and indexed once per process."""
    return RoadGraph.from_graphml(GRAPH_FILE, city=CITY)


@lru_cache(maxsize=1)
def grid_world() -> World:
    """The fixture graph with its POIs and their access edges resolved."""
    _city, pois = load_pois(POIS_FILE)
    return World.from_graph(grid_graph(), pois, city=CITY)


def at(east_blocks: float, north_blocks: float) -> tuple[float, float]:
    """A lon/lat a given number of blocks east and north of the grid origin."""
    import math

    lat = ORIGIN_LAT + north_blocks * BLOCK_M / M_PER_DEG_LAT
    metres_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(ORIGIN_LAT))
    lon = ORIGIN_LON + east_blocks * BLOCK_M / metres_per_deg_lon
    return lon, lat


def block_box(
    west_blocks: float, south_blocks: float, east_blocks: float, north_blocks: float
) -> ShapelyPolygon:
    """A rectangle in block coordinates, e.g. `block_box(1, 1, 2, 2)`."""
    west, south = at(west_blocks, south_blocks)
    east, north = at(east_blocks, north_blocks)
    return ShapelyPolygon(
        [(west, south), (east, south), (east, north), (west, north), (west, south)]
    )


def advisory_box(
    west_blocks: float, south_blocks: float, east_blocks: float, north_blocks: float
) -> Polygon:
    """The same rectangle as a contract `Polygon`, to hand to the compiler."""
    ring = [
        list(point)
        for point in block_box(west_blocks, south_blocks, east_blocks, north_blocks).exterior.coords
    ]
    return Polygon(coordinates=[ring])


def street_box(
    blocks_from: tuple[float, float], blocks_to: tuple[float, float], *, margin: float = 0.2
) -> ShapelyPolygon:
    """A thin box over the middle of one street, clear of both intersections.

    `margin` is in blocks, so the default covers the middle 60% of a street
    and touches neither intersection - which is what makes the expected edge
    set exactly the two directions of that one street.
    """
    (west_blocks, south_blocks), (east_blocks, north_blocks) = blocks_from, blocks_to
    if south_blocks == north_blocks:  # an east-west street
        return block_box(
            west_blocks + margin, south_blocks - 0.05, east_blocks - margin, north_blocks + 0.05
        )
    return block_box(
        west_blocks - 0.05, south_blocks + margin, east_blocks + 0.05, north_blocks - margin
    )


def brute_force_edge_ids(graph: RoadGraph, shape: ShapelyPolygon) -> tuple[int, ...]:
    """Every edge the shape touches, found by testing all of them.

    The answer `edges_in_polygon` has to agree with: same shapely predicate,
    no H3 in the way.
    """
    return tuple(sorted(edge.id for edge in graph.edges if shape.intersects(edge.geometry)))
