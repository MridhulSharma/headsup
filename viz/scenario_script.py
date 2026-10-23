"""The scripted S1 what-if the mock stream replays, as plain data.

This module is the *script*, not the server: pure functions and frozen
tables, so the shape of every frame the God's Eye View Heads Up mode consumes
can be read in one place and exercised without a socket.

Everything here is synthetic and says so. Every advisory issuer is
`headsup/mock-stream`, every evidence row cites the `synthetic` source, and no
advisory carries a signature — nothing downstream may mistake a frame from
this file for something the compiler published.

The geometry is real places (the Salado Creek crossings under Loop 410 in
north-east Bexar County, downtown Austin) because a what-if is only legible
over a real street grid; the *events* over them are invented.

@module viz.scenario_script
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any


def rfc3339(moment: datetime) -> str:
    """Format an instant the way the advisory contract expects."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Scenario catalogue ────────────────────────────────────────────────────
#
# `GET /scenarios` serves these three rows. The viewer reads `code`, `name`,
# `city` and `what_if` for the picker and `start_view` for the camera, so the
# field names here are the contract the real API will have to keep.

#: S1's hazard area: the creek crossing a fleet would otherwise route over.
S1_FLOOD_RING = [
    [-98.3990, 29.5200],
    [-98.3930, 29.5200],
    [-98.3930, 29.5255],
    [-98.3990, 29.5255],
    [-98.3990, 29.5200],
]

#: The east-west arterial through the crossing, and the detour north of it.
#: The two share their first and last legs on purpose: a vehicle that moves
#: onto the detour mid-route then steps sideways once rather than teleporting
#: across the city, and the two paths rejoin before the far end.
S1_MAIN_ROUTE = [
    [-98.4080, 29.5228],
    [-98.4010, 29.5228],
    [-98.3960, 29.5228],
    [-98.3910, 29.5228],
    [-98.3840, 29.5228],
]
S1_DETOUR_ROUTE = [
    [-98.4080, 29.5228],
    [-98.4010, 29.5228],
    [-98.3960, 29.5290],
    [-98.3910, 29.5272],
    [-98.3840, 29.5228],
]

SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "s1_flood",
        "code": "S1",
        "name": "Flash flood",
        "city": "San Antonio",
        "hazard_class": "FLOOD",
        "what_if": (
            "What if a flash flood warning lands on a creek crossing while "
            "two robotaxi fleets are routed over it?"
        ),
        "speed": 60.0,
        "start_view": {
            "lon": -98.3960,
            "lat": 29.5228,
            "height": 900.0,
            "heading": 20.0,
            "pitch": -34.0,
        },
    },
    {
        "id": "s2_blackout",
        "code": "S2",
        "name": "Signal blackout",
        "city": "Austin",
        "hazard_class": "SIGNAL_OUTAGE",
        "what_if": (
            "What if the downtown signals go dark and every vehicle has to "
            "treat the grid as all-way stops?"
        ),
        "speed": 60.0,
        "start_view": {
            "lon": -97.7431,
            "lat": 30.2672,
            "height": 900.0,
            "heading": 15.0,
            "pitch": -32.0,
        },
    },
    {
        "id": "s3_emergency",
        "code": "S3",
        "name": "Emergency scene",
        "city": "Austin",
        "hazard_class": "EMERGENCY_SCENE",
        "what_if": (
            "What if an engine company closes two lanes and the fleet keeps "
            "feeding vehicles into the block?"
        ),
        "speed": 60.0,
        "start_view": {
            "lon": -97.7380,
            "lat": 30.2700,
            "height": 700.0,
            "heading": 340.0,
            "pitch": -30.0,
        },
    },
)

SCENARIOS_BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}

#: Only S1 has a scripted sequence in this step; S2 and S3 start, fly the
#: camera and run the idle fleet until the backend supplies theirs.
SCRIPTED_SCENARIO_ID = "s1_flood"

