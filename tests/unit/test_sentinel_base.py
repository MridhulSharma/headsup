"""What every sentinel shares: metres, the event clock, and stable ids.

These are the pieces where a degree could be mistaken for a metre, so each one
is checked against a distance worked out independently of the code: the two S1
gauges are 0.0669 degrees of latitude and 0.0459 of longitude apart, which at
29.6 north is 7.43 km north and 4.44 km east, so 8.65 km between them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from shapely.geometry import LineString, Point, box

from headsup.agents.sentinels import Sentinel, distance_m, noisy_or, pad_m, stable_id
from headsup.contracts import CandidateHazard, CivicEvent, Source
from headsup.contracts import Point as GeoPoint

T0 = datetime(2026, 4, 20, 18, 55, tzinfo=UTC)

#: The two Salado Creek gauges S1 replays, from `sim.scenarios`.
WILDERNESS_RD = (-98.5655755265633, 29.630781348986)
BLANCO_RD = (-98.5196666666667, 29.5638611111111)


class Quiet(Sentinel):
    """A sentinel that keeps nothing and says nothing."""

    name = "sentinel.quiet"
    sources = (Source.NWS,)

    async def read(self, event: CivicEvent) -> list[CandidateHazard]:
        return []


def event(at: datetime, source: Source = Source.NWS) -> CivicEvent:
    return CivicEvent(
        source=source,
        kind="flash_flood_warning",
        geometry=GeoPoint(coordinates=[-98.5, 29.6]),
        observed_at=at,
        raw_ref=f"test/{at.isoformat()}",
    )


# --- metres -----------------------------------------------------------------


def test_distance_between_two_points_is_metres_on_the_ellipsoid() -> None:
    """Straight across country between the two S1 gauges: 8.65 km."""
    metres = distance_m(Point(*WILDERNESS_RD), Point(*BLANCO_RD))
    assert 8_600 <= metres <= 8_700


def test_distance_is_zero_where_geometries_meet() -> None:
    area = box(-98.6, 29.5, -98.4, 29.7)
    assert distance_m(area, Point(-98.5, 29.6)) == 0.0
    assert distance_m(area, Point(*BLANCO_RD)) == 0.0


def test_distance_is_measured_from_the_nearest_edge_not_the_centroid() -> None:
    """A gauge just outside a warning polygon is metres away, not kilometres."""
    area = box(-98.60, 29.50, -98.40, 29.70)
    just_outside = Point(-98.3999, 29.60)
    assert distance_m(area, just_outside) < 20.0
    assert distance_m(area.centroid, just_outside) > 1_000.0


def test_padding_grows_a_shape_by_metres_in_both_axes() -> None:
    """A degree of longitude is 0.87 of a degree of latitude at 30 north."""
    padded = pad_m(Point(-97.7431, 30.2686), 100.0)
    west, south, east, north = padded.bounds
    assert distance_m(Point(west, 30.2686), Point(east, 30.2686)) == pytest.approx(200, abs=5)
    assert distance_m(Point(-97.7431, south), Point(-97.7431, north)) == pytest.approx(200, abs=5)


def test_padding_a_line_makes_an_area() -> None:
    line = LineString([WILDERNESS_RD, BLANCO_RD])
    assert line.area == 0.0
    padded = pad_m(line, 50.0)
    assert padded.geom_type == "Polygon"
    assert padded.area > 0.0


def test_padding_by_nothing_changes_nothing() -> None:
    area = box(-98.6, 29.5, -98.4, 29.7)
    assert pad_m(area, 0.0) is area
    assert pad_m(area, -10.0) is area


# --- confidence -------------------------------------------------------------


def test_noisy_or_raises_confidence_without_ever_reaching_certainty() -> None:
    assert noisy_or(0.0, 0.5) == pytest.approx(0.5)
    assert noisy_or(0.7, 0.3) == pytest.approx(0.79)
    # Twenty corroborating readings still leave room for doubt.
    confidence = 0.5
    for _ in range(20):
        confidence = noisy_or(confidence, 0.3)
    assert confidence < 1.0


def test_noisy_or_is_order_independent() -> None:
    assert noisy_or(noisy_or(0.4, 0.2), 0.6) == pytest.approx(noisy_or(noisy_or(0.4, 0.6), 0.2))


# --- ids --------------------------------------------------------------------


def test_the_same_inputs_mint_the_same_candidate_id() -> None:
    """A replayed recording must not give fusion two readings of one hazard."""
    first = stable_id("cand", "sentinel.flood", "ev_abc", "ev_def")
    assert first == stable_id("cand", "sentinel.flood", "ev_abc", "ev_def")
    assert first != stable_id("cand", "sentinel.flood", "ev_abc")
    assert first.startswith("cand_")


# --- the event clock --------------------------------------------------------


async def test_the_clock_comes_from_the_events_not_the_wall() -> None:
    sentinel = Quiet()
    assert sentinel.now is None

    await sentinel.on_event(event(T0))
    assert sentinel.now == T0
    assert sentinel.events == 1

    await sentinel.on_event(event(T0 + timedelta(hours=2)))
    assert sentinel.now == T0 + timedelta(hours=2)


async def test_a_late_event_never_rewinds_the_clock() -> None:
    """A reissued old product must not resurrect expired state."""
    sentinel = Quiet()
    await sentinel.on_event(event(T0))
    await sentinel.on_event(event(T0 - timedelta(hours=3)))
    assert sentinel.now == T0


async def test_expiry_is_relative_to_the_newest_event_seen() -> None:
    sentinel = Quiet()
    assert not sentinel.expired(T0)  # nothing seen yet: nothing is stale

    await sentinel.on_event(event(T0))
    assert not sentinel.expired(T0)
    assert not sentinel.expired(T0 - timedelta(minutes=59))
    assert sentinel.expired(T0 - timedelta(minutes=61))
    assert sentinel.expired(T0 - timedelta(minutes=16), window=timedelta(minutes=15))


def test_a_sentinel_names_feeds_and_the_worker_names_streams() -> None:
    assert Quiet().streams == ("events.nws",)
