"""The S3 recording: a real Austin incident window with one scene injected.

S1 is a real event. S2 is a generated feed on a real map. S3 is the third
possible honesty, and the one that needs saying out loud: **a real recording
with scripted records inside it.**

What is real here is the window. Twelve hours of the City of Austin incident
board over the built downtown bbox - every column, every timestamp, every
dispatch type string as the city published it, pulled back out of the dataset,
which keeps its archived rows. What is scripted is one emergency scene at 6th
and Guadalupe, written the way the board's own records read, and two decoys.
Every injected record carries `"scripted": true` and no real record carries it,
so the fixture says which is which line by line.

**The poll envelope is reconstructed, and that is the one place a value
changes.** The live feed answers with the reports that are current when you
ask; the archive answers with rows that have since been archived. So a poll at
time `t` is rebuilt as the rows whose `published_date` is at or before `t` and
whose archive time is after it, with two consequences stated here because they
are the only edits made to a real row:

* `traffic_report_status` reads `ACTIVE` in a poll taken before the row was
  archived, because that is what the board said at the time, and
  `traffic_report_status_date_time` - which in the archive holds the *future*
  moment the row was archived - is left out of those polls rather than
  back-dated or invented;
* the row appears once more, verbatim and `ARCHIVED`, in the first poll after
  its archive time, which is how a reader of this feed learns a scene is over.

**Why the scripted scene is shaped the way it is.** Four records on one report
id, five minutes apart:

1. an unlocated record typed `Traffic Impediment` - no coordinates at all, the
   cross streets only in the free-text address, and a dispatch type that no
   keyword rule can call a scene;
2. the reclassification to `AUTO/ PED`, this time with coordinates;
3. a re-list while the scene is still open, which is what keeps a report on the
   board between polls;
4. the archive record that closes it.

That first record is the whole point of S3. A rules-only reading of this feed
sees `Traffic Impediment`, has no keyword for it, and places nothing - the
record has no coordinates either. A reading that goes through the dispatch text
has a person down in the roadway, two units responding and a cross-street pair
the road graph can resolve, five minutes earlier.

The two decoys are the other half of the test: a stalled vehicle and loose
livestock, both placeable, neither a scene. A sentinel that published them
would be scoring false positives on records whose text says plainly that
nothing is blocked.

    uv run hu scenario fetch-incidents          # rewrite the fixture + descriptor

This is the only networked command in the S3 build; the replay, the demo and
the tests read the committed fixture.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from headsup.contracts import Source
from headsup.ingest.austin_incidents import (
    INCIDENT_DATASET,
    SODA_BASE,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    AustinIncidentsAdapter,
    app_token,
)
from headsup.ingest.base import DEFAULT_TIMEOUT, IngestError, RawPayload, make_ref, user_agent
from headsup.ingest.recorder import FIXTURE_DIR, Recorder, scenario_dir
from headsup.world.bbox import BBox, box

CITY = "austin"
SCENARIO = "s3_incident"

#: The feed, and where a reader can check it.
DATASET = INCIDENT_DATASET
SOURCE_URL = f"{SODA_BASE}/{DATASET}.json"

#: Seconds between reconstructed polls. The board changes as fast as dispatch
#: types, and five minutes is the interval `ingest.live` polls it at, so the
#: recording has the pacing a live run would have.
POLL_SECONDS = 300.0

#: The window: Wednesday 14 October 2026, 06:00 to 18:00 Austin time. A
#: weekday daytime window over the downtown bbox, which is where the fleet
#: drives and when it is busy.
WINDOW_START = datetime(2026, 10, 14, 11, 0, tzinfo=UTC)
WINDOW_MINUTES = 720.0

#: Demo speed: twelve hours of feed time in twelve minutes.
DEFAULT_SPEED = 60

#: Where the scene is. These are the coordinates of the junction OSM puts at
#: 6th and Guadalupe in the built Austin graph, so the scripted records that
#: do carry coordinates land on the same corner the unlocated first record is
#: geocoded to, and the ground truth covers one place rather than two.
SCENE_LON = -97.746282
SCENE_LAT = 30.269042

#: How dispatch writes that corner. The sentinel's intersection lookup has to
#: survive the abbreviations, which is why they are here rather than tidied.
SCENE_ADDRESS = "W 6TH ST / GUADALUPE ST"

#: Minutes into the window at which each scripted record is published.
SCENE_FIRST_MIN = 20.0
SCENE_RECLASSIFIED_MIN = 25.0
SCENE_RELIST_MIN = 40.0
SCENE_ARCHIVED_MIN = 75.0
DECOY_STALL_MIN = 35.0
DECOY_LIVESTOCK_MIN = 50.0

#: The injected rows carry one column the real dataset does not have:
#: `responding_units`, the dispatch line naming who is on the way and what is
#: blocked. The real feed has no narrative column at all, so rather than
#: writing prose into a column that holds addresses, the injected records keep
#: their prose in a column that is visibly theirs. `provenance` in the
#: descriptor says so, and `tests/unit/test_scenario_incidents.py` holds that
#: no real row has it.
UNITS_COLUMN = "responding_units"

#: The agency on the injected records. The real rows in this window are APD's;
#: a person down in the roadway is a joint response, which is what the units
#: line says.
SCENE_AGENCY = "AUSTIN PD"


@dataclass(frozen=True, slots=True)
class ScriptedRecord:
    """One record this repository injects into a real recording."""

    #: Minutes from `WINDOW_START`.
    at: float
    report_id: str
    issue: str
    address: str
    #: Coordinates, or None for a record the feed never located.
    point: tuple[float, float] | None
    status: str = STATUS_ACTIVE
    units: str = ""
    #: One line for the descriptor, so a reader of the scenario file knows
    #: what each injected record is for.
    note: str = ""

    @property
    def located(self) -> bool:
        return self.point is not None


#: The report id every scripted scene record shares. Nothing like a real id -
#: those are a hash and an epoch - because a reader scrolling the fixture
#: should not have to check a flag to see which report was invented.
SCENE_REPORT_ID = "scripted-6th-guadalupe-scene"
DECOY_STALL_ID = "scripted-7th-stalled-vehicle"
DECOY_LIVESTOCK_ID = "scripted-15th-loose-livestock"

#: The scripted records, in the order they are published.
SCRIPT: tuple[ScriptedRecord, ...] = (
    ScriptedRecord(
        at=SCENE_FIRST_MIN,
        report_id=SCENE_REPORT_ID,
        issue="Traffic Impediment",
        address=SCENE_ADDRESS,
        point=None,
        units="AFD E1, ATCEMS M6 DISPATCHED / PERSON DOWN IN ROADWAY / NB LANES BLKD",
        note="no coordinates, and a dispatch type no keyword calls a scene",
    ),
    ScriptedRecord(
        at=SCENE_RECLASSIFIED_MIN,
        report_id=SCENE_REPORT_ID,
        issue="AUTO/ PED",
        address=SCENE_ADDRESS,
        point=(SCENE_LON, SCENE_LAT),
        units="AFD E1, ATCEMS M6 ON SCENE, APD ENRTE / NB LANES BLKD",
        note="the status update that changes the incident type, with coordinates",
    ),
    ScriptedRecord(
        at=SCENE_RELIST_MIN,
        report_id=SCENE_REPORT_ID,
        issue="AUTO/ PED",
        address=SCENE_ADDRESS,
        point=(SCENE_LON, SCENE_LAT),
        units="AFD E1, ATCEMS M6, APD ON SCENE / 1 TRANSPORTED / NB LANES BLKD",
        note="re-listed while the scene is open",
    ),
    ScriptedRecord(
        at=SCENE_ARCHIVED_MIN,
        report_id=SCENE_REPORT_ID,
        issue="AUTO/ PED",
        address=SCENE_ADDRESS,
        point=(SCENE_LON, SCENE_LAT),
        status=STATUS_ARCHIVED,
        units="SCENE CLEAR, ALL UNITS AVAILABLE",
        note="the archive record that closes it",
    ),
    ScriptedRecord(
        at=DECOY_STALL_MIN,
        report_id=DECOY_STALL_ID,
        issue="Stalled Vehicle",
        address="W 5TH ST / SAN ANTONIO ST",
        point=None,
        units="NO UNITS DISPATCHED / VEH IN PARKING LN, NOT BLKG",
        note="decoy: placeable, not a scene",
    ),
    ScriptedRecord(
        at=DECOY_LIVESTOCK_MIN,
        report_id=DECOY_LIVESTOCK_ID,
        issue="LOOSE LIVESTOCK",
        address="W 15TH ST / SAN ANTONIO ST",
        point=(-97.745_0, 30.278_9),
        units="APD ENRTE / ANIMAL OFF ROADWAY",
        note="decoy: a real dispatch type that is not an emergency scene",
    ),
)


def _stamp(moment: datetime) -> str:
    """A Socrata timestamp: UTC, no offset marker but the `Z` the feed writes."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Row:
    """One record of the recording, real or scripted, with its own clock."""

    report_id: str
    published_at: datetime
    #: When the board archived this report, or None while it was still open.
    archived_at: datetime | None
    #: The record exactly as it will be written into a poll, minus the status
    #: column, which the poll decides (see the module docstring).
    body: dict[str, Any]
    scripted: bool

    def at(self, moment: datetime, *, archived: bool) -> dict[str, Any]:
        """This row as one poll would have shown it."""
        record = dict(self.body)
        if archived:
            record["traffic_report_status"] = STATUS_ARCHIVED
            record["traffic_report_status_date_time"] = _stamp(
                self.archived_at if self.archived_at is not None else moment
            )
        else:
            record["traffic_report_status"] = STATUS_ACTIVE
        return record