ADVISORY_ID = "adv-mock-s1-flood-1"
HAZARD_ID = "haz-mock-s1-flood-1"
CANDIDATE_ID = "cand-mock-s1-flood-1"
PROPOSAL_ID = "prop-mock-s1-flood-1"
#: How long the scripted advisory stays in force once it is published.
ADVISORY_HOURS = 6


# ── The scripted fleet ────────────────────────────────────────────────────


def _vehicle(
    index: int,
    op: str,
    *,
    ghost: bool = False,
    reroutes: bool = False,
) -> dict[str, Any]:
    """One scripted vehicle: an operator, a phase on the route, a speed."""
    return {
        "id": f"{op}-{index:02d}",
        "op": op,
        "ghost": ghost,
        "reroutes": reroutes,
        # Spread the fleet along the route and vary the pace a little, so the
        # cohort reads as traffic rather than as one rigid convoy.
        "phase": (index * 0.137) % 1.0,
        "speed": 0.030 + 0.004 * ((index % 3) - 1),
    }


#: Three cohorts, which is what makes the screen worth looking at.
#:
#: ``op_a`` takes the advisory and reroutes, so its vehicles leave deflection
#: trails around the hazard. ``op_b`` DECLINES it (see ``ACK_STATUSES``) and
#: therefore does not reroute — a fleet that refused and kept driving is the
#: case an operator most needs to see, and it is only legible if the refusal
#: and the route agree. The ghost fleet never had an advisory at all.
S1_FLEET: tuple[dict[str, Any], ...] = (
    *(_vehicle(i, "op_a", reroutes=True) for i in range(6)),
    *(_vehicle(i, "op_b", reroutes=False) for i in range(6, 10)),
    *(_vehicle(i, "op_ghost", ghost=True) for i in range(10, 16)),
)


def _point_on_route(route: list[list[float]], progress: float) -> tuple[float, float, float]:
    """Interpolate a position and a bearing along a polyline.

    `progress` wraps, so a vehicle laps the arterial instead of falling off
    its end. Legs are treated as equal-length, which is true enough for five
    waypoints spanning two kilometres and keeps the pacing even.
    """
    legs = len(route) - 1
    walked = (progress % 1.0) * legs
    leg = min(int(walked), legs - 1)
    fraction = walked - leg
    (from_lon, from_lat), (to_lon, to_lat) = route[leg], route[leg + 1]
    lon = from_lon + (to_lon - from_lon) * fraction
    lat = from_lat + (to_lat - from_lat) * fraction
    # Bearing of travel, clockwise from north, with the cosine correction so
    # a degree of longitude is not treated as a degree of latitude.
    east = (to_lon - from_lon) * math.cos(math.radians(lat))
    north = to_lat - from_lat
    heading = math.degrees(math.atan2(east, north)) % 360.0
    return lon, lat, heading


def s1_vehicles(elapsed: float, *, rerouted: bool) -> list[dict[str, Any]]:
    """The S1 fleet at `elapsed` seconds into the replay.

    Before the advisory every vehicle drives the arterial through the creek
    crossing. After it, the two live fleets take the detour and say which
    advisory moved them; the ghost fleet keeps driving into the water.
    """
    rows = []
    for vehicle in S1_FLEET:
        reroute = rerouted and vehicle["reroutes"]
        route = S1_DETOUR_ROUTE if reroute else S1_MAIN_ROUTE
        lon, lat, heading = _point_on_route(route, vehicle["phase"] + elapsed * vehicle["speed"])
        row: dict[str, Any] = {
            "id": vehicle["id"],
            "op": vehicle["op"],
            "lon": round(lon, 7),
            "lat": round(lat, 7),
            "heading": round(heading, 1),
        }
        if vehicle["ghost"]:
            # The control fleet, drawn apart from the advised one.
            row["ghost"] = True
        if reroute:
            row["rerouted_by"] = ADVISORY_ID
        rows.append(row)
    return rows


