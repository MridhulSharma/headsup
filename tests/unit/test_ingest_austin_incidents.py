"""The Austin incident adapter: dispatch rows in, CivicEvents out.

The three things this feed does that the structured ones do not, and that the
adapter therefore has to carry rather than clean up: a type string with no
vocabulary behind it, an address that is free text, and a row with no
coordinates at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from headsup.contracts import Source
from headsup.ingest import RawPayload, make_ref
from headsup.ingest.austin_incidents import (
    INCIDENT_DATASET,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    AustinIncidentsAdapter,
    fallback_point,
)

T0 = datetime(2026, 10, 14, 11, 20, tzinfo=UTC)

#: One real row of `dx9v-zd7x`, copied from the dataset.
REAL_ROW = {
    "traffic_report_id": "23C48E1AA4867FD335AC3497FA1061DE28CC7819_1791981373_traffic_incident",
    "published_date": "2026-10-14T12:36:13.000Z",
    "issue_reported": "Crash Service",
    "location": {"type": "Point", "coordinates": [-97.748001, 30.264367]},
    "latitude": "30.264367",
    "longitude": "-97.748001",
    "address": "Guadalupe St / W Cesar Chavez St",
    "traffic_report_status": STATUS_ACTIVE,
    "agency": "AUSTIN PD           ",
}

#: A row the board published without a position, which the city does.
UNLOCATED_ROW = {
    "traffic_report_id": "scripted-6th-guadalupe-scene",
    "published_date": "2026-10-14T11:20:00.000Z",
    "issue_reported": "Traffic Impediment",
    "address": "W 6TH ST / GUADALUPE ST",
    "traffic_report_status": STATUS_ACTIVE,
    "agency": "AUSTIN PD",
    "responding_units": "AFD E1, ATCEMS M6 DISPATCHED / PERSON DOWN IN ROADWAY",
    "scripted": True,
}


def events(*records: dict, at: datetime = T0) -> list:
    payload = RawPayload(
        source=Source.AUSTIN_INCIDENTS,
        ref=make_ref(Source.AUSTIN_INCIDENTS, at, 0),
        url=f"https://data.austintexas.gov/resource/{INCIDENT_DATASET}.json",
        fetched_at=at,
        body={"dataset": INCIDENT_DATASET, "records": list(records)},
    )
    return AustinIncidentsAdapter().parse(payload)


def test_a_real_row_keeps_its_type_string_its_address_and_its_point() -> None:
    (event,) = events(REAL_ROW)
    assert event.source is Source.AUSTIN_INCIDENTS
    # The `Kind` is the feed's wording slugified, so a type nobody has taught
    # this code still reaches the bus under a name a human recognises.
    assert event.kind == "crash_service"
    assert event.geometry.type == "Point"
    assert event.geometry.coordinates[:2] == [-97.748001, 30.264367]
    assert event.observed_at == datetime(2026, 10, 14, 12, 36, 13, tzinfo=UTC)
    assert event.payload["issue_reported"] == "Crash Service"
    assert event.payload["location_text"] == "Guadalupe St / W Cesar Chavez St"
    assert event.payload["located"] is True
    assert event.payload["archived"] is False
    # No flag on a real row: `scripted` is only ever written by this
    # repository, and only on the records it injected.
    assert "scripted" not in event.payload


def test_a_row_with_no_coordinates_is_published_as_unlocated() -> None:
    """The geometry is a placeholder and the payload says so.

    `CivicEvent.geometry` is required, and placing dispatch text is the
    sentinel's job against the road graph, so the adapter publishes the row at
    the city bbox centre with `located: false`. Nothing downstream may read
    that point as a position - and the sentinel does not.
    """
    (event,) = events(UNLOCATED_ROW)
    assert event.payload["located"] is False
    assert event.geometry.coordinates == fallback_point().coordinates
    assert event.payload["location_text"] == "W 6TH ST / GUADALUPE ST"
    assert event.payload["units_text"].startswith("AFD E1")
    assert event.payload["scripted"] is True


def test_a_report_relisted_unchanged_keeps_one_event_id() -> None:
    """The board lists an open report in every poll; that is one observation."""
    first = events(REAL_ROW, at=T0)[0]
    later = events(REAL_ROW, at=datetime(2026, 10, 14, 12, 45, tzinfo=UTC))[0]
    assert first.id == later.id


def test_a_status_update_is_a_new_observation_of_the_same_report() -> None:
    """A changed status time is what makes the archive row its own event."""
    archived = {
        **REAL_ROW,
        "traffic_report_status": STATUS_ARCHIVED,
        "traffic_report_status_date_time": "2026-10-14T13:10:25.000Z",
    }
    open_event = events(REAL_ROW)[0]
    (closed_event,) = events(archived)
    assert closed_event.id != open_event.id
    assert closed_event.payload["archived"] is True
    assert closed_event.payload["report_id"] == open_event.payload["report_id"]


def test_a_row_with_no_report_id_is_dropped() -> None:
    assert events({**REAL_ROW, "traffic_report_id": "  "}) == []


def test_the_poll_filter_asks_for_the_open_and_the_just_archived() -> None:
    """The archive row is the only thing that ever says a scene is over.

    A poll that asked for ACTIVE alone could never learn that, and one that
    asked for every archived row would answer with years of them.
    """
    where = AustinIncidentsAdapter().where(T0)
    assert f"traffic_report_status='{STATUS_ACTIVE}'" in where
    assert "traffic_report_status_date_time > '2026-10-14T11:05:00'" in where
