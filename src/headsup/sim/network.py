"""The immutable driving network the tick loop reads.

`RoadGraph` is the right shape for queries -- an igraph object plus a tuple of
`Edge` dataclasses with shapely geometry on each one. It is the wrong shape for
a hot loop: attribute lookups on a frozen dataclass, and a shapely object per
edge, are costs to pay once rather than nine thousand times a second.

So `DriveNetwork` flattens everything the loop touches into tuples indexed by
edge id or vertex id, and precomputes the geometry as plain float tuples so
that interpolating a vehicle's position never calls into shapely. The edge ids
are `RoadGraph`'s own, which `world/graph.py` guarantees are stable igraph
indices -- that invariant is what lets the trajectory hash mean anything.

It also decides the two things that bound every later cost: the strongly
connected core that trips are drawn from, and the destination zones.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from shapely.geometry.base import BaseGeometry

from headsup.sim.config import SimConfig
from headsup.sim.rng import SeedTree
from headsup.world.bbox import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR
from headsup.world.graph import as_shapely
from headsup.world.model import World

MM_PER_M = 1000.0

#: Metres of road one vehicle occupies when queued, for the DENSITY_CAP
#: default. Roughly a car plus a gap.
METRES_PER_VEHICLE = 7.5

#: How far from a hazard crossing to place the paired zones that make trips
#: cross it. Far enough that the crossing is the natural route, close enough
#: that the pair stays inside one city bbox.
ANCHOR_REACH_M = 1800.0

#: Minimum separation between a crossing and the node chosen as its zone, so
#: the pair does not collapse onto the crossing itself.
ANCHOR_MIN_M = 500.0


class NetworkError(RuntimeError):
    """The world cannot support a simulation (no core, no zones)."""


@dataclass(frozen=True, slots=True, eq=False)
class DriveNetwork:
    """Flat, immutable arrays for the tick loop, plus the core and the zones."""

    world: World
    #: Per vertex: its outgoing edge ids, ascending. Ascending order is the
    #: next-hop tie-break, so it is load-bearing, not incidental.
    out_edges: tuple[tuple[int, ...], ...]
    #: Per edge.
    head: tuple[int, ...]
    tail: tuple[int, ...]
    length_mm: tuple[int, ...]
    free_flow_s: tuple[float, ...]
    #: Free-flow millimetres per second, before congestion.
    free_flow_mm_s: tuple[int, ...]
    #: Millimetres per tick after the seeded static congestion factor.
    base_step_mm: tuple[int, ...]
    capacity: tuple[int, ...]
    #: Geometry as flat tuples: per edge, the vertex coordinates, the
    #: cumulative distance in millimetres, and the per-segment bearing.
    geom_x: tuple[tuple[float, ...], ...]
    geom_y: tuple[tuple[float, ...], ...]
    geom_cum_mm: tuple[tuple[int, ...], ...]
    geom_head: tuple[tuple[float, ...], ...]

    #: The largest strongly connected component, ascending. Trips live here:
    #: `san_antonio` has 112 strong components, so drawing origins or
    #: destinations from the whole graph would make some trips unroutable.
    core: tuple[int, ...]
    #: Destination zones, ascending vertex ids.
    zones: tuple[int, ...]
    #: Edges with both endpoints in the core, ascending. Vehicles start here.
    core_edges: tuple[int, ...]
    #: `Edge.osmid` -> edge ids. An advisory's `applies_to.edges` carries
    #: osmids, not indices, and one osmid can span many edges (in
    #: `san_antonio`, 2110 distinct osmids over 6038 edges, one of them 44
    #: segments), so the mapping is one-to-many.
    osmid_edges: dict[str, tuple[int, ...]]

    @property
    def edge_count(self) -> int:
        return len(self.head)

    @property
    def node_count(self) -> int:
        return len(self.out_edges)

    def zone_index(self, node: int) -> int:
        """Position of a zone vertex in `zones`, or -1."""
        try:
            return self.zones.index(node)
        except ValueError:
            return -1

    def interpolate(self, edge: int, offset_mm: int) -> tuple[float, float, float]:
        """`(lon, lat, heading)` for a position along an edge.

        Pure Python over precomputed tuples: no shapely, which is what keeps
        telemetry off the list of things that could blow the tick budget.
        """
        cum = self.geom_cum_mm[edge]
        xs = self.geom_x[edge]
        ys = self.geom_y[edge]
        heads = self.geom_head[edge]
        if offset_mm <= 0:
            return xs[0], ys[0], heads[0]
        total = cum[-1]
        if offset_mm >= total:
            return xs[-1], ys[-1], heads[-1]
        # Linear scan: a simplified OSM edge has a handful of vertices, so this
        # beats bisect's call overhead at this size.
        seg = 0
        while cum[seg + 1] < offset_mm:
            seg += 1
        span = cum[seg + 1] - cum[seg]
        frac = (offset_mm - cum[seg]) / span if span else 0.0
        return (
            xs[seg] + (xs[seg + 1] - xs[seg]) * frac,
            ys[seg] + (ys[seg + 1] - ys[seg]) * frac,
            heads[seg],
        )

    @classmethod
    def build(
        cls,
        world: World,
        cfg: SimConfig,
        *,
        anchors: Sequence[BaseGeometry] = (),
    ) -> DriveNetwork:
        """Flatten a world and choose its core and zones.

        `anchors` are hazard areas a scenario cares about. Zones are placed in
        pairs across the roads that cross them, so a meaningful share of
        seeded trips routes through the hazard -- which is what makes a
        baseline run fail visibly and an advised run succeed.
        """
        graph = world.graph
        edges = graph.edges
        if not edges:
            # Checked before reaching into igraph, so an empty world reports
            # what is wrong with it rather than an AttributeError from two
            # layers down.
            raise NetworkError(f"{world.city} has no edges to drive on")
        igraph_obj = graph.graph
        node_count = igraph_obj.vcount()

        head = tuple(edge.v for edge in edges)
        tail = tuple(edge.u for edge in edges)

        buckets: list[list[int]] = [[] for _ in range(node_count)]
        for edge in edges:
            buckets[edge.u].append(edge.id)
        out_edges = tuple(tuple(sorted(bucket)) for bucket in buckets)

        length_mm = tuple(max(1, round(edge.length_m * MM_PER_M)) for edge in edges)
        free_flow_s = tuple(edge.free_flow_s for edge in edges)
        free_flow_mm_s = tuple(
            max(1, round(edge.speed_kph * 1000.0 / 3600.0 * MM_PER_M)) for edge in edges
        )
        capacity = tuple(max(1, int(edge.length_m // METRES_PER_VEHICLE)) for edge in edges)

        congestion = SeedTree(cfg.seed).stream("congestion", "base")
        base_step_mm = tuple(
            max(1, round(mm * congestion.uniform(cfg.congestion_low, cfg.congestion_high)))
            for mm in free_flow_mm_s
        )

        geom_x, geom_y, geom_cum_mm, geom_head = _flatten_geometry(edges, length_mm)

        by_osmid: dict[str, list[int]] = {}
        for edge in edges:
            by_osmid.setdefault(edge.osmid, []).append(edge.id)
        osmid_edges = {name: tuple(ids) for name, ids in by_osmid.items()}

        core = _largest_strong_core(igraph_obj)
        if len(core) < 2:
            raise NetworkError(
                f"{world.city}'s largest strongly connected component has "
                f"{len(core)} vertices; a simulation needs at least 2"
            )
        core_set = frozenset(core)
        core_edges = tuple(edge.id for edge in edges if edge.u in core_set and edge.v in core_set)
        if not core_edges:
            raise NetworkError(f"{world.city} has no edges inside its strongly connected core")

        partial = cls(
            world=world,
            out_edges=out_edges,
            head=head,
            tail=tail,
            length_mm=length_mm,
            free_flow_s=free_flow_s,
            free_flow_mm_s=free_flow_mm_s,
            base_step_mm=base_step_mm,
            capacity=capacity,
            geom_x=geom_x,
            geom_y=geom_y,
            geom_cum_mm=geom_cum_mm,
            geom_head=geom_head,
            core=core,
            zones=(),
            core_edges=core_edges,
            osmid_edges=osmid_edges,
        )
        zones = select_zones(partial, cfg, anchors=anchors)
        return cls(
            world=world,
            out_edges=out_edges,
            head=head,
            tail=tail,
            length_mm=length_mm,
            free_flow_s=free_flow_s,
            free_flow_mm_s=free_flow_mm_s,
            base_step_mm=base_step_mm,
            capacity=capacity,
            geom_x=geom_x,
            geom_y=geom_y,
            geom_cum_mm=geom_cum_mm,
            geom_head=geom_head,
            core=core,
            zones=zones,
            core_edges=core_edges,
            osmid_edges=osmid_edges,
        )


def _flatten_geometry(
    edges: Sequence[object], length_mm: Sequence[int]
) -> tuple[
    tuple[tuple[float, ...], ...],
    tuple[tuple[float, ...], ...],
    tuple[tuple[int, ...], ...],
    tuple[tuple[float, ...], ...],
]:
    """Per-edge coordinate, cumulative-length and bearing tuples."""
    all_x: list[tuple[float, ...]] = []
    all_y: list[tuple[float, ...]] = []
    all_cum: list[tuple[int, ...]] = []
    all_head: list[tuple[float, ...]] = []
    for edge, total_mm in zip(edges, length_mm, strict=True):
        coords = list(edge.geometry.coords)  # type: ignore[attr-defined]
        if len(coords) < 2:
            coords = [coords[0], coords[0]]
        xs = tuple(float(c[0]) for c in coords)
        ys = tuple(float(c[1]) for c in coords)
        # Chord lengths in metres, then rescaled so the final cumulative value
        # is exactly the edge's own length: the geodesic length from the world
        # build is authoritative, and the loop must not disagree with it.
        raw: list[float] = [0.0]
        for i in range(len(xs) - 1):
            raw.append(raw[-1] + _chord_m(xs[i], ys[i], xs[i + 1], ys[i + 1]))
        scale = (total_mm / raw[-1]) if raw[-1] > 0 else 0.0
        cum = tuple(round(value * scale) for value in raw)
        cum = (*cum[:-1], total_mm)
        heads = tuple(_bearing(xs[i], ys[i], xs[i + 1], ys[i + 1]) for i in range(len(xs) - 1))
        all_x.append(xs)
        all_y.append(ys)
        all_cum.append(cum)
        all_head.append(heads or (0.0,))
    return tuple(all_x), tuple(all_y), tuple(all_cum), tuple(all_head)


def _chord_m(x0: float, y0: float, x1: float, y1: float) -> float:
    """Local flat-earth chord length. Fine inside one city bbox."""
    mid_lat = math.radians((y0 + y1) / 2.0)
    dx = (x1 - x0) * M_PER_DEG_LON_EQUATOR * math.cos(mid_lat)
    dy = (y1 - y0) * M_PER_DEG_LAT
    return math.hypot(dx, dy)


def _bearing(x0: float, y0: float, x1: float, y1: float) -> float:
    """Compass bearing in degrees, 0 = north, clockwise -- the viewer's convention."""
    mid_lat = math.radians((y0 + y1) / 2.0)
    dx = (x1 - x0) * math.cos(mid_lat)
    dy = y1 - y0
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _largest_strong_core(igraph_obj: object) -> tuple[int, ...]:
    """Vertices of the largest strongly connected component, ascending."""
    components = igraph_obj.connected_components(mode="strong")  # type: ignore[attr-defined]
    best: list[int] = []
    for component in components:
        if len(component) > len(best):
            best = list(component)
    return tuple(sorted(best))