# ── The scripted frames ───────────────────────────────────────────────────


def _evidence(observed_at: datetime) -> dict[str, str]:
    return {
        "event_id": "evt-mock-s1-nws-1",
        "source": "synthetic",
        "kind": "mock.nws.flash_flood_warning",
        "observed_at": rfc3339(observed_at),
        "raw_ref": "viz/scenario_script.py",
    }


def scenario_state_frame(
    scenario: dict[str, Any] | None,
    *,
    elapsed: float = 0.0,
    state: str = "idle",
) -> dict[str, Any]:
    """The clock-and-name frame the Heads Up readout is driven from.

    ``scripted`` is what the viewer's SCRIPTED DATA badge is driven from, so
    it says whether THIS scenario has a written timeline rather than being
    hardcoded true: an idle feed is not a scripted replay, and S2 and S3 run
    the idle fleet until the backend supplies their own scripts.
    """
    speed = float(scenario["speed"]) if scenario else 1.0
    frame: dict[str, Any] = {
        "type": "scenario_state",
        "state": state,
        "sim_time": rfc3339(datetime.now(UTC) + timedelta(seconds=elapsed * speed)),
        "elapsed_s": round(elapsed, 2),
        "speed": speed,
        "scripted": bool(scenario and scenario["id"] == SCRIPTED_SCENARIO_ID),
    }
    if scenario:
        frame["scenario"] = {
            "id": scenario["id"],
            "code": scenario["code"],
            "name": scenario["name"],
            "city": scenario["city"],
        }
    return frame


def candidate_frame(now: datetime) -> dict[str, Any]:
    """A sentinel's unfused reading: one source, no decision taken yet."""
    return {
        "type": "candidate",
        "candidate": {
            "id": CANDIDATE_ID,
            "sentinel": "flood",
            "hazard_class": "FLOOD",
            "confidence": 0.62,
            "observed_at": rfc3339(now),
            "evidence": [_evidence(now)],
            "note": "flash flood warning polygon over Salado Creek crossings",
        },
    }


def hazard_frame(now: datetime) -> dict[str, Any]:
    """The fused hazard: evidence combined, severity settled."""
    return {
        "type": "hazard",
        "hazard": {
            "id": HAZARD_ID,
            "class": "FLOOD",
            "severity": 4,
            "confidence": 0.91,
            "geometry": {"type": "Polygon", "coordinates": [S1_FLOOD_RING]},
            "evidence": [_evidence(now)],
            "valid_until": rfc3339(now + timedelta(hours=ADVISORY_HOURS)),
            "rationale": (
                "One authoritative warning polygon and two gauges above action stage inside it."
            ),
        },
    }


def proposal_frame(now: datetime) -> dict[str, Any]:
    """What the proposer asks for, before the critic or the compiler see it."""
    return {
        "type": "proposal",
        "proposal": {
            "id": PROPOSAL_ID,
            "hazard_id": HAZARD_ID,
            "action": {"type": "NO_ENTRY"},
            "playbook": "flood",
            "policy_rule": "flood.no_entry.mock",
            "valid_until": rfc3339(now + timedelta(hours=ADVISORY_HOURS)),
            "rationale": "No-entry over the inundated crossings for six hours.",
        },
    }


def verdict_frame(now: datetime) -> dict[str, Any]:
    """The critic's answer. Scripted as a pass, with its reasons shown."""
    return {
        "type": "verdict",
        "verdict": {
            "proposal_id": PROPOSAL_ID,
            "approved": True,
            "decided_at": rfc3339(now),
            "checks": [
                {"rule": "action_allowed_for_class", "passed": True},
                {"rule": "area_under_cap", "passed": True},
                {"rule": "lifetime_under_cap", "passed": True},
                {"rule": "hospital_access_preserved", "passed": True},
            ],
            "rationale": "Action is in the flood playbook and the area is under cap.",
        },
    }


