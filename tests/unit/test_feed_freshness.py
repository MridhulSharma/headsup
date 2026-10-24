"""The stale-feed breaker: three missed polls, per feed, in feed time.

Two halves, tested here against one clock the test owns. `ingest.freshness`
answers "has this feed stopped", `FeedHealth` is what `GET /healthz` reports
off, and `Fusion` is what spends the verdict: a candidate whose feed has gone
quiet keeps less of its vote. The end-to-end half - a replay filling
`/healthz` and a hazard losing confidence while it runs - is
`tests/e2e/test_fail_open.py`.

Every clock here is feed time. Nothing in this module reads the wall clock,
which is the property the breaker depends on: a recording replayed at 60x has
to trip it where the live run would.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fixtures.ingest import WARNING_RING
from headsup.agents.fusion import STALE_BREAKER, STALE_FLOOR, Fusion
from headsup.agents.sentinels import noisy_or
from headsup.contracts import CandidateHazard, Evidence, HazardClass, Polygon, Source
from headsup.ingest.freshness import (
    DEFAULT_INTERVAL,
    INTERVALS,
    NWS_INTERVAL,
    STALE_FACTOR,
    USGS_INTERVAL,
    FeedHealth,
    interval_s,
    is_stale,
    stale_after_s,
)

T0 = datetime(2026, 4, 20, 18, 50, tzinfo=UTC)

#: Fusion's own half-life, as a plain number of seconds, so the expected
#: confidences below are arithmetic a reader can follow.
HALF_LIFE_S = 45 * 60


def candidate(
    *,
    name: str,
    source: Source,
    confidence: float = 0.5,
    detected_by: str = "sentinel.flood",
    observed_at: datetime = T0,
) -> CandidateHazard:
    """One sentinel's reading off one feed, at one feed time."""
    return CandidateHazard(
        id=f"cand_{name}",
        hazard_class=HazardClass.FLOOD,
        severity=4,
        confidence=confidence,
        geometry=Polygon(coordinates=[WARNING_RING]),
        evidence=[
            Evidence(
                event_id=f"ev_{name}",
                source=source,
                kind="flash_flood_warning",
                observed_at=observed_at,
                raw_ref=f"data/recordings/test/{name}.jsonl#0",
            )
        ],
        detected_by=detected_by,
        detected_at=T0,
        valid_until=T0 + timedelta(hours=12),
    )


# --- the rule itself --------------------------------------------------------


def test_each_feed_is_measured_against_its_own_cadence() -> None:
    """One timeout for all four feeds would flap on NWS or never trip on USGS."""
    assert interval_s(Source.NWS) == NWS_INTERVAL
    assert interval_s(Source.USGS) == USGS_INTERVAL
    assert stale_after_s(Source.NWS) == NWS_INTERVAL * STALE_FACTOR
    assert stale_after_s(Source.USGS) == USGS_INTERVAL * STALE_FACTOR
    assert stale_after_s(Source.USGS) > stale_after_s(Source.NWS)


def test_a_feed_with_no_published_cadence_gets_the_benefit_of_the_doubt() -> None:
    """An unknown feed is assumed to be the slowest, not tripped early."""
    assert Source.SYNTHETIC not in INTERVALS
    assert interval_s(Source.SYNTHETIC) == DEFAULT_INTERVAL
    assert max(INTERVALS.values()) == DEFAULT_INTERVAL


@pytest.mark.parametrize("factor", [0.0, 1.0, 2.0, STALE_FACTOR])
def test_a_feed_inside_three_intervals_is_not_stale(factor: float) -> None:
    """Two missed polls is a slow morning. Exactly three is still allowed."""
    now = T0 + timedelta(seconds=NWS_INTERVAL * factor)
    assert not is_stale(Source.NWS, observed_at=T0, now=now)


def test_a_feed_past_three_intervals_is_stale() -> None:
    now = T0 + timedelta(seconds=NWS_INTERVAL * STALE_FACTOR + 1)
    assert is_stale(Source.NWS, observed_at=T0, now=now)


def test_the_same_silence_trips_nws_and_not_usgs() -> None:
    """Five minutes is five missed NWS polls and one missed USGS reading."""
    now = T0 + timedelta(minutes=5)
    assert is_stale(Source.NWS, observed_at=T0, now=now)
    assert not is_stale(Source.USGS, observed_at=T0, now=now)


def test_a_citation_from_slightly_ahead_of_the_clock_is_fresh() -> None:
    """A feed clock a little ahead of fusion's is not a feed that has stopped."""
    assert not is_stale(Source.NWS, observed_at=T0 + timedelta(seconds=30), now=T0)


# --- what /healthz reports off ----------------------------------------------


