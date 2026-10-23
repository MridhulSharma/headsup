"""The road graph: edge ids, free-flow times, and the two queries.

`edges_in_polygon` is the one with teeth. An edge it misses is a road an
advisory silently fails to cover, so the H3 pass is checked against a
brute-force shapely scan as well as against hand-counted expectations on the
fixture grid.
"""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, Point

from fixtures.world import (
    BLOCK_M,
    CITY,
    EDGES,
    HOSPITAL_ACCESS_EDGES,
    NODES,
    SIGNALIZED,
    advisory_box,
    at,
    block_box,
    brute_force_edge_ids,
    grid_graph,
    grid_world,
    street_box,
)
from headsup.ingest.cities import CITIES
from headsup.world import BOXES, RoadGraph, WorldError, box
from headsup.world.graph import (
    DEFAULT_SPEED_KPH,
    FALLBACK_SPEED_KPH,
    MPS_PER_KPH,
    first_tag,
    geodesic_length_m,
    is_signalized,
    parse_maxspeed,
    speed_kph,
    tag_values,
)

#: The two directions of the street from n05 to n09, the hospital block's
#: southern edge. Edge ids are stable: the graph is built in OSM id order.
SOUTH_STREET = ((1.0, 1.0), (2.0, 1.0))


def test_the_fixture_graph_has_the_shape_the_tests_assume() -> None:
    graph = grid_graph()
    assert (graph.node_count, graph.edge_count) == (NODES, EDGES)
    assert graph.city == CITY
    assert graph.edge_count == len(graph.edges)


def test_every_edge_id_is_its_index_in_both_copies() -> None:
    """The igraph edge index and the `edges` index have to stay equal."""
    graph = grid_graph()
    for edge_id, edge in enumerate(graph.edges):
        assert edge.id == edge_id
        assert graph.graph.es[edge_id]["eid"] == edge_id
        assert graph.edge(edge_id) is edge


def test_edges_carry_length_and_free_flow_time() -> None:
    graph = grid_graph()
    edge = graph.edge(0)
    assert edge.length_m == pytest.approx(BLOCK_M, abs=1.0)
    # 100 m at 30 mph (48.28 km/h) is 7.5 seconds.
    assert edge.free_flow_s == pytest.approx(edge.length_m / edge.speed_mps)
    assert edge.free_flow_s == pytest.approx(7.46, abs=0.1)
    assert graph.graph.es[0]["free_flow_s"] == pytest.approx(edge.free_flow_s)


# --- signalised junctions --------------------------------------------------


def test_the_node_traffic_signals_tag_survives_the_igraph_copy() -> None:
    """OSM marks a signal-controlled junction on the *node*, and it is kept.

    It used to be dropped: only `x` and `y` came across, so every
    intersection inside a SIGNAL_OUTAGE area counted as a dark signal,
    including the stop-sign corners that have no signal to lose. The fixture's
    two middle avenues are signalised and the outer two are not, which is what
    makes that difference visible.
    """
    graph = grid_graph()
    assert graph.signalized_nodes == SIGNALIZED
    # Eight of sixteen: the tag is carried, not assumed.
    assert len(graph.signalized_nodes) == NODES // 2
    for node in SIGNALIZED:
        assert graph.is_signalized_node(node)
        assert graph.graph.vs[node]["highway"] == "traffic_signals"
    for node in set(range(NODES)) - SIGNALIZED:
        assert not graph.is_signalized_node(node)
        assert graph.graph.vs[node]["highway"] == ""


def test_a_graph_with_no_node_tags_has_no_signals_rather_than_an_error() -> None:
    """A hand-built igraph is still a usable road graph."""
    import igraph

    from headsup.world.h3index import EdgeCellIndex

    bare = RoadGraph("nowhere", igraph.Graph(directed=True), (), EdgeCellIndex(cells={}))
    assert bare.signalized_nodes == frozenset()
    assert not bare.is_signalized_node(0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("traffic_signals", True),
        ("TRAFFIC_SIGNALS", True),
        # OSM tags a junction that is both signalised and a marked crossing
        # with a semicolon list, and simplification can leave several values.
        ("traffic_signals;crossing", True),
        (["crossing", "traffic_signals"], True),
        ("crossing", False),
        ("stop", False),
        # A substring is not the tag: `traffic_signals:direction` is a
        # different key and `motorway_junction` is a different junction.
        ("traffic_signals_vending", False),
        (None, False),
        ([], False),
    ],
)
def test_is_signalized_reads_every_value_of_the_tag(value: object, expected: bool) -> None:
    assert is_signalized(value) is expected


def test_graph_summary_names_the_city_and_the_index() -> None:
    summary = grid_graph().summary()
    assert CITY in summary
    assert "h3 r10" in summary
    assert f"{EDGES} edges" in summary
    assert f"{len(SIGNALIZED)} signalized" in summary


