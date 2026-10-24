"""The `scenarios/s3_incident.yaml` that says what the S3 recording is.

S1's descriptor records that everything in its recording was observed. S2's
records that its signal map is real and its outage invented. S3's has to
record something finer than either: the recording is a real window of a real
feed with six records injected into it, and the descriptor says which six, why
each one is there, and what the eval may score against.

Generated from the same `Incidents` the recording is written from, so the
counts cannot drift from the fixture they describe. The prose is a template
rather than `yaml.safe_dump` output for the reason S2's is: a scenario
descriptor is read by people, and the comments are half of what it says.

The `ground_truth` block is the scripted scene and nothing else. That is a
decision worth stating: the real records in the window are real incidents, but
nothing in this repository knows how far they reached or when they ended
beyond what the board said, so labelling them would be inventing the labels
the eval is scored against. They are marked unlabelled under `scoring`
instead, and a candidate raised from one counts as neither a hit nor a false
positive.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from shapely.geometry import Point as ShapelyPoint

from headsup.agents.sentinels.incident import SCENE_RADIUS_M, SCENE_SEVERITY
from headsup.scenario.incidents import (
    CITY,
    DECOY_LIVESTOCK_ID,
    DECOY_STALL_ID,
    DEFAULT_SPEED,
    POLL_SECONDS,
    SCENARIO,
    SCENE_ADDRESS,
    SCENE_ARCHIVED_MIN,
    SCENE_FIRST_MIN,
    SCENE_LAT,
    SCENE_LON,
    SCENE_RECLASSIFIED_MIN,
    SCENE_REPORT_ID,
    SCRIPT,
    UNITS_COLUMN,
    Incidents,
)
from headsup.world.bbox import buffer_m

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"

#: Decimal places for the ground-truth ring: the same centimetre the sentinel
#: rounds its own scene disc to, so the two shapes are written the same way as
#: well as computed the same way.
RING_PLACES = 7


def path_for(scenario: str = SCENARIO, *, root: Path | None = None) -> Path:
    return (root or SCENARIOS_DIR) / f"{scenario}.yaml"


def ground_truth_ring() -> list[list[float]]:
    """The scene disc as GeoJSON coordinates.

    `SCENE_RADIUS_M` around the junction, which is exactly what
    `agents.sentinels.incident` publishes for a scene it places there. The
    area the sentinel draws and the area the simulator closes are therefore
    one shape, and an advisory this run produces covers every edge this run
    blocks - the invariant S2's descriptor spells out, for the same reason.
    """
    disc = buffer_m(ShapelyPoint(SCENE_LON, SCENE_LAT), SCENE_RADIUS_M)
    return [
        [round(float(x), RING_PLACES), round(float(y), RING_PLACES)]
        for x, y in disc.exterior.coords
    ]


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _scripted_lines() -> str:
    """One commented entry per injected record, in publication order."""
    rows = []
    for record in SCRIPT:
        rows.append(
            f"      - {{ at: {record.at:g}, report: {record.report_id!r}, "
            f"issue: {record.issue!r}, located: {str(record.located).lower()}, "
            f"status: {record.status} }}"
        )
        rows.append(f"        # {record.note}")
    return "\n".join(rows)


def _real_lines(incidents: Incidents) -> str:
    """One entry per real record kept, so a reader can check the dataset."""
    rows = []
    for row in sorted(incidents.real_rows, key=lambda item: item.published_at):
        issue = str(row.body.get("issue_reported") or "")
        address = str(row.body.get("address") or "")
        rows.append(
            f"      - {{ published: {_stamp(row.published_at)}, "
            f"issue: {issue!r}, address: {address!r} }}"
        )
    return "\n".join(rows)


def render(incidents: Incidents, *, recording: Path, events: int, bytes_: int) -> str:
    """The descriptor for one built incident recording."""
    start, end = incidents.window
    first_scene = incidents.first_scene_at
    ring = ground_truth_ring()
    # One ring, one vertex a line: a long flow-style ring would be a single
    # 1800-character line in a file whose whole point is being read.
    ring_indented = "        - " + "\n          ".join(f"- [{x}, {y}]" for x, y in ring)
    try:
        relative = recording.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:  # a recording written outside the repo, e.g. in a test
        relative = recording.as_posix()
    bounds = incidents.bbox
    first_offset = int(SCENE_FIRST_MIN * 60 / DEFAULT_SPEED)

    return f"""\