def select_zones(
    net: DriveNetwork,
    cfg: SimConfig,
    *,
    anchors: Sequence[BaseGeometry] = (),
) -> tuple[int, ...]:
    """Choose destination zones, biased to make trips cross the anchors.

    Two groups. First, for every road that crosses an anchor area, a pair of
    zones placed on opposite sides of it roughly `ANCHOR_REACH_M` away, so the
    shortest route between the pair uses the crossing. Second, a seeded
    uniform sample of the core to fill the rest, which keeps ordinary traffic
    spread across the city rather than shuttling through the hazard.

    Falls back to a pure uniform sample when there are no anchors, which is
    what the fixture-graph unit tests use.
    """
    core = net.core
    core_set = frozenset(core)
    chosen: list[int] = []
    seen: set[int] = set()

    def take(node: int) -> None:
        if node in core_set and node not in seen:
            seen.add(node)
            chosen.append(node)

    for anchor in anchors:
        for near, far in _crossing_pairs(net, anchor):
            if len(seen) >= cfg.zones:
                break
            take(near)
            take(far)

    # Fill the remainder uniformly. `sample` over a sorted tuple, so the draw
    # depends on the seed and nothing else.
    remaining = cfg.zones - len(chosen)
    if remaining > 0:
        pool = tuple(node for node in core if node not in seen)
        rng = SeedTree(cfg.seed).stream("zones")
        if pool:
            picked = rng.sample(pool, min(remaining, len(pool)))
            for node in picked:
                take(node)

    if len(chosen) < 2:
        raise NetworkError(
            f"could only place {len(chosen)} zones in {net.world.city}; "
            "the strongly connected core is too small to simulate trips"
        )
    return tuple(sorted(chosen))


