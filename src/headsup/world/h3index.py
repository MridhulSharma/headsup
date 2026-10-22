"""An H3 grid index from cells to the road edges that pass through them.

A hazard polygon covers a few city blocks; the graph has tens of thousands of
edges. Testing every edge against every polygon with shapely is the obvious
way and the wrong one, so a query runs in two passes: H3 narrows the graph
down to the edges in the polygon's cells, then shapely decides exactly which
of those the polygon really touches. The first pass is a dict lookup and may
over-answer; the second pass is exact and is what the answer is based on.

Resolution 10 cells are about 15 000 m2, roughly 66 m from centre to edge -
the scale of a city block, which is the scale advisories are written at.

Keeping the first pass a superset is the whole job, because an edge the index
misses is an edge the advisory silently does not cover. Two things buy that:

* an edge is indexed into the cell of every point along it at `SAMPLE_STEP_M`
  spacing, so no sample on an edge is more than half a step from its
  neighbour;
* a query cell set is every cell that *overlaps* the shape, grown by one ring.
  A step of 20 m is far shorter than a cell, so the cell holding any point on
  an edge is at worst a neighbour of the cell of the nearest sample - and the
  ring makes that neighbour a candidate too.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import h3
from shapely.geometry import base as shapely_base

from headsup.world.bbox import M_PER_DEG_LAT

#: The H3 resolution every edge is indexed at: ~15 000 m2, a city block.
DEFAULT_RESOLUTION = 10

#: Spacing of the sample points an edge is indexed from, in metres.
SAMPLE_STEP_M = 20.0

#: Shapes with no interior to cover cell by cell; handled by sampling instead.
_LINEAR_TYPES = frozenset(
    {"Point", "MultiPoint", "LineString", "MultiLineString", "LinearRing", "GeometryCollection"}
)


def _step_degrees(step_m: float) -> float:
    """A metre spacing as a degree spacing, erring on the side of too fine.

    Shapely works in degrees and does not know about latitude, so one spacing
    has to serve both axes. A degree of longitude in Texas is shorter than a
    degree of latitude, so converting through latitude makes the longitude
    steps shorter than asked for, never longer.
    """
    return step_m / M_PER_DEG_LAT


def sample_cells(
    shape: shapely_base.BaseGeometry,
    *,
    resolution: int = DEFAULT_RESOLUTION,
    step_m: float = SAMPLE_STEP_M,
) -> frozenset[str]:
    """Every cell containing a point of `shape` sampled at `step_m` spacing."""
    dense = shape.segmentize(_step_degrees(step_m)) if step_m > 0 else shape
    cells = {h3.latlng_to_cell(lat, lon, resolution) for lon, lat in _coordinates(dense)}
    return frozenset(cells)


def _coordinates(shape: shapely_base.BaseGeometry) -> Iterable[tuple[float, float]]:
    """Every (lon, lat) vertex of any geometry, nesting and all."""
    if hasattr(shape, "geoms"):
        for part in shape.geoms:
            yield from _coordinates(part)
        return
    if shape.geom_type == "Polygon":
        yield from shape.exterior.coords
        for ring in shape.interiors:
            yield from ring.coords
        return
    yield from shape.coords


def cover_cells(
    shape: shapely_base.BaseGeometry,
    *,
    resolution: int = DEFAULT_RESOLUTION,
    step_m: float = SAMPLE_STEP_M,
    dilate: int = 1,
) -> frozenset[str]:
    """The cells to look an edge up in, for a shape being queried with.

    Deliberately generous: every cell the shape overlaps, every cell its
    outline passes through, and one ring around the result. Over-answering
    costs a shapely test; under-answering loses a road.
    """
    cells: set[str] = set()
    for part in _polygonal_parts(shape):
        cells.update(
            h3.h3shape_to_cells_experimental(h3.geo_to_h3shape(part), resolution, contain="overlap")
        )
    # The outline (and any point or line shape) by sampling: a polygon smaller
    # than a cell can overlap no cell centre at all.
    cells.update(sample_cells(shape, resolution=resolution, step_m=step_m))
    for _ in range(max(dilate, 0)):
        cells.update({ring for cell in tuple(cells) for ring in h3.grid_disk(cell, 1)})
    return frozenset(cells)


def _polygonal_parts(shape: shapely_base.BaseGeometry) -> Iterable[shapely_base.BaseGeometry]:
    """The polygons in a shape, if any; nothing for points and lines."""
    if shape.geom_type in _LINEAR_TYPES:
        if shape.geom_type == "GeometryCollection":
            for part in shape.geoms:
                yield from _polygonal_parts(part)
        return
    if shape.geom_type == "MultiPolygon":
        yield from shape.geoms
        return
    yield shape


@dataclass(frozen=True, slots=True)
class EdgeCellIndex:
    """Cell id to the ids of the edges passing through that cell."""

    cells: Mapping[str, tuple[int, ...]]
    resolution: int = DEFAULT_RESOLUTION
    step_m: float = SAMPLE_STEP_M

    @classmethod
    def build(
        cls,
        geometries: Sequence[shapely_base.BaseGeometry],
        *,
        resolution: int = DEFAULT_RESOLUTION,
        step_m: float = SAMPLE_STEP_M,
    ) -> EdgeCellIndex:
        """Index edge geometries by position in the sequence as the edge id."""
        buckets: dict[str, list[int]] = {}
        for edge_id, geometry in enumerate(geometries):
            for cell in sample_cells(geometry, resolution=resolution, step_m=step_m):
                buckets.setdefault(cell, []).append(edge_id)
        return cls(
            cells={cell: tuple(ids) for cell, ids in buckets.items()},
            resolution=resolution,
            step_m=step_m,
        )

    def __len__(self) -> int:
        """How many cells the graph occupies."""
        return len(self.cells)

    @property
    def entries(self) -> int:
        """Total cell-to-edge pairs: the index's real size."""
        return sum(len(ids) for ids in self.cells.values())

    def candidates(self, shape: shapely_base.BaseGeometry) -> frozenset[int]:
        """Edge ids that might touch `shape`; a superset, not an answer."""
        cells = cover_cells(shape, resolution=self.resolution, step_m=self.step_m)
        found: set[int] = set()
        for cell in cells:
            found.update(self.cells.get(cell, ()))
        return frozenset(found)

    def cell_for(self, lon: float, lat: float) -> str:
        """The cell one coordinate falls in, at this index's resolution."""
        return h3.latlng_to_cell(lat, lon, self.resolution)