# S3: downtown Austin incident board, {start:%d %B %Y}, with one scripted scene.
#
# GENERATED. Rebuild with:
#   uv run hu scenario fetch-incidents
#
# This scenario is the third kind of honesty in `scenarios/`. S1 is a
# recording of something that happened. S2 is a generated feed on a real map.
# S3 is a real recording with scripted records inside it: twelve hours of the
# City of Austin incident board over the built downtown bbox, every column and
# timestamp as the city published them, plus {len(SCRIPT)} records this repository wrote
# and marked `scripted: true` in the fixture itself.
#
# `scripted: true` below is therefore the truth about the run as a whole, and
# the per-record flag in the recording is what says which records it is about.

id: {SCENARIO}
name: Downtown Austin emergency scene
city: {CITY}
hazard_class: EMERGENCY_SCENE

# True because the scene the run is about was written here. The God's Eye View
# labels any run of a scripted scenario.
scripted: true

# The one line the Test Scene launcher shows under the title.
what_if: >-
  What if a fleet kept the approach from the fire station clear before the
  second engine arrived, on a report dispatch had not yet called a crash?

# Where the viewer puts the camera when this scene opens: 6th at Guadalupe,
# looking east down 6th with the fire station block in frame.
start_view:
  lon: {SCENE_LON}
  lat: {SCENE_LAT}
  height: 900
  heading: 95
  pitch: -35

summary: >-
  An emergency scene at 6th and Guadalupe, reported the way this feed really
  reports one: first as a {SCRIPT[0].issue!r} with no coordinates at all and
  the cross streets only in the address line, then reclassified five minutes
  later to {SCRIPT[1].issue!r} with a position attached. Around it runs the
  real board for the same twelve hours - {len(incidents.real_rows)} downtown records, crashes
  and hazards, published and archived exactly as the city published and
  archived them. Two scripted decoys, a stalled vehicle and loose livestock,
  are placeable and are not scenes.

window:
  start: {_stamp(start)}
  end: {_stamp(end)}
  duration_minutes: {incidents.minutes:g}
  # The first scripted scene record: the clock the time-to-advisory budget is
  # measured from, and {first_offset} s into the run at the demo speed of {DEFAULT_SPEED}x.
  first_scene_at: {_stamp(first_scene)}
  first_scene_offset_seconds: {first_offset}
  # The reclassification, which is the first record a keyword rule on the
  # structured type field could act on.
  reclassified_at: {_stamp(start + timedelta(minutes=SCENE_RECLASSIFIED_MIN))}
  reclassified_offset_seconds: {int(SCENE_RECLASSIFIED_MIN * 60 / DEFAULT_SPEED)}

recording:
  fixture: {relative}
  payloads: {len(incidents.payloads)}
  # Rows written across every poll: a report that is open for an hour appears
  # in twelve of them, which is what a reader of this feed really sees.
  records: {incidents.records}
  events: {events}
  bytes: {bytes_}
  feeds:
    austin_incidents:
      payloads: {len(incidents.payloads)}
      events: {events}

feeds:
  austin_incidents:
    dataset: {incidents.dataset}
    poll_seconds: {POLL_SECONDS:g}
    bbox: [{bounds.west}, {bounds.south}, {bounds.east}, {bounds.north}]
    # The only structured field this feed has is `issue_reported`, a dispatch
    # type string with no closed vocabulary behind it. The address is free
    # text, and some records carry no coordinates. That is what makes this the
    # scenario a rules-only variant is expected to struggle with.
    structured_field: issue_reported
    real_records: {len(incidents.real_rows)}
    scripted_records: {len(incidents.scripted_rows)}
    # Every record below was written here. Minutes from `window.start`.
    scripted:
{_scripted_lines()}
    # Every record below is the city's own, kept verbatim.
    real:
{_real_lines(incidents)}

