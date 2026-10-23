"""The scripted what-ifs the mock stream replays, as plain data.

This module is the *script*, not the server: pure functions and frozen
tables, so the shape of every frame the God's Eye View Heads Up mode consumes
can be read in one place and exercised without a socket.

Everything here is synthetic and says so. Every advisory issuer is
`headsup/mock-stream`, every evidence row cites a `synthetic` source, and no
advisory carries a real signature — nothing downstream may mistake a frame
from this file for something the compiler published.

The geometry is real places (the Salado Creek crossings under Loop 410 in
north-east Bexar County, downtown Austin) because a what-if is only legible
over a real street grid; the *events* over them are invented.

Three scripts, chosen so that between them the agent rail shows every state
it can draw rather than only the happy one:

===  ======================  =============  ====================  ============
ID   Hazard                  Critic         Compiler              Fleet answer
===  ======================  =============  ====================  ============
S1   FLOOD, severity 4       APPROVE        SIGNED                ACCEPTED,
                                                                  DECLINED
S2   SIGNAL_OUTAGE, sev 3    REVISE, then   SIGNED (2nd           ACCEPTED,
                             APPROVE        proposal)             ACCEPTED
S3   EMERGENCY_SCENE, sev 4  ESCALATE       PENDING_APPROVAL      ACCEPTED,
                                            until two-key,        DELAYED
                                            then SIGNED
===  ======================  =============  ====================  ============

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

#: S2's dark-signal area: eight downtown Austin blocks either side of
#: Congress Avenue, which is where the city's own outage feed would put it.
S2_OUTAGE_RING = [
    [-97.7465, 30.2650],
    [-97.7395, 30.2650],
    [-97.7395, 30.2710],
    [-97.7465, 30.2710],
    [-97.7465, 30.2650],
]
#: The narrower area the critic sends the proposal back for. Same hazard,
#: four blocks instead of eight: a REVISE that changes nothing would not be
#: a revision.
S2_REVISED_RING = [
    [-97.7448, 30.2662],
    [-97.7408, 30.2662],
    [-97.7408, 30.2698],
    [-97.7448, 30.2698],
    [-97.7448, 30.2662],
]
#: Congress Avenue north-south through the dark grid, and the detour one
#: street east over Brazos, which keeps its signals.
S2_MAIN_ROUTE = [
    [-97.7431, 30.2625],
    [-97.7431, 30.2660],
    [-97.7431, 30.2690],
    [-97.7431, 30.2720],
    [-97.7431, 30.2755],
]
S2_DETOUR_ROUTE = [
    [-97.7431, 30.2625],
    [-97.7431, 30.2660],
    [-97.7405, 30.2690],
    [-97.7405, 30.2720],
    [-97.7431, 30.2755],
]

#: S3's corridor: the two closed lanes of the 600 block of East 6th Street.
#: A LINE, not an area — a KEEP_CLEAR advisory is addressed to a route, and
#: that is what the viewer draws as a glowing ground-clamped corridor.
S3_CORRIDOR_LINE = [
    [-97.7399, 30.26712],
    [-97.7390, 30.26712],
    [-97.7381, 30.26713],
    [-97.7372, 30.26714],
]
#: East 6th Street through the scene, and the detour over East 7th.
S3_MAIN_ROUTE = [
    [-97.7432, 30.2671],
    [-97.7405, 30.2671],
    [-97.7385, 30.2671],
    [-97.7362, 30.2672],
    [-97.7338, 30.2672],
]
S3_DETOUR_ROUTE = [
    [-97.7432, 30.2671],
    [-97.7405, 30.2671],
    [-97.7385, 30.2684],
    [-97.7362, 30.2684],
    [-97.7338, 30.2672],
]
#: The longitudes between which a vehicle is inside the closed block. The
#: ghost fleet drives straight through it, which is what a control group is
#: for: its hazard entries and its assist requests are the comparison.
S3_BLOCK_WEST = -97.7399
S3_BLOCK_EAST = -97.7372

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
            "lon": -97.7385,
            "lat": 30.2676,
            "height": 620.0,
            "heading": 340.0,
            "pitch": -28.0,
        },
    },
)

SCENARIOS_BY_ID = {scenario["id"]: scenario for scenario in SCENARIOS}

#: How long a published advisory stays in force.
ADVISORY_HOURS = 6


# ── The scripted fleets ───────────────────────────────────────────────────


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


def _fleet(
    accepting: int = 6,
    declining: int = 4,
    ghosts: int = 6,
) -> tuple[dict[str, Any], ...]:
    """Three cohorts, which is what makes the screen worth looking at.

    ``op_a`` acts on the advisory and reroutes, so its vehicles leave
    deflection trails around the hazard. ``op_b`` does not reroute — it is
    the fleet whose answer is a refusal or a delay, and a fleet that said no
    and kept driving is only legible if the answer and the route agree. The
    ghost fleet never had an advisory at all.
    """
    return (
        *(_vehicle(i, "op_a", reroutes=True) for i in range(accepting)),
        *(_vehicle(i, "op_b", reroutes=False) for i in range(accepting, accepting + declining)),
        *(
            _vehicle(i, "op_ghost", ghost=True)
            for i in range(accepting + declining, accepting + declining + ghosts)
        ),
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


def _queued_ghost(
    script: dict[str, Any],
    slot: int,
    elapsed: float,
) -> tuple[float, float, float]:
    """Where one ghost in a stalling cohort is: driving in, then stopped.

    S3's claim is that an unadvised fleet keeps feeding vehicles into a
    closed block and then cannot clear them. A vehicle stopped behind an
    engine company does not drive on, so these ghosts are modelled as a
    QUEUE rather than as traffic: each one drives in from the west end of
    the arterial, reaches its slot inside the closed block, and holds there
    until the replay ends. That is what makes the ghost cohort's assist
    queue grow while the advised fleets' stays empty.
    """
    arrival = max(0.1, script["queue_opens_at"] + slot * script["queue_stagger"])
    start_lon, start_lat = script["main_route"][0]
    slot_lon = script["block_west"] + script["queue_inset"] + slot * script["queue_spacing"]
    slot_lat = script["queue_lat"]
    progress = min(1.0, max(0.0, elapsed / arrival))
    lon = start_lon + (slot_lon - start_lon) * progress
    lat = start_lat + (slot_lat - start_lat) * progress
    east = (slot_lon - start_lon) * math.cos(math.radians(slot_lat))
    north = slot_lat - start_lat
    return lon, lat, math.degrees(math.atan2(east, north)) % 360.0


def scenario_vehicles(
    scenario_id: str,
    elapsed: float,
    *,
    rerouted: bool,
) -> list[dict[str, Any]]:
    """One scenario's fleet at `elapsed` seconds into the replay.

    Before the advisory every vehicle drives the direct route. After it, the
    fleets that act on it take the detour and say which advisory moved them;
    the ghost fleet keeps driving into the hazard.
    """
    script = SCRIPTS.get(scenario_id)
    if script is None:
        return []
    rows = []
    slot = 0
    for vehicle in script["fleet"]:
        reroute = rerouted and vehicle["reroutes"]
        if vehicle["ghost"] and script.get("ghost_queue"):
            lon, lat, heading = _queued_ghost(script, slot, elapsed)
            slot += 1
        else:
            route = script["detour_route"] if reroute else script["main_route"]
            lon, lat, heading = _point_on_route(
                route, vehicle["phase"] + elapsed * vehicle["speed"]
            )
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
            row["rerouted_by"] = script["advisory_id"]
        rows.append(row)
    return rows


# ── Evidence, by feed ─────────────────────────────────────────────────────
#
# The `source` of a piece of evidence is what the rail's FEEDS row lights a
# chip for, so these ids are the contract behind "which public feed did this
# advisory actually come from". They are the real feed names Heads Up ingests
# — the OBSERVATIONS under them are invented, which is what `synthetic`
# in `kind` says.

#: Feed ids the rail knows how to label. Anything else is shown as it arrived.
FEED_NWS = "nws"
FEED_USGS = "usgs"
FEED_AUSTIN_SIGNALS = "austin_signals"
FEED_AUSTIN_INCIDENTS = "austin_incidents"


def _evidence(
    event_id: str,
    source: str,
    kind: str,
    note: str,
    observed_at: datetime,
) -> dict[str, str]:
    """One evidence row, as the advisory contract carries it.

    The argument order matches the ``evidence`` tuples in ``SCRIPTS`` so a
    row can be splatted in with the observation instant appended.
    """
    return {
        "event_id": event_id,
        "source": source,
        "kind": kind,
        "note": note,
        "observed_at": rfc3339(observed_at),
        "raw_ref": "viz/scenario_script.py",
    }


# ── Frame builders ────────────────────────────────────────────────────────


def scenario_state_frame(
    scenario: dict[str, Any] | None,
    *,
    elapsed: float = 0.0,
    state: str = "idle",
) -> dict[str, Any]:
    """The clock-and-name frame the Heads Up readout is driven from.

    ``scripted`` is what the viewer's SCRIPTED DATA badge is driven from, so
    it says whether THIS scenario has a written timeline rather than being
    hardcoded true: an idle feed is not a scripted replay.
    """
    speed = float(scenario["speed"]) if scenario else 1.0
    frame: dict[str, Any] = {
        "type": "scenario_state",
        "state": state,
        "sim_time": rfc3339(datetime.now(UTC) + timedelta(seconds=elapsed * speed)),
        "elapsed_s": round(elapsed, 2),
        "speed": speed,
        "scripted": bool(scenario and scenario["id"] in SCRIPTS),
    }
    if scenario:
        frame["scenario"] = {
            "id": scenario["id"],
            "code": scenario["code"],
            "name": scenario["name"],
            "city": scenario["city"],
        }
    return frame


def candidate_frame(scenario_id: str, now: datetime) -> dict[str, Any]:
    """A sentinel's unfused reading: the feeds it read, no decision taken.

    An S3 candidate also carries the RAW dispatch text the text sentinel was
    given and the structured fields it pulled out of it, because the one
    thing an operator needs to be able to check about a text agent is whether
    what it extracted is what the message said.
    """
    script = SCRIPTS[scenario_id]
    candidate = {
        "id": script["candidate_id"],
        "sentinel": script["sentinel"],
        "hazard_class": script["hazard_class"],
        "confidence": script["candidate_confidence"],
        "observed_at": rfc3339(now),
        "evidence": [_evidence(*row, now) for row in script["evidence"]],
        "note": script["candidate_note"],
    }
    if script.get("dispatch_text"):
        candidate["raw_text"] = script["dispatch_text"]
        candidate["extracted"] = [
            {"field": field, "value": value} for field, value in script["dispatch_fields"]
        ]
    return {"type": "candidate", "candidate": candidate}


def hazard_frame(scenario_id: str, now: datetime) -> dict[str, Any]:
    """The fused hazard: evidence combined, severity and confidence settled."""
    script = SCRIPTS[scenario_id]
    return {
        "type": "hazard",
        "hazard": {
            "id": script["hazard_id"],
            "class": script["hazard_class"],
            "severity": script["severity"],
            "confidence": script["hazard_confidence"],
            "geometry": dict(script["hazard_geometry"]),
            "evidence": [_evidence(*row, now) for row in script["evidence"]],
            "valid_until": rfc3339(now + timedelta(hours=ADVISORY_HOURS)),
            "rationale": script["fusion_rationale"],
        },
    }


def proposal_frame(scenario_id: str, revision: int, now: datetime) -> dict[str, Any]:
    """What the proposer asks for, before the critic or the compiler see it."""
    script = SCRIPTS[scenario_id]
    proposal = script["proposals"][revision - 1]
    return {
        "type": "proposal",
        "proposal": {
            "id": proposal["id"],
            "hazard_id": script["hazard_id"],
            "revision": revision,
            "action": {"type": proposal["action"]},
            "playbook": script["playbook"],
            "policy_rule": proposal["policy_rule"],
            "valid_until": rfc3339(now + timedelta(hours=ADVISORY_HOURS)),
            "rationale": proposal["rationale"],
        },
    }


def verdict_frame(scenario_id: str, revision: int, now: datetime) -> dict[str, Any]:
    """The critic's answer: APPROVE, REVISE or ESCALATE, with its reasons."""
    script = SCRIPTS[scenario_id]
    proposal = script["proposals"][revision - 1]
    return {
        "type": "verdict",
        "verdict": {
            "proposal_id": proposal["id"],
            "decision": proposal["decision"],
            "decided_at": rfc3339(now),
            "checks": [{"rule": rule, "passed": passed} for rule, passed in proposal["checks"]],
            "reasons": list(proposal["reasons"]),
            "rationale": proposal["verdict_rationale"],
        },
    }


