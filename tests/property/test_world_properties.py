"""For ANY polygon over the grid, the H3 pass loses no road.

`edges_in_polygon` is two passes: H3 narrows, shapely decides. The second pass
is exact by construction, so the only way the query can be wrong is for the
first pass to drop an edge the polygon really touches - and that failure is
invisible, because the answer still looks like a list of roads.

So these generate rectangles all over the fixture grid, from slivers thinner
than a lane to boxes bigger than the whole graph, and hold the query to what a
brute-force shapely scan of every edge says. The unit tests check the cases
with hand-counted answers; these check the ones nobody thought of.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from fixtures.world import at, block_box, brute_force_edge_ids, grid_graph

#: Block coordinates to draw rectangles in: a little outside the 3x3 grid of
#: blocks on every side, so polygons that overhang the edge are generated too.
BLOCKS = st.floats(min_value=-0.5, max_value=3.5, allow_nan=False, allow_infinity=False)

#: Rectangle sizes from a sliver (0.001 block = 10 cm) to wider than the grid.
SPANS = st.floats(min_value=0.001, max_value=4.0, allow_nan=False, allow_infinity=False)


@st.composite
def rectangles(draw):
    """A rectangle over the grid, in block coordinates."""
    west = draw(BLOCKS)
    south = draw(BLOCKS)
    return block_box(west, south, west + draw(SPANS), south + draw(SPANS))


@settings(max_examples=500, deadline=None)
@given(shape=rectangles())
def test_the_h3_pass_never_loses_an_edge(shape) -> None:
    graph = grid_graph()
    assert graph.edge_ids_in_polygon(shape) == brute_force_edge_ids(graph, shape)


@settings(max_examples=200, deadline=None)
@given(shape=rectangles())
def test_the_answer_is_sorted_unique_and_in_range(shape) -> None:
    graph = grid_graph()
    ids = graph.edge_ids_in_polygon(shape)
    assert list(ids) == sorted(set(ids))
    assert all(0 <= edge_id < graph.edge_count for edge_id in ids)


@settings(max_examples=200, deadline=None)
@given(shape=rectangles())
def test_the_candidate_set_is_a_superset_of_the_answer(shape) -> None:
    graph = grid_graph()
    assert set(graph.edge_ids_in_polygon(shape)) <= graph.index.candidates(shape)


@settings(max_examples=100, deadline=None)
@given(east=BLOCKS, north=BLOCKS)
def test_every_point_on_the_grid_has_a_block_and_ways_into_it(east, north) -> None:
    """`access_edges` has to answer for any POI position, including off-grid."""
    graph = grid_graph()
    lon, lat = at(east, north)
    assert graph.block_nodes(lon, lat)
    access = graph.access_edges(lon, lat)
    assert access
    assert len(access) <= graph.edge_count
