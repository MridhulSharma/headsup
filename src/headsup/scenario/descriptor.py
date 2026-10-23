"""The `scenarios/*.yaml` file that says what a recording is and where it came from.

S1's descriptor records that every value in its recording was observed by NWS
or USGS. S2's has to record the opposite just as plainly: which parts are the
city's own data (every signal id, name and coordinate) and which parts this
repository made up (which of them are dark, and when). `scripted: true` is
what the God's Eye View reads to badge the run.

The file is generated from the same `Blackout` the recording is written from,
so its counts cannot drift from the recording they describe. The prose is a
template here rather than `yaml.safe_dump` output for one reason: a scenario
descriptor is read by people, and the comments are half of what it says.

The `ground_truth` block is generated from the same cluster too. It is the
area the simulator darkens -- what is physically true while the outage is on,
independent of any advisory -- and it is the padded convex hull of the signals
this run made dark, which is the shape `agents.sentinels.signals` publishes
for the same set. Deriving it here is what keeps the three in step: the
recording says nine signals are flashing, the sentinel draws a hull round
them, and the simulator darkens the intersections inside that hull. A hazard
area invented separately would darken intersections no advisory could cover.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from shapely.geometry import MultiPoint

from headsup.agents.sentinels.base import pad_m
from headsup.agents.sentinels.signals import EPS_M, MIN_SIGNALS, PAD_M, SEVERITY
from headsup.scenario.blackout import (
    CITY,
    CLEAR_MIN,
    FULL_MIN,
    ONSET_MIN,
    ONSET_SIGNALS,
    PARTIAL_MIN,
    PARTIAL_SIGNALS,
    POLL_SECONDS,
    SCENARIO,
    THIN_MIN,
    THIN_SIGNALS,
    Blackout,
)
from headsup.sim.scenarios import AUSTIN_CLUSTER_HALF_M, AUSTIN_DOWNTOWN

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"

#: Demo speed. 90 minutes of feed time in 90 seconds.
DEFAULT_SPEED = 60

#: Decimal places for the ground-truth ring. 1e-7 degrees is about a
#: centimetre, and matches what the sentinel rounds its own hull to, so the
#: two shapes are written the same way as well as computed the same way.
RING_PLACES = 7


def ground_truth_ring(blackout: Blackout) -> list[list[float]]:
    """The hull of this blackout's dark signals, padded, as GeoJSON coordinates.

    `PAD_M` is the sentinel's own padding, for the reason the sentinel pads at
    all: the hull through a set of signal heads has them on its boundary and
    stops at the stop bars, and an area whose edge runs through the middle of
    a junction is ambiguous to resolve against a road graph.
    """
    hull = MultiPoint([(signal.lon, signal.lat) for signal in blackout.cluster]).convex_hull
    area = pad_m(hull, PAD_M)
    return [
        [round(float(x), RING_PLACES), round(float(y), RING_PLACES)]
        for x, y in area.exterior.coords
    ]


def path_for(scenario: str = SCENARIO, *, root: Path | None = None) -> Path:
    return (root or SCENARIOS_DIR) / f"{scenario}.yaml"


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def render(blackout: Blackout, *, recording: Path, events: int, bytes_: int) -> str:
    """The descriptor for one generated blackout."""
    start, end = blackout.window
    first_candidate = start + timedelta(minutes=FULL_MIN)
    cluster = "\n".join(
        f'      - {{ signal_id: "{signal.signal_id}", name: {signal.name!r} }}'
        for signal in blackout.cluster
    )
    noise = ", ".join(f'"{signal.signal_id}"' for signal in blackout.noise)
    decoys = ", ".join(f'"{signal.signal_id}"' for signal in blackout.decoys)
    points = ground_truth_ring(blackout)
    # One ring, one vertex a line: a long flow-style ring would be a single
    # 1800-character line in a file whose whole point is being read.
    ring_indented = "        - " + "\n          ".join(f"- [{x}, {y}]" for x, y in points)
    dark = len(blackout.cluster)
    lon, lat = AUSTIN_DOWNTOWN
    view_lat = round(lat - 0.0036, 4)
    try:
        relative = recording.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:  # a recording written outside the repo, e.g. in a test
        relative = recording.as_posix()

    return f"""\
# S2: downtown Austin signal blackout, {start:%d %B %Y}.
#
# GENERATED. Rebuild with:
#   uv run hu scenario synth-blackout --seed {blackout.seed}
#
# Unlike S1, this scenario's feed is not a recording of something that
# happened. The signal map under it is real - every signal id, intersection
# name and coordinate comes from the City of Austin inventory snapshot in
# data/signals/{CITY}.json - but which signals are dark, and when, is
# invented here. That is what `scripted: true` tells the viewer to badge.

id: {SCENARIO}
name: Downtown Austin signal blackout
city: {CITY}
hazard_class: SIGNAL_OUTAGE

# True because the signal states were generated. The God's Eye View labels
# any run of a scripted scenario.
scripted: true

# The one line the Test Scene launcher shows under the title.
what_if: >-
  What if a fleet treated a block of dark intersections as all-way stops
  instead of meeting them one vehicle at a time?

# Where the viewer puts the camera when this scene opens: the Congress Avenue
# grid the outage covers, looking north up the avenue.
start_view:
  lon: {lon}
  lat: {view_lat}
  height: 2200
  heading: 10
  pitch: -40