def compile_frame(
    scenario_id: str,
    revision: int,
    now: datetime,
    *,
    approved: bool,
) -> dict[str, Any]:
    """The compiler's result: signed, rejected, or held for a second key.

    ``signature`` is a placeholder string and ``signature_verified`` is
    ``false`` for every frame this file produces: nothing here was signed by
    the compiler's key, and the viewer must show an unverified advisory as
    unverified rather than as trusted.
    """
    script = SCRIPTS[scenario_id]
    proposal = script["proposals"][revision - 1]
    pending = proposal["decision"] == "ESCALATE" and not approved
    status = "PENDING_APPROVAL" if pending else proposal["compiles"]
    signed = status == "SIGNED"
    return {
        "type": "compile",
        "compile": {
            "proposal_id": proposal["id"],
            "advisory_id": script["advisory_id"] if signed else None,
            "status": status,
            "decided_at": rfc3339(now),
            "issuer": "headsup/mock-stream",
            "policy_rule": proposal["policy_rule"],
            # Unsigned on purpose: see the module docstring.
            "signature": "mock-unsigned" if signed else None,
            "signature_verified": False if signed else None,
            "reasons": (
                ["two_key_approval_required"]
                if pending
                else list(proposal.get("compile_reasons", ()))
            ),
            "two_key": pending,
        },
    }


