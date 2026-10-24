"""Audit-log builders: a small run written to a chain, and ways to tamper with it.

`write_run` puts a dozen plausible entries on a real chain in a tmp directory,
which is all the verifier and the report need: `AuditRecord.data` is a free
dict, so the tests do not pay for building whole contracts to record them.

`tamper` is the attacker. It rewrites one line of a log in place, either
leaving the line's `hash` alone (the careless edit, caught at that line) or
recomputing it (the careful edit, caught at the next one).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson

from headsup.audit import HASH_FIELD, AuditLog, body_of, canonical_json, chain_hash

T0 = datetime(2026, 4, 20, 18, 55, tzinfo=UTC)

RUN = "run_test"

#: One small run: a flood alert through the pipeline, one published advisory,
#: one refusal, a stopped vehicle and a frame of telemetry. Enough that the
#: digest has something to rank and the report has something to cite.
ENTRIES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "events.nws",
        "CivicEvent",
        {"id": "evt_nws_1", "kind": "flood_warning", "source": "nws"},
    ),
    (
        "events.usgs",
        "CivicEvent",
        {"id": "evt_usgs_1", "kind": "gauge_reading", "source": "usgs"},
    ),
    (
        "candidates",
        "CandidateHazard",
        {"id": "cand_1", "class": "flood", "severity": 4, "confidence": 0.7},
    ),
    (
        "hazards",
        "Hazard",
        {"id": "haz_1", "class": "flood", "severity": 4, "confidence": 0.86},
    ),
    (
        "proposals",
        "ProposedAdvisory",
        {
            "id": "prop_1",
            "class": "flood",
            "action": "NO_ENTRY",
            "policy_rule": "flood.no_entry.v1",
        },
    ),
    (
        "verdicts",
        "CriticVerdict",
        {"id": "ver_1", "proposal_id": "prop_1", "verdict": "APPROVE"},
    ),
    (
        "audit",
        "CompileAudit",
        {
            "id": "aud_1",
            "proposal_id": "prop_1",
            "decision": "PUBLISHED",
            "advisory_id": "adv_1",
            "policy_rule": "flood.no_entry.v1",
            "approved_by": "duty_officer_7",
            "area_km2": 1.4,
            "reasons": [],
        },
    ),
    (
        "advisories",
        "Advisory",
        {"id": "adv_1", "class": "flood", "severity": 4, "action": "NO_ENTRY"},
    ),
    (
        "audit",
        "CompileAudit",
        {
            "id": "aud_2",
            "proposal_id": "prop_2",
            "decision": "REJECTED",
            "reasons": ["area 31.2 km2 is over the 12.0 km2 cap for flood.no_entry.v1"],
        },
    ),
    (
        "assist",
        "AssistRequest",
        {"id": "asr_1", "vehicle_id": "veh_7", "reason": "FLOODED_EDGE", "operator": "op_a"},
    ),
    (
        "telemetry",
        "Telemetry",
        {"id": "tel_1", "vehicle_id": "veh_7", "speed_mps": 0.0},
    ),
    (
        "acks",
        "Ack",
        {"id": "ack_1", "advisory_id": "adv_1", "operator": "op_a"},
    ),
)


def write_run(
    directory: Path,
    *,
    run_id: str = RUN,
    entries: Sequence[tuple[str, str, dict[str, Any]]] = ENTRIES,
    start: datetime = T0,
) -> Path:
    """Write one run's chain and return its path."""
    with AuditLog(run_id, directory=directory) as log:
        for index, (stream, type_name, data) in enumerate(entries):
            moment = start + timedelta(seconds=index)
            log.append(
                stream=stream,
                entry_id=f"{int(moment.timestamp() * 1000)}-0",
                type=type_name,
                data=data,
                published_at=moment.isoformat(),
                recorded_at=moment,
            )
        return log.path


def lines(path: Path) -> list[dict[str, Any]]:
    """Every line of a log as a parsed object, straight off disk."""
    return [orjson.loads(raw) for raw in path.read_bytes().splitlines() if raw.strip()]


def rewrite(path: Path, payloads: Sequence[Mapping[str, Any]]) -> None:
    """Replace a log's contents with these lines, canonically dumped."""
    body = b"".join(canonical_json(payload) + b"\n" for payload in payloads)
    path.write_bytes(body)


def tamper(path: Path, line: int, changes: Mapping[str, Any], *, rehash: bool = False) -> None:
    """Edit one line (1-based) of a log in place.

    With `rehash`, the line's own digest is recomputed so the line is
    self-consistent - the careful attacker, who is caught one line later by a
    `prev_hash` that no longer names its predecessor.
    """
    payloads = lines(path)
    payload = {**payloads[line - 1], **changes}
    if rehash:
        payload[HASH_FIELD] = chain_hash(str(payload["prev_hash"]), body_of(payload))
    payloads[line - 1] = payload
    rewrite(path, payloads)


def hashes(path: Path) -> list[str]:
    """Every line's chain hash, oldest first."""
    return [str(payload[HASH_FIELD]) for payload in lines(path)]