summary: >-
  A feeder failure drops {len(blackout.cluster)} signals on the Congress Avenue grid into
  unscheduled flash during the Wednesday evening peak. The outage starts with
  {ONSET_SIGNALS} intersections, which is one short of what the signal sentinel will
  publish; the rest follow five minutes later and the cluster holds for an
  hour before crews restore it in two steps. Every intersection in it is a
  real one, and together they cover the block grid a fleet crossing downtown
  cannot avoid.

window:
  start: {_stamp(start)}
  end: {_stamp(end)}
  duration_minutes: {blackout.minutes:g}
  # The poll at which the cluster first reaches {MIN_SIGNALS} signals, which is
  # the first one that can produce a candidate.
  first_candidate_at: {_stamp(first_candidate)}
  first_candidate_offset_seconds: {int(FULL_MIN * 60 / DEFAULT_SPEED)}

recording:
  fixture: {relative}
  payloads: {len(blackout.payloads)}
  records: {blackout.records}
  events: {events}
  bytes: {bytes_}
  feeds:
    austin_signals:
      payloads: {len(blackout.payloads)}
      events: {events}

feeds:
  austin_signals:
    dataset: {blackout.payloads[0].body["dataset"]}
    poll_seconds: {POLL_SECONDS:g}
    # Feed state codes, as the dataset documents them: 1 scheduled flash,
    # 2 and 7 unscheduled (conflict) flash, 3 communication issue.
    outage_state: 2
    # Minutes from `window.start`, and how many signals are dark after each.
    phases:
      - {{ at: {ONSET_MIN:g}, dark: {ONSET_SIGNALS}, note: under the sentinel threshold }}
      - {{ at: {FULL_MIN:g}, dark: {len(blackout.cluster)}, note: one SIGNAL_OUTAGE candidate }}
      - {{ at: {PARTIAL_MIN:g}, dark: {PARTIAL_SIGNALS}, note: partly restored, still a cluster }}
      - {{ at: {THIN_MIN:g}, dark: {THIN_SIGNALS}, note: back under the threshold }}
      - {{ at: {CLEAR_MIN:g}, dark: 0, note: clear }}
    cluster:
      centre: [{lon}, {lat}]
      radius_m: {AUSTIN_CLUSTER_HALF_M:g}
      signals:
{cluster}
    # Flashing throughout, far enough out that DBSCAN at eps {EPS_M:g} m has to
    # label them noise rather than fold them into the cluster.
    noise_signals: [{noise}]
    # Inside the cluster area and not part of it: one scheduled flash and two
    # communication issues. A sentinel that counted either would draw a
    # different hull.
    decoy_signals: [{decoys}]

# What is physically true while the outage is on, independent of any advisory:
# the signals above are dark, so the signalised intersections among them stop
# controlling anything. The simulator reads this block and nothing else - the
# ghost fleet meets it as well, which is the whole point of keeping ground
# truth out of the agent pipeline.
#
# The area is the convex hull of the {dark} dark signal heads padded by {PAD_M:g} m,
# which is the same shape the sentinel publishes for the same cluster. So an
# advisory this run produces covers every intersection this run darkens, and
# nothing is dark that no advisory could reach.
#
# Only the signalised intersections inside it go dark. A stop-sign corner has
# no signal to lose, and `world.graph` carries OSM's highway=traffic_signals
# over onto the node so the simulator can tell them apart.
#
# The window is the feed's own, as minutes into the run rather than absolute
# instants: the run starts whenever it is started, and what the recording
# fixes is how far into it the signals drop out ({ONSET_MIN:g}) and when the
# grid is back ({CLEAR_MIN:g}). Darkening them from the first tick instead
# would charge the fleet for ten minutes of outage no feed had mentioned yet,
# on top of the time the sentinel legitimately needs to see a cluster.
ground_truth:
  - id: s2-downtown-signals
    class: SIGNAL_OUTAGE
    severity: {SEVERITY}
    from_offset_minutes: {ONSET_MIN:g}
    until_offset_minutes: {CLEAR_MIN:g}
    geometry:
      type: Polygon
      coordinates:
{ring_indented}

expected:
  # What the signal sentinel should make of the recording.
  candidate_class: SIGNAL_OUTAGE
  candidate_severity: {SEVERITY}
  min_signals: {MIN_SIGNALS}
  eps_m: {EPS_M:g}

replay:
  default_speed: {DEFAULT_SPEED}
  streams: [events.austin_signals]

provenance:
  austin_signals:
    status: synthetic
    signal_map:
      status: real
      dataset: {blackout.inventory.dataset}
      source_url: {blackout.inventory.source_url}
      retrieved_at: {blackout.inventory.retrieved_at}
      snapshot: data/signals/{CITY}.json
      signals_in_bbox: {len(blackout.inventory)}
    envelope: generated
    note: >-
      Signal ids, intersection names and coordinates are the City of Austin's
      own, taken from the Traffic Signals and Pedestrian Signals dataset and
      trimmed to the built Austin bbox. The operation states, their
      timestamps and the poll structure are generated by
      headsup.scenario.blackout from seed {blackout.seed}; the records carry the
      columns, state codes and naive Austin-local timestamps of the live
      Traffic Signals Status feed, so the same adapter parses both.
  generator:
    module: headsup.scenario.blackout
    command: hu scenario synth-blackout --seed {blackout.seed}
    seed: {blackout.seed}
"""


def write(
    blackout: Blackout,
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
        render(blackout, recording=recording, events=events, bytes_=bytes_),
        encoding="utf-8",
        newline="\n",
    )
    return target