def advisory_frame(scenario_id: str, now: datetime) -> dict[str, Any]:
    """The published advisory, as the viewer's `headsup` layer reads it."""
    script = SCRIPTS[scenario_id]
    hazard = hazard_frame(scenario_id, now)["hazard"]
    proposal = script["proposals"][-1]
    return {
        "type": "advisory",
        "feature": {
            "type": "Feature",
            "id": script["advisory_id"],
            "geometry": dict(script["advisory_geometry"]),
            "properties": {
                "id": script["advisory_id"],
                "version": 1,
                "hazard": hazard,
                "action": {
                    "type": proposal["action"],
                    "applies_to": {"operators": ["*"], "vehicle_classes": ["*"]},
                },
                "valid_from": rfc3339(now),
                "valid_until": rfc3339(now + timedelta(hours=ADVISORY_HOURS)),
                "evidence": [_evidence(*row, now) for row in script["evidence"]],
                "policy_rule": proposal["policy_rule"],
                "issuer": "headsup/mock-stream",
                # Unsigned on purpose: this never came from the compiler, and
                # the viewer must show it as unsigned rather than trusted.
                "signature": None,
            },
        },
    }


def ack_frame(scenario_id: str, operator: str, now: datetime) -> dict[str, Any]:
    """One operator answering the advisory for a named vehicle count.

    ``status`` is the field the viewer draws from: an ``ACCEPTED`` answer
    flashes that fleet once, and anything else tags it. A ``DELAYED`` answer
    is neither a yes nor a no — the fleet has the advisory and has not
    applied it yet — which is exactly the state an operator needs to see.
    """
    answer = SCRIPTS[scenario_id]["acks"][operator]
    return {
        "type": "ack",
        "ack": {
            "advisory_id": SCRIPTS[scenario_id]["advisory_id"],
            "operator": operator,
            "vehicles": answer["vehicles"],
            "status": answer["status"],
            "received_at": rfc3339(now),
            "action_taken": answer["action_taken"],
            "reason": answer.get("reason"),
        },
    }


