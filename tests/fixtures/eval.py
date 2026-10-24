"""Synthetic audit logs shaped like a run, for the ablation's metrics tests.

`write_log` seals a real chain with real contract payloads: the S1 samples from
`samples.py` for the pipeline stages, and telemetry frames whose counters carry
the names `sim.counters` defines. So the metrics are tested against the shape
the auditor really writes -- aliases, ISO timestamps, counters keyed by
operator and total -- without needing Redis, a world build or a simulator.

Two shapes, which is what the ablation compares:

* **a V0 log**: telemetry from the ghost fleet only, no feeds read, nobody
  advised, and a lot of hazard entries.
* **a V4 log**: the whole pipeline on the streams, the advised fleet beside
  the ghost, and few hazard entries.

Nothing here invents a counter name or a field name. If a payload here drifts
from what the system publishes, the test that reads it is measuring a shape
nothing writes, which is exactly the bug these fixtures exist to catch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from headsup.audit import AuditLog
from headsup.bus import (
    STREAM_ADVISORIES,
    STREAM_ASSIST,
    STREAM_AUDIT,
    STREAM_CANDIDATES,
    STREAM_HAZARDS,
    STREAM_PROPOSALS,
    STREAM_TELEMETRY,
    STREAM_VERDICTS,
    events_stream,
)
from headsup.contracts import Source
from headsup.sim import counters as C
from headsup.sim.config import PERSONAS
from samples import ADVISORY, ASSIST_REQUEST, CANDIDATE, HAZARD, NWS_EVENT, PROPOSAL, T0, VERDICT

#: The fleet the frames describe. Three operators, as every run has.
OPERATORS: tuple[str, ...] = PERSONAS

#: Vehicles per frame, and the telemetry stride in ticks. Small, and both are
#: read back out of the frames by the metrics rather than assumed.
VEHICLES = 6
STRIDE = 30

#: Which vehicle waits for a human in the generated frames, and for how many
#: frames. The p95 wait the test asserts is derived from these two.
WAITING_VEHICLE = "veh-0001"
WAITING_FRAMES = 3

#: What one call cost, in the generated `LlmCall` lines.
CALL_USD = 0.0125


@dataclass(frozen=True, slots=True)
class LogShape:
    """What one synthetic log contains. The knobs the tests turn."""

    run_id: str
    #: Fleet sets to write frames for, advised first.
    ghost_only: bool = False
    #: Hazard entries the advised fleet ends on, and the ghost fleet.
    hazard_entries: int = 2
    ghost_hazard_entries: int = 11
    frames: int = 8
    #: Write the pipeline stages, the advisory and the LLM calls.
    pipeline: bool = True
    advisories: int = 1
    llm_calls: int = 4
    #: Assist requests, from the fleet the log is about.
    assists: int = 2
    blocked_seconds: int = 240
    ghost_blocked_seconds: int = 900
    rides: int = 24
    ghost_rides: int = 27
    rerouted: int = 6
    start: datetime = T0


def counters(
    *,
    hazard_entries: int,
    blocked_seconds: int,
    rides: int,
    rerouted: int,
    queue: int,
) -> dict[str, int]:
    """One frame's counters, keyed the way `Counters.snapshot` keys them.

    Totals and per-operator values both, because that is what a frame carries
    and the metrics read the totals off it by name.
    """
    out: dict[str, int] = {}
    families = {
        C.HAZARD_ENTRIES: hazard_entries,
        C.BLOCKED_SECONDS: blocked_seconds,
        C.CORRIDOR_ENTRIES: 0,
        C.ASSIST_OPEN: queue,
        C.ASSIST_OPENED: queue,
        C.RIDES_COMPLETED: rides,
        C.VEHICLES_STALLED: 0,
        C.VEHICLES_REROUTED: rerouted,
    }
    for metric, total in families.items():
        share, extra = divmod(total, len(OPERATORS))
        for index, op in enumerate(OPERATORS):
            out[C.key(metric, op)] = share + (extra if index == 0 else 0)
        out[C.key(metric)] = total
    for op in OPERATORS:
        out[C.key(C.BLOCKED_MINUTES, op)] = out[C.key(C.BLOCKED_SECONDS, op)] // 60
    out[C.key(C.BLOCKED_MINUTES)] = blocked_seconds // 60
    out.update(
        {
            C.ADVISORIES_SEEN: 1,
            C.ADVISORIES_ACCEPTED: 1,
            C.ADVISORIES_DECLINED: 0,
            C.ADVISORIES_REJECTED: 0,
        }
    )
    return out


def vehicles(frame: int, *, waiting: bool) -> list[dict[str, Any]]:
    """The vehicle states of one frame, with one of them in the queue."""
    states: list[dict[str, Any]] = []
    for index in range(VEHICLES):
        vid = f"veh-{index:04d}"
        status = "AWAITING_ASSIST" if (waiting and vid == WAITING_VEHICLE) else "MOVING"
        states.append(
            {
                "id": vid,
                "op": OPERATORS[index % len(OPERATORS)],
                "lon": -98.4933 + index * 0.0005,
                "lat": 29.4204,
                "heading": 90.0,
                "speed_mph": 0.0 if status != "MOVING" else 18.0,
                "status": status,
                "trip_id": f"trip-{frame:03d}-{index:02d}",
            }
        )
    return states


def telemetry_frame(
    shape: LogShape, index: int, *, ghost: bool, total_frames: int
) -> dict[str, Any]:
    """One telemetry payload, as `Telemetry.to_wire` writes it.

    The counters ramp to the shape's totals over the frames, so a cumulative
    metric read off the last frame is a different number from the first one
    and a peak is not simply the total.
    """
    share = (index + 1) / total_frames
    entries = shape.ghost_hazard_entries if ghost else shape.hazard_entries
    blocked = shape.ghost_blocked_seconds if ghost else shape.blocked_seconds
    rides = shape.ghost_rides if ghost else shape.rides
    waiting = index in range(1, 1 + WAITING_FRAMES)
    queue = 1 if waiting else 0
    tick = index * STRIDE
    return {
        "type": "telemetry",
        "tick": tick,
        "sim_time": (shape.start + timedelta(seconds=tick)).isoformat(),
        "vehicles": vehicles(index, waiting=waiting),
        "ghost": ghost,
        "counters": counters(
            hazard_entries=round(entries * share),
            blocked_seconds=round(blocked * share),
            rides=round(rides * share),
            rerouted=0 if ghost else round(shape.rerouted * share),
            queue=queue,
        ),
    }


def llm_call(index: int, at: datetime) -> dict[str, Any]:
    """One `LlmCall` payload, as the spend meter publishes it."""
    return {
        "id": f"llm-{index:04d}",
        "purpose": "fusion_rationale",
        "tier": "judgment",
        "mode": "mock",
        "model": "mock",
        "cached": False,
        "attempts": 1,
        "ok": True,
        "input_tokens": 900,
        "output_tokens": 120,
        "usd": CALL_USD,
        "spent_usd": CALL_USD * (index + 1),
        "latency_ms": 180.0,
        "at": at.isoformat(),
    }


def compile_audit(advisory_id: str, proposal_id: str) -> dict[str, Any]:
    """One `CompileAudit` payload: the line that joins a proposal to an advisory."""
    return {
        "id": f"aud_{advisory_id}",
        "proposal_id": proposal_id,
        "decision": "PUBLISHED",
        "hazard_class": "FLOOD",
        "severity": 4,
        "action": "NO_ENTRY",
        "verdict": "APPROVE",
        "policy_rule": "flood.no_entry.v1",
        "area_km2": 0.07,
        "advisory_id": advisory_id,
        "advisory_version": 1,
        "approved_by": "duty_officer_7",
    }


def assist(index: int, at: datetime, *, ghost: bool) -> dict[str, Any]:
    """One `AssistRequest` payload, for the vehicle the frames show waiting."""
    payload = ASSIST_REQUEST.to_wire()
    payload |= {
        "id": f"assist-{index:04d}",
        "vehicle_id": WAITING_VEHICLE,
        "requested_at": at.isoformat(),
        "ghost": ghost,
    }
    return payload


def entries(shape: LogShape) -> list[tuple[str, str, dict[str, Any], datetime]]:
    """Every line of one synthetic run, in the order the auditor would seal it."""
    out: list[tuple[str, str, dict[str, Any], datetime]] = []
    clock = shape.start

    if shape.pipeline:
        out.append((events_stream(Source.NWS), "CivicEvent", NWS_EVENT.to_wire(), clock))
        clock += timedelta(seconds=1)
        out.append((STREAM_CANDIDATES, "CandidateHazard", CANDIDATE.to_wire(), clock))
        clock += timedelta(seconds=1)
        out.append((STREAM_HAZARDS, "Hazard", HAZARD.to_wire(), clock))
        clock += timedelta(seconds=1)
        out.append((STREAM_PROPOSALS, "ProposedAdvisory", PROPOSAL.to_wire(), clock))
        clock += timedelta(seconds=1)
        out.append((STREAM_VERDICTS, "CriticVerdict", VERDICT.to_wire(), clock))
        clock += timedelta(seconds=1)
        for index in range(shape.advisories):
            advisory = _advisory(index, shape)
            out.append((STREAM_ADVISORIES, "Advisory", advisory, clock))
            out.append(
                (
                    STREAM_AUDIT,
                    "CompileAudit",
                    compile_audit(str(advisory["id"]), PROPOSAL.id),
                    clock,
                )
            )
            clock += timedelta(seconds=1)
        for index in range(shape.llm_calls):
            out.append((STREAM_AUDIT, "LlmCall", llm_call(index, clock), clock))
            clock += timedelta(milliseconds=200)

    ghost_modes = [True] if shape.ghost_only else [False, True]
    for index in range(shape.frames):
        for ghost in ghost_modes:
            moment = shape.start + timedelta(seconds=index * STRIDE)
            out.append(
                (
                    STREAM_TELEMETRY,
                    "Telemetry",
                    telemetry_frame(shape, index, ghost=ghost, total_frames=shape.frames),
                    moment,
                )
            )

    for index in range(shape.assists):
        at = shape.start + timedelta(seconds=STRIDE + index)
        out.append((STREAM_ASSIST, "AssistRequest", assist(index, at, ghost=shape.ghost_only), at))

    return out


def _advisory(index: int, shape: LogShape) -> dict[str, Any]:
    """One advisory payload, in force from the run's start for an hour."""
    payload = ADVISORY.to_wire()
    advisory_id = f"adv_eval_{index:04d}"
    payload["id"] = advisory_id
    properties = dict(payload["properties"])
    properties |= {
        "id": advisory_id,
        "valid_from": shape.start.isoformat(),
        "valid_until": (shape.start + timedelta(hours=1)).isoformat(),
    }
    payload["properties"] = properties
    return payload


