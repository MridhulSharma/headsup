"""Flood the viewer socket: 1,000 vehicles and 200 deflection trails for 60 s.

The load case behind the Heads Up viz budget — *hold 60 fps with 1,000
vehicles* — as a feed rather than as a fixture, because the budget is about
what the browser does with frames arriving at the simulator's own cadence, not
about what it can draw from a static snapshot.

What it sends, on one connection, at 2 Hz:

* ``scenario_state`` — a running, scripted replay. The viewer gates its event
  animations on a run being in progress, so a load case that claims to be idle
  would quietly measure the cheap path.
* ``hazard`` then ``advisory`` at +2 s — one FLOOD area straddling the lanes.
  Publication is what fires the hazard pulse and the dispatch beams.
* ``ack`` at +3 s — one operator accepts (its fleet flashes once), one
  declines (its fleet is tagged). Both are drawn, so both are measured.
* ``telemetry`` throughout — 1,000 vehicles on a lattice of downtown Austin
  arterials. 800 belong to four operators and 200 are the ghost control fleet.
  The 200 advised vehicles whose lanes cross the hazard carry ``rerouted_by``
  and swing clear of it, which is what puts 200 fading trails on screen at
  once; the ghost lanes cross it and keep going, because that is what a
  control group is for.

Run it from the repository root, then open the viewer::

    uv run python viz/load_stream.py
    cd gev && npm run dev            # then press ` for the FPS readout

It serves the FALLBACK port (8766), standing in for ``viz/mock_stream.py``, so
a viewer with no Heads Up API running finds it with no configuration at all.
Pass ``--port 8765`` to stand in for the real API instead.

``gev/scripts/qa-headsup-load.mjs`` drives both ends of this and records the
fps the fork's own HUD reports; this file is the feed half, usable on its own.

Everything here is synthetic and says so: the advisory's issuer is
``headsup/load-stream``, its evidence cites the ``synthetic`` source, and it
carries no signature.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from scenario_script import rfc3339

LOGGER = logging.getLogger("viz.load_stream")

VIEWER_PATH = "/viewer"
#: The fallback port, as `viz/mock_stream.py` serves.
DEFAULT_PORT = 8766

#: Downtown Austin, as a lattice of east-west arterials.
LANE_COUNT = 25
VEHICLES_PER_LANE = 40
WEST_LON = -97.7560
EAST_LON = -97.7300
SOUTH_LAT = 30.2620
LANE_SPACING_LAT = 0.00042

#: The four advised operators.
OPERATORS = ("op_a", "op_b", "op_c", "op_d")
#: The ghost control fleet's lanes.
#:
#: Chosen to STRADDLE the hazard's northern half, so the control fleet drives
#: into the area while the advised lanes swing clear of it. A control group
#: parked somewhere nothing happens would make the cohort colours a decoration
#: rather than a comparison.
GHOST_LANES = range(13, 18)

#: The hazard: one block straddling five of the arterials.
HAZARD_WEST = -97.7466
HAZARD_EAST = -97.7414
HAZARD_SOUTH = 30.2651
HAZARD_NORTH = 30.2680
HAZARD_RING = [
    [HAZARD_WEST, HAZARD_SOUTH],
    [HAZARD_EAST, HAZARD_SOUTH],
    [HAZARD_EAST, HAZARD_NORTH],
    [HAZARD_WEST, HAZARD_NORTH],
    [HAZARD_WEST, HAZARD_SOUTH],
]
#: How far west of the hazard a detour begins, and east of it rejoins. The
#: ramps sit OUTSIDE the hazard's own span on purpose: a vehicle that climbed
#: through the polygon's western edge would be drawn driving into the area the
#: advisory told it to keep out of.
DETOUR_RAMP_LON = 0.0030
#: Clearance between the hazard's northern edge and the innermost detour.
DETOUR_CLEARANCE_LAT = 0.0004

ADVISORY_ID = "adv-load-flood-1"
HAZARD_ID = "haz-load-flood-1"
ADVISORY_HOURS = 6

#: Seconds from the first frame at which each scripted step is due.
ADVISORY_AT = 2.0
ACKS_AT = 3.0
#: How each operator answers. One refusal, so its tag is drawn and measured.
ACK_STATUSES = {
    "op_a": "ACCEPTED",
    "op_b": "DECLINED",
    "op_c": "ACCEPTED",
    "op_d": "ACCEPTED",
}


def _lane_lat(lane: int) -> float:
    return SOUTH_LAT + lane * LANE_SPACING_LAT


def _is_ghost(lane: int) -> bool:
    return lane in GHOST_LANES


#: The lanes that cross the hazard, DERIVED from it rather than listed beside
#: it: the two cannot drift apart, so every advised vehicle whose lane enters
#: the area is rerouted and no vehicle that stays clear of it pretends to be.
REROUTED_LANES = tuple(
    lane
    for lane in range(LANE_COUNT)
    if not _is_ghost(lane) and HAZARD_SOUTH <= _lane_lat(lane) <= HAZARD_NORTH
)
#: How many deflection trails the run therefore puts on screen at once.
REROUTED_VEHICLES = len(REROUTED_LANES) * VEHICLES_PER_LANE


def _detour_target_lat(lane: int) -> float:
    """The latitude one rerouted lane holds while it passes the hazard.

    North of the area, and fanned by lane, so two hundred detoured vehicles
    read as traffic moving onto the next arterials rather than as one line.
    """
    return HAZARD_NORTH + DETOUR_CLEARANCE_LAT + (lane - REROUTED_LANES[0]) * LANE_SPACING_LAT


def _operator(lane: int, index: int) -> str:
    return "op_ghost" if _is_ghost(lane) else OPERATORS[(lane + index) % len(OPERATORS)]


def build_fleet() -> list[dict[str, Any]]:
    """Every vehicle's fixed identity and lane, built once.

    Pure bookkeeping, so a tick only has to walk this list and advance a
    phase: a thousand vehicles twice a second must cost the generator almost
    nothing, or the load case measures Python instead of the browser.
    """
    fleet: list[dict[str, Any]] = []
    for lane in range(LANE_COUNT):
        for index in range(VEHICLES_PER_LANE):
            fleet.append(
                {
                    "id": f"veh-{lane:02d}-{index:02d}",
                    "op": _operator(lane, index),
                    "lane": lane,
                    "lat": _lane_lat(lane),
                    "ghost": _is_ghost(lane),
                    # Only a vehicle whose lane crosses the hazard is moved by
                    # the advisory, and a ghost never is.
                    "reroutes": lane in REROUTED_LANES and not _is_ghost(lane),
                    "detour_lat": (_detour_target_lat(lane) if lane in REROUTED_LANES else 0.0),
                    "phase": (lane * 0.037 + index / VEHICLES_PER_LANE) % 1.0,
                    # A little spread in pace, so the lattice reads as traffic
                    # rather than as one rigid formation.
                    "speed": 0.013 + 0.0022 * ((lane + index) % 5),
                }
            )
    return fleet


def _detour_lat(lane_lat: float, target_lat: float, lon: float) -> float:
    """Where one rerouted vehicle actually is, at longitude `lon`.

    It climbs out of its lane over the ramp west of the hazard, holds clear of
    the area all the way across, and rejoins over the ramp to the east. The
    ramps are cosine shoulders rather than steps: the trail that comes out of
    this is the point of the load case, and a trail with a right angle in it
    would say the simulator teleported the vehicle.
    """
    if lon <= HAZARD_WEST - DETOUR_RAMP_LON or lon >= HAZARD_EAST + DETOUR_RAMP_LON:
        return lane_lat
    if lon < HAZARD_WEST:
        along = (lon - (HAZARD_WEST - DETOUR_RAMP_LON)) / DETOUR_RAMP_LON
    elif lon > HAZARD_EAST:
        along = ((HAZARD_EAST + DETOUR_RAMP_LON) - lon) / DETOUR_RAMP_LON
    else:
        along = 1.0
    eased = 0.5 * (1.0 - math.cos(along * math.pi))
    return lane_lat + (target_lat - lane_lat) * eased


def vehicles_at(
    fleet: list[dict[str, Any]],
    elapsed: float,
    *,
    rerouted: bool,
) -> list[dict[str, Any]]:
    """The whole fleet at `elapsed` seconds, in the viewer's field names."""
    span = EAST_LON - WEST_LON
    rows = []
    for vehicle in fleet:
        lon = WEST_LON + span * ((vehicle["phase"] + elapsed * vehicle["speed"]) % 1.0)
        reroute = rerouted and vehicle["reroutes"]
        lat = _detour_lat(vehicle["lat"], vehicle["detour_lat"], lon) if reroute else vehicle["lat"]
        # The bearing of travel: due east on a lane, and off true through the
        # detour's shoulders, which is what the heading chevron reads.
        ahead = _detour_lat(vehicle["lat"], vehicle["detour_lat"], lon + 1e-5) if reroute else lat
        heading = math.degrees(math.atan2(1e-5 * math.cos(math.radians(lat)), ahead - lat))
        row: dict[str, Any] = {
            "id": vehicle["id"],
            "op": vehicle["op"],
            "lon": round(lon, 7),
            "lat": round(lat, 7),
            "heading": round(heading % 360.0, 1),
        }
        if vehicle["ghost"]:
            row["ghost"] = True
        if reroute:
            row["rerouted_by"] = ADVISORY_ID
        rows.append(row)
    return rows


