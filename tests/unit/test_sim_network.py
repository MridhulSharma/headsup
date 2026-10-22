"""The flattened network must agree with the graph it came from."""

from __future__ import annotations

import math

import pytest

from fixtures.sim import grid_net, tiny_config
from fixtures.world import EDGES, NODES, grid_world
from headsup.sim.network import DriveNetwork, NetworkError


def test_the_csr_matches_the_graphs_own_adjacency() -> None:
    net = grid_net()
    graph = grid_world().graph.graph
    for node in range(net.node_count):
        assert list(net.out_edges[node]) == sorted(graph.incident(node, mode="out"))


def test_out_edges_are_ascending_because_the_tie_break_depends_on_it() -> None:
    net = grid_net()
    for node in range(net.node_count):
        edges = net.out_edges[node]
        assert list(edges) == sorted(edges)


def test_the_flattened_shape_matches_the_fixture() -> None:
    net = grid_net()
    assert net.node_count == NODES
    assert net.edge_count == EDGES


def test_edge_ids_are_igraph_indices() -> None:
    """The invariant the trajectory hash rests on."""
    net = grid_net()
    edges = grid_world().graph.edges
    for edge in edges:
        assert net.tail[edge.id] == edge.u
        assert net.head[edge.id] == edge.v


def test_lengths_round_trip_to_within_a_millimetre() -> None:
    net = grid_net()
    for edge in grid_world().graph.edges:
        assert abs(net.length_mm[edge.id] / 1000.0 - edge.length_m) < 0.001


def test_the_geometry_cumulative_length_ends_exactly_at_the_edge_length() -> None:
    """The geodesic length from the world build is authoritative.

    Interpolation must not disagree with it, or a vehicle's position and its
    progress along the edge drift apart.
    """
    net = grid_net()
    for edge in range(net.edge_count):
        assert net.geom_cum_mm[edge][0] == 0
        assert net.geom_cum_mm[edge][-1] == net.length_mm[edge]


def test_interpolation_hits_both_endpoints_and_stays_between_them() -> None:
    net = grid_net()
    graph = grid_world().graph
    for edge_id in range(net.edge_count):
        geometry = graph.edges[edge_id].geometry
        first, last = geometry.coords[0], geometry.coords[-1]
        start_lon, start_lat, _ = net.interpolate(edge_id, 0)
        end_lon, end_lat, _ = net.interpolate(edge_id, net.length_mm[edge_id])
        assert (start_lon, start_lat) == pytest.approx(first)
        assert (end_lon, end_lat) == pytest.approx(last)

        mid_lon, mid_lat, _ = net.interpolate(edge_id, net.length_mm[edge_id] // 2)
        assert min(first[0], last[0]) - 1e-9 <= mid_lon <= max(first[0], last[0]) + 1e-9
        assert min(first[1], last[1]) - 1e-9 <= mid_lat <= max(first[1], last[1]) + 1e-9


def test_interpolation_clamps_outside_the_edge() -> None:
    net = grid_net()
    assert net.interpolate(0, -500) == net.interpolate(0, 0)
    beyond = net.interpolate(0, net.length_mm[0] + 10_000)
    assert beyond == net.interpolate(0, net.length_mm[0])


def test_heading_is_a_compass_bearing() -> None:
    """0 is north, clockwise: the viewer's convention, not maths convention."""
    net = grid_net()
    graph = grid_world().graph
    for edge_id in range(net.edge_count):
        edge = graph.edges[edge_id]
        _lon, _lat, heading = net.interpolate(edge_id, 0)
        assert 0.0 <= heading < 360.0
        (x0, y0), (x1, y1) = edge.geometry.coords[0], edge.geometry.coords[1]
        if abs(y1 - y0) > abs(x1 - x0):  # mostly north-south
            assert heading == pytest.approx(0.0 if y1 > y0 else 180.0, abs=1.0)
        else:  # mostly east-west
            assert heading == pytest.approx(90.0 if x1 > x0 else 270.0, abs=1.0)


def test_the_core_is_the_largest_strongly_connected_component() -> None:
    net = grid_net()
    components = grid_world().graph.graph.connected_components(mode="strong")
    largest = max(components, key=len)
    assert set(net.core) == set(largest)
    assert list(net.core) == sorted(net.core)


def test_the_fixture_grid_is_one_strong_component() -> None:
    """Unlike the real city, which has 112 -- worth stating so the contrast is visible."""
    assert len(grid_net().core) == NODES


def test_zones_are_drawn_from_the_core_and_are_sorted() -> None:
    net = grid_net()
    assert set(net.zones) <= set(net.core)
    assert list(net.zones) == sorted(net.zones)
    assert len(net.zones) == tiny_config().zones


def test_core_edges_have_both_endpoints_in_the_core() -> None:
    net = grid_net()
    core = set(net.core)
    for edge in net.core_edges:
        assert net.tail[edge] in core
        assert net.head[edge] in core


def test_the_step_table_is_congestion_scaled_and_never_zero() -> None:
    net = grid_net()
    cfg = tiny_config()
    for edge in range(net.edge_count):
        free = net.free_flow_mm_s[edge]
        step = net.base_step_mm[edge]
        assert 1 <= step <= free
        assert step >= math.floor(free * cfg.congestion_low) - 1


def test_the_same_seed_gives_the_same_congestion() -> None:
    first = DriveNetwork.build(grid_world(), tiny_config(seed=5))
    second = DriveNetwork.build(grid_world(), tiny_config(seed=5))
    other = DriveNetwork.build(grid_world(), tiny_config(seed=6))
    assert first.base_step_mm == second.base_step_mm
    assert first.base_step_mm != other.base_step_mm


def test_asking_for_more_zones_than_the_core_holds_is_capped_not_fatal() -> None:
    net = DriveNetwork.build(grid_world(), tiny_config(zones=12, vehicles=12))
    assert len(net.zones) <= NODES


def test_a_world_with_no_edges_is_refused_with_a_useful_message() -> None:
    """The message is what someone reads when their world failed to build."""
    from types import SimpleNamespace

    hollow = SimpleNamespace(city="hollow", graph=SimpleNamespace(edges=()))
    with pytest.raises(NetworkError, match="hollow has no edges to drive on"):
        DriveNetwork.build(hollow, tiny_config())  # type: ignore[arg-type]
