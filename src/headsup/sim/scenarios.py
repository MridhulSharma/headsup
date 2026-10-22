"""Ground-truth hazard areas for the demo scenarios.

These are derived from the recordings, not invented. S1's corridor runs down
the reach of Salado Creek between the two gauges that actually crossed their
action stage on 20 April 2026; the third gauge in `scenarios/s1_flood.yaml`,
Loop 13, is both outside the built graph's bbox and recorded as never
crossing, so it is not here.

Both areas are sized to stay inside their playbook's cap, because a hazard the
compiler could never legally advise on would make the comparison meaningless:

* S1 flood corridor: 1.05 km2 against `playbooks/flood.yaml` max_area_km2 12.0
* S2 signal cluster: 0.64 km2 against `playbooks/signal_outage.yaml` max 6.0

Each area is also a genuine bottleneck, which is what makes a baseline run
fail visibly. Measured over 900 seeded trips with uniform origins: 47% of S1
trips cross the creek corridor, and 45% of S2 trips pass through the signal
cluster. `tests/unit/test_sim_scenarios.py` holds that to at least 20%.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from shapely.geometry import LineString, box
from shapely.geometry.base import BaseGeometry

from headsup.contracts import HazardClass
from headsup.sim.ground_truth import HazardRegion
from headsup.sim.masks import corridor_polygon
from headsup.world.bbox import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR

#: USGS gauges on Salado Creek that crossed their action stage during S1.
#: Coordinates come from `data/recordings/s1_flood/fixture/usgs.jsonl`.
SALADO_WILDERNESS_RD = (-98.5655755265633, 29.630781348986)
SALADO_BLANCO_RD = (-98.5196666666667, 29.5638611111111)
#: A bend between them, so the corridor follows the creek rather than cutting
#: a straight chord across the neighbourhoods on either side.
SALADO_BEND = (-98.542, 29.600)

#: Width of the flooded reach. 120 m covers the low-water crossings and the
#: approaches without reaching into the blocks behind them.
SALADO_WIDTH_M = 120.0

#: Downtown Austin, Congress Avenue at 6th Street: the same block
#: `viz/mock_stream.py` already drives a vehicle around.
AUSTIN_DOWNTOWN = (-97.7431, 30.2686)
AUSTIN_CLUSTER_HALF_M = 400.0

#: S1's window, from `scenarios/s1_flood.yaml`: the first warning through the
#: cancellation.
S1_FROM = datetime(2026, 4, 20, 18, 50, tzinfo=UTC)
S1_UNTIL = datetime(2026, 4, 21, 3, 46, tzinfo=UTC)


def metric_box(lon: float, lat: float, half_m: float) -> BaseGeometry:
    """A square of `2 * half_m` a side, centred on a point.

    Sized in metres and converted per axis, so it is square on the ground
    rather than square in degrees.
    """
    d_lat = half_m / M_PER_DEG_LAT
    d_lon = half_m / (M_PER_DEG_LON_EQUATOR * max(math.cos(math.radians(lat)), 1e-6))
    return box(lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat)


def salado_creek_corridor(width_m: float = SALADO_WIDTH_M) -> BaseGeometry:
    """The flooded reach of Salado Creek, as an area."""
    centre = LineString([SALADO_WILDERNESS_RD, SALADO_BEND, SALADO_BLANCO_RD])
    return corridor_polygon(centre, width_m)


def austin_signal_cluster(half_m: float = AUSTIN_CLUSTER_HALF_M) -> BaseGeometry:
    """The downtown Austin intersections S2 darkens."""
    return metric_box(*AUSTIN_DOWNTOWN, half_m)


def s1_flood_regions(
    *, valid_from: datetime = S1_FROM, valid_until: datetime = S1_UNTIL
) -> tuple[HazardRegion, ...]:
    """S1 ground truth: Salado Creek over its banks."""
    return (
        HazardRegion(
            id="s1-salado-reach",
            hazard_class=HazardClass.FLOOD,
            geometry=salado_creek_corridor(),
            valid_from=valid_from,
            valid_until=valid_until,
            severity=4,
        ),
    )


def s2_blackout_regions(*, valid_from: datetime, valid_until: datetime) -> tuple[HazardRegion, ...]:
    """S2 ground truth: a cluster of dark signals downtown.

    The scenario file itself (`scenarios/s2_blackout.yaml`) arrives in D3.1
    with the synthetic signal recording behind it; this is the hazard area it
    will describe, available now so the simulator and its tests do not have to
    wait for the feed.
    """
    return (
        HazardRegion(
            id="s2-downtown-signals",
            hazard_class=HazardClass.SIGNAL_OUTAGE,
            geometry=austin_signal_cluster(),
            valid_from=valid_from,
            valid_until=valid_until,
            severity=3,
        ),
    )


#: City each scenario's hazards belong to, for tests and for `hu sim`.
SCENARIO_CITY: dict[str, str] = {"s1_flood": "san_antonio", "s2_blackout": "austin"}


def scenario_regions(name: str, *, valid_from: datetime, valid_until: datetime):
    """Ground-truth regions for a scenario by name."""
    if name == "s1_flood":
        return s1_flood_regions(valid_from=valid_from, valid_until=valid_until)
    if name == "s2_blackout":
        return s2_blackout_regions(valid_from=valid_from, valid_until=valid_until)
    raise ValueError(f"no ground truth for scenario {name!r}; known: {sorted(SCENARIO_CITY)}")