def _evidence(observed_at: datetime) -> dict[str, str]:
    return {
        "event_id": "evt-load-flood-1",
        "source": "synthetic",
        "kind": "load.flood",
        "observed_at": rfc3339(observed_at),
        "raw_ref": "viz/load_stream.py",
    }


def scenario_state_frame(elapsed: float, state: str = "running") -> dict[str, Any]:
    """The run the viewer gates its event animations on."""
    return {
        "type": "scenario_state",
        "state": state,
        "sim_time": rfc3339(datetime.now(UTC) + timedelta(seconds=elapsed)),
        "elapsed_s": round(elapsed, 2),
        "speed": 1.0,
        "scripted": True,
        "scenario": {
            "id": "load_1000",
            "code": "LOAD",
            "name": "Thousand-vehicle load",
            "city": "Austin",
        },
    }


def hazard_frame(now: datetime) -> dict[str, Any]:
    return {
        "type": "hazard",
        "hazard": {
            "id": HAZARD_ID,
            "class": "FLOOD",
            "severity": 4,
            "confidence": 0.92,
            "geometry": {"type": "Polygon", "coordinates": [HAZARD_RING]},
            "evidence": [_evidence(now)],
            "valid_until": rfc3339(now + timedelta(hours=ADVISORY_HOURS)),
        },
    }