def test_an_unknown_edge_id_is_an_error_not_an_index_error() -> None:
    with pytest.raises(WorldError, match="no edge 999"):
        grid_graph().edge(999)


def test_a_missing_cache_file_says_how_to_build_it() -> None:
    with pytest.raises(WorldError, match="hu world build"):
        RoadGraph.from_graphml("data/cache/nowhere_drive.graphml")


# --- speeds ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("maxspeed", "expected"),
    [
        ("35 mph", 56.3),
        ("30 mph", 48.3),
        (["45 mph", "35 mph"], 56.3),
        ("50", 50.0),
        (None, DEFAULT_SPEED_KPH["residential"]),
        ("", DEFAULT_SPEED_KPH["residential"]),
        ("none", DEFAULT_SPEED_KPH["residential"]),
        ("signals", DEFAULT_SPEED_KPH["residential"]),
    ],
)
def test_speed_comes_from_maxspeed_then_the_class(maxspeed, expected) -> None:
    assert speed_kph("residential", maxspeed) == pytest.approx(expected, abs=0.1)


def test_an_untagged_class_falls_back_rather_than_stopping_the_vehicle() -> None:
    assert speed_kph("escalator") == FALLBACK_SPEED_KPH
    assert speed_kph(None) == FALLBACK_SPEED_KPH
    assert speed_kph(["motorway", "trunk"]) == DEFAULT_SPEED_KPH["trunk"]


# --- merged ways, read the same way twice ----------------------------------


def test_a_merged_edge_reads_the_same_whichever_order_its_tags_arrive_in() -> None:
    """OSMnx builds these lists from a set, so their order is not stable.

    Two builds of the same city were found writing `['30 mph', '35 mph']` and
    `['35 mph', '30 mph']` for one edge. A speed read off element zero changed
    between builds, which would have made the simulator's trajectory hash
    depend on nothing but the process.
    """
    assert speed_kph("residential", ["30 mph", "35 mph"]) == speed_kph(
        "residential", ["35 mph", "30 mph"]
    )
    assert speed_kph(["residential", "secondary"]) == speed_kph(["secondary", "residential"])
    assert first_tag(["B St", "A St"]) == first_tag(["A St", "B St"]) == "A St"


def test_the_slowest_of_a_merged_edges_speeds_governs() -> None:
    """A chain of ways cannot free-flow faster than its slowest link."""
    assert speed_kph("residential", ["45 mph", "25 mph"]) == pytest.approx(40.2, abs=0.1)
    assert speed_kph(["residential", "secondary"]) == DEFAULT_SPEED_KPH["residential"]


def test_one_unparseable_speed_does_not_hide_a_real_one() -> None:
    assert speed_kph("residential", ["none", "35 mph"]) == pytest.approx(56.3, abs=0.1)
    assert speed_kph("residential", ["none", "signals"]) == DEFAULT_SPEED_KPH["residential"]


def test_tag_values_sorts_and_drops_the_gaps() -> None:
    assert tag_values(["b", "a", None]) == ("a", "b")
    assert tag_values("a") == ("a",)
    assert tag_values(None) == ()
    assert tag_values({"c", "a", "b"}) == ("a", "b", "c")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("35 mph", 56.3),
        ("50", 50.0),
        ("50 km/h", 50.0),
        ("none", None),
        ("signals", None),
        ("", None),
        ("0", None),
        ("-5", None),
    ],
)
def test_parse_maxspeed_reads_one_tag(value, expected) -> None:
    parsed = parse_maxspeed(value)
    if expected is None:
        assert parsed is None
    else:
        assert parsed == pytest.approx(expected, abs=0.1)


def test_free_flow_time_is_length_over_speed() -> None:
    assert 100.0 / (DEFAULT_SPEED_KPH["residential"] * MPS_PER_KPH) == pytest.approx(9.0, abs=0.1)


def test_geodesic_length_measures_a_block_in_metres() -> None:
    west, south = at(0, 0)
    east, _north = at(1, 0)
    assert geodesic_length_m(LineString([(west, south), (east, south)])) == pytest.approx(
        BLOCK_M, abs=1.0
    )
    assert geodesic_length_m(LineString()) == 0.0


# --- edges_in_polygon ------------------------------------------------------


def test_a_box_over_one_street_returns_exactly_that_street() -> None:
    """Both directions of one street, and nothing from the intersections."""
    graph = grid_graph()
    shape = street_box(*SOUTH_STREET)
    edges = graph.edges_in_polygon(shape)
    assert len(edges) == 2
    assert {edge.u for edge in edges} == {edge.v for edge in edges}
    assert all(edge.name == "W 1 St" for edge in edges)
    assert graph.edge_ids_in_polygon(shape) == brute_force_edge_ids(graph, shape)