def assist_frame(assists: list[dict[str, Any]]) -> dict[str, Any]:
    """Every remote-assist request open right now, as one whole picture.

    Full state per frame, the way telemetry is: an assist a human cleared has
    to leave the screen, and a list of deltas would make that an inference
    rather than a fact. ``cluster`` is null until surge triage groups the
    request with others; once it is set, the viewer collapses every member of
    that cluster into one marker carrying the count.
    """
    return {"type": "assist", "assists": assists}


def counters_frame(
    hazard_entries: dict[str, int],
    assist_queue: dict[str, int],
) -> dict[str, Any]:
    """The two tallies only the simulator can report.

    "A vehicle entered the hazard" and "a vehicle is waiting on a human" are
    facts about the fleet, not about the picture, so the viewer is told them
    rather than inferring them from dots on a globe.
    """
    return {
        "type": "counters",
        "counters": {
            "hazard_entries": dict(hazard_entries),
            "assist_queue": dict(assist_queue),
        },
    }


# ── The three scripts ─────────────────────────────────────────────────────
#
# Each script is a table. `timeline` is seconds from `POST
# /scenarios/{id}/start` and the step due at that offset; `reroute_at` is
# when the acting fleets are on the detour, which is the instant the
# advisory goes out, because the advisory is what moves them.

