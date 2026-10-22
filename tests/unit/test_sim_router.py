"""Routing: the greedy next-hop must be the real shortest path.

The whole performance argument depends on this. Storing a cost-to-zone per
vertex instead of a path per vehicle is only legitimate if descending the
label gives the same route a shortest-path call would -- so that is checked
directly against igraph rather than assumed.
"""

from __future__ import annotations

import warnings

import pytest

from fixtures.sim import advisory_for, grid_net, tiny_config
from fixtures.world import grid_world
from headsup.sim.masks import EMPTY_KEY, resolve
from headsup.sim.rng import SeedTree
from headsup.sim.router import Router

INF = float("inf")


@pytest.fixture
def router() -> Router:
    return Router(grid_net(), tiny_config())


def _descend(router: Router, net, mask_key: str, zone_ix: int, source: int):
    """Follow the greedy next-hop to the zone, returning (cost, edges)."""
    weights = grid_world().graph.graph.es["free_flow_s"]
    node = source
    target = net.zones[zone_ix]
    cost = 0.0
    edges: list[int] = []
    while node != target and len(edges) < 500:
        edge = router.next_hop(mask_key, zone_ix, node)
        if edge < 0:
            return None, edges
        cost += weights[edge]
        edges.append(edge)
        node = net.head[edge]
    return (cost if node == target else None), edges


def test_greedy_descent_matches_igraph_shortest_paths(router: Router) -> None:
    net = grid_net()
    graph = grid_world().graph.graph
    weights = list(graph.es["free_flow_s"])
    rng = SeedTree(7).stream("probe")

    checked = 0
    for _ in range(60):
        source = rng.choice(net.core)
        zone_ix = rng.randrange(len(net.zones))
        target = net.zones[zone_ix]
        if source == target:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            truth = graph.distances(source=[source], target=[target], weights=weights)[0][0]
        if truth == INF:
            continue
        cost, _edges = _descend(router, net, EMPTY_KEY, zone_ix, source)
        assert cost is not None
        assert cost == pytest.approx(truth, rel=1e-9)
        checked += 1
    assert checked >= 40, f"only exercised {checked} routable pairs"


def test_a_masked_edge_is_never_selected(router: Router) -> None:
    net = grid_net()
    mask = resolve(advisory_for(), net, tiny_config())
    router.ensure_mask(mask)
    router.refresh(len(net.zones))
    assert mask.no_entry

    for zone_ix in range(len(net.zones)):
        for node in net.core:
            edge = router.next_hop(mask.key, zone_ix, node)
            assert edge not in mask.no_entry


def test_a_stale_label_still_never_selects_a_closed_edge() -> None:
    """Correctness comes from the membership filter, not from the label.

    A mask registered but not yet refreshed falls back to the unmasked label,
    which is the whole reason a new advisory is safe on the tick it lands.
    """
    net = grid_net()
    router = Router(net, tiny_config())
    mask = resolve(advisory_for(), net, tiny_config())
    router.ensure_mask(mask)  # deliberately no refresh

    for zone_ix in range(len(net.zones)):
        for node in net.core:
            assert router.next_hop(mask.key, zone_ix, node) not in mask.no_entry


def test_a_cost_tie_is_broken_by_the_lower_edge_id(router: Router) -> None:
    """`out_edges` is sorted ascending, and the comparison is strict `<`.

    On a uniform grid ties are common, so this is the rule that keeps two runs
    identical rather than merely equivalent.
    """
    net = grid_net()
    for zone_ix in range(len(net.zones)):
        for node in net.core:
            chosen = router.next_hop(EMPTY_KEY, zone_ix, node)
            if chosen < 0:
                continue
            label = router.label(EMPTY_KEY, zone_ix)
            weights = grid_world().graph.graph.es["free_flow_s"]
            best = min(
                weights[edge] + label[net.head[edge]]
                for edge in net.out_edges[node]
                if label[net.head[edge]] != INF
            )
            equals = [
                edge
                for edge in net.out_edges[node]
                if label[net.head[edge]] != INF and weights[edge] + label[net.head[edge]] == best
            ]
            assert chosen == min(equals)


def test_refresh_converges_within_the_expected_number_of_ticks() -> None:
    net = grid_net()
    cfg = tiny_config(label_budget=2)
    router = Router(net, cfg)
    mask = resolve(advisory_for(), net, cfg)
    router.ensure_mask(mask)

    expected = -(-len(net.zones) // cfg.label_budget)  # ceil
    ticks = 0
    while router.pending and ticks < expected + 5:
        computed = router.refresh()
        assert computed <= cfg.label_budget
        ticks += 1
    assert not router.pending
    assert ticks == expected


def test_an_unreachable_zone_reports_itself_unreachable() -> None:
    """A fully enclosed destination must be detectable, not silently wrong."""
    net = grid_net()
    cfg = tiny_config()
    router = Router(net, cfg)
    zone_ix = 0
    zone = net.zones[zone_ix]
    sealing = frozenset(edge for edge, node in enumerate(net.head) if node == zone)
    assert sealing

    from headsup.sim.masks import Mask, _keyed

    mask = _keyed(Mask(no_entry=sealing))
    router.ensure_mask(mask)
    router.refresh(len(net.zones))
    others = [node for node in net.core if node != zone]
    assert not router.reachable(mask.key, zone_ix, others[0])


def test_no_igraph_warning_escapes_a_refresh() -> None:
    """A graph with several strong components warns on every Dijkstra.

    Unsuppressed that is thousands of warnings a run, and a hard failure under
    any `-W error` configuration.
    """
    net = grid_net()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        router = Router(net, tiny_config())
        mask = resolve(advisory_for(), net, tiny_config())
        router.ensure_mask(mask)
        router.refresh(len(net.zones))


def test_forgetting_a_mask_does_not_evict_another_owners_mask() -> None:
    """One router serves the live and the ghost fleet.

    If each owner pruned on its own view, the two would evict each other's
    masks every tick and recompute the labels on the next one.
    """
    net = grid_net()
    cfg = tiny_config()
    router = Router(net, cfg)
    mask = resolve(advisory_for(), net, cfg)
    router.ensure_mask(mask)
    router.refresh(len(net.zones))
    before = router.refreshes

    router.forget_unused(frozenset({mask.key}), owner=1)  # the live fleet
    router.forget_unused(frozenset(), owner=2)  # the ghost, holding nothing
    assert mask.key in router.mask_keys

    router.refresh(len(net.zones))
    assert router.refreshes == before, "labels were recomputed after a spurious eviction"


def test_a_mask_nobody_holds_is_eventually_dropped() -> None:
    net = grid_net()
    cfg = tiny_config()
    router = Router(net, cfg)
    mask = resolve(advisory_for(), net, cfg)
    router.ensure_mask(mask)
    assert mask.key in router.mask_keys
    router.forget_unused(frozenset(), owner=1)
    assert mask.key not in router.mask_keys
    assert EMPTY_KEY in router.mask_keys, "the fallback must never be dropped"
