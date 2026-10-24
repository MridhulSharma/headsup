"""`hu scenario fetch-incidents`: the committed recording says what it is.

Most of these run against a handful of hand-written "real" rows, so they do
not depend on what the city's board happened to hold on 14 October 2026. The
last few read the committed fixture and descriptor, because "the real rows are
real and only the injected ones are flagged" is the claim this scenario rests
on, and only the committed files can support it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import orjson
import pytest
import yaml

from headsup.agents.sentinels.incident import SCENE_RADIUS_M, IncidentType, rules_fields
from headsup.contracts import Polygon, Source
from headsup.ingest import AustinIncidentsAdapter, RawPayload, Recording
from headsup.ingest.austin_incidents import STATUS_ACTIVE, STATUS_ARCHIVED
from headsup.scenario import incident_descriptor as descriptor_mod
from headsup.scenario import incidents as mod
from headsup.sim.ground_truth import load_regions

#: Two rows in the shape the dataset answers with, one archived inside the
#: window and one still open when it ended.
REAL_ROWS = [
    {
        "traffic_report_id": "AAA_1791981373_traffic_incident",
        "published_date": "2026-10-14T12:36:13.000Z",
        "issue_reported": "Crash Service",
        "location": {"type": "Point", "coordinates": [-97.748001, 30.264367]},
        "latitude": "30.264367",
        "longitude": "-97.748001",
        "address": "Guadalupe St / W Cesar Chavez St",
        "traffic_report_status": STATUS_ARCHIVED,
        "traffic_report_status_date_time": "2026-10-14T13:10:25.000Z",
        "agency": "AUSTIN PD           ",
    },
    {
        "traffic_report_id": "BBB_1792018078_traffic_incident",
        "published_date": "2026-10-14T22:47:58.000Z",
        "issue_reported": "Crash Urgent",
        "location": {"type": "Point", "coordinates": [-97.733441, 30.262253]},
        "latitude": "30.262253",
        "longitude": "-97.733441",
        "address": "1000-1020 E 3rd St",
        "traffic_report_status": STATUS_ARCHIVED,
        "traffic_report_status_date_time": "2026-10-15T03:05:14.000Z",
        "agency": "AUSTIN PD           ",
    },
]


@pytest.fixture
def built() -> mod.Incidents:
    return mod.build(REAL_ROWS, retrieved_at="2026-10-23")


def records_of(incidents: mod.Incidents) -> list[dict]:
    return [row for payload in incidents.payloads for row in payload.body["records"]]


# --- the polls --------------------------------------------------------------


def test_every_poll_is_one_feed_refresh_apart(built: mod.Incidents) -> None:
    moments = [payload.fetched_at for payload in built.payloads]
    assert moments[0] == mod.WINDOW_START
    gaps = {(later - earlier).total_seconds() for earlier, later in pairwise(moments)}
    assert gaps == {mod.POLL_SECONDS}
    assert len(built.payloads) == int(mod.WINDOW_MINUTES * 60 / mod.POLL_SECONDS)


def test_a_report_is_listed_in_every_poll_while_it_is_open(built: mod.Incidents) -> None:
    """Which is what the live board does, and what the sentinel has to survive."""
    showing = [
        payload.fetched_at
        for payload in built.payloads
        if any(
            row["traffic_report_id"] == "AAA_1791981373_traffic_incident"
            for row in payload.body["records"]
        )
    ]
    # Published 12:36, archived 13:10: seven polls open plus the archive one.
    assert showing[0] == datetime(2026, 10, 14, 12, 40, tzinfo=UTC)
    assert showing[-1] == datetime(2026, 10, 14, 13, 15, tzinfo=UTC)


def test_a_poll_before_the_archive_time_says_the_report_was_active(
    built: mod.Incidents,
) -> None:
    """The one place a real value is rewritten, and it is rewritten to the truth.

    The archive row holds the *future* moment the board closed the report. A
    poll taken before then would have said ACTIVE and would have carried no
    archive time, so that is what the reconstruction writes - rather than
    back-dating a status the board had not set yet.
    """
    early = next(
        payload
        for payload in built.payloads
        if payload.fetched_at == datetime(2026, 10, 14, 12, 40, tzinfo=UTC)
    )
    (row,) = [r for r in early.body["records"] if r["traffic_report_id"].startswith("AAA")]
    assert row["traffic_report_status"] == STATUS_ACTIVE
    assert "traffic_report_status_date_time" not in row
    # Everything else is the dataset's own.
    assert row["address"] == "Guadalupe St / W Cesar Chavez St"
    assert row["agency"] == "AUSTIN PD           "


def test_a_report_appears_once_more_archived_after_it_closes(built: mod.Incidents) -> None:
    closing = [
        payload
        for payload in built.payloads
        if any(
            r["traffic_report_id"].startswith("AAA")
            and r["traffic_report_status"] == STATUS_ARCHIVED
            for r in payload.body["records"]
        )
    ]
    assert len(closing) == 1
    assert closing[0].fetched_at == datetime(2026, 10, 14, 13, 15, tzinfo=UTC)


def test_a_report_still_open_when_the_window_ended_never_archives(
    built: mod.Incidents,
) -> None:
    rows = [row for row in records_of(built) if row["traffic_report_id"].startswith("BBB")]
    assert rows
    assert {row["traffic_report_status"] for row in rows} == {STATUS_ACTIVE}


# --- the scripted records ---------------------------------------------------


def test_only_the_injected_records_are_flagged(built: mod.Incidents) -> None:
    flagged = {row["traffic_report_id"] for row in records_of(built) if row.get("scripted")}
    assert flagged == {
        mod.SCENE_REPORT_ID,
        mod.DECOY_STALL_ID,
        mod.DECOY_LIVESTOCK_ID,
    }
    real = [row for row in records_of(built) if not row.get("scripted")]
    assert real
    assert all("scripted" not in row for row in real)
    # And the one column the real dataset does not have is only on theirs.
    assert all(mod.UNITS_COLUMN not in row for row in real)


def test_the_first_scene_record_has_no_coordinates_at_all(built: mod.Incidents) -> None:
    first = min(
        (row for row in built.scripted_rows if row.report_id == mod.SCENE_REPORT_ID),
        key=lambda row: row.published_at,
    )
    assert first.published_at == built.first_scene_at
    assert "location" not in first.body
    assert "latitude" not in first.body
    assert first.body["address"] == mod.SCENE_ADDRESS


def test_the_first_scene_record_is_a_type_no_keyword_calls_a_scene(
    built: mod.Incidents,
) -> None:
    """The whole point of S3, asserted against the rules path itself."""
    parser = AustinIncidentsAdapter()
    first_poll = next(
        payload
        for payload in built.payloads
        if any(row["traffic_report_id"] == mod.SCENE_REPORT_ID for row in payload.body["records"])
    )
    (event,) = [
        event
        for event in parser.parse(first_poll)
        if event.payload["report_id"] == mod.SCENE_REPORT_ID
    ]
    assert rules_fields(event).incident_type is IncidentType.UNKNOWN
    assert event.payload["located"] is False


def test_the_reclassification_changes_the_type_on_the_same_report(
    built: mod.Incidents,
) -> None:
    scene = sorted(
        (row for row in built.scripted_rows if row.report_id == mod.SCENE_REPORT_ID),
        key=lambda row: row.published_at,
    )
    types = [row.body["issue_reported"] for row in scene]
    assert types[0] != types[1]
    assert rules_fields_type(types[1]) is IncidentType.CRASH
    # One report id throughout, which is what makes it an update rather than
    # a second incident.
    assert {row.report_id for row in scene} == {mod.SCENE_REPORT_ID}
    assert scene[-1].archived_at is not None


def rules_fields_type(issue: str) -> IncidentType:
    """The rules path's reading of one type string, without a whole event."""
    from headsup.agents.sentinels.incident import TYPE_KEYWORDS

    for keyword, found in TYPE_KEYWORDS:
        if keyword in issue.lower():
            return found
    return IncidentType.UNKNOWN


