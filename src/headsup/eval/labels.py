"""What was actually true during a run: the labels the metrics are scored against.

A log records what a run did. It cannot record what was true -- a run that
advised on a hazard nobody ever had and a run that advised on a real one write
the same kind of line. So the two scored metrics that need a ground truth,
false-positive area-hours and missed hazards, take it from the same place the
simulator takes it: the scenario's own `ground_truth` block, through
`sim.ground_truth.load_regions` and `sim.scenarios.scenario_regions`. Nothing
here reads a run.

**The window is the run's, not the recording's.** Ground truth is in force for
the run that replays a scenario, and a descriptor states offsets into it --
S2's signals drop out ten minutes in, S3's scene is reported at twenty and
archived at seventy-five. The caller passes the run's start instant, which the
metrics read off the log's own telemetry, so the labels line up with the
advisories tick for tick.

**Some records carry no label at all.** `scenarios/s3_incident.yaml` says so
in its `scoring` block: the real incidents in that twelve-hour window are real,
but nothing in this repository knows how far they reached or when they truly
ended, so an advisory over one is neither a hit nor a false positive. It is
counted as unlabelled and left out of both figures. The same block names the
two scripted decoys that *are* labelled -- a stalled vehicle and loose
livestock, placeable and not emergency scenes -- and an EMERGENCY_SCENE over
either is the one false positive S3 can state with certainty.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from shapely.geometry import shape as shapely_shape
from shapely.geometry.base import BaseGeometry

from headsup.contracts import HazardClass
from headsup.sim.ground_truth import HazardRegion
from headsup.sim.scenarios import SCENARIO_CITY, SCENARIOS_DIR, scenario_regions


def shape_of(geometry: Any) -> BaseGeometry:
    """A shapely geometry from a contract model, a GeoJSON mapping, or shapely.

    A log holds geometry as the mapping it was published as, and a descriptor
    holds it as the mapping it was authored as, so both ends of the scoring
    need this. `world.graph.as_shapely` does the same thing and is not used
    here: it lives behind igraph and osmnx, and nothing about measuring a
    finished run should need the routing stack loaded.
    """
    if isinstance(geometry, BaseGeometry):
        return geometry
    convert = getattr(geometry, "to_shapely", None)
    if callable(convert):
        return convert()
    return shapely_shape(geometry)


#: How long the ground-truth areas stay in force, matching `api.runner`'s own
#: `GROUND_TRUTH_HOURS`: they are the physical situation rather than an
#: advisory, so they outlast any one run.
GROUND_TRUTH_HOURS = 24

#: Hazard classes a decoy record must not be advised on as. Read from the
#: descriptor's `false_positive_if_scene`, which names the class in its key.
DECOY_CLASS = HazardClass.EMERGENCY_SCENE


@dataclass(frozen=True, slots=True)
class Decoy:
    """A record the descriptor labels as *not* the hazard class it looks like."""

    report_id: str
    wrong_class: HazardClass = DECOY_CLASS


@dataclass(slots=True)
class ScenarioLabels:
    """One scenario's ground truth, placed in one run's clock."""

    scenario_id: str
    start: datetime
    regions: tuple[HazardRegion, ...] = ()
    #: Records that are placeable and are not the class they resemble.
    decoys: tuple[Decoy, ...] = ()
    #: What the descriptor says carries no label, e.g. `real_records`.
    unlabelled: str = ""
    shapes: dict[str, BaseGeometry] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.shapes:
            self.shapes = {region.id: shape_of(region.geometry) for region in self.regions}

    def active(self, at: datetime) -> tuple[HazardRegion, ...]:
        """The regions in force at an instant."""
        return tuple(region for region in self.regions if region.active_at(at))

    def covers(self, geometry: Any, at: datetime) -> bool:
        """True if a geometry meets any region in force at `at`.

        Intersection, not containment. An advisory is scored as being about a
        hazard if it reaches it: a keep-clear corridor *to* an emergency scene
        is the advisory S3 exists to produce, and it covers the approach
        rather than the scene itself.
        """
        try:
            shape = shape_of(geometry)
        except Exception:
            return False
        return any(
            self.shapes[region.id].intersects(shape)
            for region in self.active(at)
            if region.id in self.shapes
        )

    def windows(self) -> tuple[tuple[HazardRegion, datetime, datetime], ...]:
        """Each region with its window, for the missed-hazard count."""
        return tuple((region, region.valid_from, region.valid_until) for region in self.regions)

    @property
    def labelled(self) -> bool:
        """True when this scenario states a ground truth at all."""
        return bool(self.regions)


def labels_for(
    scenario_id: str, *, start: datetime, hours: int = GROUND_TRUTH_HOURS
) -> ScenarioLabels:
    """One scenario's labels, placed in a run that started at `start`.

    The regions come from `sim.scenarios.scenario_regions`, which is what the
    simulator itself was given, so the areas scored against are byte for byte
    the areas the fleets met. A scenario with no ground truth of its own yields
    empty labels rather than raising: a run of it is still measurable on every
    metric that does not need one.
    """
    regions: tuple[HazardRegion, ...] = ()
    if scenario_id in SCENARIO_CITY:
        regions = tuple(
            scenario_regions(
                scenario_id,
                valid_from=start,
                valid_until=start + timedelta(hours=hours),
            )
        )
    scoring = _scoring(scenario_id)
    decoys = tuple(
        Decoy(report_id=str(name))
        for name in scoring.get("false_positive_if_scene") or ()
        if str(name).strip()
    )
    return ScenarioLabels(
        scenario_id=scenario_id,
        start=start,
        regions=regions,
        decoys=decoys,
        unlabelled=str(scoring.get("unlabelled") or ""),
    )


def _scoring(scenario_id: str, directory: Path | None = None) -> dict[str, Any]:
    """The descriptor's `scoring` block, or an empty one."""
    path = (directory or SCENARIOS_DIR) / f"{scenario_id}.yaml"
    if not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = document.get("scoring")
    return dict(block) if isinstance(block, dict) else {}


def decoy_hit(evidence_ids: Iterable[str], labels: ScenarioLabels) -> bool:
    """True if an advisory's citations name a record the descriptor calls a decoy.

    The citation carries the feed's own `raw_ref`, which for the incident board
    is the report id, so a decoy is recognised by what the advisory cited and
    not by where it landed. That matters: the decoys sit on streets of their
    own, and an advisory over one of them would otherwise be scored only as
    "not over a labelled region", which is also what an advisory over an
    unlabelled real incident looks like.
    """
    wanted = {decoy.report_id for decoy in labels.decoys}
    if not wanted:
        return False
    return any(any(name in str(ref) for name in wanted) for ref in evidence_ids)