def test_feed_health_reports_an_age_and_a_verdict_per_feed() -> None:
    health = FeedHealth()
    health.seen(Source.NWS, T0)
    health.seen(Source.USGS, T0 + timedelta(minutes=10))

    rows = {row.source: row for row in health.status()}
    assert set(rows) == {Source.NWS, Source.USGS}
    # The clock is the newest feed time seen on any feed, so NWS has been
    # quiet for ten minutes and USGS has just spoken.
    assert rows[Source.NWS].age_s == pytest.approx(600.0)
    assert rows[Source.NWS].stale is True
    assert rows[Source.USGS].age_s == pytest.approx(0.0)
    assert rows[Source.USGS].stale is False
    assert health.stale_sources() == (Source.NWS,)


def test_a_feed_that_has_never_reported_is_not_called_stale() -> None:
    """Or every run would open with four tripped breakers."""
    health = FeedHealth()
    assert health.status() == []
    assert health.stale_sources() == ()


def test_an_out_of_order_payload_does_not_move_a_feed_backwards() -> None:
    """A replay can hand back two payloads of one poll; the newest one wins."""
    health = FeedHealth()
    health.seen(Source.USGS, T0 + timedelta(minutes=30))
    health.seen(Source.USGS, T0)
    row = health.status(now=T0 + timedelta(minutes=30))[0]
    assert row.last_event_at == T0 + timedelta(minutes=30)
    assert row.events == 2
    assert row.stale is False


def test_the_wire_shape_says_what_tripped_and_what_it_was_measured_against() -> None:
    health = FeedHealth()
    health.seen(Source.NWS, T0)
    wire = health.status(now=T0 + timedelta(minutes=10))[0].to_wire()
    assert wire == {
        "source": "nws",
        "interval_s": NWS_INTERVAL,
        "stale_after_s": NWS_INTERVAL * STALE_FACTOR,
        "last_event_at": T0.isoformat(),
        "age_s": 600.0,
        "events": 1,
        "stale": True,
    }


def test_a_stopped_run_leaves_no_feed_times_behind() -> None:
    health = FeedHealth()
    health.seen(Source.NWS, T0)
    health.reset()
    assert health.status() == []
    assert health.latest is None


# --- what fusion does with it -----------------------------------------------


async def test_a_hazard_on_a_tripped_feed_loses_confidence() -> None:
    """The same silence, against two cadences, is two different confidences.

    One NWS candidate and one USGS candidate, both read five minutes before
    fusion's clock. That is five missed NWS polls and one missed USGS reading,
    so only the NWS vote is cut by `STALE_BREAKER` - and the hazard built on
    the stopped feed is the less confident of the two.
    """
    quiet = timedelta(minutes=5)
    hazards = {}
    for source in (Source.NWS, Source.USGS):
        fusion = Fusion(window=timedelta(hours=24))
        await fusion.on_candidate(candidate(name="aged", source=source, confidence=0.8))
        hazards[source] = await fusion.on_candidate(
            # A second sentinel, far enough ahead to move fusion's clock
            # without corroborating the first candidate's feed.
            candidate(
                name="clock",
                source=Source.CITY_DISPATCH,
                confidence=0.1,
                detected_by="sentinel.other",
                observed_at=T0 + quiet,
            )
        )

    decay = max(STALE_FLOOR, 0.5 ** (quiet.total_seconds() / HALF_LIFE_S))
    assert hazards[Source.NWS].confidence == pytest.approx(
        noisy_or(0.8 * decay * STALE_BREAKER, 0.1), abs=1e-4
    )
    assert hazards[Source.USGS].confidence == pytest.approx(noisy_or(0.8 * decay, 0.1), abs=1e-4)
    assert hazards[Source.NWS].confidence < hazards[Source.USGS].confidence


async def test_a_tripped_breaker_lowers_confidence_and_expires_nothing() -> None:
    """The hazard stays, keeps its id and keeps the window its candidate asked for."""
    fusion = Fusion(window=timedelta(hours=24))
    first = await fusion.on_candidate(candidate(name="aged", source=Source.NWS, confidence=0.8))
    later = await fusion.on_candidate(
        candidate(
            name="clock",
            source=Source.CITY_DISPATCH,
            confidence=0.1,
            detected_by="sentinel.other",
            observed_at=T0 + timedelta(hours=2),
        )
    )
    assert fusion.stale_sources() == (Source.NWS,)
    assert later is not None
    assert later.id == first.id
    assert later.valid_until == first.valid_until
    assert later.confidence > 0.0


async def test_a_feed_that_is_still_reporting_trips_nothing() -> None:
    fusion = Fusion(window=timedelta(hours=24))
    await fusion.on_candidate(candidate(name="first", source=Source.USGS, confidence=0.6))
    await fusion.on_candidate(
        candidate(
            name="second",
            source=Source.USGS,
            confidence=0.6,
            detected_by="sentinel.other",
            observed_at=T0 + timedelta(minutes=4),
        )
    )
    assert fusion.stale_sources() == ()