def test_the_decoys_are_not_scenes_to_either_path(built: mod.Incidents) -> None:
    decoys = [
        row
        for row in built.scripted_rows
        if row.report_id in {mod.DECOY_STALL_ID, mod.DECOY_LIVESTOCK_ID}
    ]
    assert len(decoys) == 2
    for row in decoys:
        assert rules_fields_type(str(row.body["issue_reported"])) in {
            IncidentType.STALL,
            IncidentType.HAZARD,
        }


def test_writing_twice_replaces_the_recording_rather_than_appending(
    built: mod.Incidents, tmp_path: Path
) -> None:
    first = mod.write(built, root=tmp_path)
    size = first.stat().st_size
    again = mod.write(built, root=tmp_path)
    assert again == first
    assert again.stat().st_size == size


def test_the_rows_are_what_the_adapter_parses_back_out(
    built: mod.Incidents, tmp_path: Path
) -> None:
    """Written, read back and parsed: the recording's value is the round trip."""
    mod.write(built, root=tmp_path)
    recording = Recording.locate(mod.SCENARIO, root=tmp_path)
    assert recording.sources() == [Source.AUSTIN_INCIDENTS]
    parser = AustinIncidentsAdapter()
    events = [event for payload in recording.payloads() for event in parser.parse(payload)]
    assert len(events) == built.events()
    assert {event.source for event in events} == {Source.AUSTIN_INCIDENTS}


# --- the committed files ----------------------------------------------------


@pytest.fixture
def descriptor() -> dict:
    path = descriptor_mod.path_for()
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def committed() -> Recording:
    return Recording.locate(mod.SCENARIO)


