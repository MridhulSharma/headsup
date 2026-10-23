"""Advisory to mask: one case per action type, plus the content key.

Two actions deliberately leave edge weights alone -- DENSITY_CAP because it is
a queue rather than a cost, KEEP_CLEAR because it forbids stopping rather than
passing. Both are asserted here, because "re-reads as an oversight" is exactly
how they would get quietly added later.
"""

from __future__ import annotations

import pytest

from fixtures.sim import advisory_for, barrier_edges, grid_net, tiny_config
from fixtures.world import SIGNALIZED, block_box, grid_world
from headsup.contracts import ActionType
from headsup.sim.masks import EMPTY_MASK, Mask, _keyed, merge, resolve, weights_for

INF = float("inf")
CFG = tiny_config()


def _resolve(action_type: ActionType, **kwargs):
    return resolve(advisory_for(action_type, **kwargs), grid_net(), CFG)


def test_no_entry_closes_exactly_the_edges_under_the_polygon() -> None:
    mask = _resolve(ActionType.NO_ENTRY)
    assert mask.no_entry == barrier_edges()
    assert not mask.speed_cap_mm_s
    assert not mask.keep_clear_edges


def test_no_entry_weights_are_infinite_not_merely_large() -> None:
    """A finite sentinel would let Dijkstra route *through* a closure.

    With a large-but-finite penalty a long enough detour makes the closed edge
    look attractive again, and the NO_ENTRY guarantee quietly becomes a
    preference.
    """
    net = grid_net()
    mask = _resolve(ActionType.NO_ENTRY)
    weights = weights_for(mask, net)
    for edge in mask.no_entry:
        assert weights[edge] == INF
    assert all(weights[edge] < INF for edge in range(net.edge_count) if edge not in mask.no_entry)


def test_speed_cap_scales_the_weight_and_leaves_the_edge_open() -> None:
    net = grid_net()
    mask = _resolve(ActionType.SPEED_CAP, params={"speed_mph": 10.0})
    assert mask.speed_cap_mm_s
    assert not mask.no_entry
    weights = weights_for(mask, net)
    for edge, capped in mask.speed_cap_mm_s:
        assert capped < net.free_flow_mm_s[edge]
        assert weights[edge] > net.free_flow_s[edge]
        assert weights[edge] < INF


def test_a_speed_cap_above_the_free_flow_speed_changes_nothing() -> None:
    """Capping a 48 km/h street at 60 mph is not a restriction."""
    mask = _resolve(ActionType.SPEED_CAP, params={"speed_mph": 60.0})
    assert mask.speed_cap_mm_s == ()


def test_all_way_stop_penalises_incoming_edges_only() -> None:
    net = grid_net()
    mask = _resolve(ActionType.ALL_WAY_STOP, params={"delay_s": 4})
    assert mask.all_way_stop
    weights = weights_for(mask, net)
    delayed = {edge for edge in range(net.edge_count) if net.head[edge] in mask.all_way_stop}
    for edge in delayed:
        assert weights[edge] == pytest.approx(net.free_flow_s[edge] + 4)
    for edge in range(net.edge_count):
        if edge not in delayed:
            assert weights[edge] == pytest.approx(net.free_flow_s[edge])


def test_an_all_way_stop_names_signalised_junctions_and_steers_around_them() -> None:
    """The delay lands on the dark signals, so the stop-sign corners win.

    "Treat it as an all-way stop" is an instruction about a junction that had
    a signal. Putting the delay on every intersection in the area would both
    charge a cost nothing changed and flatten the weights again, which is
    exactly the routing advantage the unsignalised streets are supposed to
    have under an outage.
    """
    net = grid_net()
    whole_grid = block_box(-0.2, -0.2, 3.2, 3.2)
    mask = _resolve(
        ActionType.ALL_WAY_STOP, geometry=whole_grid.__geo_interface__, params={"delay_s": 4}
    )
    assert mask.all_way_stop == SIGNALIZED

    weights = weights_for(mask, net)
    into_signal = [edge for edge in range(net.edge_count) if net.head[edge] in SIGNALIZED]
    into_corner = [edge for edge in range(net.edge_count) if net.head[edge] not in SIGNALIZED]
    assert into_signal and into_corner
    for edge in into_signal:
        assert weights[edge] == pytest.approx(net.free_flow_s[edge] + 4)
    for edge in into_corner:
        assert weights[edge] == pytest.approx(net.free_flow_s[edge])


