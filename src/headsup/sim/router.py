"""Routing: reverse distance labels per (mask, zone), plus greedy next-hop.

The measurement that decided this design, on `san_antonio` (2757 vertices,
6038 edges, mean out-degree 2.19):

* 900 per-vehicle `get_shortest_paths` calls: **313 ms** -- three times the
  entire 100 ms tick budget at 10x.
* 48 reverse `distances` labels, batched into one call: **23 ms**, and each
  label serves every vehicle heading for that zone until the mask changes.

So instead of storing a path per vehicle, store a cost-to-zone per vertex and
let each vehicle re-decide at every intersection. Greedy descent over a
reverse Dijkstra label reproduces the true shortest path exactly -- verified
against `get_shortest_paths` on 60/60 seeded sources -- because the label is
internally consistent.

It also removes the worst failure mode a per-vehicle design has. There is no
stored route to invalidate, so an advisory landing costs only its labels; no
vehicle does any routing work at all. And those labels are refreshed on a
per-tick budget, so even that cost is spread rather than paid in one tick.

Correctness while a label is stale rests on a separation of duties: the
`no_entry` membership filter decides what is *legal*, the label only decides
what is *quick*. A stale label can send a vehicle on a locally poor detour for
a few seconds; it can never send one onto a closed edge.
"""

from __future__ import annotations

import warnings
from collections import OrderedDict

from headsup.sim.config import SimConfig
from headsup.sim.masks import EMPTY_KEY, EMPTY_MASK, Mask, weights_for
from headsup.sim.network import DriveNetwork

INF = float("inf")

#: Per-vehicle path cache size, used only in `any_vertex_destinations` mode.
ROUTE_CACHE = 4096