SCRIPTS: dict[str, dict[str, Any]] = {
    "s1_flood": {
        "code": "S1",
        "hazard_class": "FLOOD",
        "severity": 4,
        "playbook": "flood",
        "sentinel": "flood",
        "candidate_id": "cand-mock-s1-flood-1",
        "hazard_id": "haz-mock-s1-flood-1",
        "advisory_id": "adv-mock-s1-flood-1",
        "candidate_confidence": 0.62,
        "hazard_confidence": 0.91,
        "candidate_note": "flash flood warning polygon over Salado Creek crossings",
        "evidence": (
            (
                "evt-mock-s1-nws-1",
                FEED_NWS,
                "synthetic.nws.flash_flood_warning",
                "warning polygon covers both Loop 410 crossings",
            ),
            (
                "evt-mock-s1-usgs-1",
                FEED_USGS,
                "synthetic.usgs.gauge_above_action_stage",
                "gauge 08178050 1.2 ft over action stage, rising",
            ),
        ),
        "fusion_rationale": (
            "One authoritative warning polygon and two gauges above action stage inside it."
        ),
        "hazard_geometry": {"type": "Polygon", "coordinates": [S1_FLOOD_RING]},
        "advisory_geometry": {"type": "Polygon", "coordinates": [S1_FLOOD_RING]},
        "proposals": (
            {
                "id": "prop-mock-s1-flood-1",
                "action": "NO_ENTRY",
                "policy_rule": "flood.no_entry.mock",
                "rationale": "No-entry over the inundated crossings for six hours.",
                "decision": "APPROVE",
                "checks": (
                    ("action_allowed_for_class", True),
                    ("area_under_cap", True),
                    ("lifetime_under_cap", True),
                    ("hospital_access_preserved", True),
                ),
                "reasons": (),
                "verdict_rationale": ("Action is in the flood playbook and the area is under cap."),
                "compiles": "SIGNED",
            },
        ),
        "acks": {
            "op_a": {
                "vehicles": 6,
                "status": "ACCEPTED",
                "action_taken": "NO_ENTRY applied; affected routes recomputed",
            },
            "op_b": {
                "vehicles": 4,
                "status": "DECLINED",
                "action_taken": "advisory declined; routes unchanged",
                "reason": "local policy requires a dispatcher to confirm a no-entry",
            },
        },
        "fleet": _fleet(),
        "main_route": S1_MAIN_ROUTE,
        "detour_route": S1_DETOUR_ROUTE,
        # The hazard window a ghost drives into, as a longitude band on the
        # arterial; used for the hazard-entry and assist tallies.
        "block_west": -98.3990,
        "block_east": -98.3930,
        "assist_opens_at": 11.0,
        "cluster_at": None,
        "timeline": (
            (0.0, "scenario_state"),
            (1.5, "candidate"),
            (3.0, "hazard"),
            (4.5, "proposal:1"),
            (6.0, "verdict:1"),
            (7.0, "compile:1"),
            (7.5, "advisory"),
            (8.5, "ack:op_a"),
            (9.5, "ack:op_b"),
        ),
        "reroute_at": 7.5,
    },
    "s2_blackout": {
        "code": "S2",
        "hazard_class": "SIGNAL_OUTAGE",
        "severity": 3,
        "playbook": "signal_outage",
        "sentinel": "signal",
        "candidate_id": "cand-mock-s2-signal-1",
        "hazard_id": "haz-mock-s2-signal-1",
        "advisory_id": "adv-mock-s2-signal-1",
        "candidate_confidence": 0.58,
        "hazard_confidence": 0.84,
        "candidate_note": "eleven signalized intersections reporting dark downtown",
        "evidence": (
            (
                "evt-mock-s2-signals-1",
                FEED_AUSTIN_SIGNALS,
                "synthetic.austin.signal_status_dark",
                "11 of 14 downtown controllers dark for over 4 minutes",
            ),
        ),
        "fusion_rationale": (
            "One authoritative city feed, eleven controllers, no conflicting report."
        ),
        "hazard_geometry": {"type": "Polygon", "coordinates": [S2_OUTAGE_RING]},
        "advisory_geometry": {"type": "Polygon", "coordinates": [S2_REVISED_RING]},
        "proposals": (
            {
                "id": "prop-mock-s2-signal-1",
                "action": "ALL_WAY_STOP",
                "policy_rule": "signal_outage.all_way_stop.mock",
                "rationale": "Treat every dark intersection downtown as an all-way stop.",
                "decision": "REVISE",
                "checks": (
                    ("action_allowed_for_class", True),
                    ("area_under_cap", False),
                    ("lifetime_under_cap", True),
                    ("hospital_access_preserved", True),
                ),
                "reasons": (
                    "area 2.4 km2 is over the 1.5 km2 cap for SIGNAL_OUTAGE",
                    "three of the eight blocks have no dark controller reporting",
                ),
                "verdict_rationale": (
                    "The action is right and the area is not: narrow it to the "
                    "blocks that actually reported dark."
                ),
                "compiles": "REJECTED",
                "compile_reasons": ("area_over_cap",),
            },
            {
                "id": "prop-mock-s2-signal-2",
                "action": "ALL_WAY_STOP",
                "policy_rule": "signal_outage.all_way_stop.mock",
                "rationale": "All-way stop over the four blocks reporting dark.",
                "decision": "APPROVE",
                "checks": (
                    ("action_allowed_for_class", True),
                    ("area_under_cap", True),
                    ("lifetime_under_cap", True),
                    ("hospital_access_preserved", True),
                ),
                "reasons": (),
                "verdict_rationale": "Narrowed area is under cap and covers every dark controller.",
                "compiles": "SIGNED",
            },
        ),
        "acks": {
            "op_a": {
                "vehicles": 6,
                "status": "ACCEPTED",
                "action_taken": "ALL_WAY_STOP applied; dark nodes treated as stops",
            },
            "op_b": {
                "vehicles": 4,
                "status": "ACCEPTED",
                "action_taken": "ALL_WAY_STOP applied; unsignalized routes preferred",
            },
        },
        "fleet": _fleet(),
        "main_route": S2_MAIN_ROUTE,
        "detour_route": S2_DETOUR_ROUTE,
        "block_west": -97.7465,
        "block_east": -97.7395,
        # A ghost that stalls at a dark signal is the assist case S2 is about.
        "assist_opens_at": 9.0,
        "cluster_at": 24.0,
        "timeline": (
            (0.0, "scenario_state"),
            (1.5, "candidate"),
            (3.0, "hazard"),
            (4.5, "proposal:1"),
            (6.0, "verdict:1"),
            (7.0, "compile:1"),
            (9.0, "proposal:2"),
            (10.5, "verdict:2"),
            (11.5, "compile:2"),
            (12.0, "advisory"),
            (13.0, "ack:op_a"),
            (14.0, "ack:op_b"),
        ),
        "reroute_at": 12.0,
    },
    "s3_emergency": {
        "code": "S3",
        "hazard_class": "EMERGENCY_SCENE",
        "severity": 4,
        "playbook": "emergency_scene",
        "sentinel": "dispatch-text",
        "candidate_id": "cand-mock-s3-scene-1",
        "hazard_id": "haz-mock-s3-scene-1",
        "advisory_id": "adv-mock-s3-scene-1",
        "candidate_confidence": 0.71,
        "hazard_confidence": 0.88,
        "candidate_note": "structure fire response closing two eastbound lanes",
        # The raw CAD line the text sentinel was handed, and the fields it
        # pulled out of it. Shown side by side in the Sentinels card: the
        # text is DATA, displayed and never interpreted as an instruction.
        "dispatch_text": (
            "AFD INC 2026-0423-00871 BOX ALARM / STRUCTURE E 6TH ST 600 BLK "
            "BTWN RED RIVER + SABINE // E4 L2 B1 ON SCENE // 2 LANES EB "
            "CLOSED // EST 45 MIN // PD TRAFFIC CONTROL REQUESTED"
        ),
        "dispatch_fields": (
            ("incident", "AFD INC 2026-0423-00871"),
            ("type", "STRUCTURE FIRE / BOX ALARM"),
            ("location", "E 6th St, 600 block"),
            ("between", "Red River St and Sabine St"),
            ("units", "Engine 4, Ladder 2, Battalion 1"),
            ("lanes_closed", "2 eastbound"),
            ("expected_clear", "45 min"),
        ),
        "evidence": (
            (
                "evt-mock-s3-incident-1",
                FEED_AUSTIN_INCIDENTS,
                "synthetic.austin.active_incident_dispatch",
                "box alarm, three units on scene, two lanes closed",
            ),
        ),
        "fusion_rationale": (
            "One authoritative dispatch record; units on scene confirm the lane closure."
        ),
        "hazard_geometry": {"type": "LineString", "coordinates": S3_CORRIDOR_LINE},
        "advisory_geometry": {"type": "LineString", "coordinates": S3_CORRIDOR_LINE},
        "proposals": (
            {
                "id": "prop-mock-s3-scene-1",
                "action": "KEEP_CLEAR",
                "policy_rule": "emergency_scene.keep_clear.mock",
                "rationale": (
                    "Keep the two closed lanes clear for 45 minutes so the engine company can work."
                ),
                "decision": "ESCALATE",
                "checks": (
                    ("action_allowed_for_class", True),
                    ("area_under_cap", True),
                    ("lifetime_under_cap", True),
                    ("hospital_access_preserved", True),
                    ("two_key_required", False),
                ),
                "reasons": ("severity 4 requires a human approval token",),
                "verdict_rationale": (
                    "Every policy check passes; severity 4 is over the two-key "
                    "line, so a person has to sign this one."
                ),
                "compiles": "SIGNED",
            },
        ),
        "acks": {
            "op_a": {
                "vehicles": 6,
                "status": "ACCEPTED",
                "action_taken": "KEEP_CLEAR applied; eastbound routes moved to E 7th",
            },
            "op_b": {
                "vehicles": 4,
                "status": "DELAYED",
                "action_taken": "advisory received; applying at next route refresh",
                "reason": "fleet-side route solver runs on a 90 second cycle",
            },
        },
        "fleet": _fleet(),
        "main_route": S3_MAIN_ROUTE,
        "detour_route": S3_DETOUR_ROUTE,
        "block_west": S3_BLOCK_WEST,
        "block_east": S3_BLOCK_EAST,
        # A ghost stopped behind a fire apparatus cannot clear itself: these
        # are the assist requests the ops console counts and clusters. See
        # `_queued_ghost` for why this cohort queues instead of driving on.
        "ghost_queue": True,
        "queue_opens_at": 12.0,
        "queue_stagger": 2.0,
        "queue_lat": 30.26712,
        "queue_inset": 0.00025,
        "queue_spacing": 0.00040,
        "assist_opens_at": 12.0,
        "cluster_at": 20.0,
        "timeline": (
            (0.0, "scenario_state"),
            (2.0, "candidate"),
            (4.0, "hazard"),
            (6.0, "proposal:1"),
            (8.0, "verdict:1"),
            (9.0, "compile:1"),
            # Nothing after this until a person presses Approve: the two-key
            # rule is the point of S3, so the script stops here on purpose.
        ),
        "approved_steps": (
            "compile:1",
            "advisory",
            "ack:op_a",
            "ack:op_b",
        ),
        "reroute_at": None,
    },
}

