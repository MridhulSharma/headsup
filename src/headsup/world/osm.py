"""The only part of the world model that touches the network.

Everything here runs once per city, under `hu world build`, and writes a cache
that the rest of the system reads offline. Nothing in `src/` calls it at
import time and no test calls it at all: the tests run on the committed
fixture graph instead, so a unit run never waits on Overpass and never fails
because somebody else's download was slow.

OSMnx is imported inside the functions. Importing it pulls in geopandas and
pyproj's data files, which is a second and a half this project otherwise never
spends - `hu compile` and the sentinels only ever load a cached graph.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from headsup.ingest.base import user_agent
from headsup.world import raw as raw_mod
from headsup.world.bbox import BBox
from headsup.world.bbox import box as lookup_box
from headsup.world.graph import DEFAULT_BLOCK_RADIUS_M, RoadGraph, WorldError
from headsup.world.h3index import DEFAULT_RESOLUTION, SAMPLE_STEP_M
from headsup.world.model import World, graph_path, pois_path, world_cache_dir
from headsup.world.pois import PoiKind, WorldPoi, from_features, merged_tags

#: Roads a robotaxi can legally drive: OSMnx `drive` drops footways, cycle
#: paths, service alleys behind buildings and anything tagged access=private.
NETWORK_TYPE = "drive"

#: Seconds before an Overpass request is given up on. The wait is the public
#: instance's queue, not the size of the query: a cold build has measured 68 s
#: for the 97 km2 San Antonio corridor and 106 s for 8 km2 of downtown Austin
#: on the same afternoon.
REQUEST_TIMEOUT_S = 180

#: An Overpass instance to use instead of the public default, as a base URL
#: with no `/interpreter` (OSMnx appends it): `https://overpass.kumi.systems/api`,
#: or a local one. The default instance is volunteer-funded and queues, which
#: is most of a cold build's wall time; nothing else about the build changes.
ENV_OVERPASS_URL = "HU_OVERPASS_URL"


def configure(cache_dir: Path | str | None = None, *, offline: bool = False) -> None:
    """Point OSMnx at our cache and make it say who it is.

    Overpass is a volunteer-funded public service. It gets the same contact
    address every other feed in this project gets, and the HTTP cache on, so a
    rebuild of the same bbox costs it nothing - which is also why only the
    first build of a bbox is slow.
    """
    import osmnx as ox

    ox.settings.use_cache = True
    ox.settings.cache_folder = str(Path(cache_dir or world_cache_dir()) / "osmnx")
    ox.settings.http_user_agent = user_agent()
    ox.settings.requests_timeout = REQUEST_TIMEOUT_S
    ox.settings.log_console = False
    endpoint = os.environ.get(ENV_OVERPASS_URL, "").strip()
    if endpoint:
        ox.settings.overpass_url = endpoint.rstrip("/").removesuffix("/interpreter")
    # An offline build serves every query from disk, so there is no server to
    # be polite to and nothing to wait for. OSMnx's own `cache_only_mode` is
    # not the lever it sounds like - it downloads and then refuses to parse -
    # so what keeps an offline build off the network is the replay in
    # `headsup.world.raw` standing in for the one function every Overpass
    # query goes through, plus `build` checking afterwards that it was used.
    ox.settings.overpass_rate_limit = not offline


def download_graph(bbox: BBox, *, simplify: bool = True) -> Any:
    """The drivable, simplified OSM graph for one bbox, as a networkx graph.

    `truncate_by_edge` keeps an edge that leaves the bbox rather than cutting
    it at the boundary, so a street at the edge of the window still reaches
    its next intersection and vehicles do not pile up on a dead end that only
    exists because of where the rectangle was drawn.
    """
    import osmnx as ox

    try:
        return ox.graph_from_bbox(
            bbox.as_tuple,
            network_type=NETWORK_TYPE,
            simplify=simplify,
            truncate_by_edge=True,
        )
    except Exception as exc:  # OSMnx raises its own errors plus requests'
        raise WorldError(f"cannot download the graph for {bbox}: {exc}") from exc


def download_pois(bbox: BBox) -> tuple[WorldPoi, ...]:
    """Hospitals, fire and EMS stations and schools inside one bbox.

    One query for all four kinds, classified locally by `pois.classify`. It
    used to be one query per kind, which read better and cost four trips
    through the Overpass queue - two minutes of a five minute build on the San
    Antonio bbox, for data that comes back in a single response.

    A rectangle where nobody mapped any of them is not an error: Overpass says
    "insufficient response" and the city gets no POIs, which leaves the
    critical-access rule with nothing to guard rather than failing the build.

    Access edges are not resolved here - that needs the graph, and
    `World.from_graph` does it.
    """
    import osmnx as ox

    try:
        features = ox.features_from_bbox(bbox.as_tuple, merged_tags())
    except ox._errors.InsufficientResponseError:
        return ()
    except Exception as exc:
        raise WorldError(f"cannot download POIs for {bbox}: {exc}") from exc
    return from_features(None, features, within=bbox)


def _check_replayed(served: list[str], phase: str, directory: Path) -> None:
    """Fail an offline build that never actually read a raw response.

    An empty list means OSMnx asked nothing of the seam the replay stands on,
    so whatever built the graph did not come from `directory` - which would
    make the report's timing a measurement of nothing.
    """
    if not served:
        raise WorldError(
            f"the offline {phase} rebuild read no raw responses from "
            f"{directory.as_posix()}; this OSMnx does not query through "
            f"{raw_mod._SEAM_MODULE}.{raw_mod._SEAM_NAME} any more"
        )


@dataclass(frozen=True, slots=True)
class BuildReport:
    """What one `hu world build` did, and how long each part took."""

    world: World
    bbox: BBox
    graph_file: Path
    pois_file: Path
    download_s: float
    index_s: float
    poi_s: float
    #: True when every Overpass response came off disk and nothing was asked
    #: of the network. The only build worth holding to a time budget.
    offline: bool = False
    #: The raw Overpass responses this build read or wrote.
    raw: raw_mod.RawArchive | None = None
    #: What `check_manifest` had to say, if anything.
    note: str | None = None

    @property
    def total_s(self) -> float:
        return self.download_s + self.index_s + self.poi_s

    @property
    def source(self) -> str:
        return "raw responses on disk" if self.offline else "Overpass"

    def __str__(self) -> str:
        lines = [
            self.world.summary(),
            f"bbox {self.bbox} ({self.bbox.area_km2:.1f} km2), from {self.source}",
            f"download {self.download_s:.1f}s, index {self.index_s:.1f}s, "
            f"pois {self.poi_s:.1f}s, total {self.total_s:.1f}s",
        ]
        if self.raw is not None:
            lines.append(str(self.raw))
        if self.note:
            lines.append(f"note: {self.note}")
        return "\n".join(lines)


def build(
    city: str,
    *,
    cache_dir: Path | str | None = None,
    refresh: bool = False,
    offline: bool = False,
    resolution: int = DEFAULT_RESOLUTION,
    step_m: float = SAMPLE_STEP_M,
    block_radius_m: float = DEFAULT_BLOCK_RADIUS_M,
) -> BuildReport:
    """Download, simplify, index and cache one city's world.

    Three ways in, and which one ran is on the report:

    * `offline` - every Overpass response is read from
      `data/cache/<city>/raw/` and nothing is asked of the network. The
      graphml cache is rebuilt from those responses rather than read from
      disk, because the point of an offline build is to check that the raw
      set really does rebuild the city.
    * a graphml cache already there and `refresh` false - the download is
      skipped and the graph read from it. No Overpass query is made, so none
      is recorded and an existing raw set is left as it was.
    * otherwise - Overpass, with every response recorded on the way past.

    The H3 index and the POI access edges are rebuilt in all three, because
    they are derived data and take under a second.
    """
    bbox = lookup_box(city)
    cache_dir = cache_dir or world_cache_dir()
    configure(cache_dir, offline=offline)
    graph_file = graph_path(city, cache_dir)
    pois_file = pois_path(city, cache_dir)
    graph_file.parent.mkdir(parents=True, exist_ok=True)
    directory = raw_mod.raw_dir(city, cache_dir)
    note = raw_mod.check_manifest(directory, bbox) if offline else None

    import osmnx as ox

    started = time.perf_counter()
    if offline:
        with raw_mod.replay(directory, raw_mod.PHASE_NETWORK) as served:
            nx_graph = download_graph(bbox)
        _check_replayed(served, raw_mod.PHASE_NETWORK, directory)
        nx_graph.graph["city"] = city
        ox.save_graphml(nx_graph, graph_file)
    elif graph_file.exists() and not refresh:
        nx_graph = ox.load_graphml(graph_file)
    else:
        with raw_mod.record(directory, raw_mod.PHASE_NETWORK):
            nx_graph = download_graph(bbox)
        nx_graph.graph["city"] = city
        ox.save_graphml(nx_graph, graph_file)
    download_s = time.perf_counter() - started

    started = time.perf_counter()
    graph = RoadGraph.from_nx(nx_graph, city=city, bbox=bbox, resolution=resolution, step_m=step_m)
    index_s = time.perf_counter() - started

    started = time.perf_counter()
    if offline:
        with raw_mod.replay(directory, raw_mod.PHASE_FEATURES) as served:
            pois = download_pois(bbox)
        _check_replayed(served, raw_mod.PHASE_FEATURES, directory)
    else:
        with raw_mod.record(directory, raw_mod.PHASE_FEATURES):
            pois = download_pois(bbox)
        raw_mod.write_manifest(
            directory, city=city, bbox=bbox, network_type=NETWORK_TYPE, tags=merged_tags()
        )
    world = World.from_graph(graph, pois, city=city, block_radius_m=block_radius_m)
    world.save(cache_dir=cache_dir)
    poi_s = time.perf_counter() - started

    return BuildReport(
        world=world,
        bbox=bbox,
        graph_file=graph_file,
        pois_file=pois_file,
        download_s=download_s,
        index_s=index_s,
        poi_s=poi_s,
        offline=offline,
        raw=raw_mod.archive(city, cache_dir),
        note=note,
    )


__all__ = [
    "ENV_OVERPASS_URL",
    "NETWORK_TYPE",
    "BuildReport",
    "PoiKind",
    "build",
    "configure",
    "download_graph",
    "download_pois",
]