def test_a_box_over_one_block_returns_its_streets_and_the_ways_in() -> None:
    """A polygon over a block reaches its four intersections, so it covers
    the block's own four streets *and* the eight streets leading out of them:
    8 + 16 directed edges, which is the block's streets plus exactly the
    hospital's access edges."""
    graph = grid_graph()
    shape = block_box(0.98, 0.98, 2.02, 2.02)
    edges = graph.edges_in_polygon(shape)
    assert len(edges) == 24
    assert brute_force_edge_ids(graph, shape) == tuple(edge.id for edge in edges)
    lon, lat = at(1.5, 1.5)
    access = {edge.id for edge in graph.access_edges(lon, lat)}
    own = {edge.id for edge in edges} - access
    assert len(access) == HOSPITAL_ACCESS_EDGES
    assert len(own) == 8


def test_a_polygon_over_the_whole_grid_returns_every_edge() -> None:
    graph = grid_graph()
    assert len(graph.edges_in_polygon(block_box(-1, -1, 4, 4))) == EDGES


def test_a_polygon_off_the_grid_returns_nothing() -> None:
    assert grid_graph().edges_in_polygon(block_box(10, 10, 11, 11)) == ()


def test_an_edge_the_polygon_only_touches_still_counts() -> None:
    """A closure drawn down a street closes that street, not nothing."""
    graph = grid_graph()
    west, south = at(1.0, 1.0)
    east, _north = at(2.0, 1.0)
    street = LineString([(west, south), (east, south)])
    # A box whose southern boundary lies exactly along that street.
    shape = block_box(1.2, 1.0, 1.8, 1.5)
    on_street = {edge.id for edge in graph.edges if edge.geometry.within(street.buffer(1e-9))}
    assert on_street
    assert on_street <= set(graph.edge_ids_in_polygon(shape))


def test_a_contract_polygon_is_accepted_as_well_as_shapely() -> None:
    """The compiler hands over contract models, not shapely geometries."""
    graph = grid_graph()
    assert graph.edge_ids_in_polygon(advisory_box(0.98, 0.98, 2.02, 2.02)) == (
        graph.edge_ids_in_polygon(block_box(0.98, 0.98, 2.02, 2.02))
    )


def test_an_empty_geometry_returns_nothing() -> None:
    from shapely.geometry import Polygon as ShapelyPolygon

    assert grid_graph().edge_ids_in_polygon(ShapelyPolygon()) == ()


def test_an_unusable_geometry_is_an_error() -> None:
    with pytest.raises(WorldError, match="not a usable geometry"):
        grid_graph().edge_ids_in_polygon({"type": "Pyramid", "coordinates": []})


def test_the_h3_pass_is_a_superset_of_the_answer() -> None:
    """H3 may over-answer; it may never under-answer."""
    graph = grid_graph()
    for shape in (
        street_box(*SOUTH_STREET),
        block_box(0.98, 0.98, 2.02, 2.02),
        block_box(1.4, 1.4, 1.6, 1.6),  # smaller than one H3 cell
        block_box(-1, -1, 4, 4),
    ):
        candidates = graph.index.candidates(shape)
        answer = set(graph.edge_ids_in_polygon(shape))
        assert answer <= candidates
        assert answer == set(brute_force_edge_ids(graph, shape))


def test_a_line_geometry_is_queryable_too() -> None:
    """A keep-clear corridor is a line, and still has roads under it."""
    graph = grid_graph()
    west, south = at(1.0, 1.0)
    east, _north = at(2.0, 1.0)
    line = LineString([(west, south), (east, south)])
    ids = graph.edge_ids_in_polygon(line)
    assert ids == brute_force_edge_ids(graph, line)
    assert len(ids) >= 2


# --- nodes and access edges ------------------------------------------------


def test_nearest_node_and_edge_find_the_block_the_hospital_is_on() -> None:
    graph = grid_graph()
    lon, lat = at(1.5, 1.5)
    # The four corners of the block are equidistant from the middle of it, so
    # which one is nearest is arbitrary; that it is one of them is not.
    corners = graph.block_nodes(lon, lat)
    assert len(corners) == 4
    assert graph.nearest_node(lon, lat) in corners
    assert graph.nearest_edge(lon, lat).length_m == pytest.approx(BLOCK_M, abs=1.0)


def test_nodes_near_measures_in_metres_not_degrees() -> None:
    graph = grid_graph()
    lon, lat = at(1.0, 1.0)
    assert len(graph.nodes_near(lon, lat, 10.0)) == 1
    # The four neighbouring intersections are 100 m away, the diagonals 141 m,
    # so a radius between the two picks up the first ring and not the corners.
    assert len(graph.nodes_near(lon, lat, 105.0)) == 5
    assert len(graph.nodes_near(lon, lat, 130.0)) == 5
    assert len(graph.nodes_near(lon, lat, 150.0)) == 9


