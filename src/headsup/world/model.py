"""One city's world: its road graph, its POIs, and where they are cached.

`World` is what every other track asks for. The simulator wants the graph, the
compiler wants the POIs, the viewer wants the roads under a polygon, and none
of them should have to know that the graph arrived as graphml and the POIs as
JSON.

The cache under `data/cache/` is the source of truth for a built world, not
PostGIS: `World.load` needs no Docker, which is what lets a unit test, a
replay and a compile on a laptop with nothing running all see the same roads.
PostGIS gets the same data for spatial SQL and for the viewer to query.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from headsup.world.bbox import BOXES, BBox
from headsup.world.graph import DEFAULT_BLOCK_RADIUS_M, RoadGraph, WorldError
from headsup.world.h3index import DEFAULT_RESOLUTION, SAMPLE_STEP_M
from headsup.world.pois import WorldPoi, counts, load_pois, save_pois

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Where built worlds live by default. Gitignored: a graph is downloaded, not
#: committed.
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

#: Points the whole world model at another cache directory - a CI box with
#: one built city, or a test that must not see what this machine has built.
ENV_CACHE = "HU_WORLD_CACHE"


def world_cache_dir() -> Path:
    """The cache directory to use: `HU_WORLD_CACHE`, else `data/cache`.

    Every default in this module resolves through here *at call time* rather
    than binding `CACHE_DIR` as a default argument, because a default argument
    is fixed at import and would quietly ignore both the environment and a
    test that tried to point somewhere else.
    """
    return Path(os.environ.get(ENV_CACHE, "").strip() or CACHE_DIR)


def graph_path(city: str, cache_dir: Path | str | None = None) -> Path:
    """Cached OSMnx graphml for one city."""
    return Path(cache_dir or world_cache_dir()) / f"{city}_drive.graphml"


def pois_path(city: str, cache_dir: Path | str | None = None) -> Path:
    """Cached POI set for one city, with access edges resolved."""
    return Path(cache_dir or world_cache_dir()) / f"{city}_pois.json"


def is_built(city: str, cache_dir: Path | str | None = None) -> bool:
    """True when both halves of a city's world are on disk."""
    return graph_path(city, cache_dir).exists() and pois_path(city, cache_dir).exists()


#: Worlds already loaded in this process, keyed by city and cache directory.
#: A graph does not change once built - a rebuild means a restart - and
#: parsing one takes about a second, which a compiler judging a stream of
#: proposals should not pay per proposal.
_LOADED: dict[tuple[str, str], World] = {}


def load_world(city: str, *, cache_dir: Path | str | None = None) -> World:
    """`World.load`, remembered per city and cache directory.

    Only successful loads are remembered: a city that is not built yet may be
    built while the process runs, and the next call should find it.
    """
    key = (city, str(cache_dir or world_cache_dir()))
    world = _LOADED.get(key)
    if world is None:
        world = World.load(city, cache_dir=cache_dir)
        _LOADED[key] = world
    return world


def clear_world_cache() -> None:
    """Forget every loaded world. For tests, and for a rebuild in place."""
    _LOADED.clear()


@dataclass(frozen=True, slots=True)
class World:
    """One city's road graph and POIs, loaded and ready to query."""

    city: str
    graph: RoadGraph
    pois: tuple[WorldPoi, ...]
    bbox: BBox | None = None

    @classmethod
    def load(
        cls,
        city: str,
        *,
        cache_dir: Path | str | None = None,
        resolution: int = DEFAULT_RESOLUTION,
        step_m: float = SAMPLE_STEP_M,
    ) -> World:
        """Load a city built earlier by `hu world build`.

        Raises `WorldError` with the command to run when the cache is missing,
        because "no such file" is not a useful thing to tell someone whose
        compiler just came up unguarded.
        """
        graph_file = graph_path(city, cache_dir)
        poi_file = pois_path(city, cache_dir)
        missing = [path for path in (graph_file, poi_file) if not path.exists()]
        if missing:
            names = ", ".join(path.as_posix() for path in missing)
            raise WorldError(f"{city} is not built ({names} missing) - run `hu world build`")
        graph = RoadGraph.from_graphml(
            graph_file, city=city, bbox=BOXES.get(city), resolution=resolution, step_m=step_m
        )
        stored_city, pois = load_pois(poi_file)
        if stored_city and stored_city != city:
            raise WorldError(f"{poi_file.as_posix()} holds {stored_city!r}, not {city!r}")
        return cls(city=city, graph=graph, pois=pois, bbox=BOXES.get(city))

    @classmethod
    def from_graph(
        cls,
        graph: RoadGraph,
        pois: tuple[WorldPoi, ...] = (),
        *,
        city: str | None = None,
        block_radius_m: float = DEFAULT_BLOCK_RADIUS_M,
    ) -> World:
        """A world around a graph already in memory, resolving POI access.

        Used by the tests on the fixture graph, and by `build` once the
        download is done.
        """
        from headsup.world.pois import resolve_access

        resolved = resolve_access(pois, graph, radius_m=block_radius_m)
        return cls(city=city or graph.city, graph=graph, pois=resolved, bbox=graph.bbox)

    def save(self, *, cache_dir: Path | str | None = None) -> Path:
        """Write the POI half of the cache; the graphml half is OSMnx's own."""
        return save_pois(pois_path(self.city, cache_dir), self.city, self.pois)

    @property
    def critical_pois(self) -> tuple[WorldPoi, ...]:
        """The hospitals, fire stations and EMS stations, in id order."""
        return tuple(poi for poi in self.pois if poi.critical)

    def poi(self, poi_id: str) -> WorldPoi:
        for poi in self.pois:
            if poi.id == poi_id:
                return poi
        raise WorldError(f"no POI {poi_id!r} in {self.city}")

    def checker(self):
        """A `WorldPoiChecker` over this world, for `Compiler(poi=...)`."""
        from headsup.world.checker import WorldPoiChecker

        return WorldPoiChecker(self)

    def summary(self) -> str:
        """Two lines: the graph, then the POIs by kind."""
        tally = ", ".join(
            f"{kind.value.lower()}={count}" for kind, count in counts(self.pois).items()
        )
        return f"{self.graph.summary()}\n{len(self.pois)} POIs ({tally})"