def _real_row(record: dict[str, Any]) -> Row:
    """One archived row of the real dataset, as a recording row.

    Every value is kept. The two status columns are dropped from the body
    because a poll sets them: a row that had not been archived yet showed
    `ACTIVE` and no archive time.
    """
    published_at = _parse(record.get("published_date"))
    if published_at is None:
        raise IngestError(f"incident row {record.get('traffic_report_id')!r} has no published_date")
    status = str(record.get("traffic_report_status") or "").strip().upper()
    archived_at = _parse(record.get("traffic_report_status_date_time"))
    body = {
        key: value
        for key, value in record.items()
        if key not in {"traffic_report_status", "traffic_report_status_date_time"}
    }
    return Row(
        report_id=str(record.get("traffic_report_id") or ""),
        published_at=published_at,
        archived_at=archived_at if status == STATUS_ARCHIVED else None,
        body=body,
        scripted=False,
    )


def _scripted_row(record: ScriptedRecord, start: datetime) -> Row:
    """One scripted record as a recording row, in the feed's own columns."""
    published_at = start + timedelta(minutes=record.at)
    body: dict[str, Any] = {
        "traffic_report_id": record.report_id,
        "published_date": _stamp(published_at),
        "issue_reported": record.issue,
        "address": record.address,
        "agency": SCENE_AGENCY,
        # The one column the real dataset does not have, on the records that
        # are not the real dataset's.
        UNITS_COLUMN: record.units,
        # The flag the viewer badges and the tests hold: only here.
        "scripted": True,
    }
    if record.point is not None:
        lon, lat = record.point
        body["location"] = {"type": "Point", "coordinates": [lon, lat]}
        body["latitude"] = f"{lat:.6f}"
        body["longitude"] = f"{lon:.6f}"
    return Row(
        report_id=record.report_id,
        published_at=published_at,
        archived_at=published_at if record.status == STATUS_ARCHIVED else None,
        body=body,
        scripted=True,
    )


