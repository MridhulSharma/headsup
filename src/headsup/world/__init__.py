"""OSM road graph, H3 edge index, PostGIS access and POI lookups.

One city's world is a `RoadGraph` plus the POIs an advisory may not cut off:

    from headsup.world import World

    world = World.load("san_antonio")          # from data/cache, no network
    edges = world.graph.edges_in_polygon(poly) # H3 narrows, shapely decides
    compiler = Compiler(poi=world.checker())   # the real critical-access rule

What lives where:

| module | what it does |
| --- | --- |
| `bbox` | the rectangle each city is downloaded for |
| `osm` | the only network code: download, simplify, cache graphml |
| `graph` | the igraph copy, edge ids, free-flow times, the two queries |
| `h3index` | resolution-10 cell index from cells to edges |
| `pois` | hospitals, fire, EMS and schools, with their access edges |
| `db` | the `roads` and `pois` tables in PostGIS |
| `checker` | `WorldPoiChecker`, the real version of D1.3's stub |
| `raw` | saved Overpass responses, and the offline rebuild from them |
| `model` | `World`: the two halves together, and the cache paths |

`hu world build --city austin|san_antonio` is the one command that reaches the
network; everything else reads the cache it leaves. `--offline` rebuilds from the
Overpass responses that build saved, which is the form of the build worth holding
to a time budget.
"""

from __future__ import annotations

from headsup.world import raw
from headsup.world.bbox import AUSTIN_BOX, BOXES, SAN_ANTONIO_BOX, BBox, box
from headsup.world.checker import WorldPoiChecker, default_poi_checker
from headsup.world.graph import (
    DEFAULT_BLOCK_RADIUS_M,
    DEFAULT_SPEED_KPH,
    Edge,
    RoadGraph,
    WorldError,
    as_shapely,
    speed_kph,
)
from headsup.world.h3index import DEFAULT_RESOLUTION, SAMPLE_STEP_M, EdgeCellIndex, cover_cells
from headsup.world.model import (
    CACHE_DIR,
    ENV_CACHE,
    World,
    clear_world_cache,
    graph_path,
    is_built,
    load_world,
    pois_path,
    world_cache_dir,
)
from headsup.world.pois import OSM_TAGS, PoiKind, WorldPoi, load_pois, resolve_access, save_pois

__all__ = [
    "AUSTIN_BOX",
    "BOXES",
    "CACHE_DIR",
    "DEFAULT_BLOCK_RADIUS_M",
    "DEFAULT_RESOLUTION",
    "DEFAULT_SPEED_KPH",
    "ENV_CACHE",
    "OSM_TAGS",
    "SAMPLE_STEP_M",
    "SAN_ANTONIO_BOX",
    "BBox",
    "Edge",
    "EdgeCellIndex",
    "PoiKind",
    "RoadGraph",
    "World",
    "WorldError",
    "WorldPoi",
    "WorldPoiChecker",
    "as_shapely",
    "box",
    "clear_world_cache",
    "cover_cells",
    "default_poi_checker",
    "graph_path",
    "is_built",
    "load_pois",
    "load_world",
    "pois_path",
    "raw",
    "resolve_access",
    "save_pois",
    "speed_kph",
    "world_cache_dir",
]
