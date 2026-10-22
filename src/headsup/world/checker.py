"""The real critical-access checker: the one D1.3 left a stub for.

`StubPoiChecker` answered from POI coordinates alone, which can only ever
approximate the question. The question is about roads: a hospital is cut off
when every edge entering its block is closed, whether or not the building
itself is inside the polygon. A closure can cover a hospital's grounds and
leave the ambulance bay reachable, and it can miss the building entirely and
still seal both ends of the only street in.

So this checker asks the graph. One `edges_in_polygon` call per compile gives
the edge ids under the advisory; each critical POI already carries the ids of
the ways into its block, resolved at build time. The rule is then a set
intersection, and the audit record says which graph answered it.

It satisfies `headsup.agents.compiler.poi.PoiChecker` structurally, so
`Compiler(poi=world.checker())` is the whole of the integration - no rule in
the compiler changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from headsup.agents.compiler.poi import BLOCKING_ACTIONS, BlockedAccess, NullPoiChecker
from headsup.contracts import ActionType, Geometry
from headsup.world.graph import WorldError
from headsup.world.model import World, load_world


@dataclass(frozen=True, slots=True)
class WorldPoiChecker:
    """Answers the critical-access question from the city's road graph."""

    world: World

    def blocked_access(self, geometry: Geometry, action: ActionType) -> Sequence[BlockedAccess]:
        """The critical POIs this action would cut off entirely.

        A speed cap or a density cap slows traffic to a hospital rather than
        closing it, so only the blocking actions are looked at - the same set
        the stub used, and the same set `check_critical_access` re-checks.
        """
        if action not in BLOCKING_ACTIONS:
            return ()
        critical = self.world.critical_pois
        if not critical:
            return ()
        closed = set(self.world.graph.edge_ids_in_polygon(geometry))
        if not closed:
            return ()
        blocked: list[BlockedAccess] = []
        for poi in critical:
            access = poi.access_edge_ids
            if not access:
                # The graph does not reach this POI, so there is no "every way
                # in" to have closed. `describes_itself` reports the count.
                continue
            shut = sum(1 for edge_id in access if edge_id in closed)
            if shut == len(access):
                blocked.append(
                    BlockedAccess(
                        poi_id=poi.id,
                        kind=poi.kind,
                        name=poi.name or poi.id,
                        access_total=len(access),
                        access_blocked=shut,
                    )
                )
        return tuple(blocked)

    def describes_itself(self) -> str:
        """One line for the audit record: which graph answered, over what."""
        critical = self.world.critical_pois
        unreached = sum(1 for poi in critical if not poi.access_edge_ids)
        graph = self.world.graph
        note = f", {unreached} not on the graph" if unreached else ""
        return (
            f"world-poi-checker ({len(critical)} critical POIs{note}, "
            f"{self.world.city} graph {graph.node_count} nodes/{graph.edge_count} edges, "
            f"h3 r{graph.index.resolution} index)"
        )


def default_poi_checker(city: str, *, cache_dir: Path | str | None = None):
    """The checker to give `Compiler` for a city, built if the cache is there.

    Falls back to `NullPoiChecker` rather than refusing, because `hu compile`
    has to be runnable before a world is built. That fallback is not silent:
    it says "unguarded" in `describes_itself`, which goes into the audit
    record of every advisory compiled without a graph. `CompilerService`,
    which is the only thing that publishes, refuses to start on it instead.

    With no `cache_dir`, `headsup.world.model.world_cache_dir` decides - read
    at call time, so `HU_WORLD_CACHE` is honoured.
    """
    try:
        return WorldPoiChecker(load_world(city, cache_dir=cache_dir))
    except (WorldError, ValueError):
        return NullPoiChecker()