def _crossing_pairs(net: DriveNetwork, anchor: BaseGeometry) -> list[tuple[int, int]]:
    """Zone pairs straddling each road that crosses an anchor area.

    The crossing edge itself defines the two sides: `tail` is before the
    water, `head` is after it. Walking away from each endpoint along the
    edge's own direction finds a node on that side, far enough out that the
    crossing is the sensible way between them.
    """
    shape = as_shapely(anchor)
    if shape.is_empty:
        return []
    crossing = net.world.graph.edge_ids_in_polygon(shape)
    if not crossing:
        return []

    core_set = frozenset(net.core)
    pairs: list[tuple[int, int]] = []
    used: set[int] = set()
    for edge_id in crossing:
        u, v = net.tail[edge_id], net.head[edge_id]
        if u not in core_set or v not in core_set or u in used or v in used:
            continue
        ux, uy = net.world.graph.position_of(u)
        vx, vy = net.world.graph.position_of(v)
        # Direction of travel across the hazard, as a local metric vector.
        mid_lat = math.radians((uy + vy) / 2.0)
        dx = (vx - ux) * math.cos(mid_lat)
        dy = vy - uy
        norm = math.hypot(dx, dy)
        if norm == 0.0:
            continue
        dx, dy = dx / norm, dy / norm

        before = _reach_along(net, u, -dx, -dy, mid_lat)
        after = _reach_along(net, v, dx, dy, mid_lat)
        if before is None or after is None or before == after:
            continue
        used.update({u, v, before, after})
        pairs.append((before, after))
    return pairs


def _reach_along(
    net: DriveNetwork, origin: int, dx: float, dy: float, mid_lat: float
) -> int | None:
    """The core vertex furthest along `(dx, dy)` from `origin`, within reach.

    A straight-line projection rather than a graph walk: it only has to find
    *a* node plausibly on that side of the water, and the measured crossing
    fraction is what decides whether the choice was good.
    """
    ox, oy = net.world.graph.position_of(origin)
    cos_lat = math.cos(mid_lat)
    best: int | None = None
    best_score = 0.0
    for node in net.core:
        nx, ny = net.world.graph.position_of(node)
        ex = (nx - ox) * M_PER_DEG_LON_EQUATOR * cos_lat
        ey = (ny - oy) * M_PER_DEG_LAT
        distance = math.hypot(ex, ey)
        if distance < ANCHOR_MIN_M or distance > ANCHOR_REACH_M:
            continue
        # Favour nodes both far away and well aligned with the crossing.
        alignment = (ex * dx + ey * dy) / distance
        if alignment <= 0.5:
            continue
        score = alignment * distance
        if score > best_score:
            best_score, best = score, node
    return best