# What is physically true while the scene is open, independent of any advisory:
# the junction at {SCENE_ADDRESS} is blocked, so the edges inside the
# area below are impassable and a vehicle that drives into one stops there.
# The simulator reads this block and nothing else - the ghost fleet meets it
# as well, which is the whole point of keeping ground truth out of the agent
# pipeline.
#
# The area is {SCENE_RADIUS_M:g} m around the junction, which is the same disc
# `agents.sentinels.incident` publishes for a scene it places there. So an
# advisory this run produces covers the edges this run blocks, and nothing is
# blocked that no advisory could reach.
#
# The window is the recording's own, as minutes into the run rather than
# absolute instants: the run starts whenever it is started, and what the
# recording fixes is how far into it the scene is reported ({SCENE_FIRST_MIN:g}) and
# when it is archived ({SCENE_ARCHIVED_MIN:g}).
#
# Only the scripted scene is here. The real records in this window are real
# incidents, but nothing in this repository knows how far they reached or when
# they truly ended, so labelling them would be inventing the labels the eval is
# scored against. See `scoring` below.
ground_truth:
  - id: s3-sixth-guadalupe-scene
    class: EMERGENCY_SCENE
    severity: {SCENE_SEVERITY}
    from_offset_minutes: {SCENE_FIRST_MIN:g}
    until_offset_minutes: {SCENE_ARCHIVED_MIN:g}
    geometry:
      type: Polygon
      coordinates:
{ring_indented}

# How the ablation harness scores a run of this scenario.
scoring:
  # A hazard that overlaps a `ground_truth` entry while it is in force.
  hits_from: ground_truth
  # Candidates raised from a record that is not `scripted` are neither hits
  # nor false positives: the record is a real incident with no label.
  unlabelled: real_records
  # These two scripted records are placeable and are not emergency scenes. A
  # run that publishes an EMERGENCY_SCENE over either is a false positive, and
  # that is the one false positive this scenario can state with certainty.
  false_positive_if_scene:
    - {DECOY_STALL_ID}
    - {DECOY_LIVESTOCK_ID}

expected:
  # What the incident sentinel should make of the recording.
  candidate_class: EMERGENCY_SCENE
  scene_report: {SCENE_REPORT_ID}
  # The first record is unlocated, so the scene is placed by intersection
  # lookup against the OSM graph rather than by the feed.
  located_by: intersection_lookup
  scene_point: [{SCENE_LON}, {SCENE_LAT}]
  scene_radius_m: {SCENE_RADIUS_M:g}
  # The rules-only path matches keywords against `issue_reported` alone, so
  # the first record it can call a scene is the reclassification.
  rules_only_first_record: {SCRIPT[1].issue!r}
  action: KEEP_CLEAR

replay:
  default_speed: {DEFAULT_SPEED}
  streams: [events.austin_incidents]

provenance:
  austin_incidents:
    status: mixed
    real_records:
      status: real
      dataset: {incidents.dataset}
      source_url: {incidents.source_url}
      retrieved_at: {incidents.retrieved_at}
      window: {_stamp(start)} .. {_stamp(end)}
      bbox: [{bounds.west}, {bounds.south}, {bounds.east}, {bounds.north}]
      note: >-
        Every column of every real record is the city's own, taken from the
        Real-Time Traffic Incident Reports dataset, which keeps its archived
        rows. Report ids, dispatch type strings, addresses, coordinates,
        agencies and timestamps are verbatim.
    scripted_records:
      status: synthetic
      count: {len(incidents.scripted_rows)}
      extra_column: {UNITS_COLUMN}
      note: >-
        The {len(SCRIPT)} records listed under `feeds.austin_incidents.scripted` were
        written by headsup.scenario.incidents. They carry the feed's own
        columns plus `{UNITS_COLUMN}`, which the real dataset does not have:
        this feed has no narrative column, and rather than write dispatch prose
        into a column that holds addresses, the injected records keep it in a
        column that is visibly theirs. Only these records carry
        `scripted: true`.
    envelope: reconstructed
    note: >-
      The live feed answers with the reports current when it is asked; the
      archive answers with rows that have since been archived. A poll here is
      rebuilt as the rows published by that instant whose archive time is
      after it, newest first. Two columns therefore differ from the archive
      row: `traffic_report_status` reads ACTIVE in a poll taken before the row
      was archived, because that is what the board said then, and
      `traffic_report_status_date_time` is left out of those polls rather than
      back-dated. Each report appears once more, verbatim and ARCHIVED, in the
      first poll after its archive time.
  generator:
    module: headsup.scenario.incidents
    command: hu scenario fetch-incidents
"""


def write(
    incidents: Incidents,
    *,
    recording: Path,
    events: int,
    bytes_: int,
    root: Path | None = None,
) -> Path:
    """Write the descriptor beside the other scenarios."""
    target = path_for(root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render(incidents, recording=recording, events=events, bytes_=bytes_),
        encoding="utf-8",
        newline="\n",
    )
    return target