def test_an_all_way_stop_still_takes_a_node_the_advisory_names_itself() -> None:
    """`applies_to.nodes` is the advisory's own word, signal or not.

    The signal filter removes intersections the *geometry* happened to clip.
    A node an advisory names by osmid was chosen, and dropping it would
    silently discard part of what was asked for.
    """
    unsignalised = sorted(set(range(grid_net().node_count)) - SIGNALIZED)[0]
    osmid = grid_world().graph.osmid_of(unsignalised)
    mask = _resolve(ActionType.ALL_WAY_STOP, nodes=(osmid,), params={"delay_s": 4})
    assert unsignalised in mask.all_way_stop


def test_density_cap_is_a_queue_and_never_touches_the_weights() -> None:
    """Occupancy in the weights would change them every tick.

    That would invalidate the label cache continuously, which is the single
    most expensive mistake available in this design.
    """
    net = grid_net()
    mask = _resolve(ActionType.DENSITY_CAP, params={"max_vehicles": 2})
    assert mask.density_cap
    assert weights_for(mask, net) == list(net.free_flow_s)


def test_keep_clear_never_touches_the_weights_either() -> None:
    """It forbids stopping, not passing, so routing is unaffected."""
    net = grid_net()
    mask = _resolve(ActionType.KEEP_CLEAR, params={"width_m": 30.0})
    assert mask.keep_clear_edges
    assert weights_for(mask, net) == list(net.free_flow_s)


def test_pause_service_resolves_rather_than_raising() -> None:
    """Not in the D2.2 brief, but it is in `ActionType`.

    An unhandled member would raise on a legitimately published advisory.
    """
    mask = _resolve(ActionType.PAUSE_SERVICE)
    assert mask.paused


def test_applies_to_edges_are_matched_by_osmid_not_by_index() -> None:
    """An advisory carries `Edge.osmid` strings, and osmids are not unique.

    `tests/fixtures/advisory_s1_flood.json` carries "58291-58294-0", which is
    the `u-v-key` fallback form of an osmid, not an edge index.
    """
    net = grid_net()
    osmid, expected = next(iter(sorted(net.osmid_edges.items())))
    assert len(expected) >= 1

    # A polygon far off the grid, so the osmid is the *only* thing that can
    # contribute an edge. If osmids were read as indices this set would be
    # wrong rather than empty, which is the mistake worth catching.
    elsewhere = block_box(90.0, 90.0, 90.1, 90.1).__geo_interface__
    nothing = resolve(advisory_for(ActionType.NO_ENTRY, geometry=elsewhere), net, CFG)
    assert nothing.no_entry == frozenset()

    named = resolve(advisory_for(ActionType.NO_ENTRY, geometry=elsewhere, edges=(osmid,)), net, CFG)
    assert named.no_entry == frozenset(expected)

    # One osmid can name several segments, so the mapping is one-to-many.
    widest = max(net.osmid_edges.values(), key=len)
    assert len(widest) > 1


def test_the_content_key_ignores_parameter_insertion_order() -> None:
    """Two advisories expressing the same restriction share a label cache."""
    first = resolve(
        advisory_for(ActionType.SPEED_CAP, params={"speed_mph": 15.0, "radius_m": 50.0}),
        grid_net(),
        CFG,
    )
    second = resolve(
        advisory_for(ActionType.SPEED_CAP, params={"radius_m": 50.0, "speed_mph": 15.0}),
        grid_net(),
        CFG,
    )
    assert first.key == second.key