#: Scenarios with a written timeline — all three, in this build. The
#: viewer's SCRIPTED DATA badge is driven from membership of this set.
SCRIPTED_SCENARIO_IDS = frozenset(SCRIPTS)


def pending_proposal_id(scenario_id: str) -> str | None:
    """The proposal a person has to approve before this script goes on.

    None when the script needs no second key — which is every scenario but
    S3, and is why the Approve button only appears on S3.
    """
    script = SCRIPTS.get(scenario_id)
    if not script:
        return None
    for proposal in script["proposals"]:
        if proposal["decision"] == "ESCALATE":
            return proposal["id"]
    return None


def scripted_frame(
    step: str,
    elapsed: float,
    scenario: dict[str, Any],
    *,
    approved: bool = False,
) -> dict[str, Any] | None:
    """Build the frame one timeline step names."""
    now = datetime.now(UTC)
    scenario_id = scenario["id"]
    if step == "scenario_state":
        return scenario_state_frame(scenario, elapsed=elapsed, state="running")
    if step == "candidate":
        return candidate_frame(scenario_id, now)
    if step == "hazard":
        return hazard_frame(scenario_id, now)
    if step.startswith("proposal:"):
        return proposal_frame(scenario_id, int(step.split(":", 1)[1]), now)
    if step.startswith("verdict:"):
        return verdict_frame(scenario_id, int(step.split(":", 1)[1]), now)
    if step.startswith("compile:"):
        return compile_frame(scenario_id, int(step.split(":", 1)[1]), now, approved=approved)
    if step == "advisory":
        return advisory_frame(scenario_id, now)
    if step.startswith("ack:"):
        return ack_frame(scenario_id, step.split(":", 1)[1], now)
    return None


