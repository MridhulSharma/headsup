"""The H3 edge index: the cheap first pass, and why it never under-answers.

A cell set that misses an edge is a road an advisory silently fails to cover,
so these tests are about recall. Precision is shapely's job afterwards.
"""

from __future__ import annotations

import h3
import pytest
from shapely.geometry import LineString, MultiPolygon, Point
from shapely.geometry import Polygon as ShapelyPolygon

from fixtures.world import at, block_box, grid_graph
from headsup.world.h3index import (
    DEFAULT_RESOLUTION,
    SAMPLE_STEP_M,
    EdgeCellIndex,
    cover_cells,
    sample_cells,
)


def test_the_index_is_built_at_block_resolution() -> None:
    index = grid_graph().index
    assert index.resolution == DEFAULT_RESOLUTION == 10
    assert index.step_m == SAMPLE_STEP_M
    # Roughly a city block: a few thousand square metres, not a county.
    assert 10_000 < h3.average_hexagon_area(index.resolution, unit="m^2") < 20_000


def test_every_cell_in_the_index_is_a_valid_cell() -> None:
    index = grid_graph().index
    assert index.cells
    assert all(h3.is_valid_cell(cell) for cell in index.cells)
    assert index.entries >= index.resolution  # sanity: pairs outnumber nothing
    assert len(index) == len(index.cells)


def test_every_edge_is_in_the_index_somewhere() -> None:
    graph = grid_graph()
    indexed = {edge_id for ids in graph.index.cells.values() for edge_id in ids}
    assert indexed == {edge.id for edge in graph.edges}


def test_an_edge_is_indexed_into_the_cells_along_it() -> None:
    """A 100 m street crosses more than one 66 m cell, usually."""
    graph = grid_graph()
    edge = graph.edge(0)
    cells = sample_cells(edge.geometry)
    assert cells
    assert all(edge.id in graph.index.cells[cell] for cell in cells)


def test_sampling_a_long_line_finds_more_cells_than_its_endpoints() -> None:
    west, south = at(0, 0)
    east, _north = at(3, 0)
    line = LineString([(west, south), (east, south)])
    endpoints = {h3.latlng_to_cell(lat, lon, DEFAULT_RESOLUTION) for lon, lat in line.coords}
    assert len(sample_cells(line)) > len(endpoints)


def test_a_cover_includes_the_shapes_own_cells_and_a_ring_around_them() -> None:
    shape = block_box(1, 1, 2, 2)
    inner = sample_cells(shape)
    cover = cover_cells(shape)
    assert inner <= cover
    # Every cell of the shape has all six neighbours in the cover.
    for cell in inner:
        assert set(h3.grid_disk(cell, 1)) <= cover


def test_dilation_can_be_turned_off() -> None:
    shape = block_box(1, 1, 2, 2)
    assert len(cover_cells(shape, dilate=0)) < len(cover_cells(shape, dilate=1))


def test_a_shape_smaller_than_one_cell_still_covers_a_cell() -> None:
    """Containment by cell centre would answer "no cells" here."""
    lon, lat = at(1.5, 1.5)
    tiny = Point(lon, lat).buffer(0.000001)
    assert h3.latlng_to_cell(lat, lon, DEFAULT_RESOLUTION) in cover_cells(tiny)


def test_a_point_and_a_line_are_coverable_too() -> None:
    lon, lat = at(1.0, 1.0)
    assert cover_cells(Point(lon, lat))
    assert cover_cells(LineString([at(1.0, 1.0), at(2.0, 1.0)]))


def test_a_multipolygon_covers_both_of_its_parts() -> None:
    first, second = block_box(0, 0, 1, 1), block_box(2, 2, 3, 3)
    both = cover_cells(MultiPolygon([first, second]))
    assert sample_cells(first) <= both
    assert sample_cells(second) <= both


def test_a_polygon_with_a_hole_covers_the_whole_outline() -> None:
    outer = block_box(0, 0, 3, 3)
    inner = block_box(1, 1, 2, 2)
    donut = ShapelyPolygon(outer.exterior.coords, [inner.exterior.coords])
    cover = cover_cells(donut)
    assert sample_cells(donut.exterior) <= cover
    assert sample_cells(inner.exterior) <= cover


def test_candidates_are_a_superset_of_the_exact_answer() -> None:
    graph = grid_graph()
    for shape in (
        block_box(1.2, 0.98, 1.8, 1.02),
        block_box(1, 1, 2, 2),
        block_box(-1, -1, 4, 4),
        block_box(1.45, 1.45, 1.55, 1.55),
    ):
        exact = {edge.id for edge in graph.edges if shape.intersects(edge.geometry)}
        assert exact <= graph.index.candidates(shape)


def test_candidates_off_the_graph_are_empty() -> None:
    assert grid_graph().index.candidates(block_box(50, 50, 51, 51)) == frozenset()


def test_an_index_over_nothing_is_empty_but_usable() -> None:
    index = EdgeCellIndex.build([])
    assert len(index) == 0
    assert index.entries == 0
    assert index.candidates(block_box(0, 0, 1, 1)) == frozenset()


def test_a_coarser_index_holds_fewer_cells() -> None:
    graph = grid_graph()
    geometries = [edge.geometry for edge in graph.edges]
    coarse = EdgeCellIndex.build(geometries, resolution=8)
    assert len(coarse) < len(graph.index)
    assert coarse.resolution == 8


def test_cell_for_agrees_with_h3() -> None:
    lon, lat = at(1.5, 1.5)
    index = grid_graph().index
    assert index.cell_for(lon, lat) == h3.latlng_to_cell(lat, lon, index.resolution)


def test_a_finer_sample_step_never_loses_a_cell() -> None:
    line = LineString([at(0, 0), at(3, 3)])
    coarse = sample_cells(line, step_m=60.0)
    fine = sample_cells(line, step_m=10.0)
    assert coarse <= fine


@pytest.mark.parametrize("step_m", [0.0, -5.0])
def test_a_zero_step_falls_back_to_the_vertices(step_m) -> None:
    """No densification, no crash: the vertices are still indexed."""
    line = LineString([at(0, 0), at(3, 0)])
    assert sample_cells(line, step_m=step_m)
