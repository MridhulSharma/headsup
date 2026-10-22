"""Advisory -> mask: what an operator's graph looks like once it accepts.

Resolution is the only place shapely runs, and it runs once per advisory
version. Everything the tick loop then asks is a frozenset membership test or
a tuple lookup. That split is the whole performance story for masks: a 2 km
flood polygon costs 3.3 ms to resolve and nothing per tick, while testing the
same polygon per vehicle per tick would cost about 45 ms a tick and miss the
budget on its own.

Two actions deliberately do **not** touch edge weights:

* `DENSITY_CAP` is a queue, not a cost. Putting occupancy into the weights
  would change the weight vector every tick and destroy the label cache that
  makes routing affordable.
* `KEEP_CLEAR` forbids *stopping*, not *through travel*. Penalising the
  corridor in the weights would invent a routing rule the advisory never
  asked for, and would cost a full label refresh for nothing.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from hashlib import blake2b

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from headsup.contracts import ActionType, Advisory
from headsup.sim.config import SimConfig
from headsup.sim.network import DriveNetwork
from headsup.world.bbox import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR
from headsup.world.graph import as_shapely

#: mph -> m/s. `SPEED_CAP` params are mph; the graph is kph.
MPS_PER_MPH = 1609.344 / 3600.0
MM_PER_M = 1000.0

#: What a SPEED_CAP with an absurd value still leaves a vehicle: never zero,
#: or a capped edge becomes an undrivable trap rather than a slow road.
MIN_STEP_MM = 1

#: Node osmid -> vertex id, per network. Keyed by identity, which is why
#: `DriveNetwork` is declared `eq=False`.
_NODE_INDEX: dict[DriveNetwork, dict[str, int]] = {}
#: Edge id -> incoming edge ids, per network, for the ALL_WAY_STOP penalty.
_IN_EDGES: dict[DriveNetwork, tuple[tuple[int, ...], ...]] = {}


def _node_osmids(net: DriveNetwork) -> dict[str, int]:
    index = _NODE_INDEX.get(net)
    if index is None:
        graph = net.world.graph.graph
        index = {str(osmid): ix for ix, osmid in enumerate(graph.vs["osmid"])}
        _NODE_INDEX[net] = index
    return index


def _in_edges(net: DriveNetwork) -> tuple[tuple[int, ...], ...]:
    cached = _IN_EDGES.get(net)
    if cached is None:
        buckets: list[list[int]] = [[] for _ in range(net.node_count)]
        for edge_id, node in enumerate(net.head):
            buckets[node].append(edge_id)
        cached = tuple(tuple(bucket) for bucket in buckets)
        _IN_EDGES[net] = cached
    return cached


@dataclass(frozen=True, slots=True)
class Mask:
    """One operator's accepted restrictions, resolved to graph elements.

    Fields that the tick loop *iterates* are sorted tuples; fields it only
    *tests* are frozensets. Nothing here is a set that gets walked, because
    set iteration order is not a guarantee worth betting reproducibility on.
    """

    no_entry: frozenset[int] = frozenset()
    #: (edge id, capped millimetres per second), ascending by edge.
    speed_cap_mm_s: tuple[tuple[int, int], ...] = ()
    all_way_stop: frozenset[int] = frozenset()
    #: (vertex id, delay seconds), ascending by vertex.
    stop_delay_s: tuple[tuple[int, int], ...] = ()
    #: (vertex id, max vehicles), ascending by vertex.
    density_cap: tuple[tuple[int, int], ...] = ()
    keep_clear_edges: frozenset[int] = frozenset()
    keep_clear_nodes: frozenset[int] = frozenset()
    paused: bool = False
    #: Content hash. Two masks with the same restrictions share a key, which
    #: is what lets the router cache labels across operators: op_c and the
    #: ghost both carry the empty mask, and op_a and op_b converge once op_b's
    #: delay elapses, so the steady state is two keys rather than four.
    key: str = field(default="")

    @property
    def empty(self) -> bool:
        return self.key == EMPTY_KEY

    def speed_overrides(self) -> dict[int, int]:
        """Edge -> capped mm/s, as a dict for the tick loop."""
        return dict(self.speed_cap_mm_s)

    def stop_delays(self) -> dict[int, int]:
        return dict(self.stop_delay_s)

    def density_caps(self) -> dict[int, int]:
        return dict(self.density_cap)


def _content_key(mask: Mask) -> str:
    """blake2b over every field, canonically ordered."""
    digest = blake2b(digest_size=12)
    digest.update(repr(sorted(mask.no_entry)).encode())
    digest.update(repr(mask.speed_cap_mm_s).encode())
    digest.update(repr(sorted(mask.all_way_stop)).encode())
    digest.update(repr(mask.stop_delay_s).encode())
    digest.update(repr(mask.density_cap).encode())
    digest.update(repr(sorted(mask.keep_clear_edges)).encode())
    digest.update(repr(sorted(mask.keep_clear_nodes)).encode())
    digest.update(b"1" if mask.paused else b"0")
    return digest.hexdigest()


def _keyed(mask: Mask) -> Mask:
    """The same mask with its content key filled in."""
    from dataclasses import replace

    return replace(mask, key=_content_key(mask))


EMPTY_MASK: Mask = _keyed(Mask())
EMPTY_KEY: str = EMPTY_MASK.key


def corridor_polygon(
    geometry: object, width_m: float, *, lat0: float | None = None
) -> BaseGeometry:
    """Widen a corridor centre line into an area, buffering in metres.

    A single buffer radius in degrees cannot be right in both axes at once:
    at 29.5 degrees north a degree of longitude is about 0.87 of a degree of
    latitude, so a degree-space circle is 15% too wide east to west. So scale
    into a local metric frame, buffer there, and scale back.

    A polygon corridor is already an area and passes through untouched.
    """
    shape = as_shapely(geometry)
    if shape.is_empty or shape.geom_type in {"Polygon", "MultiPolygon"}:
        return shape
    if lat0 is None:
        lat0 = float(shape.centroid.y)
    cos_lat = max(math.cos(math.radians(lat0)), 1e-6)
    sx = M_PER_DEG_LON_EQUATOR * cos_lat
    sy = M_PER_DEG_LAT

    from shapely.affinity import scale as affine_scale

    metric = affine_scale(shape, xfact=sx, yfact=sy, origin=(0.0, 0.0))
    buffered = metric.buffer(max(width_m, 0.1) / 2.0)
    return affine_scale(buffered, xfact=1.0 / sx, yfact=1.0 / sy, origin=(0.0, 0.0))


def resolve(advisory: Advisory, net: DriveNetwork, cfg: SimConfig) -> Mask:
    """Turn one advisory into the restrictions it implies on this graph.

    Edges come from the advisory geometry, unioned with any osmid matches in
    `applies_to.edges`. The geometry is authoritative: `applies_to.edges`
    carries `Edge.osmid` strings rather than graph indices, and an osmid can
    name many segments, so it can only ever add to what the polygon found.
    """
    action = advisory.action
    graph = net.world.graph
    shape = as_shapely(advisory.geometry)

    named_edges: set[int] = set()
    for name in action.applies_to.edges:
        named_edges.update(net.osmid_edges.get(str(name), ()))

    node_index = _node_osmids(net)
    named_nodes: set[int] = set()
    for name in action.applies_to.nodes:
        vertex = node_index.get(str(name))
        if vertex is not None:
            named_nodes.add(vertex)

    kind = action.type
    params = action.params

    if kind is ActionType.KEEP_CLEAR:
        width = params.get("width_m", cfg.keep_clear_default_width_m)
        area = corridor_polygon(advisory.geometry, float(width))
        edge_ids = set(graph.edge_ids_in_polygon(area)) | named_edges
        nodes = _nodes_inside(net, edge_ids, area) | named_nodes
        return _keyed(Mask(keep_clear_edges=frozenset(edge_ids), keep_clear_nodes=frozenset(nodes)))

    area_edges = set(graph.edge_ids_in_polygon(shape)) | named_edges

    if kind is ActionType.NO_ENTRY:
        return _keyed(Mask(no_entry=frozenset(area_edges)))

    if kind is ActionType.SPEED_CAP:
        cap_mph = float(params["speed_mph"])
        cap_mm_s = max(MIN_STEP_MM, round(cap_mph * MPS_PER_MPH * MM_PER_M))
        capped = tuple(
            (edge_id, cap_mm_s)
            for edge_id in sorted(area_edges)
            if net.free_flow_mm_s[edge_id] > cap_mm_s
        )
        return _keyed(Mask(speed_cap_mm_s=capped))

    if kind is ActionType.ALL_WAY_STOP:
        delay = int(params.get("delay_s", cfg.all_way_stop_delay_s))
        nodes = _endpoint_nodes(net, area_edges) | named_nodes
        return _keyed(
            Mask(
                all_way_stop=frozenset(nodes),
                stop_delay_s=tuple((node, delay) for node in sorted(nodes)),
            )
        )

    if kind is ActionType.DENSITY_CAP:
        limit = int(params["max_vehicles"])
        nodes = _endpoint_nodes(net, area_edges) | named_nodes
        return _keyed(Mask(density_cap=tuple((node, limit) for node in sorted(nodes))))

    if kind is ActionType.PAUSE_SERVICE:
        # Not in the D2.2 brief's mask list, but it is in `ActionType`, and an
        # unhandled action would raise rather than degrade. Minimal honest
        # behaviour: stop dispatching, let in-flight trips finish.
        return _keyed(Mask(paused=True))

    raise ValueError(f"no mask rule for action {kind!r}")


def _endpoint_nodes(net: DriveNetwork, edge_ids: Iterable[int]) -> set[int]:
    """Both endpoints of every edge in the set."""
    nodes: set[int] = set()
    for edge_id in edge_ids:
        nodes.add(net.tail[edge_id])
        nodes.add(net.head[edge_id])
    return nodes


def _nodes_inside(net: DriveNetwork, edge_ids: Iterable[int], area: BaseGeometry) -> set[int]:
    """Endpoints that actually fall inside the area.

    Tighter than every endpoint of a matched edge: a corridor drawn down one
    street clips edges whose far end is a block away, and banning that far
    node would take a zone out of service for no reason. Tested over a few
    dozen candidate points, never over the whole graph.
    """
    position = net.world.graph.position_of
    inside: set[int] = set()
    for node in sorted(_endpoint_nodes(net, edge_ids)):
        lon, lat = position(node)
        if area.contains(Point(lon, lat)):
            inside.add(node)
    return inside


def merge(masks: Sequence[Mask]) -> Mask:
    """Union several masks into the one an operator is actually driving under.

    Order-insensitive by construction: every field is a union, a max, or an
    OR. Where two advisories cap the same thing differently the stricter wins
    -- the lower speed, the shorter density cap, the longer stop delay.
    """
    live = [mask for mask in masks if not mask.empty]
    if not live:
        return EMPTY_MASK
    if len(live) == 1:
        return live[0]

    no_entry: set[int] = set()
    keep_clear_edges: set[int] = set()
    keep_clear_nodes: set[int] = set()
    all_way_stop: set[int] = set()
    speeds: dict[int, int] = {}
    delays: dict[int, int] = {}
    caps: dict[int, int] = {}
    paused = False

    for mask in live:
        no_entry |= mask.no_entry
        keep_clear_edges |= mask.keep_clear_edges
        keep_clear_nodes |= mask.keep_clear_nodes
        all_way_stop |= mask.all_way_stop
        paused = paused or mask.paused
        for edge_id, mm_s in mask.speed_cap_mm_s:
            current = speeds.get(edge_id)
            speeds[edge_id] = mm_s if current is None else min(current, mm_s)
        for node, delay in mask.stop_delay_s:
            delays[node] = max(delays.get(node, 0), delay)
        for node, limit in mask.density_cap:
            current = caps.get(node)
            caps[node] = limit if current is None else min(current, limit)

    return _keyed(
        Mask(
            no_entry=frozenset(no_entry),
            speed_cap_mm_s=tuple(sorted(speeds.items())),
            all_way_stop=frozenset(all_way_stop),
            stop_delay_s=tuple(sorted(delays.items())),
            density_cap=tuple(sorted(caps.items())),
            keep_clear_edges=frozenset(keep_clear_edges),
            keep_clear_nodes=frozenset(keep_clear_nodes),
            paused=paused,
        )
    )


def weights_for(mask: Mask, net: DriveNetwork) -> list[float]:
    """The igraph weight vector for routing under a mask.

    `float("inf")` for a closed edge, not a large finite number. A finite
    sentinel lets Dijkstra route *through* a closure when the detour is long
    enough, which would quietly put vehicles in the flood zone and make the
    NO_ENTRY guarantee a lie. Infinity makes the edge non-existent for
    routing while `RoadGraph.edges` is left untouched, which is the invariant
    `world/graph.py` promises and the trajectory hash depends on.
    """
    weights = list(net.free_flow_s)
    for edge_id, cap_mm_s in mask.speed_cap_mm_s:
        # Travel time at the capped speed, from the edge's own length.
        weights[edge_id] = net.length_mm[edge_id] / cap_mm_s
    if mask.all_way_stop:
        incoming = _in_edges(net)
        for node, delay in mask.stop_delay_s:
            for edge_id in incoming[node]:
                weights[edge_id] += delay
    for edge_id in mask.no_entry:
        weights[edge_id] = float("inf")
    return weights
