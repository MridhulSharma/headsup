"""Every figure in the ablation table, computed by reading one audit log.

    report = measure(run)      # run: RunContext naming the log and the variant

This module opens a run's hash-chained JSONL, verifies it, and counts. It
imports no simulator, starts no agent and reads no in-process state: the only
inputs are the lines of `data/audit/<run_id>.jsonl` and -- for the two metrics
that need to know what was *true* -- the scenario's labels from
`eval.labels`. That is a deliberate constraint rather than a convenience.
`llm/meter.py` promises that dollars per simulated hour comes from the audit
log and nothing else; `sim/counters.py` says the ablation table reads the
counter names; and the point of a sealed log is that a number computed off it
can be recomputed by anyone holding the file, with `hu audit verify` to say
the file is the one the run wrote.

**Which fleet a figure is about.** Every variant but V0 steps the advised
fleet beside the unadvised baseline on one clock, and telemetry says which is
which (`ghost`). A variant's own figures are read off the advised fleet; V0
has only the baseline, which is what V0 means. The ghost side of a V1-V4 run
is read too, because the within-run baseline is a better control than another
run: same seed, same roads, same tick loop, and the only difference is whether
the advice was followed.

**What the log can and cannot say.**

* Counters are cumulative and arrive on every telemetry frame, so a total is
  the last frame's value and a peak is the maximum over frames. Nothing is
  summed across frames, which would count the same vehicle-second many times.
* An assist wait is measured from the frames themselves: a request names its
  vehicle, and the frames after it say how long that vehicle stayed
  `AWAITING_ASSIST`. Nothing publishes "assist closed", so this is the honest
  reconstruction and it is bounded below by the telemetry stride.
* Stage latency is the hand-off between two lines whose ids match -- the
  event a candidate cites, the candidate a hazard fuses, the proposal an
  advisory was compiled from. It is wall-clock between two publishes, which at
  a replay speed of 60x is still wall-clock: the agents run in real time
  whatever the feed clock does.
* Trip minutes are derived, not recorded: no line says how long a trip took.
  `vehicles x simulated minutes / completed rides` is the mean trip length
  over the run, and the extra minutes a reroute cost is the difference between
  the advised fleet's mean and the baseline's, spread over the reroutes the
  counters recorded. It is an average of averages and the table says so.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from headsup.agents.compiler.geo import GeometryError, area_km2
from headsup.audit.chain import AuditRecord, ChainError, records, verify, verify_file
from headsup.bus import (
    EVENTS_PREFIX,
    STREAM_ADVISORIES,
    STREAM_ASSIST,
    STREAM_AUDIT,
    STREAM_CANDIDATES,
    STREAM_HAZARDS,
    STREAM_PROPOSALS,
    STREAM_TELEMETRY,
)
from headsup.eval.labels import ScenarioLabels, decoy_hit, labels_for, shape_of
from headsup.eval.variants import Variant, VariantSpec, spec_for
from headsup.sim import counters as C
from headsup.sim.config import SimMode

#: The percentile the brief asks for, on waits and on agent latency.
P95 = 0.95

SECONDS_PER_HOUR = 3600.0
SECONDS_PER_MINUTE = 60.0

#: Vehicle states that mean a vehicle is in the assist queue. `STALLED` is
#: included because a vehicle stalls first and is only promoted to
#: `AWAITING_ASSIST` after `stall_to_assist_s`, and a wait that started at the
#: request has already been running for those seconds.
WAITING_STATES = frozenset({"AWAITING_ASSIST", "STALLED"})

#: The type name the LLM meter publishes on the `audit` stream.
LLM_CALL_TYPE = "LlmCall"
#: The type name the compiler publishes there.
COMPILE_AUDIT_TYPE = "CompileAudit"


@dataclass(frozen=True, slots=True)
class RunContext:
    """Which run a log belongs to. Written by the sweep, read by the metrics."""

    run_id: str
    scenario: str
    variant: Variant
    seed: int
    #: Vehicles the run was configured with, for a sanity check against what
    #: the frames actually carried.
    vehicles: int = 0
    speed: float = 1.0
    #: Wall-clock minutes the run was given.
    minutes: float = 0.0
    #: Where the log lives, when it is not under the default audit directory.
    directory: Path | None = None
    error: str = ""

    @property
    def spec(self) -> VariantSpec:
        return spec_for(self.variant)


@dataclass(slots=True)
class FleetSeries:
    """One fleet set's telemetry, as the counters and the states over time."""

    mode: str
    ticks: tuple[int, ...] = ()
    sim_times: tuple[datetime, ...] = ()
    #: `assist_open_total` per frame, for the peak and the queue-depth chart.
    queue: tuple[int, ...] = ()
    #: The last frame's counters: every cumulative total of the run.
    last: Mapping[str, int] = field(default_factory=dict)
    #: Vehicles seen in any one frame.
    vehicles: int = 0
    frames: int = 0
    #: `{vehicle_id: ((tick, status), ...)}`, only for vehicles that ever
    #: waited: the whole fleet's history would be hundreds of thousands of
    #: pairs and nothing reads it.
    waiting: Mapping[str, tuple[tuple[int, str], ...]] = field(default_factory=dict)

    @property
    def sim_seconds(self) -> float:
        """Simulated seconds the frames span. The tick is one second."""
        if len(self.ticks) < 2:
            return 0.0
        return float(self.ticks[-1] - self.ticks[0])

    @property
    def sim_hours(self) -> float:
        return self.sim_seconds / SECONDS_PER_HOUR

    @property
    def sim_minutes(self) -> float:
        return self.sim_seconds / SECONDS_PER_MINUTE

    def total(self, metric: str) -> int:
        """One cumulative counter at the end of the run."""
        return int(self.last.get(C.key(metric), 0))

    @property
    def peak_queue(self) -> int:
        return max(self.queue, default=0)

    def mean_trip_minutes(self) -> float | None:
        """Vehicle-minutes per completed ride, or None if nothing completed."""
        rides = self.total(C.RIDES_COMPLETED)
        if rides <= 0 or self.vehicles <= 0 or self.sim_minutes <= 0:
            return None
        return (self.vehicles * self.sim_minutes) / rides


