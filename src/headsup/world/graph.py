"""The road graph the simulator drives on and advisories are written against.

OSMnx downloads and simplifies; everything after that is an igraph copy,
because the simulator reroutes thousands of vehicles a tick and igraph's
shortest paths are C. The copy is built once, deterministically: nodes in OSM
id order, edges in (from, to, key) order, so an edge id means the same thing
in the cache, in PostGIS and in a trajectory hash.

An edge id is its index in `RoadGraph.edges` *and* its igraph edge index. The
two stay equal because nothing is ever removed from the graph - a NO_ENTRY
advisory masks edges in the simulator rather than deleting them.

Two queries matter to the rest of the system:

* `edges_in_polygon(poly)` - the roads a hazard polygon covers. H3 first,
  shapely second (see `h3index`).
* `access_edges(lon, lat)` - the roads that enter the block a POI sits on. The
  compiler's critical-access rule asks this about every hospital, fire and EMS
  station under a closure.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import igraph
from pyproj import Geod
from shapely.errors import ShapelyError
from shapely.geometry import LineString, Point
from shapely.geometry import base as shapely_base
from shapely.geometry import shape as shapely_shape

from headsup.world.bbox import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR, BBox
from headsup.world.h3index import DEFAULT_RESOLUTION, SAMPLE_STEP_M, EdgeCellIndex

#: Edge lengths are measured on the WGS84 ellipsoid when OSM carries none.
GEOD = Geod(ellps="WGS84")

#: How far from a POI still counts as its block. One short city block: at this
#: radius the corners of the block a POI sits in are inside and the next
#: intersection out is not, which is what makes `access_edges` the ways in
#: rather than the whole neighbourhood.
DEFAULT_BLOCK_RADIUS_M = 90.0

#: Free-flow speed in km/h by OSM highway class, from Texas urban posted
#: limits (a residential street is 25 mph, a city arterial 35-45 mph). Used
#: when the way carries no `maxspeed`, which most of them do not.
DEFAULT_SPEED_KPH: dict[str, float] = {
    "motorway": 105.0,
    "motorway_link": 72.0,
    "trunk": 89.0,
    "trunk_link": 64.0,
    "primary": 72.0,
    "primary_link": 56.0,
    "secondary": 64.0,
    "secondary_link": 48.0,
    "tertiary": 56.0,
    "tertiary_link": 40.0,
    "unclassified": 48.0,
    "residential": 40.0,
    "living_street": 16.0,
    "service": 24.0,
    "busway": 40.0,
    "road": 40.0,
}

#: For a class not in the table: a city street nobody tagged.
FALLBACK_SPEED_KPH = 40.0

KPH_PER_MPH = 1.609344
MPS_PER_KPH = 1000.0 / 3600.0


class WorldError(RuntimeError):
    """A graph could not be built, loaded or queried."""


def tag_values(value: Any) -> tuple[str, ...]:
    """Every value of one OSM tag, sorted.

    Simplification collapses a chain of OSM ways into one edge, so `highway`,
    `maxspeed` and `name` can arrive as lists - and OSMnx builds those lists
    from a set, so their order changes between processes. Two builds of the
    same city were found writing `['30 mph', '35 mph']` and
    `['35 mph', '30 mph']` for the same edge.

    Sorting is what makes reading them repeatable. Nothing here may depend on
    the order OSM happened to arrive in: the simulator hashes trajectories
    that are computed from these speeds, and a free-flow time that changed
    between two runs of the same seed would make that hash meaningless.
    """
    if isinstance(value, (list, tuple, set)):
        return tuple(sorted(str(item) for item in value if item is not None))
    return () if value is None else (str(value),)


def first_tag(value: Any) -> Any:
    """One value for a tag that may have several: the first in sorted order.

    Used where a tag is a label rather than a number - `name`, `highway`,
    `osmid`. Which of a merged edge's two street names is shown is arbitrary;
    that it is the same one every build is not.
    """
    values = tag_values(value)
    return values[0] if values else None


def parse_maxspeed(value: Any) -> float | None:
    """One `maxspeed` tag in km/h, or None if it says nothing usable.

    OSM tags US speed limits as `"35 mph"`; a bare number is km/h by the OSM
    convention. `"none"`, `"signals"` and the like give None.
    """
    text = str(value).strip().lower()
    head = text.split()[0] if text.split() else ""
    try:
        number = float(head)
    except ValueError:
        return None
    if number <= 0:
        return None
    return number * KPH_PER_MPH if "mph" in text else number


def speed_kph(highway: Any, maxspeed: Any = None) -> float:
    """Free-flow speed for one edge, from its `maxspeed` or its class.

    An edge that merged several ways carries several of each tag, and the
    slowest of them is the one that governs: a vehicle crossing a chain of
    ways cannot free-flow faster than its slowest link, and treating a
    residential stretch as secondary would have the simulator drive 40 km/h
    down a street posted at 25 mph. Taking the minimum is also stable, which
    taking "the first" was not - see `tag_values`.

    Anything unparseable falls back to the class default, because a speed of
    zero would be a free-flow time of infinity.
    """
    posted = [
        parsed for value in tag_values(maxspeed) if (parsed := parse_maxspeed(value)) is not None
    ]
    if posted:
        return min(posted)
    classes = [
        DEFAULT_SPEED_KPH[key]
        for key in (value.strip().lower() for value in tag_values(highway))
        if key in DEFAULT_SPEED_KPH
    ]
    return min(classes) if classes else FALLBACK_SPEED_KPH


def geodesic_length_m(line: LineString) -> float:
    """Length of a lon/lat line on the WGS84 ellipsoid, in metres."""
    coords = list(line.coords)
    if len(coords) < 2:
        return 0.0
    lons = [coord[0] for coord in coords]
    lats = [coord[1] for coord in coords]
    return float(GEOD.line_length(lons, lats))


@dataclass(frozen=True, slots=True)
class Edge:
    """One directed road segment: what the simulator moves along.

    `id` indexes both `RoadGraph.edges` and the igraph edge sequence. `u` and
    `v` are igraph vertex ids, not OSM node ids; `RoadGraph.osmid_of` maps back
    when a human needs to look an intersection up.
    """

    id: int
    u: int
    v: int
    osmid: str
    name: str
    highway: str
    oneway: bool
    length_m: float
    speed_kph: float
    free_flow_s: float
    geometry: LineString

    @property
    def speed_mps(self) -> float:
        return self.speed_kph * MPS_PER_KPH

    def __str__(self) -> str:
        label = self.name or self.highway or "unnamed"
        return f"e{self.id} {label} ({self.length_m:.0f} m, {self.free_flow_s:.1f} s)"


def as_shapely(geometry: Any) -> shapely_base.BaseGeometry:
    """A shapely geometry from a contract model, a mapping, or shapely itself."""
    if isinstance(geometry, shapely_base.BaseGeometry):
        return geometry
    to_shapely = getattr(geometry, "to_shapely", None)
    if callable(to_shapely):
        return to_shapely()
    try:
        return shapely_shape(geometry)
    except (ShapelyError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise WorldError(f"not a usable geometry: {geometry!r} ({exc})") from exc


class RoadGraph:
    """An igraph road network with an H3 edge index over it."""

    def __init__(
        self,
        city: str,
        graph: igraph.Graph,
        edges: Sequence[Edge],
        index: EdgeCellIndex,
        *,
        bbox: BBox | None = None,
    ) -> None:
        self.city = city
        self.graph = graph
        self.edges: tuple[Edge, ...] = tuple(edges)
        self.index = index
        self.bbox = bbox

    # --- construction ------------------------------------------------------

    @classmethod
    def from_nx(
        cls,
        nx_graph: Any,
        *,
        city: str,
        bbox: BBox | None = None,
        resolution: int = DEFAULT_RESOLUTION,
        step_m: float = SAMPLE_STEP_M,
    ) -> RoadGraph:
        """Build the igraph copy and the H3 index from an OSMnx graph."""
        osmids = sorted(nx_graph.nodes)
        position = {osmid: index for index, osmid in enumerate(osmids)}
        xs: list[float] = []
        ys: list[float] = []
        for osmid in osmids:
            node = nx_graph.nodes[osmid]
            try:
                xs.append(float(node["x"]))
                ys.append(float(node["y"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise WorldError(f"node {osmid} has no usable x/y: {node!r}") from exc

        pairs: list[tuple[int, int]] = []
        edges: list[Edge] = []
        geometries: list[LineString] = []
        ordered = sorted(
            nx_graph.edges(keys=True, data=True),
            key=lambda item: (item[0], item[1], item[2]),
        )
        for u_osmid, v_osmid, key, data in ordered:
            u, v = position[u_osmid], position[v_osmid]
            geometry = data.get("geometry")
            if not isinstance(geometry, LineString):
                geometry = LineString([(xs[u], ys[u]), (xs[v], ys[v])])
            try:
                length_m = float(data.get("length"))
            except (TypeError, ValueError):
                length_m = geodesic_length_m(geometry)
            if not math.isfinite(length_m) or length_m <= 0:
                length_m = max(geodesic_length_m(geometry), 0.1)
            kph = speed_kph(data.get("highway"), data.get("maxspeed"))
            pairs.append((u, v))
            geometries.append(geometry)
            edges.append(
                Edge(
                    id=len(edges),
                    u=u,
                    v=v,
                    osmid=str(first_tag(data.get("osmid")) or f"{u_osmid}-{v_osmid}-{key}"),
                    name=str(first_tag(data.get("name")) or ""),
                    highway=str(first_tag(data.get("highway")) or ""),
                    oneway=bool(data.get("oneway", False)),
                    length_m=length_m,
                    speed_kph=kph,
                    free_flow_s=length_m / (kph * MPS_PER_KPH),
                    geometry=geometry,
                )
            )

        graph = igraph.Graph(n=len(osmids), directed=True)
        graph["city"] = city
        graph.vs["osmid"] = [str(osmid) for osmid in osmids]
        graph.vs["x"] = xs
        graph.vs["y"] = ys
        graph.add_edges(pairs)
        if edges:
            graph.es["eid"] = [edge.id for edge in edges]
            graph.es["length_m"] = [edge.length_m for edge in edges]
            graph.es["speed_kph"] = [edge.speed_kph for edge in edges]
            graph.es["free_flow_s"] = [edge.free_flow_s for edge in edges]
            graph.es["highway"] = [edge.highway for edge in edges]
            graph.es["name"] = [edge.name for edge in edges]
            graph.es["oneway"] = [edge.oneway for edge in edges]

        index = EdgeCellIndex.build(geometries, resolution=resolution, step_m=step_m)
        return cls(city, graph, edges, index, bbox=bbox)

    @classmethod
    def from_graphml(
        cls,
        path: str | Path,
        *,
        city: str | None = None,
        bbox: BBox | None = None,
        resolution: int = DEFAULT_RESOLUTION,
        step_m: float = SAMPLE_STEP_M,
    ) -> RoadGraph:
        """Load a cached OSMnx graphml file and rebuild the igraph copy.

        OSMnx is imported here, not at module scope, so that reading a cached
        graph does not pay for the download stack.
        """
        import osmnx as ox

        target = Path(path)
        if not target.exists():
            raise WorldError(f"no cached graph at {target.as_posix()} - run `hu world build`")
        try:
            nx_graph = ox.load_graphml(target)
        except Exception as exc:  # osmnx wraps a zoo of parser errors
            raise WorldError(f"cannot read {target.as_posix()}: {exc}") from exc
        name = city or str(nx_graph.graph.get("city") or target.stem.split("_")[0])
        return cls.from_nx(nx_graph, city=name, bbox=bbox, resolution=resolution, step_m=step_m)

    # --- shape -------------------------------------------------------------

    @property
    def node_count(self) -> int:
        return self.graph.vcount()

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def length_km(self) -> float:
        return sum(edge.length_m for edge in self.edges) / 1000.0

    def edge(self, edge_id: int) -> Edge:
        if not 0 <= edge_id < len(self.edges):
            raise WorldError(f"no edge {edge_id} in a graph of {self.edge_count}")
        return self.edges[edge_id]

    def osmid_of(self, node: int) -> str:
        """The OSM node id behind an igraph vertex id."""
        return str(self.graph.vs[node]["osmid"])

    def position_of(self, node: int) -> tuple[float, float]:
        vertex = self.graph.vs[node]
        return float(vertex["x"]), float(vertex["y"])

    def summary(self) -> str:
        """One line for a log or a CLI: size, road length and index size."""
        return (
            f"{self.city}: {self.node_count} nodes, {self.edge_count} edges, "
            f"{self.length_km:.1f} km of road, {len(self.index)} h3 "
            f"r{self.index.resolution} cells ({self.index.entries} cell-edge pairs)"
        )

    # --- queries -----------------------------------------------------------

    def edge_ids_in_polygon(self, geometry: Any) -> tuple[int, ...]:
        """Ids of every edge the geometry touches, ascending.

        H3 narrows the graph to candidate edges, then shapely decides. An edge
        counts when it intersects, so an edge that only runs along the
        polygon's boundary is covered: a closure drawn down a street closes
        that street.
        """
        shape = as_shapely(geometry)
        if shape.is_empty:
            return ()
        return tuple(
            sorted(
                edge_id
                for edge_id in self.index.candidates(shape)
                if shape.intersects(self.edges[edge_id].geometry)
            )
        )

    def edges_in_polygon(self, geometry: Any) -> tuple[Edge, ...]:
        """Every edge the geometry touches, in edge id order."""
        return tuple(self.edges[edge_id] for edge_id in self.edge_ids_in_polygon(geometry))

    def nodes_near(self, lon: float, lat: float, radius_m: float) -> frozenset[int]:
        """Vertices within `radius_m` of a point, measured geodesically.

        A degree box prefilters and `Geod.inv` measures: the box is cheap and
        generous, the measurement is exact.
        """
        d_lat = radius_m / M_PER_DEG_LAT
        cos_lat = max(math.cos(math.radians(lat)), 1e-6)
        d_lon = radius_m / (M_PER_DEG_LON_EQUATOR * cos_lat)
        found: set[int] = set()
        for vertex in self.graph.vs:
            x, y = float(vertex["x"]), float(vertex["y"])
            if abs(y - lat) > d_lat or abs(x - lon) > d_lon:
                continue
            *_bearings, distance = GEOD.inv(lon, lat, x, y)
            if distance <= radius_m:
                found.add(vertex.index)
        return frozenset(found)

    def nearest_node(self, lon: float, lat: float) -> int:
        """The closest vertex to a point. Raises on an empty graph."""
        if self.node_count == 0:
            raise WorldError(f"{self.city} graph has no nodes")
        best = 0
        best_distance = math.inf
        for vertex in self.graph.vs:
            *_bearings, distance = GEOD.inv(lon, lat, float(vertex["x"]), float(vertex["y"]))
            if distance < best_distance:
                best, best_distance = vertex.index, distance
        return best

    def nearest_edge(self, lon: float, lat: float) -> Edge:
        """The edge whose geometry passes closest to a point.

        Ranked in degrees, not metres: this only ever compares candidates near
        one point, where the scale factor is the same for all of them.
        """
        if not self.edges:
            raise WorldError(f"{self.city} graph has no edges")
        point = Point(lon, lat)
        return min(self.edges, key=lambda edge: edge.geometry.distance(point))

    def block_nodes(
        self, lon: float, lat: float, *, radius_m: float = DEFAULT_BLOCK_RADIUS_M
    ) -> frozenset[int]:
        """The intersections that make up the block a point sits on.

        Falls back to the single nearest intersection when nothing is in
        range, so a POI set back from the street still has a block.
        """
        near = self.nodes_near(lon, lat, radius_m)
        return near if near else frozenset({self.nearest_node(lon, lat)})

    def access_edges(
        self, lon: float, lat: float, *, radius_m: float = DEFAULT_BLOCK_RADIUS_M
    ) -> tuple[Edge, ...]:
        """The edges entering the block a point sits on.

        An edge enters the block when exactly one of its endpoints is a block
        intersection: it crosses the boundary between the block and the rest
        of the city. Edges with both endpoints inside are the block's own
        streets, not ways into it.

        Both directions of a crossing street are returned - one entering, one
        leaving. They are the same piece of road, so a polygon that closes one
        closes the other, and the critical-access rule counts them together.

        A graph small enough to sit entirely inside one block has no boundary
        to cross. There the edges touching the block are the answer, which
        keeps the rule meaningful on a fixture graph.
        """
        inside = self.block_nodes(lon, lat, radius_m=radius_m)
        crossing = tuple(edge for edge in self.edges if (edge.u in inside) != (edge.v in inside))
        if crossing:
            return crossing
        return tuple(edge for edge in self.edges if edge.u in inside or edge.v in inside)

    def edge_rows(self, *, city: str | None = None) -> Iterable[Mapping[str, Any]]:
        """Every edge as a row for the PostGIS `roads` table.

        `city` overrides the graph's own name. The city column is the key the
        table is written and deleted by, so the caller that owns the name -
        `World`, not the graph it was built from - supplies it.
        """
        name = city or self.city
        for edge in self.edges:
            yield {
                "city": name,
                "edge_id": edge.id,
                "osmid": edge.osmid,
                "name": edge.name,
                "highway": edge.highway,
                "oneway": edge.oneway,
                "length_m": edge.length_m,
                "speed_kph": edge.speed_kph,
                "free_flow_s": edge.free_flow_s,
                "from_node": self.osmid_of(edge.u),
                "to_node": self.osmid_of(edge.v),
                "wkt": edge.geometry.wkt,
            }
