"""Per-feed poll intervals, and when a feed has been quiet long enough to doubt.

A feed going quiet is the failure Heads Up cannot see from the data: a gauge
that stops reporting looks exactly like a gauge whose reading has not changed,
and a hazard built on it keeps its confidence while nobody is actually
watching the water any more. The breaker here is what turns that silence into
something stated rather than assumed.

The rule is one number: a feed is **stale** once `STALE_FACTOR` of its own
poll interval has passed with nothing new from it. Three missed polls, not a
fixed wall-clock age, because the feeds run at very different cadences - NWS
reissues within the minute, a USGS gauge reports every fifteen - and one
timeout across all of them would either flap on NWS or never trip on USGS.

Two things read this table and nothing else does:

* `GET /healthz` reports each feed's age and whether it has tripped, so the
  operator sees a dead feed instead of inferring it from a quiet map.
* `fusion` cuts the vote of a candidate whose freshest citation came from a
  tripped feed (`STALE_BREAKER` there).

Staleness is measured in **feed time**, never wall-clock: a recording replayed
at 60x ages its feeds 60 times faster, and a replay has to trip the same
breaker the live run would. A tripped breaker lowers confidence and expires
nothing - that decision stays where every other expiry decision is, on the
advisory's own `valid_until`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from headsup.contracts import Source

#: Seconds between polls, per feed: `live.py` polls on these and the breaker
#: measures against them, so the cadence and the doubt cannot drift apart.
#: NWS reissues alerts within a minute; USGS gauges report every 15 minutes,
#: and Austin refreshes the signal status table every five, which is the
#: feed's own `processed_datetime` cadence rather than a guess.
NWS_INTERVAL = 60.0
USGS_INTERVAL = 300.0
SIGNALS_INTERVAL = 300.0
INCIDENTS_INTERVAL = 300.0

#: The interval a feed with no entry above is assumed to poll on. The slowest
#: of the known ones, so an unknown feed is given the benefit of the doubt
#: rather than tripped early.
DEFAULT_INTERVAL = 300.0

INTERVALS: dict[Source, float] = {
    Source.NWS: NWS_INTERVAL,
    Source.USGS: USGS_INTERVAL,
    Source.AUSTIN_SIGNALS: SIGNALS_INTERVAL,
    Source.AUSTIN_INCIDENTS: INCIDENTS_INTERVAL,
}

#: How many of its own intervals a feed may miss before it is called stale.
#: One missed poll is a dropped request and two is a slow morning; three is a
#: feed that has stopped.
STALE_FACTOR = 3.0


def interval_s(source: Source) -> float:
    """How often this feed is expected to say something, in seconds."""
    return INTERVALS.get(source, DEFAULT_INTERVAL)


def stale_after_s(source: Source) -> float:
    """The silence this feed is allowed before the breaker trips."""
    return interval_s(source) * STALE_FACTOR


def is_stale(source: Source, *, observed_at: datetime, now: datetime) -> bool:
    """Whether `now` is more than three poll intervals past `observed_at`.

    Both moments are feed time. A citation from the future - a feed clock
    slightly ahead of the one fusion is keeping - is fresh, not stale.
    """
    return (now - observed_at).total_seconds() > stale_after_s(source)


@dataclass(frozen=True, slots=True)
class FeedStatus:
    """One feed's freshness, as `GET /healthz` reports it."""

    source: Source
    interval_s: float
    stale_after_s: float
    #: Feed time of the newest thing this feed has said, or None if it has
    #: never said anything during this run.
    last_event_at: datetime | None
    #: Feed seconds since then, or None when there is nothing to measure from.
    age_s: float | None
    events: int
    stale: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "interval_s": round(self.interval_s, 3),
            "stale_after_s": round(self.stale_after_s, 3),
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "age_s": round(self.age_s, 3) if self.age_s is not None else None,
            "events": self.events,
            "stale": self.stale,
        }


@dataclass(slots=True)
class FeedHealth:
    """When each feed last said something, on the feed's own clock.

    Held by whatever is driving the feeds - the scenario runner during a
    replay - and read by `GET /healthz`. Deliberately not derived from the
    `events.*` streams: a Redis entry id is wall-clock milliseconds, and in a
    60x replay wall-clock age and feed age differ by a factor of sixty, which
    is the difference between a feed that has tripped and one that has not.
    """

    #: Feed time of the newest event seen per feed.
    last_event_at: dict[Source, datetime] = field(default_factory=dict)
    counts: dict[Source, int] = field(default_factory=dict)
    #: The latest feed time seen on any feed: what "now" means to a replay
    #: that has no other clock to offer.
    latest: datetime | None = None

    def seen(self, source: Source, at: datetime) -> None:
        """Record that `source` published something observed at feed time `at`."""
        previous = self.last_event_at.get(source)
        if previous is None or at > previous:
            self.last_event_at[source] = at
        self.counts[source] = self.counts.get(source, 0) + 1
        if self.latest is None or at > self.latest:
            self.latest = at

    def reset(self) -> None:
        self.last_event_at.clear()
        self.counts.clear()
        self.latest = None

    def status(self, *, now: datetime | None = None) -> list[FeedStatus]:
        """Every feed this run has heard from, with the breaker's verdict.

        A feed that has never reported is reported with `stale` False and no
        age: it has not gone quiet, it has not started. Saying otherwise would
        have every run begin with four tripped breakers.
        """
        moment = now or self.latest
        rows = []
        for source in sorted(self.last_event_at, key=lambda item: item.value):
            last = self.last_event_at[source]
            age = (moment - last).total_seconds() if moment is not None else None
            rows.append(
                FeedStatus(
                    source=source,
                    interval_s=interval_s(source),
                    stale_after_s=stale_after_s(source),
                    last_event_at=last,
                    age_s=age,
                    events=self.counts.get(source, 0),
                    stale=(moment is not None and is_stale(source, observed_at=last, now=moment)),
                )
            )
        return rows

    def stale_sources(self, *, now: datetime | None = None) -> tuple[Source, ...]:
        """The feeds whose breaker has tripped, in feed-name order."""
        return tuple(row.source for row in self.status(now=now) if row.stale)
