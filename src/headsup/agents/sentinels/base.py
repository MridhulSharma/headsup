"""What every sentinel shares: the interface, event-time state, and geodesy.

A sentinel is one feed's reader. It takes `CivicEvent`s off the streams it
names, keeps whatever short-lived state it needs to interpret the next one,
and answers with `CandidateHazard`s - never with an advisory, never with a
decision. Fusion weighs what several sentinels say; the compiler decides.

Three things are deliberately shared rather than left to each sentinel:

* **Event time, not wall-clock time.** `observed` advances a sentinel's clock
  from the events it reads, so a recording replayed at 60x expires its own
  state 60 times faster and a sentinel under test needs no monkeypatched
  clock. A feed that goes quiet therefore never expires anything, which is
  the safe direction: the hazard stays until something says otherwise.
* **Stable candidate ids.** The same inputs produce the same id, so replaying
  a recording twice does not give fusion two readings of one hazard.
* **Metres.** Degrees are not a distance. Both sentinels measure on the WGS84
  ellipsoid, the same way `agents.compiler.geo` measures area.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

from pyproj import Geod
from shapely.affinity import scale as affine_scale
from shapely.geometry import base as shapely_base
from shapely.ops import nearest_points

from headsup.bus import events_stream
from headsup.contracts import CandidateHazard, CivicEvent, Identifier, Source
from headsup.ingest.base import derive_id
from headsup.world.bbox import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR

#: Distances are measured on the same ellipsoid the compiler measures area on.
GEOD = Geod(ellps="WGS84")


def stable_id(prefix: str, *parts: str) -> Identifier:
    """A candidate id derived from what the candidate was read off.

    The same helper the feeds mint event ids with: one hash implementation in
    the repository, and the same property - a replay reproduces the ids.
    """
    return derive_id(prefix, *parts)


def distance_m(left: shapely_base.BaseGeometry, right: shapely_base.BaseGeometry) -> float:
    """Geodesic metres between the nearest points of two geometries.

    0.0 when they touch or overlap. Shapely finds the nearest pair in degrees
    and `Geod` measures that pair properly, which is accurate enough at city
    scale and far cheaper than projecting either shape.
    """
    if left.is_empty or right.is_empty:
        return math.inf
    if left.intersects(right):
        return 0.0
    here, there = nearest_points(left, right)
    _fwd, _back, metres = GEOD.inv(here.x, here.y, there.x, there.y)
    return float(metres)


def pad_m(geometry: shapely_base.BaseGeometry, metres: float) -> shapely_base.BaseGeometry:
    """Grow a geometry by a fixed number of metres on the ground.

    One buffer radius in degrees cannot be right in both axes at once, so the
    shape is scaled into a local metric frame, buffered there and scaled back
    - the same trick `sim.masks.corridor_polygon` uses on a corridor.
    """
    if geometry.is_empty or metres <= 0:
        return geometry
    cos_lat = max(math.cos(math.radians(float(geometry.centroid.y))), 1e-6)
    sx = M_PER_DEG_LON_EQUATOR * cos_lat
    sy = M_PER_DEG_LAT
    metric = affine_scale(geometry, xfact=sx, yfact=sy, origin=(0.0, 0.0))
    return affine_scale(metric.buffer(metres), xfact=1 / sx, yfact=1 / sy, origin=(0.0, 0.0))


def noisy_or(confidence: float, weight: float) -> float:
    """Combine one more piece of corroboration into a confidence.

    The same combiner D2.5 fuses candidates with, applied inside a sentinel to
    its own corroborating readings: two independent reasons to believe raise
    the confidence without either one being allowed to reach certainty.
    """
    return 1.0 - (1.0 - confidence) * (1.0 - weight)


class Sentinel(ABC):
    """One feed's reader: events in, candidate hazards out.

    Subclasses name the streams they read and implement `read`. `on_event`
    advances the event clock and prunes expired state first, so a subclass
    never has to remember to.
    """

    #: Identifier written into `CandidateHazard.detected_by`.
    name: str
    #: The feeds this sentinel reads. A sentinel names feeds; the worker turns
    #: them into stream names, so nothing here knows about Redis.
    sources: tuple[Source, ...]
    #: How long state read off an event stays relevant.
    window: timedelta = timedelta(minutes=60)

    def __init__(self) -> None:
        self._now: datetime | None = None
        #: Events seen, candidates emitted: what the worker reports.
        self.events = 0
        self.emitted = 0

    @property
    def streams(self) -> tuple[str, ...]:
        """The `events.*` streams this sentinel's feeds arrive on."""
        return tuple(events_stream(source) for source in self.sources)

    @property
    def now(self) -> datetime | None:
        """The newest `observed_at` this sentinel has seen, or None."""
        return self._now

    def observed(self, event: CivicEvent) -> datetime:
        """Advance the event clock, never backwards, and return it.

        A late-arriving event does not rewind the clock: a feed that reissues
        an old product must not resurrect state that has already expired.
        """
        if self._now is None or event.observed_at > self._now:
            self._now = event.observed_at
        return self._now

    def expired(self, moment: datetime, *, window: timedelta | None = None) -> bool:
        """True if `moment` is older than the window, by event time."""
        if self._now is None:
            return False
        return moment < self._now - (window or self.window)

    async def on_event(self, event: CivicEvent) -> list[CandidateHazard]:
        """Read one event: the entry point the worker calls."""
        self.events += 1
        self.observed(event)
        self.prune()
        candidates = await self.read(event)
        self.emitted += len(candidates)
        return candidates

    def prune(self) -> None:
        """Drop state the window has passed.

        A hook: a sentinel that keeps nothing between events overrides nothing
        and prunes nothing.
        """
        return None

    @abstractmethod
    async def read(self, event: CivicEvent) -> list[CandidateHazard]:
        """What this sentinel makes of one event. Empty is the normal answer."""