@dataclass(frozen=True, slots=True)
class Incidents:
    """One built recording: the real window, the scripted records, the polls."""

    city: str
    dataset: str
    source_url: str
    retrieved_at: str
    bbox: BBox
    start: datetime
    minutes: float
    rows: tuple[Row, ...]
    payloads: tuple[RawPayload, ...]
    #: Rows written across every poll: a report current for an hour is in
    #: twelve of them, so this is far larger than `len(rows)`.
    records: int = field(default=0)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)

    @property
    def window(self) -> tuple[datetime, datetime]:
        return self.start, self.end

    @property
    def real_rows(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if not row.scripted)

    @property
    def scripted_rows(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if row.scripted)

    @property
    def first_scene_at(self) -> datetime:
        """When the first scripted scene record is published."""
        return self.start + timedelta(minutes=SCENE_FIRST_MIN)

    def events(self) -> int:
        """How many `CivicEvent`s the adapter parses out of the whole recording."""
        parser = AustinIncidentsAdapter()
        return sum(len(parser.parse(payload)) for payload in self.payloads)


def poll_times(start: datetime, minutes: float, *, every: float = POLL_SECONDS) -> list[datetime]:
    """The instants the reconstruction polls at, from `start` inclusive."""
    count = int(minutes * 60 / every)
    return [start + timedelta(seconds=every * index) for index in range(count)]