@dataclass(slots=True)
class RunMetrics:
    """Every figure the ablation reports about one run. One row of the CSV."""

    run_id: str
    scenario: str
    variant: str
    seed: int
    #: The fleet these figures are about: `live`, or `ghost` for V0.
    fleet: str = ""
    chain_ok: bool = False
    chain: str = ""
    entries: int = 0
    error: str = ""

    # what the run was
    vehicles: int = 0
    sim_minutes: float = 0.0

    # the metrics the brief names
    hazard_entries: int = 0
    time_to_advisory_s: float | None = None
    time_to_advisory_median_s: float | None = None
    assist_peak: int = 0
    assist_p95_wait_s: float = 0.0
    blocked_corridor_minutes: float = 0.0
    completed_rides: int = 0
    completed_ride_rate_per_vehicle_hour: float = 0.0
    rerouted: int = 0
    extra_minutes_per_rerouted_trip: float | None = None
    false_positive_area_hours: float = 0.0
    llm_usd: float = 0.0
    llm_usd_per_sim_hour: float = 0.0
    p95_agent_latency_ms: float | None = None

    # what the figures rest on, so a row can be read without the log
    advisories: int = 0
    advisories_unlabelled: int = 0
    missed_hazards: int = 0
    labelled_hazards: int = 0
    candidates: int = 0
    hazards: int = 0
    proposals: int = 0
    compile_published: int = 0
    compile_rejected: int = 0
    llm_calls: int = 0
    assist_requests: int = 0
    mean_trip_minutes: float | None = None
    ghost_mean_trip_minutes: float | None = None
    ghost_hazard_entries: int | None = None
    ghost_blocked_corridor_minutes: float | None = None

    #: `(simulated minute, queue depth)` for the advised fleet, and the same
    #: for the baseline. Not a CSV column: the S2 chart reads them.
    queue_curve: tuple[tuple[float, int], ...] = ()
    ghost_queue_curve: tuple[tuple[float, int], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.error

    def row(self) -> dict[str, Any]:
        """The CSV row: every scalar field, in declaration order."""
        return {
            name: getattr(self, name)
            for name in RunMetrics.__dataclass_fields__
            if name not in ("queue_curve", "ghost_queue_curve")
        }


def measure(context: RunContext, *, labels: ScenarioLabels | None = None) -> RunMetrics:
    """Read one run's log and compute every metric. Never raises.

    A log that cannot be read, or a chain that does not verify, produces a row
    that says so rather than an exception: one unreadable run must not end a
    sweep of a hundred and forty-seven, and a row with `chain_ok=false` is a
    result -- it says the numbers beside it cannot be trusted.
    """
    spec = context.spec
    fleet = SimMode.GHOST.value if SimMode.LIVE not in spec.modes else SimMode.LIVE.value
    result = RunMetrics(
        run_id=context.run_id,
        scenario=context.scenario,
        variant=context.variant.value,
        seed=context.seed,
        fleet=fleet,
        vehicles=context.vehicles,
        error=context.error,
    )
    try:
        entries = records(context.run_id, directory=context.directory)
        chain = verify(context.run_id, directory=context.directory)
    except ChainError as exc:
        result.error = result.error or str(exc)
        result.chain = str(exc)
        return result

    result.entries = len(entries)
    result.chain_ok = chain.ok
    result.chain = chain.summary()
    _fill(result, entries, context=context, labels=labels)
    return result


def measure_file(path: Path | str, context: RunContext) -> RunMetrics:
    """The same, for a log held somewhere other than the audit directory."""
    target = Path(path)
    spec = context.spec
    fleet = SimMode.GHOST.value if SimMode.LIVE not in spec.modes else SimMode.LIVE.value
    result = RunMetrics(
        run_id=context.run_id,
        scenario=context.scenario,
        variant=context.variant.value,
        seed=context.seed,
        fleet=fleet,
        vehicles=context.vehicles,
        error=context.error,
    )
    try:
        chain = verify_file(target, run_id=context.run_id)
        entries = [AuditRecord.model_validate(line) for line in _lines(target)]
    except ChainError as exc:
        result.error = result.error or str(exc)
        result.chain = str(exc)
        return result
    result.entries = len(entries)
    result.chain_ok = chain.ok
    result.chain = chain.summary()
    _fill(result, entries, context=context, labels=None)
    return result


def _lines(path: Path) -> list[dict[str, Any]]:
    from headsup.audit.chain import read_lines

    return list(read_lines(path))


def _fill(
    result: RunMetrics,
    entries: Sequence[AuditRecord],
    *,
    context: RunContext,
    labels: ScenarioLabels | None,
) -> None:
    """Every figure, off one pass of grouped lines."""
    by_stream: dict[str, list[AuditRecord]] = defaultdict(list)
    for record in entries:
        by_stream[record.stream].append(record)
    events = [
        record
        for stream, lines in by_stream.items()
        if stream.startswith(f"{EVENTS_PREFIX}.")
        for record in lines
    ]

    series = _fleets(by_stream.get(STREAM_TELEMETRY, ()))
    own = series.get(result.fleet) or FleetSeries(mode=result.fleet)
    ghost = series.get(SimMode.GHOST.value) if result.fleet != SimMode.GHOST.value else None

    result.vehicles = own.vehicles or context.vehicles
    result.sim_minutes = round(own.sim_minutes, 3)
    result.hazard_entries = own.total(C.HAZARD_ENTRIES)
    result.blocked_corridor_minutes = _blocked_minutes(own)
    result.completed_rides = own.total(C.RIDES_COMPLETED)
    result.rerouted = own.total(C.VEHICLES_REROUTED)
    result.assist_peak = own.peak_queue
    result.queue_curve = _curve(own)
    result.mean_trip_minutes = _round(own.mean_trip_minutes())
    if own.vehicles and own.sim_hours > 0:
        result.completed_ride_rate_per_vehicle_hour = round(
            result.completed_rides / (own.vehicles * own.sim_hours), 4
        )
    if ghost is not None:
        result.ghost_hazard_entries = ghost.total(C.HAZARD_ENTRIES)
        result.ghost_blocked_corridor_minutes = _blocked_minutes(ghost)
        result.ghost_mean_trip_minutes = _round(ghost.mean_trip_minutes())
        result.ghost_queue_curve = _curve(ghost)
        result.extra_minutes_per_rerouted_trip = _extra_minutes(own, ghost)

    assists = [
        record
        for record in by_stream.get(STREAM_ASSIST, ())
        if bool(record.data.get("ghost", False)) == (result.fleet == SimMode.GHOST.value)
    ]
    result.assist_requests = len(assists)
    result.assist_p95_wait_s = round(percentile(_waits(assists, own), P95), 2)

    result.candidates = len(by_stream.get(STREAM_CANDIDATES, ()))
    result.hazards = len(by_stream.get(STREAM_HAZARDS, ()))
    result.proposals = len(by_stream.get(STREAM_PROPOSALS, ()))

    audit_lines = by_stream.get(STREAM_AUDIT, ())
    calls = [line for line in audit_lines if line.type == LLM_CALL_TYPE]
    result.llm_calls = len(calls)
    result.llm_usd = round(sum(float(line.data.get("usd") or 0.0) for line in calls), 6)
    if own.sim_hours > 0:
        result.llm_usd_per_sim_hour = round(result.llm_usd / own.sim_hours, 4)

    compiles = [line for line in audit_lines if line.type == COMPILE_AUDIT_TYPE]
    result.compile_published = sum(
        1 for line in compiles if line.data.get("decision") == "PUBLISHED"
    )
    result.compile_rejected = sum(1 for line in compiles if line.data.get("decision") == "REJECTED")

    advisories = by_stream.get(STREAM_ADVISORIES, ())
    result.advisories = len(advisories)
    first, median = _time_to_advisory(advisories, events)
    result.time_to_advisory_s = _round(first)
    result.time_to_advisory_median_s = _round(median)
    result.p95_agent_latency_ms = _round(
        percentile(
            _stage_latencies(
                events=events,
                candidates=by_stream.get(STREAM_CANDIDATES, ()),
                hazards=by_stream.get(STREAM_HAZARDS, ()),
                proposals=by_stream.get(STREAM_PROPOSALS, ()),
                advisories=advisories,
                compiles=compiles,
            ),
            P95,
        )
        or None
    )

    ground = labels if labels is not None else _labels(context, own)
    if ground is not None:
        scored = _score_advisories(advisories, ground, own)
        result.false_positive_area_hours = round(scored.false_positive_area_hours, 4)
        result.advisories_unlabelled = scored.unlabelled
        result.missed_hazards = scored.missed
        result.labelled_hazards = len(ground.regions)


# --- telemetry --------------------------------------------------------------


def _fleets(lines: Iterable[AuditRecord]) -> dict[str, FleetSeries]:
    """Group telemetry frames by fleet set, keeping only what is measured.

    One pass, and the per-vehicle history is kept only for vehicles that were
    ever in the queue: a 300-vehicle run at 2 Hz is tens of thousands of
    states and the only question asked of them is how long the stopped ones
    stayed stopped.
    """
    ticks: dict[str, list[int]] = defaultdict(list)
    stamps: dict[str, list[datetime]] = defaultdict(list)
    queues: dict[str, list[int]] = defaultdict(list)
    last: dict[str, Mapping[str, int]] = {}
    vehicles: dict[str, int] = defaultdict(int)
    frames: dict[str, int] = defaultdict(int)
    waiting: dict[str, dict[str, list[tuple[int, str]]]] = defaultdict(lambda: defaultdict(list))

    for record in lines:
        data = record.data
        mode = SimMode.GHOST.value if bool(data.get("ghost", False)) else SimMode.LIVE.value
        tick = int(data.get("tick") or 0)
        counters = data.get("counters") or {}
        ticks[mode].append(tick)
        moment = _at(data.get("sim_time"))
        if moment is not None:
            stamps[mode].append(moment)
        queues[mode].append(int(counters.get(C.key(C.ASSIST_OPEN), 0)))
        last[mode] = {str(key): int(value) for key, value in counters.items()}
        states = data.get("vehicles") or []
        vehicles[mode] = max(vehicles[mode], len(states))
        frames[mode] += 1
        for state in states:
            status = str(state.get("status") or "")
            if status in WAITING_STATES:
                waiting[mode][str(state.get("id"))].append((tick, status))

    out: dict[str, FleetSeries] = {}
    for mode in ticks:
        out[mode] = FleetSeries(
            mode=mode,
            ticks=tuple(ticks[mode]),
            sim_times=tuple(stamps[mode]),
            queue=tuple(queues[mode]),
            last=last.get(mode, {}),
            vehicles=vehicles[mode],
            frames=frames[mode],
            waiting={vehicle: tuple(history) for vehicle, history in waiting.get(mode, {}).items()},
        )
    return out


def _blocked_minutes(series: FleetSeries) -> float:
    """Blocked corridor vehicle-minutes, from the seconds under the minutes.

    The published minute figure is a floor division, so the seconds counter is
    read instead where it is there: a run of a few minutes would otherwise
    report zero for a corridor that held vehicles for fifty seconds.
    """
    seconds = series.last.get(C.key(C.BLOCKED_SECONDS))
    if seconds is not None:
        return round(float(seconds) / SECONDS_PER_MINUTE, 3)
    return float(series.total(C.BLOCKED_MINUTES))


def _curve(series: FleetSeries) -> tuple[tuple[float, int], ...]:
    """Queue depth against simulated minutes, for the S2 chart."""
    if not series.ticks:
        return ()
    first = series.ticks[0]
    return tuple(
        ((tick - first) / SECONDS_PER_MINUTE, depth)
        for tick, depth in zip(series.ticks, series.queue, strict=False)
    )


def _extra_minutes(own: FleetSeries, ghost: FleetSeries) -> float | None:
    """Extra minutes a rerouted trip cost, against the within-run baseline.

    The advised fleet's mean trip is longer than the baseline's by whatever
    the advice cost it; multiplied by the trips it completed, that is the total
    extra vehicle-minutes, and spread over the reroutes the counters recorded
    it is the figure the brief asks for. None when the run recorded no
    reroutes, which is the honest answer: there is nothing to divide by.
    """
    advised, baseline = own.mean_trip_minutes(), ghost.mean_trip_minutes()
    rerouted = own.total(C.VEHICLES_REROUTED)
    if advised is None or baseline is None or rerouted <= 0:
        return None
    return round(((advised - baseline) * own.total(C.RIDES_COMPLETED)) / rerouted, 3)


def _waits(assists: Sequence[AuditRecord], series: FleetSeries) -> list[float]:
    """How long each request's vehicle stayed in the queue, in simulated seconds.

    Measured off the frames: the request names a vehicle, and the frames from
    the request onwards say when that vehicle stopped being in a waiting
    state. A request whose vehicle never appears waiting in any later frame
    counts as one telemetry stride, not as zero -- it waited at least as long
    as the gap between frames.
    """
    waits: list[float] = []
    stride = _stride(series)
    for record in assists:
        vehicle = str(record.data.get("vehicle_id") or "")
        history = series.waiting.get(vehicle, ())
        if not history:
            waits.append(stride)
            continue
        opened = _at(record.data.get("requested_at"))
        start = _tick_at(series, opened)
        after = [tick for tick, _status in history if tick >= start]
        if not after:
            waits.append(stride)
            continue
        # The queue this vehicle is in now: consecutive waiting frames, which
        # is the one episode the request is about. A vehicle that stalls again
        # later in the run opens its own request and is measured there.
        episode = [after[0]]
        for tick in after[1:]:
            if tick - episode[-1] > stride:
                break
            episode.append(tick)
        waits.append(max(float(episode[-1] - episode[0]), stride))
    return waits


def _stride(series: FleetSeries) -> float:
    """Simulated seconds between telemetry frames, from the frames themselves."""
    if len(series.ticks) < 2:
        return 1.0
    gaps = [
        later - earlier
        for earlier, later in zip(series.ticks, series.ticks[1:], strict=False)
        if later > earlier
    ]
    return float(min(gaps)) if gaps else 1.0


def _tick_at(series: FleetSeries, moment: datetime | None) -> int:
    """The tick of the first frame at or after an instant; 0 without one."""
    if moment is None or not series.sim_times:
        return 0
    for tick, stamp in zip(series.ticks, series.sim_times, strict=False):
        if stamp >= moment:
            return tick
    return series.ticks[-1] if series.ticks else 0


# --- the pipeline's own timing ---------------------------------------------


def _time_to_advisory(
    advisories: Sequence[AuditRecord], events: Sequence[AuditRecord]
) -> tuple[float | None, float | None]:
    """Seconds from the earliest cited observation to each advisory.

    The citation is the join: an advisory carries `evidence[].event_id`, and
    the event with that id is a line of this same log. So this is measured
    between two publishes of one run rather than inferred from a descriptor's
    offsets -- which is also why a run that published nothing has no figure
    rather than a zero.
    """
    when: dict[str, datetime] = {}
    for record in events:
        event_id = str(record.data.get("id") or "")
        moment = _published(record)
        if event_id and moment is not None and event_id not in when:
            when[event_id] = moment
    gaps: list[float] = []
    for record in advisories:
        published = _published(record)
        if published is None:
            continue
        cited = [
            when[str(item.get("event_id"))]
            for item in _evidence(record.data)
            if str(item.get("event_id")) in when
        ]
        if not cited:
            continue
        gaps.append((published - min(cited)).total_seconds())
    if not gaps:
        return None, None
    ordered = sorted(gaps)
    return ordered[0], median(ordered)


def _stage_latencies(
    *,
    events: Sequence[AuditRecord],
    candidates: Sequence[AuditRecord],
    hazards: Sequence[AuditRecord],
    proposals: Sequence[AuditRecord],
    advisories: Sequence[AuditRecord],
    compiles: Sequence[AuditRecord],
) -> list[float]:
    """Every hand-off in the pipeline, in milliseconds.

    Four stages, each matched on an id the messages already carry: the
    observation a candidate cites, the candidate a hazard fused, the hazard a
    proposal was built on, and the proposal an advisory was compiled from. A
    variant with fewer stages (V2 publishes all four in one go) simply has
    smaller gaps, which is a result rather than a gap in the measurement.
    """
    event_at = _stamps(events)
    candidate_at = _stamps(candidates)
    hazard_at = _stamps(hazards)
    proposal_at = _stamps(proposals)
    advisory_at = _stamps(advisories)

    out: list[float] = []

    for record in candidates:
        cited = [
            event_at[str(item.get("event_id"))]
            for item in _evidence(record.data)
            if str(item.get("event_id")) in event_at
        ]
        moment = _published(record)
        if cited and moment is not None:
            out.append((moment - min(cited)).total_seconds() * 1000.0)

    for record in hazards:
        moment = _published(record)
        parents = [
            candidate_at[str(name)]
            for name in record.data.get("candidates") or ()
            if str(name) in candidate_at
        ]
        if parents and moment is not None:
            out.append((moment - max(parents)).total_seconds() * 1000.0)

    for record in proposals:
        moment = _published(record)
        hazard_id = str((record.data.get("hazard") or {}).get("id") or "")
        if moment is not None and hazard_id in hazard_at:
            out.append((moment - hazard_at[hazard_id]).total_seconds() * 1000.0)

    # The gate: a compile record names the proposal it judged and the advisory
    # it published, which is the only line that joins the two.
    for record in compiles:
        advisory_id = str(record.data.get("advisory_id") or "")
        proposal_id = str(record.data.get("proposal_id") or "")
        if advisory_id in advisory_at and proposal_id in proposal_at:
            out.append(
                (advisory_at[advisory_id] - proposal_at[proposal_id]).total_seconds() * 1000.0
            )

    return [value for value in out if value >= 0.0]


def _stamps(lines: Sequence[AuditRecord]) -> dict[str, datetime]:
    """`{message id: when it was published}`, first write wins."""
    out: dict[str, datetime] = {}
    for record in lines:
        name = str(record.data.get("id") or "")
        moment = _published(record)
        if name and moment is not None and name not in out:
            out[name] = moment
    return out


# --- what was true ----------------------------------------------------------


@dataclass(slots=True)
class Scored:
    """How a run's advisories measured against the scenario's labels."""

    hits: int = 0
    false_positives: int = 0
    unlabelled: int = 0
    false_positive_area_hours: float = 0.0
    missed: int = 0


def _score_advisories(
    advisories: Sequence[AuditRecord], labels: ScenarioLabels, series: FleetSeries
) -> Scored:
    """Hits, false positives by area-hours, and hazards nothing was said about.

    An advisory counts as a hit when it reaches a labelled region that was in
    force while it was: intersection, not containment, because the advisory S3
    exists to produce is a corridor *to* a scene and not the scene itself.

    A false positive is an advisory that reached no labelled region, and it is
    charged as `area x hours in force` rather than counted: a 10 km2 closure
    for three hours is not the same mistake as a 0.1 km2 one for ten minutes,
    and the area-hour is the unit that says so.

    An advisory whose citations name a record the scenario says it cannot
    label is neither. `scenarios/s3_incident.yaml` is explicit about this: the
    real incidents in that window are real, and labelling them would be
    inventing the labels the eval is scored against.
    """
    result = Scored()
    reached: set[str] = set()
    for record in advisories:
        geometry = record.data.get("geometry")
        properties = record.data.get("properties") or {}
        valid_from = _at(properties.get("valid_from")) or _published(record)
        valid_until = _at(properties.get("valid_until"))
        if geometry is None or valid_from is None:
            continue
        middle = _middle(valid_from, valid_until, series)
        if labels.covers(geometry, middle) or labels.covers(geometry, valid_from):
            result.hits += 1
            reached |= {
                region.id
                for region in labels.active(middle)
                if labels.covers(geometry, middle) or labels.covers(geometry, valid_from)
            }
            continue
        refs = [str(item.get("raw_ref") or "") for item in _evidence(properties)]
        if decoy_hit(refs, labels):
            result.false_positives += 1
            result.false_positive_area_hours += _area_hours(
                geometry, valid_from, valid_until, series
            )
            continue
        if labels.unlabelled:
            result.unlabelled += 1
            continue
        result.false_positives += 1
        result.false_positive_area_hours += _area_hours(geometry, valid_from, valid_until, series)

    # A hazard is missed when its window passed inside this run and nothing
    # published reached it. A region whose window never opened before the run
    # ended is not missed: nobody was told about it because it had not
    # happened yet.
    run_from, run_to = _window(series)
    for region in labels.regions:
        if region.id in reached:
            continue
        if run_from is None or run_to is None:
            continue
        if region.valid_from >= run_to or region.valid_until <= run_from:
            continue
        result.missed += 1
    return result


def _middle(valid_from: datetime, valid_until: datetime | None, series: FleetSeries) -> datetime:
    """An instant inside an advisory's window that the run actually covered."""
    _run_from, run_to = _window(series)
    end = min(filter(None, (valid_until, run_to))) if (valid_until or run_to) else valid_from
    if end <= valid_from:
        return valid_from
    return valid_from + (end - valid_from) / 2


def _area_hours(
    geometry: Any, valid_from: datetime, valid_until: datetime | None, series: FleetSeries
) -> float:
    """`km2 x hours`, clipped to the part of the window the run covered.

    Clipped because an advisory asks for sixty minutes and a run may be one
    minute long: charging the whole asked-for window to a one-minute run would
    make the figure a function of the playbook's lifetime cap rather than of
    what the run did.
    """
    try:
        area = area_km2(shape_of(geometry))
    except (GeometryError, ValueError, TypeError, AttributeError):
        return 0.0
    run_from, run_to = _window(series)
    start = max(valid_from, run_from) if run_from else valid_from
    end = valid_until or run_to
    if run_to is not None:
        end = min(end, run_to) if end is not None else run_to
    if end is None or end <= start:
        return 0.0
    return area * ((end - start).total_seconds() / SECONDS_PER_HOUR)


def _window(series: FleetSeries) -> tuple[datetime | None, datetime | None]:
    """The simulated window the frames cover."""
    if not series.sim_times:
        return None, None
    return series.sim_times[0], series.sim_times[-1]


def _labels(context: RunContext, series: FleetSeries) -> ScenarioLabels | None:
    """The scenario's labels, placed at the instant the run's fleets started.

    The start comes off the log: the first telemetry frame's `sim_time` is the
    `start` the runner handed the simulator and the ground truth alike, so the
    offsets in a descriptor line up with the advisories without the harness
    having to remember anything about the run.
    """
    start, _end = _window(series)
    if start is None:
        return None
    try:
        return labels_for(context.scenario, start=start)
    except (ValueError, FileNotFoundError, KeyError):
        return None


# --- small shared helpers ---------------------------------------------------


def _evidence(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The `evidence` list of a message, or an empty one.

    A published advisory is a GeoJSON Feature, so its citations are one level
    down in `properties` -- the same list, in the place the Feature contract
    puts it. Looked for in both so one function answers for every stage.
    """
    found = payload.get("evidence")
    if not isinstance(found, list):
        properties = payload.get("properties")
        found = properties.get("evidence") if isinstance(properties, Mapping) else None
    return [item for item in found if isinstance(item, Mapping)] if isinstance(found, list) else []


def _published(record: AuditRecord) -> datetime | None:
    """When the publisher wrote this entry, or when it was recorded."""
    return _at(record.published_at) or record.recorded_at


def _at(value: Any) -> datetime | None:
    """An ISO instant as a timezone-aware datetime, or None."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        from datetime import UTC

        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def percentile(values: Sequence[float], fraction: float) -> float:
    """The `fraction` percentile by nearest rank, 0.0 for no values.

    Nearest rank rather than interpolated: these are a handful of waits read
    off a short run, and an interpolated p95 of nine samples invents a number
    between two of them.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


def median(values: Sequence[float]) -> float | None:
    """The middle value, or the mean of the middle two. None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)
