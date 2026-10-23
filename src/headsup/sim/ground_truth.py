"""What is physically true about the roads, independent of any advisory.

This separation is the point of the whole experiment. Ground truth says *the
crossing is under water*; an advisory says *Heads Up believes it is and asks
you to avoid it*. The first binds every fleet, including the ghost. The second
binds only an operator that accepted it.

Collapse the two and the comparison becomes circular: the ghost fleet would
only fail where an advisory existed, which is exactly the thing being
measured. So hazards are an input to the simulation, not a consequence of the
agent pipeline, and `hu sim` can run them from a scenario file with no agents
running at all.

A SIGNAL_OUTAGE resolves to the *signalised* intersections inside its area
and to no edges at all. Signals are what a blackout takes away; the stop-sign
corners between them keep working, and treating them as dark made an outage
region's severity a function of how finely OSM happened to split the streets
in it.

Resolution to edge and node id sets happens once, at load. `snapshot()` is
memoised by validity boundary, so the tick loop pays a dict lookup on all but
the two or three ticks per run where the hazard set actually changes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from shapely.geometry import Point

from headsup.contracts import HazardClass
from headsup.sim.network import DriveNetwork
from headsup.world.graph import as_shapely

#: Hazard classes that make a road impassable: a vehicle that enters one and
#: has not been advised away stalls there.
IMPASSABLE_CLASSES: frozenset[HazardClass] = frozenset(
    {HazardClass.FLOOD, HazardClass.WORK_ZONE, HazardClass.EMERGENCY_SCENE}
)

#: Hazard classes that darken intersections rather than close roads.
#:
#: Only *signalised* intersections inside the area go dark: a stop-sign corner
#: has no signal to lose, and counting it would make an outage region as
#: hostile as the number of intersections in it rather than as the number of
#: signals in it. `RoadGraph.signalized_nodes` is the authority, and it is OSM
#: `highway=traffic_signals` carried over from the node tags.
DARK_CLASSES: frozenset[HazardClass] = frozenset({HazardClass.SIGNAL_OUTAGE})


@dataclass(frozen=True, slots=True)
class HazardRegion:
    """One authored hazard, before it is resolved against a graph."""

    id: str
    hazard_class: HazardClass
    geometry: object
    valid_from: datetime
    valid_until: datetime
    severity: int = 3

    def active_at(self, at: datetime) -> bool:
        return self.valid_from <= at < self.valid_until


@dataclass(frozen=True, slots=True)
class GroundHazard:
    """An authored hazard resolved to the graph elements it affects."""

    id: str
    hazard_class: HazardClass
    severity: int
    edges: frozenset[int]
    nodes: frozenset[int]
    valid_from: datetime
    valid_until: datetime

    def active_at(self, at: datetime) -> bool:
        return self.valid_from <= at < self.valid_until


@dataclass(frozen=True, slots=True)
class HazardSnapshot:
    """Which edges are impassable and which nodes are dark, at one instant."""

    impassable: Mapping[int, GroundHazard]
    dark: Mapping[int, GroundHazard]
    version: int

    @property
    def empty(self) -> bool:
        return not self.impassable and not self.dark


EMPTY_SNAPSHOT = HazardSnapshot(impassable={}, dark={}, version=0)


@runtime_checkable
class HazardField(Protocol):
    """Ground truth over time. The seam S1, S2 and the unit tests share."""

    def snapshot(self, at: datetime) -> HazardSnapshot: ...


class EmptyHazardField:
    """No hazards ever. For tests that are about something else."""

    __slots__ = ()

    def snapshot(self, at: datetime) -> HazardSnapshot:
        return EMPTY_SNAPSHOT


class ScriptedHazardField:
    """Authored hazards, resolved once and served by validity window.

    Authored rather than inferred on purpose: a replay's hazards should be a
    property of the scenario, not of how fast the agents happened to react, or
    the determinism of the simulation would depend on the pipeline's timing.
    """

    __slots__ = ("_boundaries", "_cache", "_hazards")

    def __init__(self, hazards: Sequence[GroundHazard]) -> None:
        self._hazards = tuple(sorted(hazards, key=lambda h: (h.valid_from, h.id)))
        self._cache: dict[int, HazardSnapshot] = {}
        # Every instant at which the active set can change. A snapshot is
        # constant between consecutive boundaries, which is what makes the
        # memo effective.
        edges: set[datetime] = set()
        for hazard in self._hazards:
            edges.add(hazard.valid_from)
            edges.add(hazard.valid_until)
        self._boundaries = tuple(sorted(edges))

    @property
    def hazards(self) -> tuple[GroundHazard, ...]:
        return self._hazards

    @classmethod
    def from_regions(
        cls, regions: Sequence[HazardRegion], net: DriveNetwork
    ) -> ScriptedHazardField:
        """Resolve authored regions against a graph."""
        graph = net.world.graph
        resolved: list[GroundHazard] = []
        for region in regions:
            shape = as_shapely(region.geometry)
            edge_ids = frozenset(graph.edge_ids_in_polygon(shape))
            if region.hazard_class in DARK_CLASSES:
                nodes = frozenset(_nodes_inside(net, edge_ids, shape)) & graph.signalized_nodes
                edge_ids = frozenset()
            else:
                nodes = frozenset()
            resolved.append(
                GroundHazard(
                    id=region.id,
                    hazard_class=region.hazard_class,
                    severity=region.severity,
                    edges=edge_ids,
                    nodes=nodes,
                    valid_from=region.valid_from,
                    valid_until=region.valid_until,
                )
            )
        return cls(resolved)

    @classmethod
    def from_hazards(cls, hazards: Sequence[object], net: DriveNetwork) -> ScriptedHazardField:
        """Resolve fused `Hazard` contract models as ground truth.

        Honest for a live run, where the fusion agent's output *is* the best
        available account of the world. Avoided in tests, because it ties the
        simulation's determinism to agent timing.
        """
        regions = [
            HazardRegion(
                id=str(getattr(hazard, "id", f"haz-{index}")),
                hazard_class=hazard.hazard_class,  # type: ignore[attr-defined]
                geometry=hazard.geometry,  # type: ignore[attr-defined]
                valid_from=getattr(hazard, "first_seen", None) or hazard.valid_until,  # type: ignore[attr-defined]
                valid_until=hazard.valid_until,  # type: ignore[attr-defined]
                severity=int(getattr(hazard, "severity", 3)),
            )
            for index, hazard in enumerate(hazards)
        ]
        return cls.from_regions(regions, net)

    def snapshot(self, at: datetime) -> HazardSnapshot:
        """The active hazard sets at an instant, memoised by window."""
        bucket = self._bucket(at)
        found = self._cache.get(bucket)
        if found is not None:
            return found
        impassable: dict[int, GroundHazard] = {}
        dark: dict[int, GroundHazard] = {}
        for hazard in self._hazards:
            if not hazard.active_at(at):
                continue
            for edge_id in sorted(hazard.edges):
                impassable.setdefault(edge_id, hazard)
            for node in sorted(hazard.nodes):
                dark.setdefault(node, hazard)
        snapshot = HazardSnapshot(impassable=impassable, dark=dark, version=bucket)
        self._cache[bucket] = snapshot
        return snapshot

    def _bucket(self, at: datetime) -> int:
        """How many validity boundaries `at` is past. Constant within a window."""
        count = 0
        for boundary in self._boundaries:
            if at >= boundary:
                count += 1
            else:
                break
        return count


def _nodes_inside(net: DriveNetwork, edge_ids: frozenset[int], shape: object) -> set[int]:
    """Endpoints of the matched edges that actually fall inside the area.

    Every endpoint, signalised or not. `from_regions` is what narrows a dark
    class down to the signals, because the same question - which intersections
    does this area contain - is also the right one for a class that is not
    about signals at all.
    """
    area = as_shapely(shape)
    position = net.world.graph.position_of
    candidates: set[int] = set()
    for edge_id in edge_ids:
        candidates.add(net.tail[edge_id])
        candidates.add(net.head[edge_id])
    inside: set[int] = set()
    for node in sorted(candidates):
        lon, lat = position(node)
        if area.contains(Point(lon, lat)):
            inside.add(node)
    return inside


def load_regions(
    path: str | Path,
    *,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> tuple[HazardRegion, ...]:
    """Read a `ground_truth:` block from a scenario YAML file.

    Shape, per entry: `{id, class, severity, geometry, valid_from, valid_until}`
    where `geometry` is GeoJSON. A scenario without the block yields nothing,
    so an existing scenario file stays valid.

    `valid_from` and `valid_until` are the window for an entry that names
    none, and this is the usual case: a descriptor states *where* the ground
    truth is, and the caller states *when*, because a hazard is in force for
    the run that replays the scenario rather than for the feed's own
    timestamps. A run starts when it is started; the recording's clock is
    months old. An entry that names its own window keeps it.

    An entry can also place itself *within* the run, with
    `from_offset_minutes` and `until_offset_minutes` measured from
    `valid_from`. That is how a hazard lines up with the recording that
    reports it: S2's signals drop out ten minutes into the feed and are
    restored at eighty-five, and a simulator that darkened them from the
    first tick would be charging the fleet for an outage no feed had
    mentioned yet -- which is the sentinel's unavoidable latency plus ten
    minutes of invented damage.
    """
    import yaml

    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"no scenario file at {target.as_posix()}")
    document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    entries = document.get("ground_truth") or []
    regions: list[HazardRegion] = []
    for index, entry in enumerate(entries):
        region_id = str(entry.get("id") or f"{document.get('id', 'scenario')}-haz-{index}")
        regions.append(
            HazardRegion(
                id=region_id,
                hazard_class=HazardClass(entry["class"]),
                geometry=entry["geometry"],
                valid_from=_edge(
                    entry, "valid_from", valid_from, origin=valid_from, path=target, rid=region_id
                ),
                valid_until=_edge(
                    entry, "valid_until", valid_until, origin=valid_from, path=target, rid=region_id
                ),
                severity=int(entry.get("severity", 3)),
            )
        )
    return tuple(regions)


#: Which offset key shifts which end of the window.
_OFFSET_KEY = {"valid_from": "from_offset_minutes", "valid_until": "until_offset_minutes"}


def _edge(
    entry: Mapping[str, object],
    field: str,
    default: datetime | None,
    *,
    origin: datetime | None,
    path: Path,
    rid: str,
) -> datetime:
    """One end of an entry's window: its own instant, an offset, or the caller's.

    An offset is measured from `origin`, the start of the caller's window, for
    *both* ends, so a pair of them reads as "from ten minutes in until
    eighty-five minutes in" rather than one of them counting backwards from
    the end. An offset with no caller window to measure from is the same
    error as naming no window at all.
    """
    value = entry.get(field)
    if value is not None:
        return _as_datetime(value)
    offset = entry.get(_OFFSET_KEY[field])
    if offset is not None and origin is not None:
        return origin + timedelta(minutes=float(offset))  # type: ignore[arg-type]
    if default is not None:
        return default
    raise ValueError(
        f"{path.as_posix()}: ground truth {rid!r} names no {field} and the caller "
        "supplied no default window"
    )


def _as_datetime(value: object) -> datetime:
    from datetime import UTC

    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