def test_access_edges_are_the_streets_crossing_the_block_boundary() -> None:
    graph = grid_graph()
    lon, lat = at(1.5, 1.5)
    inside = graph.block_nodes(lon, lat)
    access = graph.access_edges(lon, lat)
    assert len(access) == HOSPITAL_ACCESS_EDGES
    for edge in access:
        assert (edge.u in inside) != (edge.v in inside)
    # The block's own four streets are not ways into it.
    own = {edge.id for edge in graph.edges if edge.u in inside and edge.v in inside}
    assert own and own.isdisjoint({edge.id for edge in access})


def test_a_block_that_swallows_the_graph_falls_back_to_touching_edges() -> None:
    """A fixture-sized graph has no boundary to cross; the rule still works."""
    graph = grid_graph()
    lon, lat = at(1.5, 1.5)
    access = graph.access_edges(lon, lat, radius_m=10_000.0)
    assert len(access) == EDGES


def test_a_poi_set_back_from_the_street_still_has_a_block() -> None:
    graph = grid_graph()
    lon, lat = at(1.5, 1.5)
    assert graph.block_nodes(lon, lat, radius_m=1.0) == frozenset({graph.nearest_node(lon, lat)})
    assert graph.access_edges(lon, lat, radius_m=1.0)


def test_an_empty_graph_cannot_answer_nearest() -> None:
    import igraph

    from headsup.world.h3index import EdgeCellIndex

    empty = RoadGraph("nowhere", igraph.Graph(directed=True), (), EdgeCellIndex(cells={}))
    with pytest.raises(WorldError, match="no nodes"):
        empty.nearest_node(0.0, 0.0)
    with pytest.raises(WorldError, match="no edges"):
        empty.nearest_edge(0.0, 0.0)


# --- rows for PostGIS ------------------------------------------------------


def test_edge_rows_carry_wkt_and_osm_node_ids() -> None:
    row = next(iter(grid_graph().edge_rows()))
    assert row["city"] == CITY
    assert row["wkt"].startswith("LINESTRING")
    assert row["from_node"].isdigit() and row["to_node"].isdigit()
    assert row["length_m"] > 0 and row["free_flow_s"] > 0


# --- the bbox registry -----------------------------------------------------


def test_every_ingested_city_has_a_bbox() -> None:
    """The two registries have to agree, or `hu world build` cannot run S1."""
    assert set(BOXES) == set(CITIES)


def test_the_bboxes_are_the_right_way_up_and_city_sized() -> None:
    for key, bbox in BOXES.items():
        assert bbox.west < bbox.east and bbox.south < bbox.north
        assert 1.0 < bbox.width_km < 25.0, key
        assert 1.0 < bbox.height_km < 25.0, key
        assert bbox.to_shapely().is_valid


def test_san_antonio_covers_the_s1_salado_creek_gauges() -> None:
    """The gauges the S1 recording replays have to be on the graph."""
    bbox = box("san_antonio")
    assert bbox.contains(-98.5656, 29.6308)  # Salado Ck at Wilderness Rd
    assert bbox.contains(-98.5197, 29.5639)  # Salado Ck at Blanco Rd


def test_austin_covers_downtown() -> None:
    assert box("austin").contains(-97.7431, 30.2672)  # the Capitol


def test_a_transposed_bbox_is_refused() -> None:
    from headsup.world import BBox

    with pytest.raises(ValueError, match="not west of"):
        BBox(west=-97.0, south=30.0, east=-98.0, north=30.1)
    with pytest.raises(ValueError, match="not south of"):
        BBox(west=-98.0, south=30.2, east=-97.9, north=30.1)


def test_an_unknown_city_lists_the_known_ones() -> None:
    with pytest.raises(ValueError, match="austin, san_antonio"):
        box("dallas")


def test_a_point_outside_the_bbox_is_not_in_it() -> None:
    assert not box("austin").contains(-98.5, 29.5)


# --- the world bundle ------------------------------------------------------


def test_the_world_loads_its_pois_with_access_edges_resolved() -> None:
    world = grid_world()
    assert len(world.pois) == 3
    assert len(world.critical_pois) == 2
    assert all(poi.access_edge_ids for poi in world.pois)
    assert "3 POIs" in world.summary()


def test_an_unknown_poi_id_is_an_error() -> None:
    with pytest.raises(WorldError, match="no POI"):
        grid_world().poi("poi_nope")


def test_the_point_helper_lands_inside_the_grid() -> None:
    lon, lat = at(1.5, 1.5)
    assert grid_graph().index.cell_for(lon, lat)
    assert Point(lon, lat).within(block_box(0, 0, 3, 3))
