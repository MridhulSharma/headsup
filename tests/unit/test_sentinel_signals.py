"""The signal-outage sentinel: a cluster publishes, three signals do not.

Every geometry here is real: the coordinates in `fixtures.signals` are
downtown Austin intersections out of the committed signal map, so the hull a
test asserts on is the shape of actual city blocks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from shapely.geometry import MultiPoint, Point

from fixtures.signals import (
    CLUSTER,
    COMMUNICATION_ISSUE,
    FAR,
    IN_AREA,
    MIXED_RECORDS,
    SCHEDULED_FLASH,
    events,
    flashing,
    record,
)
from headsup.agents.sentinels import MIN_SIGNALS, PAD_M, SignalOutageSentinel
from headsup.agents.sentinels.base import distance_m
from headsup.agents.sentinels.signals import EPS_M, SEVERITY, TTL
from headsup.contracts import CandidateHazard, HazardClass

T0 = datetime(2026, 10, 22, 0, 45, tzinfo=UTC)


@pytest.fixture
def sentinel() -> SignalOutageSentinel:
    return SignalOutageSentinel()


async def feed(
    sentinel: SignalOutageSentinel, records: list[dict], *, at: datetime = T0
) -> list[CandidateHazard]:
    """Read a whole poll, and return every candidate it produced."""
    produced = []
    for event in events(records, at=at):
        produced.extend(await sentinel.on_event(event))
    return produced


# --- the threshold ----------------------------------------------------------


async def test_fewer_than_four_flashing_signals_emits_nothing(
    sentinel: SignalOutageSentinel,
) -> None:
    """Three dark intersections are a fault; four are an outage."""
    assert MIN_SIGNALS == 4
    assert await feed(sentinel, flashing(3)) == []
    assert sentinel.flashing == 3


async def test_the_fourth_signal_is_the_one_that_publishes(
    sentinel: SignalOutageSentinel,
) -> None:
    assert await feed(sentinel, flashing(3)) == []
    produced = await feed(sentinel, [record(CLUSTER[3])])

    assert len(produced) == 1
    candidate = produced[0]
    assert candidate.hazard_class is HazardClass.SIGNAL_OUTAGE
    assert candidate.severity == SEVERITY
    assert candidate.detected_by == "sentinel.signal_outage"
    assert len(candidate.evidence) == 4
    assert candidate.valid_until - candidate.detected_at == TTL


async def test_signals_too_far_apart_are_noise_rather_than_a_cluster(
    sentinel: SignalOutageSentinel,
) -> None:
    """Four flashing signals spread across the city are four faults."""
    spread = [
        record(CLUSTER[0]),
        record(FAR),
        record(("1041", "7TH ST / WALLER ST", -97.730077, 30.265327)),
        record(("23", "GUADALUPE ST / MLK JR BLVD", -97.741928, 30.281733)),
    ]
    assert await feed(sentinel, spread) == []
    assert sentinel.flashing == 4


# --- what counts as flashing ------------------------------------------------


async def test_a_scheduled_flash_and_a_comms_issue_are_not_an_outage(
    sentinel: SignalOutageSentinel,
) -> None:
    """A timetable is not a failure, and an unreachable signal is not dark."""
    not_dark = [
        record(CLUSTER[0], SCHEDULED_FLASH),
        record(CLUSTER[1], SCHEDULED_FLASH),
        record(CLUSTER[2], COMMUNICATION_ISSUE),
        record(CLUSTER[3], COMMUNICATION_ISSUE),
        record(IN_AREA, SCHEDULED_FLASH),
    ]
    assert await feed(sentinel, not_dark) == []
    assert sentinel.flashing == 0


async def test_a_whole_poll_counts_only_its_unscheduled_flashes(
    sentinel: SignalOutageSentinel,
) -> None:
    """The mixed poll holds four dark signals, a decoy pair and a distant one."""
    produced = await feed(sentinel, MIXED_RECORDS)

    assert len(produced) == 1
    assert sentinel.flashing == 5  # the four, plus the distant fault
    cited = {item.event_id for item in produced[-1].evidence}
    assert len(cited) == 4


async def test_a_signal_that_goes_back_to_normal_leaves_the_cluster(
    sentinel: SignalOutageSentinel,
) -> None:
    assert len(await feed(sentinel, flashing(4))) == 1
    restored = await feed(sentinel, [record(CLUSTER[0], COMMUNICATION_ISSUE)])
    assert restored == []
    assert sentinel.flashing == 3


# --- the geometry -----------------------------------------------------------


async def test_the_area_is_the_padded_convex_hull_of_the_cluster(
    sentinel: SignalOutageSentinel,
) -> None:
    (candidate,) = await feed(sentinel, flashing(4))
    area = candidate.geometry.to_shapely()

    assert candidate.geometry.type == "Polygon"
    assert area.is_valid and not area.is_empty
    for _id, _name, lon, lat in CLUSTER[:4]:
        assert area.contains(Point(lon, lat))
    # Padded by one intersection's approaches, not by a block.
    hull = MultiPoint([(lon, lat) for _i, _n, lon, lat in CLUSTER[:4]]).convex_hull
    assert PAD_M - 5 <= distance_m(hull.exterior, area.exterior) <= PAD_M + 5


async def test_four_signals_in_a_line_still_make_an_area(
    sentinel: SignalOutageSentinel,
) -> None:
    """A hull through collinear signals is a line; an advisory needs an area."""
    line = [
        record(("901", "CONGRESS AVE / 1ST ST", -97.7430, 30.2660)),
        record(("902", "CONGRESS AVE / 2ND ST", -97.7430, 30.2670)),
        record(("903", "CONGRESS AVE / 3RD ST", -97.7430, 30.2680)),
        record(("904", "CONGRESS AVE / 4TH ST", -97.7430, 30.2690)),
    ]
    (candidate,) = await feed(sentinel, line)
    assert candidate.geometry.type == "Polygon"
    assert candidate.geometry.to_shapely().area > 0


async def test_the_area_stays_well_inside_the_playbooks_cap(
    sentinel: SignalOutageSentinel,
) -> None:
    """`playbooks/signal_outage.yaml` allows 6 km2; a downtown cluster is 0.1."""
    from headsup.agents.compiler import area_km2

    await feed(sentinel, flashing(4))
    (candidate,) = await feed(sentinel, [record(CLUSTER[4])])
    assert 0.0 < area_km2(candidate.geometry) < 6.0


# --- confidence and republication -------------------------------------------


async def test_a_bigger_cluster_is_more_confident_but_no_more_severe(
    sentinel: SignalOutageSentinel,
) -> None:
    four = await feed(sentinel, flashing(4))
    five = await feed(sentinel, [record(CLUSTER[4])])

    assert five[-1].confidence > four[-1].confidence
    assert five[-1].severity == four[-1].severity == SEVERITY


async def test_the_same_cluster_is_not_republished_on_every_poll(
    sentinel: SignalOutageSentinel,
) -> None:
    """The feed restates a dark signal every five minutes for hours."""
    first = await feed(sentinel, flashing(4))
    assert len(first) == 1

    at = T0
    for _poll in range(4):
        at += timedelta(minutes=5)
        assert await feed(sentinel, flashing(4, at=at), at=at) == []


async def test_an_outage_that_is_still_dark_republishes_after_its_ttl(
    sentinel: SignalOutageSentinel,
) -> None:
    assert len(await feed(sentinel, flashing(4))) == 1
    later = T0 + TTL + timedelta(minutes=5)
    again = await feed(sentinel, flashing(4, since=later, at=later), at=later)
    assert len(again) == 1


async def test_a_cluster_whose_extent_changes_is_a_new_candidate(
    sentinel: SignalOutageSentinel,
) -> None:
    (four,) = await feed(sentinel, flashing(4))
    (five,) = await feed(sentinel, [record(CLUSTER[4])])

    assert five.id != four.id
    assert five.geometry != four.geometry
    assert EPS_M == 400.0


# --- state ------------------------------------------------------------------


async def test_a_signal_the_feed_stops_reporting_expires_on_feed_time(
    sentinel: SignalOutageSentinel,
) -> None:
    """Absence is not an event, so a flash is only current for a window."""
    assert len(await feed(sentinel, flashing(4))) == 1
    assert sentinel.flashing == 4

    later = T0 + timedelta(minutes=20)
    await feed(sentinel, [record(FAR, at=later, since=later)], at=later)
    assert sentinel.flashing == 1
