"""Builders the compiler tests share: proposals, verdicts, boxes and POIs.

Everything here is explicit and fixed-clock. `box()` is the workhorse: a
rectangle of a requested area in square kilometres, so a test can say "a
polygon 3x the flood cap" instead of doing trigonometry in the assertion.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from headsup.agents.compiler import Poi, PoiKind, StubPoiChecker
from headsup.contracts import (
    Action,
    ActionType,
    AppliesTo,
    CriticVerdict,
    Hazard,
    HazardClass,
    Polygon,
    ProposedAdvisory,
    Verdict,
)
from samples import NWS_EVIDENCE, T0, USGS_EVIDENCE

#: The S1 low-water crossing, a little north-east of downtown San Antonio.
CENTRE = (-98.4933, 29.4204)

#: Metres per degree of latitude on the WGS84 ellipsoid, near enough for a
#: test fixture that only needs an area in the right ballpark.
M_PER_DEG_LAT = 111_132.0

#: The clock every compiler test runs on: just after the S1 proposal.
NOW = T0 + timedelta(seconds=55)


def clock(moment: datetime = NOW):
    """A fixed clock to inject into `Compiler(clock=...)`."""
    return lambda: moment


def box(area_km2: float, centre: tuple[float, float] = CENTRE) -> Polygon:
    """A square polygon of approximately `area_km2`, centred on `centre`.

    The exact area is measured by the compiler with pyproj; this only has to
    land close enough that "over the cap" and "under the cap" are unambiguous,
    so callers ask for 0.5x or 3x a cap rather than 1.0x.
    """
    lon, lat = centre
    side_m = math.sqrt(area_km2 * 1_000_000.0)
    half_lat = (side_m / 2) / M_PER_DEG_LAT
    half_lon = half_lat / max(math.cos(math.radians(lat)), 1e-6)
    ring = [
        [lon - half_lon, lat - half_lat],
        [lon + half_lon, lat - half_lat],
        [lon + half_lon, lat + half_lat],
        [lon - half_lon, lat + half_lat],
        [lon - half_lon, lat - half_lat],
    ]
    return Polygon(coordinates=[ring])


def hazard(
    hazard_class: HazardClass = HazardClass.FLOOD,
    *,
    severity: int = 3,
    geometry: Polygon | None = None,
    confidence: float = 0.9,
) -> Hazard:
    """A fused hazard with two authoritative citations."""
    shape = geometry or box(0.05)
    return Hazard(
        id="haz_test_1",
        **{"class": hazard_class},
        severity=severity,
        confidence=confidence,
        geometry=shape,
        evidence=[NWS_EVIDENCE, USGS_EVIDENCE],
        valid_until=NOW + timedelta(hours=3),
        first_seen=T0,
        updated_at=T0,
    )


def action(action_type: ActionType = ActionType.NO_ENTRY, **params: Any) -> Action:
    """An action with whatever params that type needs."""
    defaults: dict[ActionType, dict[str, Any]] = {
        ActionType.SPEED_CAP: {"speed_mph": 15},
        ActionType.DENSITY_CAP: {"max_vehicles": 20},
    }
    merged = {**defaults.get(action_type, {}), **params}
    return Action(
        type=action_type,
        applies_to=AppliesTo(operators=["*"], vehicle_classes=["*"]),
        params=merged,
    )


def proposal(
    *,
    hazard_class: HazardClass = HazardClass.FLOOD,
    severity: int = 3,
    action_type: ActionType = ActionType.NO_ENTRY,
    area_km2: float = 0.05,
    lifetime_minutes: float = 90.0,
    geometry: Polygon | None = None,
    valid_from: datetime | None = None,
    proposal_id: str = "prop_test_1",
    policy_rule: str = "flood.no_entry.v1",
) -> ProposedAdvisory:
    """One proposal, built from the knobs the rules care about."""
    shape = geometry or box(area_km2)
    start = valid_from or NOW
    return ProposedAdvisory(
        id=proposal_id,
        hazard=hazard(hazard_class, severity=severity, geometry=shape),
        action=action(action_type),
        geometry=shape,
        valid_from=start,
        valid_until=start + timedelta(minutes=lifetime_minutes),
        evidence=[NWS_EVIDENCE, USGS_EVIDENCE],
        policy_rule=policy_rule,
        proposed_by="proposer",
        created_at=T0,
    )


def approve(target: ProposedAdvisory | str = "prop_test_1") -> CriticVerdict:
    """An APPROVE verdict for one proposal."""
    return CriticVerdict(
        proposal_id=target if isinstance(target, str) else target.id,
        verdict=Verdict.APPROVE,
        reasons=["Two authoritative sources agree."],
        created_at=T0,
    )


def revise(
    target: ProposedAdvisory | str = "prop_test_1",
    *,
    geometry: Polygon | None = None,
    valid_until: datetime | None = None,
) -> CriticVerdict:
    """A REVISE verdict carrying a narrower geometry or a shorter window."""
    return CriticVerdict(
        proposal_id=target if isinstance(target, str) else target.id,
        verdict=Verdict.REVISE,
        reasons=["The proposed area reaches past the crossing."],
        revised_geometry=geometry,
        revised_valid_until=valid_until,
        created_at=T0,
    )


def escalate(target: ProposedAdvisory | str = "prop_test_1") -> CriticVerdict:
    """An ESCALATE verdict: a human has to look at this one."""
    return CriticVerdict(
        proposal_id=target if isinstance(target, str) else target.id,
        verdict=Verdict.ESCALATE,
        reasons=["The evidence does not explain an area this large."],
        created_at=T0,
    )


def hospital(*, inside: int = 2, outside: int = 0, centre: tuple[float, float] = CENTRE) -> Poi:
    """A hospital with `inside` access points in the box and `outside` beyond it."""
    lon, lat = centre
    points = [(lon + 0.0001 * index, lat + 0.0001 * index) for index in range(inside)]
    points += [(lon + 5.0, lat + 1.0) for _ in range(outside)]
    return Poi(
        id="poi_hospital_1",
        kind=PoiKind.HOSPITAL,
        name="Riverside General",
        access_points=tuple(points),
    )


def poi_checker(*pois: Poi) -> StubPoiChecker:
    return StubPoiChecker(pois=list(pois))
