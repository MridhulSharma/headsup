"""The points of interest an advisory is not allowed to cut off.

Four kinds come out of OSM: hospitals, fire stations, EMS stations and
schools. The first three are the critical ones - no advisory may close every
way into them - and the kinds themselves are
`headsup.agents.compiler.poi.PoiKind`, because the compiler owns that rule and
the world only supplies the data it needs to apply it.

A POI here is a point plus the edge ids of the ways into its block, resolved
once at build time against the road graph and then stored. `access_edge_ids`
is what makes the critical-access rule cheap at compile time: the checker
intersects one set of ints with the edges under the polygon.

Ids are derived from the OSM element (`poi_hospital_w12345`), not counted, so
the same hospital keeps the same id across rebuilds and across the graphml
cache, the PostGIS table and an audit record written weeks apart.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shapely.geometry import base as shapely_base

from headsup.agents.compiler.poi import CRITICAL_KINDS, Poi, PoiKind
from headsup.world.graph import DEFAULT_BLOCK_RADIUS_M, GEOD, RoadGraph

#: The OSM tags each kind is recognised by, in priority order: a building
#: tagged both `amenity=hospital` and `amenity=school` is a hospital. Keys
#: within one selector are OR-ed, so a hospital is anything tagged
#: `amenity=hospital` or `healthcare=hospital` - over-collecting here means
#: the critical-access rule guards one building too many, which is the safe
#: direction to be wrong in.
#:
#: `merged_tags` turns this table into the one Overpass query that fetches
#: every kind at once, and `classify` reads a row back out of it, so the table
#: stays the single description of what a POI is.
OSM_TAGS: dict[PoiKind, dict[str, bool | str | list[str]]] = {
    PoiKind.HOSPITAL: {"amenity": "hospital", "healthcare": "hospital"},
    PoiKind.FIRE_STATION: {"amenity": "fire_station"},
    PoiKind.EMS_STATION: {"emergency": "ambulance_station", "amenity": "ambulance_station"},
    PoiKind.SCHOOL: {"amenity": ["school", "kindergarten"]},
}

#: Two POIs of one kind sharing a name within this distance are one place
#: mapped twice - as the building outline and as a node on it, say.
DUPLICATE_RADIUS_M = 250.0

_ELEMENT_LETTER = {"node": "n", "way": "w", "relation": "r"}


@dataclass(frozen=True, slots=True)
class WorldPoi:
    """One point of interest, with the ways into its block already resolved."""

    id: str
    kind: PoiKind
    name: str
    osm_id: str
    lon: float
    lat: float
    #: Ids of the edges entering this POI's block, from `RoadGraph.access_edges`.
    access_edge_ids: tuple[int, ...] = ()

    @property
    def critical(self) -> bool:
        """True for the kinds whose access may never be fully closed."""
        return self.kind in CRITICAL_KINDS

    def with_access(self, edge_ids: Iterable[int]) -> WorldPoi:
        return WorldPoi(
            id=self.id,
            kind=self.kind,
            name=self.name,
            osm_id=self.osm_id,
            lon=self.lon,
            lat=self.lat,
            access_edge_ids=tuple(sorted(set(edge_ids))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "osm_id": self.osm_id,
            "lon": self.lon,
            "lat": self.lat,
            "access_edge_ids": list(self.access_edge_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> WorldPoi:
        try:
            return cls(
                id=str(payload["id"]),
                kind=PoiKind(str(payload["kind"])),
                name=str(payload.get("name", "")),
                osm_id=str(payload.get("osm_id", "")),
                lon=float(payload["lon"]),
                lat=float(payload["lat"]),
                access_edge_ids=tuple(int(item) for item in payload.get("access_edge_ids", ())),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"not a usable POI record: {payload!r} ({exc})") from exc

    def to_compiler_poi(self, graph: RoadGraph | None = None) -> Poi:
        """This POI in the compiler's own shape, for a geometry-only checker.

        `WorldPoiChecker` works on edge ids and needs none of this; it is here
        so a `StubPoiChecker` can be fed real POI data, and so an audit record
        can name the coordinates the rule was decided on.
        """
        points: tuple[tuple[float, float], ...] = ()
        if graph is not None:
            points = tuple(
                graph.position_of(graph.edge(edge_id).v) for edge_id in self.access_edge_ids
            )
        return Poi(id=self.id, kind=self.kind, name=self.name, access_points=points)

    def __str__(self) -> str:
        label = self.name or "unnamed"
        return (
            f"{self.kind.value} {label} ({self.id}) at {self.lon:.5f},{self.lat:.5f} "
            f"with {len(self.access_edge_ids)} access edges"
        )


def poi_id(kind: PoiKind, element: str, osm_id: Any) -> str:
    """A stable id for one OSM element: `poi_hospital_w12345`."""
    letter = _ELEMENT_LETTER.get(str(element).lower(), "x")
    return f"poi_{kind.value.lower()}_{letter}{osm_id}"


def _point_of(geometry: shapely_base.BaseGeometry) -> tuple[float, float]:
    """A lon/lat for any POI geometry: the point, or inside the building."""
    if geometry.geom_type == "Point":
        return float(geometry.x), float(geometry.y)
    inner = geometry.representative_point()
    return float(inner.x), float(inner.y)


def _metres(a: WorldPoi, b: WorldPoi) -> float:
    *_bearings, distance = GEOD.inv(a.lon, a.lat, b.lon, b.lat)
    return float(distance)


def dedupe(pois: Sequence[WorldPoi]) -> tuple[WorldPoi, ...]:
    """Drop the second copy of a place mapped both as a node and as a way.

    Only a same-kind, same-name, nearby pair counts as a duplicate. Two fire
    stations with no name stay two fire stations: merging them would quietly
    delete a building the critical-access rule protects.
    """
    kept: list[WorldPoi] = []
    for poi in pois:
        twin = next(
            (
                other
                for other in kept
                if other.kind is poi.kind
                and other.name
                and other.name == poi.name
                and _metres(other, poi) <= DUPLICATE_RADIUS_M
            ),
            None,
        )
        if twin is None:
            kept.append(poi)
    return tuple(kept)


def merged_tags() -> dict[str, bool | str | list[str]]:
    """Every kind's tags in one selector, for a single Overpass query.

    One query per kind meant four trips through the Overpass queue, which on a
    140 km2 bbox was most of the build time. One query returns every POI of
    every kind and `classify` sorts them out locally.
    """
    merged: dict[str, set[str]] = {}
    for tags in OSM_TAGS.values():
        for key, value in tags.items():
            values = {value} if isinstance(value, str) else set(value)
            merged.setdefault(key, set()).update(values)
    return {key: sorted(values) for key, values in merged.items()}


def classify(row: Any) -> PoiKind | None:
    """Which kind an OSM feature is, or None if it is none of them.

    `OSM_TAGS` is read in order, so the first kind whose tags match wins.
    """
    for kind, tags in OSM_TAGS.items():
        for key, value in tags.items():
            wanted = {value} if isinstance(value, str) else set(value)
            if str(row.get(key) or "") in wanted:
                return kind
    return None


def from_features(
    kind: PoiKind | None, features: Any, *, within: Any = None
) -> tuple[WorldPoi, ...]:
    """POIs from one OSMnx `features_from_bbox` frame, in OSM element order.

    The frame is indexed by (element type, OSM id), which is what makes the
    ids stable, and sorting on that index is what makes the output order
    stable. A `kind` of None classifies each row by its own tags, which is
    what the one combined download needs; a kind pins every row to it.
    """
    found: list[WorldPoi] = []
    if features is None or len(features) == 0:
        return ()
    frame = features.sort_index()
    for key, row in frame.iterrows():
        element, osm_id = key if isinstance(key, tuple) else ("way", key)
        row_kind = kind if kind is not None else classify(row)
        if row_kind is None:
            continue
        geometry = row.get("geometry")
        if geometry is None or geometry.is_empty:
            continue
        lon, lat = _point_of(geometry)
        if within is not None and not within.contains(lon, lat):
            continue
        raw_name = row.get("name")
        name = "" if raw_name is None or str(raw_name) == "nan" else str(raw_name).strip()
        found.append(
            WorldPoi(
                id=poi_id(row_kind, element, osm_id),
                kind=row_kind,
                name=name,
                osm_id=f"{element}/{osm_id}",
                lon=lon,
                lat=lat,
            )
        )
    return dedupe(found)


def resolve_access(
    pois: Sequence[WorldPoi],
    graph: RoadGraph,
    *,
    radius_m: float = DEFAULT_BLOCK_RADIUS_M,
) -> tuple[WorldPoi, ...]:
    """Fill in `access_edge_ids` for every POI from the road graph."""
    return tuple(
        poi.with_access(edge.id for edge in graph.access_edges(poi.lon, poi.lat, radius_m=radius_m))
        for poi in pois
    )


def save_pois(path: str | Path, city: str, pois: Sequence[WorldPoi]) -> Path:
    """Write the POI set beside the graphml cache, so the checker loads offline."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "city": city,
        "count": len(pois),
        "pois": [poi.to_dict() for poi in pois],
    }
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def load_pois(path: str | Path) -> tuple[str, tuple[WorldPoi, ...]]:
    """Read a POI file back: its city and its POIs."""
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {target.as_posix()}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{target.as_posix()} is not JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{target.as_posix()}: expected a JSON object")
    records = payload.get("pois", [])
    if not isinstance(records, list):
        raise ValueError(f"{target.as_posix()}: `pois` is not a list")
    return str(payload.get("city", "")), tuple(WorldPoi.from_dict(item) for item in records)


def counts(pois: Sequence[WorldPoi]) -> dict[PoiKind, int]:
    """How many of each kind, for a CLI table or a one-line summary."""
    tally = dict.fromkeys(PoiKind, 0)
    for poi in pois:
        tally[poi.kind] += 1
    return tally


def poi_rows(city: str, pois: Sequence[WorldPoi]) -> Iterable[Mapping[str, Any]]:
    """Every POI as a row for the PostGIS `pois` table."""
    for poi in pois:
        yield {
            "city": city,
            "poi_id": poi.id,
            "kind": poi.kind.value,
            "name": poi.name,
            "osm_id": poi.osm_id,
            "critical": poi.critical,
            "access_edge_ids": list(poi.access_edge_ids),
            "wkt": f"POINT ({poi.lon} {poi.lat})",
        }
