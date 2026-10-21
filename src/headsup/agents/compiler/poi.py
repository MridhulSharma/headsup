"""The critical-access rule, and the interface D2.1 replaces.

No advisory may close every way into a hospital, a fire station or an EMS
station. The compiler cannot answer that question on its own: it needs the
road graph, which arrives in D2.1 with the world model. Until then it asks a
`PoiChecker`, and this module ships two of them:

* `NullPoiChecker` - answers "nothing blocked" and says so loudly in
  `describes_itself`, so a run without the world model is visibly unguarded;
* `StubPoiChecker` - holds a handful of POIs with their access points and
  answers from geometry alone, which is enough for the unit and property tests.

D2.1 adds `WorldPoiChecker` with the real `access_edges(poi)` and drops it in
through the same protocol; nothing in `rules.py` changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from shapely.geometry import Point as ShapelyPoint

from headsup.agents.compiler.geo import to_shapely
from headsup.contracts import ActionType, Geometry


class PoiKind(StrEnum):
    """The points of interest the world model loads from OSM."""

    HOSPITAL = "HOSPITAL"
    FIRE_STATION = "FIRE_STATION"
    EMS_STATION = "EMS_STATION"
    SCHOOL = "SCHOOL"


#: The kinds whose access an advisory may never fully close. A school is
#: loaded for the SCHOOL_ZONE playbook, not for this rule: closing the street
#: outside a school is sometimes exactly the right advisory.
CRITICAL_KINDS = frozenset({PoiKind.HOSPITAL, PoiKind.FIRE_STATION, PoiKind.EMS_STATION})

#: Actions that stop a vehicle from entering at all. A speed cap or a density
#: cap slows traffic to a hospital; it does not cut the hospital off.
BLOCKING_ACTIONS = frozenset({ActionType.NO_ENTRY, ActionType.PAUSE_SERVICE})


@dataclass(frozen=True, slots=True)
class Poi:
    """One point of interest and the ways into it.

    `access_points` is one coordinate per access edge: where that edge meets
    the public street network. The real checker in D2.1 works on edge ids; the
    stub works on these coordinates, and both answer the same question.
    """

    id: str
    kind: PoiKind
    name: str
    access_points: tuple[tuple[float, float], ...] = ()

    @property
    def critical(self) -> bool:
        return self.kind in CRITICAL_KINDS


@dataclass(frozen=True, slots=True)
class BlockedAccess:
    """A POI whose every way in falls inside the advisory geometry."""

    poi_id: str
    kind: PoiKind
    name: str
    access_total: int
    access_blocked: int

    def __str__(self) -> str:
        return (
            f"{self.kind.value} {self.name} ({self.poi_id}): "
            f"{self.access_blocked} of {self.access_total} access edges closed"
        )


@runtime_checkable
class PoiChecker(Protocol):
    """What the compiler needs to know about the POIs under a geometry."""

    def blocked_access(
        self, geometry: Geometry, action: ActionType
    ) -> Sequence[BlockedAccess]:  # pragma: no cover - protocol
        """POIs this action would cut off entirely. Empty means nothing is cut off."""
        ...

    def describes_itself(self) -> str:  # pragma: no cover - protocol
        """One line for the audit record: which checker answered, over what data."""
        ...


class NullPoiChecker:
    """Answers "nothing blocked" because it knows nothing.

    This is the default so that the compiler runs before the world model
    exists, and the audit record carries the fact that the rule was unguarded.
    """

    def blocked_access(self, geometry: Geometry, action: ActionType) -> Sequence[BlockedAccess]:
        return ()

    def describes_itself(self) -> str:
        return "null-poi-checker (no POI data; critical-access rule unguarded until D2.1)"


@dataclass
class StubPoiChecker:
    """Answers from POI coordinates alone, with no road graph.

    An access point counts as closed when it falls inside the advisory
    geometry (or on its boundary). A POI is cut off when a blocking action
    closes every one of its access points.
    """

    pois: list[Poi] = field(default_factory=list)

    def blocked_access(self, geometry: Geometry, action: ActionType) -> Sequence[BlockedAccess]:
        if action not in BLOCKING_ACTIONS:
            return ()
        shape = to_shapely(geometry)
        blocked: list[BlockedAccess] = []
        for poi in self.pois:
            if not poi.critical or not poi.access_points:
                continue
            inside = sum(
                1 for lon, lat in poi.access_points if shape.intersects(ShapelyPoint(lon, lat))
            )
            if inside == len(poi.access_points):
                blocked.append(
                    BlockedAccess(
                        poi_id=poi.id,
                        kind=poi.kind,
                        name=poi.name,
                        access_total=len(poi.access_points),
                        access_blocked=inside,
                    )
                )
        return tuple(blocked)

    def describes_itself(self) -> str:
        critical = sum(1 for poi in self.pois if poi.critical)
        return f"stub-poi-checker ({critical} critical POIs, geometry-only, no road graph)"
