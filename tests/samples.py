"""One valid sample per contract model, shared by the unit and contract tests.

Timestamps are fixed, not `now()`, so a round-trip or a golden comparison is
reproducible. The scene is S1: a flood warning over a San Antonio low-water
crossing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from headsup.contracts import (
    Ack,
    AckStatus,
    Action,
    ActionType,
    Advisory,
    AdvisoryProperties,
    AppliesTo,
    AssistReason,
    AssistRequest,
    CandidateHazard,
    CivicEvent,
    CriticVerdict,
    Evidence,
    Hazard,
    HazardClass,
    HeadsUpModel,
    KeepClear,
    LineString,
    MoveOrder,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    ProposedAdvisory,
    Source,
    Telemetry,
    VehicleState,
    VehicleStatus,
    Verdict,
)

#: Fixed clock for every sample.
T0 = datetime(2026, 10, 20, 23, 15, 0, tzinfo=UTC)

#: A low-water crossing on the San Antonio river, closed in S1.
CROSSING_RING = [
    [-98.4946, 29.4195],
    [-98.4921, 29.4195],
    [-98.4921, 29.4213],
    [-98.4946, 29.4213],
    [-98.4946, 29.4195],
]

POLYGON = Polygon(coordinates=[CROSSING_RING])
POINT = Point(coordinates=[-98.4933, 29.4204])
LINE = LineString(coordinates=[[-98.4946, 29.4195], [-98.4921, 29.4213]])

NWS_EVENT = CivicEvent(
    id="ev_nws_flood_1",
    source=Source.NWS,
    kind="flood_warning",
    geometry=POLYGON,
    observed_at=T0,
    raw_ref="data/recordings/s1_flood/nws_0001.json",
    payload={
        "event": "Flood Warning",
        "headline": "Flood Warning issued for Bexar County",
        "severity": "Severe",
    },
    ingested_at=T0 + timedelta(seconds=4),
)

USGS_EVENT = CivicEvent(
    id="ev_usgs_gauge_1",
    source=Source.USGS,
    kind="gauge_reading",
    geometry=POINT,
    observed_at=T0 + timedelta(seconds=30),
    raw_ref="data/recordings/s1_flood/usgs_0001.json",
    payload={"site": "08178050", "gage_height_ft": 11.4, "flood_stage_ft": 9.0},
    ingested_at=T0 + timedelta(seconds=35),
)

NWS_EVIDENCE = Evidence.from_event(NWS_EVENT)
USGS_EVIDENCE = Evidence.from_event(USGS_EVENT)

CANDIDATE = CandidateHazard(
    id="cand_flood_1",
    **{"class": HazardClass.FLOOD},
    severity=4,
    confidence=0.72,
    geometry=POLYGON,
    evidence=[NWS_EVIDENCE],
    detected_by="flood_sentinel",
    detected_at=T0 + timedelta(seconds=10),
    valid_until=T0 + timedelta(hours=3),
    notes="Polygon taken from the NWS warning area, clipped to the crossing.",
)

HAZARD = Hazard(
    id="haz_flood_1",
    **{"class": HazardClass.FLOOD},
    severity=4,
    confidence=0.91,
    geometry=POLYGON,
    evidence=[NWS_EVIDENCE, USGS_EVIDENCE],
    valid_until=T0 + timedelta(hours=3),
    first_seen=T0 + timedelta(seconds=10),
    updated_at=T0 + timedelta(seconds=40),
    candidates=["cand_flood_1"],
    rationale=(
        "The NWS flood warning and the USGS gauge at 11.4 ft against a 9.0 ft flood "
        "stage agree that the crossing is under water."
    ),
)

ACTION = Action(
    type=ActionType.NO_ENTRY,
    applies_to=AppliesTo(operators=["*"], vehicle_classes=["*"], edges=["58291-58294-0"]),
    params={},
)

PROPOSAL = ProposedAdvisory(
    id="prop_flood_1",
    hazard=HAZARD,
    action=ACTION,
    geometry=POLYGON,
    valid_from=T0 + timedelta(seconds=45),
    valid_until=T0 + timedelta(hours=2),
    evidence=[NWS_EVIDENCE, USGS_EVIDENCE],
    policy_rule="flood.no_entry.v1",
    proposed_by="proposer",
    rationale="A low-water crossing under an active warning is a no-entry area, not a slow zone.",
    created_at=T0 + timedelta(seconds=45),
)

VERDICT = CriticVerdict(
    proposal_id="prop_flood_1",
    verdict=Verdict.APPROVE,
    reasons=["Two authoritative sources agree.", "Geometry is limited to the crossing."],
    reviewed_by="critic",
    created_at=T0 + timedelta(seconds=50),
)

ADVISORY = Advisory(
    geometry=POLYGON,
    properties=AdvisoryProperties(
        id="adv_flood_1",
        version=1,
        hazard=HAZARD,
        action=ACTION,
        valid_from=T0 + timedelta(seconds=55),
        valid_until=T0 + timedelta(hours=2),
        evidence=[NWS_EVIDENCE, USGS_EVIDENCE],
        policy_rule="flood.no_entry.v1",
        issuer="headsup/san_antonio",
        signature="3rUqRtQKnJ5n2Y1pZ8m0bWc4dE6fG7hI8jK9lM0nO1p2Q3r4S5t6U7v8W9x0Y1z2A3B4C5D6E7F8g==",
    ),
)

ACK = Ack(
    id="ack_flood_1",
    advisory_id="adv_flood_1",
    advisory_version=1,
    operator="op_c",
    status=AckStatus.DECLINED,
    reason="Confidence 0.91 accepted, but op_c declines advisories it cannot verify locally.",
    vehicles_rerouted=0,
    acknowledged_at=T0 + timedelta(seconds=58),
)

VEHICLE = VehicleState(
    id="veh_op_a_017",
    op="op_a",
    lon=-98.4902,
    lat=29.4221,
    heading=212.5,
    speed_mph=24.0,
    status=VehicleStatus.MOVING,
    trip_id="trip_0412",
)

TELEMETRY = Telemetry(
    tick=120,
    sim_time=T0 + timedelta(minutes=2),
    vehicles=[VEHICLE],
    ghost=False,
    counters={"hazard_entries": 0, "assist_queue": 0},
)

ASSIST_REQUEST = AssistRequest(
    id="assist_flood_1",
    vehicle_id="veh_op_c_004",
    operator="op_c",
    location=POINT,
    reason=AssistReason.FLOODED_EDGE,
    hazard_id="haz_flood_1",
    hazard_class=HazardClass.FLOOD,
    cluster_id="cluster_crossing_1",
    priority=2,
    requested_at=T0 + timedelta(minutes=3),
    ghost=True,
    notes="Stopped at the crossing with no route out; no advisory was accepted.",
)

MOVE_ORDER = MoveOrder(
    id="move_flood_1",
    responder_id="resp_safd_12",
    agency="san_antonio_fire",
    geometry=POLYGON,
    vehicle_ids=["veh_op_a_017", "veh_op_b_002"],
    reason="Swift-water rescue staging at the crossing.",
    token_fingerprint="9f2c" + "0" * 60,
    issued_at=T0 + timedelta(minutes=4),
    valid_until=T0 + timedelta(minutes=34),
)

KEEP_CLEAR = KeepClear(
    id="kc_flood_1",
    geometry=LINE,
    width_m=12.0,
    hazard_id="haz_flood_1",
    advisory_id="adv_flood_1",
    reason="Rescue approach lane.",
    valid_from=T0 + timedelta(minutes=4),
    valid_until=T0 + timedelta(minutes=34),
    priority=1,
)

#: Every exported model with one valid instance, keyed by class name.
SAMPLES: dict[str, HeadsUpModel] = {
    "CivicEvent": NWS_EVENT,
    "CandidateHazard": CANDIDATE,
    "Hazard": HAZARD,
    "ProposedAdvisory": PROPOSAL,
    "CriticVerdict": VERDICT,
    "Advisory": ADVISORY,
    "Ack": ACK,
    "Telemetry": TELEMETRY,
    "AssistRequest": ASSIST_REQUEST,
    "MoveOrder": MOVE_ORDER,
    "KeepClear": KEEP_CLEAR,
    "Evidence": NWS_EVIDENCE,
    "AppliesTo": ACTION.applies_to,
    "Action": ACTION,
    "AdvisoryProperties": ADVISORY.properties,
    "VehicleState": VEHICLE,
}

#: One valid instance of every geometry type.
GEOMETRY_SAMPLES: dict[str, HeadsUpModel] = {
    "Point": POINT,
    "MultiPoint": MultiPoint(coordinates=[[-98.4946, 29.4195], [-98.4921, 29.4213]]),
    "LineString": LINE,
    "MultiLineString": MultiLineString(
        coordinates=[
            [[-98.4946, 29.4195], [-98.4921, 29.4213]],
            [[-98.4900, 29.4220], [-98.4880, 29.4240]],
        ]
    ),
    "Polygon": POLYGON,
    "MultiPolygon": MultiPolygon(coordinates=[[CROSSING_RING]]),
}