def test_different_restrictions_get_different_keys() -> None:
    a = _resolve(ActionType.NO_ENTRY)
    b = _resolve(ActionType.SPEED_CAP, params={"speed_mph": 10.0})
    assert a.key != b.key != EMPTY_MASK.key


def test_merge_is_insensitive_to_order() -> None:
    a = _resolve(ActionType.NO_ENTRY)
    b = _resolve(ActionType.SPEED_CAP, params={"speed_mph": 10.0})
    assert merge((a, b)).key == merge((b, a)).key


def test_merge_takes_the_stricter_of_two_caps() -> None:
    edges = sorted(barrier_edges())[:2]
    loose = _keyed(Mask(speed_cap_mm_s=tuple((edge, 9000) for edge in edges)))
    tight = _keyed(Mask(speed_cap_mm_s=tuple((edge, 4000) for edge in edges)))
    merged = merge((loose, tight))
    assert dict(merged.speed_cap_mm_s) == dict.fromkeys(edges, 4000)

    roomy = _keyed(Mask(density_cap=((3, 8),)))
    cramped = _keyed(Mask(density_cap=((3, 2),)))
    assert dict(merge((roomy, cramped)).density_cap) == {3: 2}

    brief = _keyed(Mask(all_way_stop=frozenset({3}), stop_delay_s=((3, 2),)))
    long = _keyed(Mask(all_way_stop=frozenset({3}), stop_delay_s=((3, 9),)))
    assert dict(merge((brief, long)).stop_delay_s) == {3: 9}


def test_merging_nothing_gives_the_empty_mask() -> None:
    assert merge(()) is EMPTY_MASK
    assert merge((EMPTY_MASK, EMPTY_MASK)) is EMPTY_MASK


def test_a_corridor_keeps_only_the_intersections_actually_inside_it() -> None:
    """Tighter than every endpoint of every clipped edge.

    A corridor down one street clips edges whose far end is a block away;
    banning that far intersection would take a destination out of service for
    no reason.
    """
    from shapely.geometry import Point

    net = grid_net()
    # A thin band between two avenues: it clips the middle of every east-west
    # street it crosses, but contains no intersection at all.
    between = block_box(2.4, 0.0, 2.6, 3.0)
    mask = resolve(
        advisory_for(
            ActionType.KEEP_CLEAR,
            geometry=between.__geo_interface__,
            params={"width_m": 10.0},
        ),
        net,
        CFG,
    )
    assert mask.keep_clear_edges, "the band should still clip some streets"
    assert mask.keep_clear_nodes == frozenset(), (
        "a band between avenues contains no intersection, so banning one "
        "would take a destination out of service for no reason"
    )

    # A band centred *on* an avenue does contain its intersections, and
    # exactly those: the endpoints that fall inside the area, not every
    # endpoint of every clipped edge.
    position = grid_world().graph.position_of
    on_avenue = block_box(1.9, 0.0, 2.1, 3.0)
    centred = resolve(
        advisory_for(
            ActionType.KEEP_CLEAR,
            geometry=on_avenue.__geo_interface__,
            params={"width_m": 10.0},
        ),
        net,
        CFG,
    )
    endpoints = {net.tail[edge] for edge in centred.keep_clear_edges} | {
        net.head[edge] for edge in centred.keep_clear_edges
    }
    expected = {node for node in endpoints if on_avenue.contains(Point(*position(node)))}
    assert centred.keep_clear_nodes == frozenset(expected)
    assert centred.keep_clear_nodes, "an avenue band should contain intersections"
    assert centred.keep_clear_nodes < endpoints, "it must be tighter than every endpoint"


def test_an_expired_advisory_contributes_nothing() -> None:
    """Expiry is the `valid_from`/`valid_until` window, checked against sim time."""
    from datetime import timedelta

    from samples import T0

    advisory = advisory_for(
        ActionType.NO_ENTRY, valid_from=T0, valid_until=T0 + timedelta(seconds=30)
    )
    assert advisory.is_active(at=T0)
    assert not advisory.is_active(at=T0 + timedelta(seconds=31))