def advisory_frame(now: datetime) -> dict[str, Any]:
    """The published advisory, as the viewer's `headsup` layer reads it."""
    hazard = hazard_frame(now)["hazard"]
    return {
        "type": "advisory",
        "feature": {
            "type": "Feature",
            "id": ADVISORY_ID,
            "geometry": {"type": "Polygon", "coordinates": [S1_FLOOD_RING]},
            "properties": {
                "id": ADVISORY_ID,
                "version": 1,
                "hazard": hazard,
                "action": {
                    "type": "NO_ENTRY",
                    "applies_to": {"operators": ["*"], "vehicle_classes": ["*"]},
                },
                "valid_from": rfc3339(now),
                "valid_until": rfc3339(now + timedelta(hours=ADVISORY_HOURS)),
                "evidence": [_evidence(now)],
                "policy_rule": "flood.no_entry.mock",
                "issuer": "headsup/mock-stream",
                # Unsigned on purpose: this never came from the compiler, and
                # the viewer must show it as unsigned rather than trusted.
                "signature": None,
            },
        },
    }


def ack_frame(
    operator: str,
    vehicles: int,
    now: datetime,
    status: str = "ACCEPTED",
) -> dict[str, Any]:
    """One operator answering the advisory for a named vehicle count.

    ``status`` is the field the viewer draws from: an ``ACCEPTED`` answer
    flashes that fleet once, and a ``DECLINED`` one tags it. Anything other
    than an acceptance is read as a refusal downstream, so this only ever
    sends one of the two the contract defines.
    """
    declined = status == "DECLINED"
    return {
        "type": "ack",
        "ack": {
            "advisory_id": ADVISORY_ID,
            "operator": operator,
            "vehicles": vehicles,
            "status": status,
            "received_at": rfc3339(now),
            "action_taken": (
                "advisory declined; routes unchanged"
                if declined
                else "NO_ENTRY applied; affected routes recomputed"
            ),
        },
    }


#: The S1 timeline: seconds from `POST /scenarios/s1_flood/start`, and the
#: frame due at that offset. Telemetry runs continuously underneath it; the
#: fleet starts rerouting the moment the advisory goes out.
S1_TIMELINE: tuple[tuple[float, str], ...] = (
    (0.0, "scenario_state"),
    (1.5, "candidate"),
    (3.0, "hazard"),
    (4.5, "proposal"),
    (6.0, "verdict"),
    (7.5, "advisory"),
    (8.5, "ack:op_a"),
    (9.5, "ack:op_b"),
)

#: Offset at which the live fleets are on the detour. Same instant as the
#: advisory, because the advisory is what moves them.
S1_REROUTE_AT = 7.5

#: Vehicles each operator answers for, in timeline order.
ACK_FLEET_SIZES = {"op_a": 6, "op_b": 4}

#: How each operator answers. One refusal on purpose: the viewer has a tag for
#: it, and a demo in which everyone complies never shows what that tag means.
ACK_STATUSES = {"op_a": "ACCEPTED", "op_b": "DECLINED"}


def scripted_frame(step: str, elapsed: float, scenario: dict[str, Any]) -> dict[str, Any] | None:
    """Build the frame one timeline step names."""
    now = datetime.now(UTC)
    if step == "scenario_state":
        return scenario_state_frame(scenario, elapsed=elapsed, state="running")
    if step == "candidate":
        return candidate_frame(now)
    if step == "hazard":
        return hazard_frame(now)
    if step == "proposal":
        return proposal_frame(now)
    if step == "verdict":
        return verdict_frame(now)
    if step == "advisory":
        return advisory_frame(now)
    if step.startswith("ack:"):
        operator = step.split(":", 1)[1]
        return ack_frame(
            operator,
            ACK_FLEET_SIZES.get(operator, 1),
            now,
            status=ACK_STATUSES.get(operator, "ACCEPTED"),
        )
    return None
