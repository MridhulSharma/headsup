"""The signal-outage sentinel: dark intersections clustered into one area.

One flashing signal is a maintenance ticket. Four flashing signals within a
few blocks of each other is a feeder or a comms failure, and a fleet routing
through them meets several uncontrolled intersections in a row - which is the
failure the simulator reproduces in S2. So this sentinel does not publish
single signals at all: it runs DBSCAN over the signals it currently believes
are flashing and publishes one `SIGNAL_OUTAGE` candidate per cluster.

The parameters are the ones `docs/build-packet.md` names, and both are load
bearing:

* `EPS_M` 400 m is about four downtown Austin blocks. Signals further apart
  than that are on different feeders as often as not.
* `MIN_SIGNALS` 4 (DBSCAN's `min_samples`, which counts the point itself) is
  the line between a fault and an outage. Three flashing signals publish
  nothing, however close together they are.

What counts as flashing is the feed's own `operation_state`, read by the
adapter into two booleans. A *scheduled* flash is excluded: an arterial on an
overnight flash timetable is a plan, not an outage, and clustering it would
publish the same four intersections every night. A communication issue is
excluded too - it says the management centre cannot see the signal, not that
the signal is dark.

The geometry is the cluster's convex hull, padded by `PAD_M`. The hull through
four signal heads has them on its boundary and stops at the stop bars; an
intersection's approaches reach further than that, and an advisory about an
area whose edge runs through the middle of the junction would be ambiguous to
mask onto a road graph.

**Absence is not an event.** The feed lists only the signals that are *not*
running normally, so a restored signal simply stops appearing - and a sentinel
reading an event stream never sees that. State therefore expires on event
time: a flashing signal is current for `SIGNAL_WINDOW` after it was last
reported, and a cluster that stops being reported stops being published.

**A growing cluster publishes more than once, on purpose.** Events arrive one
at a time, so a poll that darkens nine intersections is nine events, and the
cluster is four signals wide when the fourth arrives and nine when the ninth
does. Each of those is a different area, so each is a candidate with its own
id: the alternative is publishing the first reading and withholding the true
extent until the next poll, which on a 60-second time-to-advisory budget is
the worse trade. Collapsing the sequence is fusion's job - D2.5 groups
candidates by class and overlapping geometry - and an *unchanged* cluster is
not republished at all until `REPUBLISH_AFTER` has passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from shapely.geometry import MultiPoint

from headsup.agents.sentinels.base import Sentinel, pad_m, stable_id
from headsup.contracts import (
    CandidateHazard,
    CivicEvent,
    Evidence,
    HazardClass,
    Polygon,
    Source,
    utcnow,
)

SENTINEL_NAME = "sentinel.signal_outage"

#: Cluster radius, in metres on the ground.
EPS_M = 400.0

#: Signals a cluster needs. DBSCAN's `min_samples` counts the point itself, so
#: this is "four flashing signals", not "a signal with four neighbours".
MIN_SIGNALS = 4

#: Mean Earth radius, for the haversine metric DBSCAN is given. Clustering in
#: degrees would make `eps` mean a different distance on each axis.
EARTH_RADIUS_M = 6_371_008.8

#: How far past the convex hull the published area reaches: one intersection's
#: approaches, not one block.
PAD_M = 50.0

#: A flashing signal is current for this long after it was last reported. Two
#: upstream refreshes of the five-minute status feed, so one missed poll does
#: not clear an outage.
SIGNAL_WINDOW = timedelta(minutes=15)

#: How long a published candidate stays valid. Well inside
#: `playbooks/signal_outage.yaml` max_lifetime_minutes of 240: an outage that
#: is still dark will have been republished a dozen times by then, and one
#: that has been repaired should stop being advised about quickly.
TTL = timedelta(minutes=30)

#: A dark intersection is a degradation, not a closure: the junction still
#: works as an all-way stop, which is what the playbook allows. Severity stays
#: at 3 however large the cluster is; a bigger cluster raises confidence.
SEVERITY = 3

#: Confidence for a cluster at exactly `MIN_SIGNALS`, and what each further
#: signal adds. The feed is the city's own SCADA view of its signals, so a
#: reported flash is rarely wrong; the uncertainty is about the *extent* of
#: the outage, which more signals settle.
BASE_CONFIDENCE = 0.7
PER_SIGNAL = 0.03
MAX_CONFIDENCE = 0.95

#: `CandidateHazard.notes` is a `FreeText`: a note built from a dozen feed
#: supplied intersection names is cut here rather than raising on the way out.
MAX_NOTE = 2000

#: Same cluster, same candidate: the id is derived from the member signal ids,
#: so a cluster that is still exactly the same signals is not republished
#: until its own TTL has passed.
REPUBLISH_AFTER = TTL


@dataclass(slots=True)
class _Signal:
    """The latest report about one signal."""

    signal_id: str
    event: CivicEvent
    lon: float
    lat: float
    name: str

    @property
    def observed_at(self) -> datetime:
        return self.event.observed_at


def cluster_labels(points: list[tuple[float, float]]) -> list[int]:
    """DBSCAN labels for `(lon, lat)` points; -1 is noise.

    Distances are haversine on radians, so `eps` is metres on the ground in
    every direction. Fewer points than `MIN_SIGNALS` cannot form a cluster, so
    the model is not even fitted - which is also why the threshold holds in a
    process that cannot load scikit-learn at all.

    scikit-learn is imported here rather than at module scope because it loads
    a native BLAS as it initialises, and a worker running only the flood
    sentinel has no use for it.
    """
    if len(points) < MIN_SIGNALS:
        return [-1] * len(points)

    from sklearn.cluster import DBSCAN

    radians = np.radians(np.array([(lat, lon) for lon, lat in points], dtype=float))
    model = DBSCAN(
        eps=EPS_M / EARTH_RADIUS_M,
        min_samples=MIN_SIGNALS,
        metric="haversine",
        algorithm="ball_tree",
    )
    return [int(label) for label in model.fit(radians).labels_]


class SignalOutageSentinel(Sentinel):
    """Flashing signals, clustered into one SIGNAL_OUTAGE candidate each."""

    name = SENTINEL_NAME
    sources = (Source.AUSTIN_SIGNALS,)
    window = SIGNAL_WINDOW

    def __init__(self) -> None:
        super().__init__()
        self._flashing: dict[str, _Signal] = {}
        #: Candidate id -> the feed time it was published at, so an unchanged
        #: cluster is not republished on every poll. Feed time, like every
        #: other window here: a replay has to age this the same way.
        self._published: dict[str, datetime] = {}

    async def read(self, event: CivicEvent) -> list[CandidateHazard]:
        if event.source is not Source.AUSTIN_SIGNALS:
            return []
        signal_id = str(event.payload.get("signal_id") or "")
        if not signal_id or event.geometry.type != "Point":
            return []

        if not self._is_dark(event):
            # Back to normal, on a timetable, or simply unreachable. Either
            # way it is not evidence of an outage any more.
            self._flashing.pop(signal_id, None)
            return []

        lon, lat = event.geometry.coordinates[0], event.geometry.coordinates[1]
        self._flashing[signal_id] = _Signal(
            signal_id=signal_id,
            event=event,
            lon=float(lon),
            lat=float(lat),
            name=str(event.payload.get("location_name") or "").strip(),
        )
        return self._clusters()

    @staticmethod
    def _is_dark(event: CivicEvent) -> bool:
        """True for an unscheduled flash: flashing, and not to a timetable."""
        return bool(event.payload.get("flashing")) and not bool(event.payload.get("scheduled"))

    def _clusters(self) -> list[CandidateHazard]:
        """Recluster what is currently flashing and publish what is new."""
        signals = sorted(self._flashing.values(), key=lambda item: item.signal_id)
        labels = cluster_labels([(item.lon, item.lat) for item in signals])

        grouped: dict[int, list[_Signal]] = {}
        for label, signal in zip(labels, signals, strict=True):
            if label < 0:
                continue  # noise: a lone fault, not an outage
            grouped.setdefault(label, []).append(signal)

        candidates: list[CandidateHazard] = []
        for members in grouped.values():
            candidate = self._candidate(members)
            last = self._published.get(candidate.id)
            if last is not None and not self.expired(last, window=REPUBLISH_AFTER):
                continue
            self._published[candidate.id] = self.now or candidate.detected_at
            candidates.append(candidate)
        return candidates

    def _candidate(self, members: list[_Signal]) -> CandidateHazard:
        """One cluster as a candidate: padded convex hull, one Evidence each."""
        hull = MultiPoint([(item.lon, item.lat) for item in members]).convex_hull
        area = pad_m(hull, PAD_M)
        confidence = min(
            MAX_CONFIDENCE, BASE_CONFIDENCE + PER_SIGNAL * (len(members) - MIN_SIGNALS)
        )
        detected_at = utcnow()
        ids = [item.signal_id for item in members]

        return CandidateHazard(
            id=stable_id("cand", self.name, *ids),
            hazard_class=HazardClass.SIGNAL_OUTAGE,
            severity=SEVERITY,
            confidence=round(confidence, 4),
            geometry=Polygon(coordinates=_rings(area)),
            evidence=[Evidence.from_event(item.event) for item in members],
            detected_by=self.name,
            detected_at=detected_at,
            valid_until=detected_at + TTL,
            notes=self._notes(members),
        )

    def _notes(self, members: list[_Signal]) -> str:
        """Which intersections are dark, for the viewer and for fusion."""
        named = [item.name for item in members if item.name]
        head = f"{len(members)} signals in unscheduled flash"
        if not named:
            return f"{head} (signals {', '.join(item.signal_id for item in members)})"[:MAX_NOTE]
        shown = ", ".join(named[:4])
        rest = f" and {len(named) - 4} more" if len(named) > 4 else ""
        return f"{head}: {shown}{rest}"[:MAX_NOTE]

    def prune(self) -> None:
        """Forget signals the feed has stopped reporting, and old publications."""
        for signal_id, signal in list(self._flashing.items()):
            if self.expired(signal.observed_at, window=SIGNAL_WINDOW):
                del self._flashing[signal_id]
        for candidate_id, at in list(self._published.items()):
            if self.expired(at, window=REPUBLISH_AFTER):
                del self._published[candidate_id]

    @property
    def flashing(self) -> int:
        """Signals currently believed to be in unscheduled flash."""
        return len(self._flashing)


def _rings(area: object) -> list[list[list[float]]]:
    """A shapely polygon's rings as GeoJSON coordinates, rounded.

    Rounded to 1e-7 degrees, about a centimetre: a buffer produces 33 digits
    of float noise per vertex, and a candidate that travels over the bus and
    into an audit record should not carry any of it.
    """
    exterior = getattr(area, "exterior", None)
    if exterior is None:  # pragma: no cover - a padded hull is always a polygon
        raise ValueError(f"cannot take rings from {type(area).__name__}")
    rings = [exterior.coords] + [hole.coords for hole in area.interiors]  # type: ignore[attr-defined]
    return [
        [[round(float(x), 7), round(float(y), 7)] for x, y in ring]
        for ring in rings
        if len(ring) >= 4
    ]