def advisory_frame(now: datetime) -> dict[str, Any]:
    """The published advisory: the frame that fires the pulse and the beams."""
    return {
        "type": "advisory",
        "feature": {
            "type": "Feature",
            "id": ADVISORY_ID,
            "geometry": {"type": "Polygon", "coordinates": [HAZARD_RING]},
            "properties": {
                "id": ADVISORY_ID,
                "version": 1,
                "hazard": hazard_frame(now)["hazard"],
                "action": {
                    "type": "NO_ENTRY",
                    "applies_to": {"operators": ["*"], "vehicle_classes": ["*"]},
                },
                "valid_from": rfc3339(now),
                "valid_until": rfc3339(now + timedelta(hours=ADVISORY_HOURS)),
                "evidence": [_evidence(now)],
                "policy_rule": "flood.no_entry.load",
                "issuer": "headsup/load-stream",
                # Unsigned on purpose: nothing here came from the compiler.
                "signature": None,
            },
        },
    }


def ack_frames(now: datetime) -> list[dict[str, Any]]:
    """Every operator's answer, one frame each."""
    return [
        {
            "type": "ack",
            "ack": {
                "advisory_id": ADVISORY_ID,
                "operator": operator,
                "vehicles": VEHICLES_PER_LANE * len(REROUTED_LANES) // len(OPERATORS),
                "status": status,
                "received_at": rfc3339(now),
            },
        }
        for operator, status in ACK_STATUSES.items()
    ]


# ── The run ───────────────────────────────────────────────────────────────


class Tally:
    """What the generator itself managed, so the fps number has a witness.

    A browser holding 60 fps against a feed that fell behind has not been
    tested, so the run reports its own worst tick: if the generator slipped,
    the measurement says so instead of flattering the viewer.
    """

    def __init__(self) -> None:
        self.frames = 0
        self.bytes = 0
        self.ticks = 0
        self.worst_late_ms = 0.0

    def note(self, payload: str) -> None:
        self.frames += 1
        self.bytes += len(payload)

    def report(self, seconds: float) -> str:
        return (
            f"sent {self.frames} frames ({self.bytes / 1e6:.1f} MB) over "
            f"{self.ticks} ticks in {seconds:.1f}s; worst tick was "
            f"{self.worst_late_ms:.0f} ms late"
        )


async def _send(connection: ServerConnection, frame: dict[str, Any], tally: Tally) -> None:
    payload = json.dumps(frame)
    tally.note(payload)
    await connection.send(payload)