def _shown_at(rows: Sequence[Row], moment: datetime, *, every: float) -> Iterator[dict[str, Any]]:
    """The records one poll at `moment` would have answered with.

    The latest version of each report that had been published by then, kept
    while the report was open and shown once more, archived, in the first poll
    after it closed. Newest first, which is the order the feed answers in.
    """
    window = timedelta(seconds=every)
    latest: dict[str, Row] = {}
    for row in rows:
        if row.published_at > moment:
            continue
        held = latest.get(row.report_id)
        if held is None or row.published_at >= held.published_at:
            latest[row.report_id] = row
    chosen: list[tuple[datetime, dict[str, Any]]] = []
    for row in latest.values():
        closed = row.archived_at
        if closed is None or moment < closed:
            chosen.append((row.published_at, row.at(moment, archived=False)))
        elif closed <= moment < closed + window:
            chosen.append((row.published_at, row.at(moment, archived=True)))
    for _published_at, record in sorted(chosen, key=lambda item: item[0], reverse=True):
        yield record


def build(
    real: Sequence[dict[str, Any]],
    *,
    start: datetime = WINDOW_START,
    minutes: float = WINDOW_MINUTES,
    every: float = POLL_SECONDS,
    retrieved_at: str | None = None,
    city: str = CITY,
) -> Incidents:
    """Build the recording from the real rows plus the scripted records."""
    rows = [_real_row(record) for record in real]
    rows.extend(_scripted_row(record, start) for record in SCRIPT)
    rows.sort(key=lambda row: (row.published_at, row.report_id))

    payloads: list[RawPayload] = []
    records = 0
    for seq, moment in enumerate(poll_times(start, minutes, every=every)):
        shown = list(_shown_at(rows, moment, every=every))
        records += len(shown)
        payloads.append(
            RawPayload(
                source=Source.AUSTIN_INCIDENTS,
                ref=make_ref(Source.AUSTIN_INCIDENTS, moment, seq),
                url=SOURCE_URL,
                fetched_at=moment,
                body={"dataset": DATASET, "records": shown},
            )
        )
    return Incidents(
        city=city,
        dataset=DATASET,
        source_url=SOURCE_URL,
        retrieved_at=retrieved_at or datetime.now(UTC).date().isoformat(),
        bbox=box(city),
        start=start,
        minutes=minutes,
        rows=tuple(rows),
        payloads=tuple(payloads),
        records=records,
    )


def _where(bounds: BBox, start: datetime, end: datetime) -> str:
    """Socrata's `within_box` takes the north-west corner, then the south-east."""
    return (
        f"published_date between '{start:%Y-%m-%dT%H:%M:%S}' and '{end:%Y-%m-%dT%H:%M:%S}'"
        f" AND within_box(location, {bounds.north}, {bounds.west},"
        f" {bounds.south}, {bounds.east})"
    )


async def fetch(
    *,
    start: datetime = WINDOW_START,
    minutes: float = WINDOW_MINUTES,
    city: str = CITY,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """The real rows of one window, straight out of the dataset's archive.

    The only networked call in this module. Archived rows stay in the dataset,
    so a window months old answers the same way twice - which is what makes a
    recording built from it reproducible.
    """
    import httpx

    bounds = box(city)
    end = start + timedelta(minutes=minutes)
    params = {
        "$where": _where(bounds, start, end),
        "$order": "published_date",
        "$limit": limit,
    }
    headers = {"User-Agent": user_agent()}
    token = app_token()
    if token:
        headers["X-App-Token"] = token

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get(SOURCE_URL, params=params, headers=headers)
            response.raise_for_status()
            rows = response.json()
        except httpx.HTTPError as exc:
            raise IngestError(f"incident window: GET {SOURCE_URL} failed: {exc}") from exc
        except ValueError as exc:
            raise IngestError(f"incident window: GET {SOURCE_URL} did not return JSON") from exc
    if not isinstance(rows, list):
        raise IngestError(f"incident window: GET {SOURCE_URL} returned {type(rows).__name__}")
    return [row for row in rows if isinstance(row, dict)]


def fixture_path(*, root: Path | None = None) -> Path:
    return scenario_dir(SCENARIO, root) / FIXTURE_DIR / f"{Source.AUSTIN_INCIDENTS.value}.jsonl"


def write(incidents: Incidents, *, root: Path | None = None) -> Path:
    """Write the recording as the committed fixture, replacing any older one."""
    path = fixture_path(root=root)
    if path.exists():
        path.unlink()
    with Recorder(SCENARIO, root=root, subdir=FIXTURE_DIR) as recorder:
        recorder.write_many(incidents.payloads)
    return path