# ── Assists, and the tallies that go with them ────────────────────────────


def _inside_block(script: dict[str, Any], lon: float) -> bool:
    """Whether a longitude is inside the closed block or hazard band."""
    return script["block_west"] <= lon <= script["block_east"]


def fleet_assists(
    scenario_id: str,
    elapsed: float,
    vehicles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The remote-assist requests this scenario's fleet has open.

    A vehicle stopped inside a closed block cannot clear itself, so it asks
    for a human. Derived from the positions actually being reported, so a
    marker is always on a vehicle that is really there.

    Once ``cluster_at`` has passed and three or more requests are open, surge
    triage groups them under one id — which is what the viewer collapses into
    a single marker with a count badge.
    """
    script = SCRIPTS.get(scenario_id)
    if script is None or elapsed < script["assist_opens_at"]:
        return []
    open_requests = [
        vehicle
        for vehicle in vehicles
        # Only a vehicle with no advisory gets stuck: that is the whole claim
        # the control cohort makes, so an advised vehicle inside the block
        # (there are none after the reroute) would still not be counted here.
        if vehicle.get("ghost") and _inside_block(script, vehicle["lon"])
    ]
    cluster_at = script["cluster_at"]
    clustered = cluster_at is not None and elapsed >= cluster_at and len(open_requests) >= 3
    cluster_id = f"trg-mock-{script['code'].lower()}-1" if clustered else None
    return [
        {
            "id": f"asr-{vehicle['id']}",
            "op": vehicle["op"],
            "lon": vehicle["lon"],
            "lat": vehicle["lat"],
            "ghost": True,
            "state": "QUEUED",
            "cluster": cluster_id,
        }
        for vehicle in open_requests
    ]


def fleet_tallies(
    scenario_id: str,
    vehicles: list[dict[str, Any]],
    assists: list[dict[str, Any]],
    entered: dict[str, set[str]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Hazard entries and assist-queue depth, live cohort against ghost.

    `entered` is the caller's cumulative record, mutated in place: a hazard
    ENTRY is a thing that happened, not a thing that is happening, so a
    vehicle that drove through and out still counts.
    """
    script = SCRIPTS.get(scenario_id)
    if script is None:
        return {"live": 0, "ghost": 0}, {"live": 0, "ghost": 0}
    for vehicle in vehicles:
        if _inside_block(script, vehicle["lon"]):
            entered["ghost" if vehicle.get("ghost") else "live"].add(vehicle["id"])
    hazard_entries = {
        "live": len(entered["live"]),
        "ghost": len(entered["ghost"]),
    }
    assist_queue = {
        "live": sum(1 for row in assists if not row["ghost"]),
        "ghost": sum(1 for row in assists if row["ghost"]),
    }
    return hazard_entries, assist_queue