def test_the_committed_fixture_is_small_enough_to_commit(committed: Recording) -> None:
    """Two megabytes is the limit `CLAUDE.md` puts on a committed recording."""
    assert 0 < committed.size_bytes < 2 * 1024 * 1024


def test_every_line_of_the_committed_fixture_is_a_raw_payload() -> None:
    path = mod.fixture_path()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        payload = RawPayload.model_validate(orjson.loads(line))
        assert payload.source is Source.AUSTIN_INCIDENTS
        assert payload.url == mod.SOURCE_URL
        assert isinstance(payload.body["records"], list)


def test_the_committed_descriptor_matches_the_committed_recording(
    descriptor: dict, committed: Recording
) -> None:
    payloads = committed.payloads()
    parser = AustinIncidentsAdapter()
    events = sum(len(parser.parse(payload)) for payload in payloads)
    assert descriptor["recording"]["payloads"] == len(payloads)
    assert descriptor["recording"]["events"] == events
    assert descriptor["recording"]["bytes"] == committed.size_bytes
    assert descriptor["recording"]["records"] == sum(
        len(payload.body["records"]) for payload in payloads
    )


def test_the_committed_recording_flags_only_the_injected_records(
    descriptor: dict, committed: Recording
) -> None:
    rows = [row for payload in committed.payloads() for row in payload.body["records"]]
    flagged = {row["traffic_report_id"] for row in rows if row.get("scripted")}
    assert len(flagged) == 3
    reported = {entry["report"] for entry in descriptor["feeds"]["austin_incidents"]["scripted"]}
    assert flagged == reported
    assert descriptor["feeds"]["austin_incidents"]["scripted_records"] == len(mod.SCRIPT)


def test_the_committed_real_records_are_the_ones_the_descriptor_lists(
    descriptor: dict, committed: Recording
) -> None:
    rows = [row for payload in committed.payloads() for row in payload.body["records"]]
    real = {
        row["traffic_report_id"]: str(row["issue_reported"])
        for row in rows
        if not row.get("scripted")
    }
    listed = descriptor["feeds"]["austin_incidents"]["real"]
    assert len(real) == descriptor["feeds"]["austin_incidents"]["real_records"] == len(listed)
    assert sorted(entry["issue"] for entry in listed) == sorted(real.values())


def test_the_ground_truth_is_the_scene_disc_the_sentinel_would_draw(
    descriptor: dict,
) -> None:
    (entry,) = descriptor["ground_truth"]
    assert entry["class"] == "EMERGENCY_SCENE"
    disc = Polygon.model_validate(entry["geometry"]).to_shapely()
    centre = disc.centroid
    assert centre.x == pytest.approx(mod.SCENE_LON, abs=1e-5)
    assert centre.y == pytest.approx(mod.SCENE_LAT, abs=1e-5)
    # The descriptor states the radius it was drawn with, and it is the
    # sentinel's own.
    assert descriptor["expected"]["scene_radius_m"] == SCENE_RADIUS_M


def test_the_ground_truth_window_is_the_recordings_own_offsets(descriptor: dict) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    (region,) = load_regions(
        descriptor_mod.path_for(), valid_from=start, valid_until=start + timedelta(hours=12)
    )
    assert region.valid_from == start + timedelta(minutes=mod.SCENE_FIRST_MIN)
    assert region.valid_until == start + timedelta(minutes=mod.SCENE_ARCHIVED_MIN)


def test_the_descriptor_says_which_records_the_eval_may_score(descriptor: dict) -> None:
    """Real records carry no label, so they are neither hits nor false positives."""
    scoring = descriptor["scoring"]
    assert scoring["hits_from"] == "ground_truth"
    assert scoring["unlabelled"] == "real_records"
    assert scoring["false_positive_if_scene"] == [mod.DECOY_STALL_ID, mod.DECOY_LIVESTOCK_ID]


def test_the_descriptor_records_what_is_real_and_what_is_not(descriptor: dict) -> None:
    provenance = descriptor["provenance"]["austin_incidents"]
    assert provenance["status"] == "mixed"
    assert provenance["real_records"]["status"] == "real"
    assert provenance["real_records"]["dataset"] == mod.DATASET
    assert provenance["scripted_records"]["status"] == "synthetic"
    assert provenance["scripted_records"]["count"] == len(mod.SCRIPT)
    assert provenance["envelope"] == "reconstructed"
    assert descriptor["scripted"] is True


def test_the_demo_budget_is_what_the_descriptor_claims(descriptor: dict) -> None:
    """20 minutes of feed time at 60x is the 20 s the e2e measures from."""
    window = descriptor["window"]
    assert window["first_scene_offset_seconds"] == int(mod.SCENE_FIRST_MIN * 60 / mod.DEFAULT_SPEED)
    assert descriptor["replay"]["default_speed"] == mod.DEFAULT_SPEED
