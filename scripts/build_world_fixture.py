"""Rebuild the committed world fixture: a tiny grid graph and three POIs.

    uv run python scripts/build_world_fixture.py

The world tests need a road graph, and they are not allowed to download one:
a unit run has to work on a plane, and a test that depends on Overpass fails
for reasons that have nothing to do with the code. So this script writes a
graph by hand - no network - in the shape the real thing arrives in, and the
tests read it back with the same `osmnx.load_graphml` that reads a real city.

The fixture is a 4x4 grid of two-way streets on 100 m blocks in downtown
Austin, which gives the queries something with an exact expected answer:

    n03 --- n07 --- n11 --- n15        y
     |       |       |       |         ^
    n02 --- n06 --- n10 --- n14        |
     |       |  H    |       |         +--> x
    n01 --- n05 --- n09 --- n13
     |       |       |       |
    n00 --- n04 --- n08 --- n12

`H` is a hospital in the middle of the block bounded by n05, n06, n09 and n10.
Those four intersections are ~71 m from it and are its block; the next ring of
intersections is ~158 m away and is not. Eight streets cross that boundary, so
the hospital has 16 directed access edges, and a polygon has to cover all
eight streets to cut it off. A fire station sits on the same block to give the
critical kinds two members, and a school sits two blocks away so that the
tests can show a school is not treated as critical.

Node ids count up the columns (n00..n03 is the westmost column, south to
north), which is also OSM id order, so the igraph vertex ids the tests assert
on are stable.
"""

from __future__ import annotations

import math
from pathlib import Path

import networkx as nx
import osmnx as ox

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"
GRAPH_FILE = FIXTURE_DIR / "world_grid.graphml"
POIS_FILE = FIXTURE_DIR / "world_grid_pois.json"

#: South-west corner of the grid: East 7th at Red River, downtown Austin.
ORIGIN_LON, ORIGIN_LAT = -97.7400, 30.2660

#: Block size. 100 m is a short Austin block and keeps the arithmetic in the
#: fixture's docstring exact enough to assert on.
BLOCK_M = 100.0

SIZE = 4

M_PER_DEG_LAT = 111_132.0

#: One OSM id per grid node, so `sorted(graph.nodes)` is the grid order.
FIRST_OSMID = 1_000_000

CITY = "fixture"


def _degrees(east_m: float, north_m: float) -> tuple[float, float]:
    """Metres east and north of the origin, as lon/lat degrees."""
    lat = ORIGIN_LAT + north_m / M_PER_DEG_LAT
    metres_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(ORIGIN_LAT))
    lon = ORIGIN_LON + east_m / metres_per_deg_lon
    return lon, lat


def node_id(column: int, row: int) -> int:
    return FIRST_OSMID + column * SIZE + row


def build_graph() -> nx.MultiDiGraph:
    """The 4x4 grid, in the shape OSMnx hands back a downloaded city."""
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "epsg:4326"
    graph.graph["simplified"] = True
    graph.graph["city"] = CITY

    for column in range(SIZE):
        for row in range(SIZE):
            lon, lat = _degrees(column * BLOCK_M, row * BLOCK_M)
            graph.add_node(node_id(column, row), x=lon, y=lat, street_count=4)

    way = 500_000
    for column in range(SIZE):
        for row in range(SIZE):
            here = node_id(column, row)
            if column + 1 < SIZE:  # an east-west street
                _street(graph, here, node_id(column + 1, row), way, f"W {row} St", "residential")
                way += 1
            if row + 1 < SIZE:  # a north-south street
                _street(
                    graph, here, node_id(column, row + 1), way, f"N {column} Ave", "residential"
                )
                way += 1
    return graph


def _street(graph: nx.MultiDiGraph, u: int, v: int, osmid: int, name: str, highway: str) -> None:
    """Add both directions of one two-way street between two intersections."""
    attrs = {
        "osmid": osmid,
        "name": name,
        "highway": highway,
        "oneway": False,
        "reversed": False,
        "length": round(BLOCK_M, 3),
        "maxspeed": "30 mph",
    }
    graph.add_edge(u, v, key=0, **attrs)
    graph.add_edge(v, u, key=0, **{**attrs, "reversed": True})


def build_pois() -> list[dict[str, object]]:
    """Three POIs: two critical ones on one block, and a school elsewhere.

    Written with no access edges; `World.from_graph` resolves those against
    the graph, which is the behaviour the tests are there to check.
    """
    hospital_lon, hospital_lat = _degrees(1.5 * BLOCK_M, 1.5 * BLOCK_M)
    fire_lon, fire_lat = _degrees(1.4 * BLOCK_M, 1.6 * BLOCK_M)
    school_lon, school_lat = _degrees(0.5 * BLOCK_M, 2.5 * BLOCK_M)
    return [
        {
            "id": "poi_hospital_n900001",
            "kind": "HOSPITAL",
            "name": "Grid General",
            "osm_id": "node/900001",
            "lon": hospital_lon,
            "lat": hospital_lat,
            "access_edge_ids": [],
        },
        {
            "id": "poi_fire_station_n900002",
            "kind": "FIRE_STATION",
            "name": "Grid Station 1",
            "osm_id": "node/900002",
            "lon": fire_lon,
            "lat": fire_lat,
            "access_edge_ids": [],
        },
        {
            "id": "poi_school_n900003",
            "kind": "SCHOOL",
            "name": "Grid Elementary",
            "osm_id": "node/900003",
            "lon": school_lon,
            "lat": school_lat,
            "access_edge_ids": [],
        },
    ]


def main() -> None:
    import json

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    graph = build_graph()
    ox.save_graphml(graph, GRAPH_FILE)
    payload = {"city": CITY, "count": 3, "pois": build_pois()}
    POIS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"{GRAPH_FILE.as_posix()}: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges"
    )
    print(f"{POIS_FILE.as_posix()}: {payload['count']} pois")


if __name__ == "__main__":
    main()