async def drive(
    connection: ServerConnection,
    *,
    rate_hz: float,
    seconds: float,
    fleet: list[dict[str, Any]],
    tally: Tally,
) -> None:
    """Flood one viewer for `seconds`, then return it to idle."""
    interval = 1.0 / rate_hz
    loop = asyncio.get_running_loop()
    started = loop.time()
    tick = 0
    sent_advisory = False
    sent_acks = False
    await _send(connection, scenario_state_frame(0.0), tally)
    while True:
        elapsed = loop.time() - started
        if elapsed >= seconds:
            break
        now = datetime.now(UTC)
        if not sent_advisory and elapsed >= ADVISORY_AT:
            sent_advisory = True
            await _send(connection, hazard_frame(now), tally)
            await _send(connection, advisory_frame(now), tally)
        if sent_advisory and not sent_acks and elapsed >= ACKS_AT:
            sent_acks = True
            for frame in ack_frames(now):
                await _send(connection, frame, tally)
        await _send(
            connection,
            {
                "type": "telemetry",
                "tick": tick,
                "sim_time": rfc3339(now),
                "vehicles": vehicles_at(fleet, elapsed, rerouted=sent_advisory),
            },
            tally,
        )
        tally.ticks += 1
        # Re-state the clock once a second, as the mock does, so a viewer that
        # connected mid-run converges on the right readout.
        if tick % max(1, round(rate_hz)) == 0:
            await _send(connection, scenario_state_frame(elapsed), tally)
        tick += 1
        # Paced against the start, not against the last send, so a slow send
        # shortens the next wait instead of drifting the whole feed late.
        target = started + tick * interval
        late = (loop.time() - target) * 1000.0
        tally.worst_late_ms = max(tally.worst_late_ms, late)
        await asyncio.sleep(max(0.0, target - loop.time()))
    await _send(connection, scenario_state_frame(seconds, state="idle"), tally)
    LOGGER.info("load run complete: %s", tally.report(loop.time() - started))


CORS_HEADERS = (
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "content-type"),
)


def _response(status: HTTPStatus, body: bytes) -> Response:
    return Response(
        status.value,
        status.phrase,
        Headers(
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
                *CORS_HEADERS,
            ]
        ),
        body,
    )


def route_http(request: Request) -> Response:
    """Answer the scenario API's three paths, so the console is not broken.

    This generator has one run and no menu, so the picker is served an EMPTY
    listing rather than a scenario it cannot start. A console that asks is
    told the truth; nothing here pretends to be the mock's catalogue.
    """
    path = request.path.split("?", 1)[0].rstrip("/") or "/"
    if request.method.upper() == "OPTIONS":
        return _response(HTTPStatus.NO_CONTENT, b"")
    if path == "/scenarios":
        return _response(HTTPStatus.OK, b'{"scenarios": []}\n')
    if path.startswith("/scenarios/"):
        return _response(HTTPStatus.OK, b'{"state": "running", "scenario": null}\n')
    return _response(HTTPStatus.NOT_FOUND, b'{"error": "expected /viewer"}\n')


def make_process_request():
    def process_request(_connection: ServerConnection, request: Request) -> Response | None:
        path = request.path.split("?", 1)[0].rstrip("/") or "/"
        if path == VIEWER_PATH and request.method.upper() == "GET":
            return None
        return route_http(request)

    return process_request


async def run(host: str, port: int, rate_hz: float, seconds: float) -> None:
    """Serve until interrupted, flooding each viewer that connects."""
    fleet = build_fleet()

    async def route(connection: ServerConnection) -> None:
        LOGGER.info(
            "viewer connected from %s: %d vehicles, %d rerouted, %g s at %g Hz",
            connection.remote_address,
            len(fleet),
            sum(1 for vehicle in fleet if vehicle["reroutes"]),
            seconds,
            rate_hz,
        )
        tally = Tally()
        try:
            await drive(
                connection,
                rate_hz=rate_hz,
                seconds=seconds,
                fleet=fleet,
                tally=tally,
            )
        except websockets.ConnectionClosed:
            LOGGER.info("viewer disconnected: %s", tally.report(seconds))

    async with serve(route, host, port, process_request=make_process_request()):
        LOGGER.info(
            "load feed on ws://%s:%d%s (Ctrl+C to stop)",
            host,
            port,
            VIEWER_PATH,
        )
        await asyncio.Future()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--rate",
        type=float,
        default=2.0,
        help="telemetry frames per second (default: 2, the simulator's own)",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=60.0,
        help="how long each viewer is flooded (default: 60)",
    )
    parser.add_argument(
        "--vehicles",
        type=int,
        default=LANE_COUNT * VEHICLES_PER_LANE,
        help="reported for the record; the lattice is fixed at 1000",
    )
    args = parser.parse_args(argv)
    if args.rate <= 0:
        parser.error("--rate must be positive")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.vehicles != LANE_COUNT * VEHICLES_PER_LANE:
        parser.error(
            "--vehicles is fixed at "
            f"{LANE_COUNT * VEHICLES_PER_LANE}: the lattice is what puts 200 "
            "of them on a detour, so a different count would be a different "
            "load case"
        )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.host, args.port, args.rate, args.seconds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