class Router:
    """Masked reverse labels for every destination zone, refreshed lazily."""

    __slots__ = (
        "_base_labels",
        "_cfg",
        "_graph",
        "_in_use",
        "_labels",
        "_masks",
        "_net",
        "_refreshes",
        "_route_cache",
        "_stale",
        "_weights",
    )

    def __init__(self, net: DriveNetwork, cfg: SimConfig) -> None:
        self._net = net
        self._cfg = cfg
        self._graph = net.world.graph.graph
        self._masks: dict[str, Mask] = {}
        self._weights: dict[str, list[float]] = {}
        self._labels: dict[tuple[str, int], tuple[float, ...]] = {}
        #: Pending (mask key, zone index) refreshes, in insertion order so the
        #: work is spread deterministically rather than by dict ordering.
        self._stale: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._refreshes = 0
        #: Mask keys each owner is driving under. A router is shared by a
        #: live and a ghost simulation, so pruning has to look at the
        #: union: one owner's view of what is unused is not the truth, and
        #: evicting the other's mask every tick would recompute its labels
        #: every tick.
        self._in_use: dict[int, frozenset[str]] = {}
        self._route_cache: OrderedDict[tuple[str, int, int], tuple[int, ...]] = OrderedDict()

        # The unmasked labels are computed eagerly and never invalidated: they
        # are the fallback that keeps a brand-new mask routable on the tick it
        # lands, before any of its own labels exist.
        self.ensure_mask(EMPTY_MASK)
        self._drain(len(net.zones), EMPTY_KEY)
        self._base_labels: tuple[tuple[float, ...], ...] = tuple(
            self._labels[(EMPTY_KEY, ix)] for ix in range(len(net.zones))
        )

    @property
    def refreshes(self) -> int:
        """Total labels computed. The perf-invariant test watches this."""
        return self._refreshes

    @property
    def pending(self) -> int:
        return len(self._stale)

    def ensure_mask(self, mask: Mask) -> None:
        """Register a mask's weights and mark its labels for refresh.

        Cheap on purpose: no Dijkstra runs here. A mask whose content matches
        one already registered is a no-op, which is what makes op_c and the
        ghost fleet share the empty mask's labels and cost nothing.
        """
        if mask.key in self._weights:
            return
        self._masks[mask.key] = mask
        self._weights[mask.key] = weights_for(
            mask, self._net, keep_clear_detour_s=self._cfg.keep_clear_detour_s
        )
        for ix in range(len(self._net.zones)):
            self._stale[(mask.key, ix)] = None

    @property
    def mask_keys(self) -> frozenset[str]:
        """Masks that currently have weights registered."""
        return frozenset(self._weights)

    def forget_unused(self, live_keys: frozenset[str], owner: int = 0) -> int:
        """Drop labels for masks nobody is driving under any more.

        `owner` identifies the caller, because one router serves both the live
        and the ghost simulation. Pruning on a single owner's keys would evict
        the other's mask on every tick and recompute its labels on the next
        one -- which turns the cheapest part of the design into the most
        expensive.

        The empty mask is never dropped: it is the fallback that keeps a
        freshly accepted advisory routable on the tick it lands.
        """
        self._in_use[owner] = live_keys
        keep = {EMPTY_KEY}
        for keys in self._in_use.values():
            keep |= keys
        dropped = 0
        for key in [k for k in self._weights if k not in keep]:
            del self._weights[key]
            self._masks.pop(key, None)
            dropped += 1
            for ix in range(len(self._net.zones)):
                self._labels.pop((key, ix), None)
                self._stale.pop((key, ix), None)
        if dropped:
            self._route_cache.clear()
        return dropped

    def refresh(self, budget: int | None = None) -> int:
        """Compute up to `budget` stale labels. Returns how many were computed.

        Batches by mask key, because one `distances` call with several sources
        shares its setup: 48 labels cost 23 ms batched against 44 ms one at a
        time.
        """
        if not self._stale:
            return 0
        limit = self._cfg.label_budget if budget is None else budget
        if limit <= 0:
            return 0
        first_key = next(iter(self._stale))[0]
        return self._drain(limit, first_key)

    def _drain(self, limit: int, mask_key: str) -> int:
        """Refresh up to `limit` stale labels belonging to one mask key."""
        wanted = [ix for (key, ix) in self._stale if key == mask_key][:limit]
        if not wanted:
            return 0
        zones = self._net.zones
        sources = [zones[ix] for ix in wanted]
        weights = self._weights[mask_key]
        # `san_antonio` has 112 strongly connected components, so a reverse
        # Dijkstra legitimately cannot reach every vertex and igraph warns
        # about it on every single call -- 80 times a wall-second at 10x.
        # Unreachable is a normal answer here (it becomes `inf`, which
        # `reachable` reads), so the warning is suppressed at this one call
        # site rather than globally.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            rows = self._graph.distances(source=sources, weights=weights, mode="in")
        for ix, row in zip(wanted, rows, strict=True):
            self._labels[(mask_key, ix)] = tuple(row)
            self._stale.pop((mask_key, ix), None)
            self._refreshes += 1
        return len(wanted)

    def label(self, mask_key: str, zone_ix: int) -> tuple[float, ...]:
        """Cost-to-zone per vertex under a mask, falling back to unmasked.

        The fallback is what makes a freshly accepted advisory safe to drive
        under immediately: legality comes from the mask's `no_entry` filter in
        `next_hop`, not from the label.
        """
        found = self._labels.get((mask_key, zone_ix))
        if found is not None:
            return found
        return self._base_labels[zone_ix]

    def reachable(self, mask_key: str, zone_ix: int, node: int) -> bool:
        """Whether the zone can be reached from this vertex under this mask."""
        return self.label(mask_key, zone_ix)[node] != INF

    def next_hop(self, mask_key: str, zone_ix: int, node: int) -> int:
        """The edge to take from `node` toward a zone, or -1 if none is legal.

        Ties go to the lowest edge id, because `net.out_edges[node]` is sorted
        ascending. That is the whole tie-break: an integer comparison, stable
        across runs and platforms.
        """
        mask = self._masks.get(mask_key, EMPTY_MASK)
        blocked = mask.no_entry
        weights = self._weights.get(mask_key)
        if weights is None:
            weights = self._weights[EMPTY_KEY]
        costs = self.label(mask_key, zone_ix)
        head = self._net.head

        best = -1
        best_cost = INF
        for edge_id in self._net.out_edges[node]:
            if edge_id in blocked:
                continue
            onward = costs[head[edge_id]]
            if onward == INF:
                continue
            total = weights[edge_id] + onward
            if total < best_cost:
                best_cost = total
                best = edge_id
        return best

    def nearest_allowed_zone(self, mask_key: str, node: int, banned: frozenset[int]) -> int:
        """Index of the closest zone that is not banned, or -1.

        Used when a KEEP_CLEAR corridor takes a vehicle's destination out of
        service mid-trip: the trip is relocated rather than abandoned.
        """
        best = -1
        best_cost = INF
        for ix, zone in enumerate(self._net.zones):
            if zone in banned:
                continue
            cost = self.label(mask_key, ix)[node]
            if cost < best_cost:
                best_cost = cost
                best = ix
        return best

    def route_to_node(self, mask_key: str, source: int, target: int) -> tuple[int, ...]:
        """Edge-by-edge path, for `any_vertex_destinations` mode only.

        This is the expensive path the zone design exists to avoid: 0.35 ms a
        call, so 900 vehicles rerouting at once is 313 ms. It is kept because
        the evaluation harness may want unquantised destinations, and it is
        off by default.
        """
        cache_key = (mask_key, source, target)
        hit = self._route_cache.get(cache_key)
        if hit is not None:
            self._route_cache.move_to_end(cache_key)
            return hit
        weights = self._weights.get(mask_key) or self._weights[EMPTY_KEY]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            paths = self._graph.get_shortest_paths(
                source, to=target, weights=weights, mode="out", output="epath"
            )
        path = tuple(paths[0]) if paths else ()
        mask = self._masks.get(mask_key, EMPTY_MASK)
        if any(edge_id in mask.no_entry for edge_id in path):
            path = ()
        self._route_cache[cache_key] = path
        if len(self._route_cache) > ROUTE_CACHE:
            self._route_cache.popitem(last=False)
        return path
