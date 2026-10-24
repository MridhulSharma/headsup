"""The route responders take to a scene, as an area a fleet can be told about.

An emergency-scene advisory is not really about the scene. The scene itself is
a hole in the road network either way: a vehicle that drives into it stops,
advised or not. What a fleet can usefully be asked is to leave the *approach*
alone - the street the next engine or ambulance is coming up - and not to
pick anybody up or sit idling inside it.

So this module answers one question for the proposer: given a scene, which
fire or EMS station is nearest, what is the shortest way from it to the scene
on this city's own road graph, and what area does that route plus a no-stop
radius around the scene cover? The answer is a polygon, because
`sim.masks.resolve` takes a `KEEP_CLEAR` area and resolves it to edges and
nodes, and because the compiler's area cap has to be able to measure it.

Three things are deliberate:

* **The station is chosen by distance, then by reachability.** The nearest
  station by air may have no route to the scene on a graph clipped to a bbox,
  so the nearest few are tried in order and the first that routes wins. A
  scene with no routable station answers None, and the proposer then falls
  back to the hazard's own geometry - a corridor nobody can drive is not worth
  publishing.
* **The route is the shortest by length, not by time.** A responder on a call
  is not obeying the speed limits the free-flow times are built from, and
  distance is the thing that does not change with traffic.
* **Every number comes from the playbook.** The corridor width, the no-stop
  radius and the longest route worth drawing are `keep_clear` in
  `playbooks/emergency_scene.yaml`. Nothing here has a default of its own,
  because a cap with a default in code is a cap that can be widened without
  editing a playbook.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pyproj import Geod
from shapely.geometry import LineString as ShapelyLine
from shapely.geometry import Point as ShapelyPoint
from shapely.geometry import base as shapely_base
from shapely.ops import unary_union

from headsup.agents.compiler.poi import PoiKind
from headsup.contracts import Geometry, MultiPolygon, Polygon, area_geometry
from headsup.world.bbox import buffer_m
from headsup.world.graph import RoadGraph, as_shapely
from headsup.world.model import World
from headsup.world.pois import WorldPoi

#: Distances on the same ellipsoid every other measurement in the repository
#: uses.
GEOD = Geod(ellps="WGS84")

#: The kinds that respond to a scene. A hospital is a destination, not a
#: responder: nothing drives *from* an emergency department to a crash.
RESPONDER_KINDS: frozenset[PoiKind] = frozenset({PoiKind.FIRE_STATION, PoiKind.EMS_STATION})

#: How many of the nearest stations are tried before giving up. A graph
#: clipped to a downtown bbox can hold a station with no path to a given
#: corner, which is a property of the clip rather than of the city.
MAX_CANDIDATES = 4


@dataclass(frozen=True, slots=True)
class SceneCorridor:
    """The area a KEEP_CLEAR for one scene covers, and where it came from."""

    #: Corridor buffer unioned with the no-stop disc around the scene.
    geometry: Polygon | MultiPolygon
    #: The route centre line, for a test and for the viewer.
    route: ShapelyLine
    station_id: str
    station_kind: PoiKind
    station_name: str
    route_m: float
    width_m: float
    radius_m: float

    @property
    def summary(self) -> str:
        """One clause for a proposal's rationale. Facts, not prose."""
        return (
            f"{self.width_m:g} m corridor along the {self.route_m:.0f} m shortest route "
            f"from {self.station_kind.value} {self.station_name} to the scene, plus a "
            f"{self.radius_m:g} m no-stop radius around it"
        )


@runtime_checkable
class SceneRouter(Protocol):
    """What the proposer needs to know about getting responders to a scene."""

    def corridor(
        self, scene: Geometry, *, width_m: float, radius_m: float, max_route_m: float
    ) -> SceneCorridor | None:  # pragma: no cover - protocol
        """The keep-clear area for this scene, or None if none can be drawn."""
        ...

    def describes_itself(self) -> str:  # pragma: no cover - protocol
        """One line for the audit record: which data answered."""
        ...


class NullSceneRouter:
    """Answers "no corridor" because it knows no city.

    The default, so a proposer runs before a world is built; it says so in
    `describes_itself`, and an `EMERGENCY_SCENE` then keeps the hazard's own
    geometry rather than silently losing the corridor.
    """

    def corridor(
        self, scene: Geometry, *, width_m: float, radius_m: float, max_route_m: float
    ) -> SceneCorridor | None:
        return None

    def describes_itself(self) -> str:
        return "null-scene-router (no road graph; emergency scenes keep their own area)"