def write_log(directory: Path, shape: LogShape) -> Path:
    """Seal one synthetic run onto a real chain and return its path."""
    with AuditLog(shape.run_id, directory=directory) as log:
        for index, (stream, type_name, data, moment) in enumerate(entries(shape)):
            log.append(
                stream=stream,
                entry_id=f"{int(moment.timestamp() * 1000)}-{index}",
                type=type_name,
                data=data,
                published_at=moment.isoformat(),
                recorded_at=moment,
            )
        return log.path


def v0_log(directory: Path, *, run_id: str = "eval_s1_flood_V0_s42", **changes: Any) -> Path:
    """The baseline shape: the ghost fleet alone, nothing read, nothing advised."""
    shape = LogShape(
        run_id=run_id,
        ghost_only=True,
        pipeline=False,
        advisories=0,
        llm_calls=0,
        **changes,
    )
    return write_log(directory, shape)


def v4_log(directory: Path, *, run_id: str = "eval_s1_flood_V4_s42", **changes: Any) -> Path:
    """The full shape: the pipeline on the streams, both fleets in the frames."""
    return write_log(directory, LogShape(run_id=run_id, **changes))


def regions_from(shapes: Sequence[Any]) -> tuple[Any, ...]:
    """Pass-through, so a test can build labels without importing the simulator."""
    return tuple(shapes)