def _centroid(scene: Geometry | shapely_base.BaseGeometry) -> ShapelyPoint:
    return as_shapely(scene).centroid


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    *_bearings, distance = GEOD.inv(a[0], a[1], b[0], b[1])
    return float(distance)


def nearest_responders(
    pois: Sequence[WorldPoi], lon: float, lat: float, *, limit: int = MAX_CANDIDATES
) -> tuple[WorldPoi, ...]:
    """The nearest fire and EMS stations to a point, nearest first."""
    stations = [poi for poi in pois if poi.kind in RESPONDER_KINDS]
    stations.sort(key=lambda poi: (_metres((poi.lon, poi.lat), (lon, lat)), poi.id))
    return tuple(stations[:limit])


def route_line(graph: RoadGraph, source: int, target: int) -> tuple[ShapelyLine | None, float]:
    """The shortest path between two graph nodes as one line, and its length.

    Weighted by edge length in metres, directed: a one-way street is one way
    for a fire engine too, which is the conservative assumption and the only
    one the graph can support. `(None, 0.0)` when there is no path.
    """
    if source == target:
        return None, 0.0
    weights = [edge.length_m for edge in graph.edges]
    paths = graph.graph.get_shortest_paths(source, to=target, weights=weights, output="epath")
    edge_ids = list(paths[0]) if paths else []
    if not edge_ids:
        return None, 0.0
    coords: list[tuple[float, float]] = []
    length = 0.0
    for edge_id in edge_ids:
        edge = graph.edges[edge_id]
        length += edge.length_m
        for point in edge.geometry.coords:
            position = (float(point[0]), float(point[1]))
            if not coords or coords[-1] != position:
                coords.append(position)
    if len(coords) < 2:
        return None, 0.0
    return ShapelyLine(coords), length


@dataclass(frozen=True, slots=True)
class WorldSceneRouter:
    """Routes responders to a scene over one city's road graph and POIs."""

    world: World

    def corridor(
        self, scene: Geometry, *, width_m: float, radius_m: float, max_route_m: float
    ) -> SceneCorridor | None:
        """The keep-clear area for this scene, or None if none can be drawn.

        None has three causes, and all three are normal: the city has no fire
        or EMS station loaded, no station within `max_route_m` has a route to
        the scene, or the union of the corridor and the disc came out as
        something that is not an area.
        """
        graph = self.world.graph
        here = _centroid(scene)
        scene_node = graph.nearest_node(here.x, here.y)
        for station in nearest_responders(self.world.pois, here.x, here.y):
            station_node = graph.nearest_node(station.lon, station.lat)
            route, length = route_line(graph, station_node, scene_node)
            if route is None or length > max_route_m:
                continue
            # `buffer_m` is the scale-buffer-unscale trick every layer needs:
            # half the width each side of the centre line is the width.
            area = unary_union([buffer_m(route, width_m / 2.0), buffer_m(here, radius_m)])
            geometry = area_geometry(area)
            if geometry is None:  # pragma: no cover - a buffered union is an area
                continue
            return SceneCorridor(
                geometry=geometry,
                route=route,
                station_id=station.id,
                station_kind=station.kind,
                station_name=station.name or station.id,
                route_m=length,
                width_m=width_m,
                radius_m=radius_m,
            )
        return None

    def describes_itself(self) -> str:
        """One line for the audit record: which data answered, over what."""
        stations = [poi for poi in self.world.pois if poi.kind in RESPONDER_KINDS]
        graph = self.world.graph
        return (
            f"world-scene-router ({len(stations)} fire/EMS stations, "
            f"{self.world.city} graph {graph.node_count} nodes/{graph.edge_count} edges)"
        )


def default_scene_router(city: str, *, cache_dir: object = None) -> SceneRouter:
    """The router to give `Proposer` for a city, built if the cache is there.

    Falls back to `NullSceneRouter` rather than refusing, for the reason
    `default_poi_checker` does: the agents have to run before a world is
    built, and the fallback states itself in the audit record.
    """
    from headsup.world.graph import WorldError
    from headsup.world.model import load_world

    try:
        return WorldSceneRouter(load_world(city, cache_dir=cache_dir))  # type: ignore[arg-type]
    except (WorldError, ValueError):
        return NullSceneRouter()
