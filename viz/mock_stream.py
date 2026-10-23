"""Fake viewer socket and scenario API: no Redis, PostGIS, compiler or key.

Serves the same two contracts the real Heads Up bridge does, on one port, so
the God's Eye View `headsup` layer and its Heads Up mode can be developed and
demonstrated against nothing.

**This is the FALLBACK, on port 8766.** The real API (`hu api`) owns 8765, and
the viewer asks it first; it drops to this one only when nothing answers
there, and then says MOCK on its feed chip. One port each means both can be up
at once, so "the API is not running" is something the page discovers rather
than something a developer has to be told. Pass ``--port 8765`` to stand in
for the real API outright.

* ``ws://localhost:8766/viewer`` — the viewer socket.
  - ``{"type": "telemetry", "vehicles": [{id, op, lon, lat, heading, ...}]}``
    at 2 Hz. Idle, that is one vehicle driving a downtown Austin block; inside
    a scenario it is that scenario's fleet, and after the advisory some
    vehicles carry ``rerouted_by``.
  - ``{"type": "advisory", "feature": <Advisory GeoJSON>}``, plus the
    ``scenario_state``, ``candidate``, ``hazard``, ``proposal``, ``verdict``
    and ``ack`` frames the agent pipeline rail is driven from.
* ``http://localhost:8766`` — the scenario API.
  - ``GET /scenarios`` lists S1, S2 and S3 with a one-line what-if each.
  - ``POST /scenarios/{id}/start`` starts one; every connected viewer picks it
    up within a tick and replays its scripted sequence.
  - ``POST /scenarios/stop`` returns every viewer to the idle feed.

Run it from the repository root::

    uv run python viz/mock_stream.py
    uv run python viz/mock_stream.py --host 0.0.0.0 --port 8766 --rate 2

Everything here is synthetic and says so: every advisory's evidence cites the
`synthetic` source and its issuer is `headsup/mock-stream`, never a real
sentinel, and it carries no signature — nothing downstream may mistake this
for a published advisory. `viz/scenario_script.py` holds the script itself.
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

from scenario_script import (
    S1_REROUTE_AT,
    S1_TIMELINE,
    SCENARIOS,
    SCENARIOS_BY_ID,
    SCRIPTED_SCENARIO_ID,
    rfc3339,
    s1_vehicles,
    scenario_state_frame,
    scripted_frame,
)

LOGGER = logging.getLogger("viz.mock_stream")

#: The path the God's Eye View layer connects to.
VIEWER_PATH = "/viewer"
#: The fallback port. 8765 belongs to the real API; see the module docstring.
DEFAULT_PORT = 8766
#: The scenario API's collection path.
SCENARIOS_PATH = "/scenarios"

#: One downtown Austin block: Congress Ave / 6th St, driven clockwise.
BLOCK_CENTRE_LON = -97.7431
BLOCK_CENTRE_LAT = 30.2686
#: Half a short block, in degrees (~90 m east-west, ~80 m north-south).
BLOCK_HALF_LON = 0.00095
BLOCK_HALF_LAT = 0.00072
#: Seconds for one full lap, so the dot is visibly moving but not twitching.
LAP_SECONDS = 48.0

#: The flood polygon, one block east of the loop, in GeoJSON ring order.
FLOOD_RING = [
    [-97.7405, 30.2676],
    [-97.7381, 30.2676],
    [-97.7381, 30.2695],
    [-97.7405, 30.2695],
    [-97.7405, 30.2676],
]

VEHICLE_ID = "veh-mock-1"
OPERATOR_ID = "op_mock"
ADVISORY_ID = "adv-mock-flood-1"
#: How long the mock advisory stays in force once a viewer connects.
ADVISORY_HOURS = 6


def _rfc3339(moment: datetime) -> str:
    """Format an instant the way the advisory contract expects."""
    return rfc3339(moment)


def vehicle_at(elapsed_seconds: float) -> dict[str, object]:
    """One vehicle state, lapping the block clockwise from its north-west corner.

    The loop is a rounded rectangle traced with sine and cosine rather than four
    straight legs: a corner the dot eases through shows interpolation working,
    while a hard turn would hide a stuttering feed behind the direction change.
    """
    phase = (elapsed_seconds % LAP_SECONDS) / LAP_SECONDS * math.tau
    lon = BLOCK_CENTRE_LON + BLOCK_HALF_LON * math.sin(phase)
    lat = BLOCK_CENTRE_LAT + BLOCK_HALF_LAT * math.cos(phase)
    # Heading is the bearing of travel: the derivative of the path above,
    # measured clockwise from north the way the contract defines it.
    east = BLOCK_HALF_LON * math.cos(phase)
    north = -BLOCK_HALF_LAT * math.sin(phase)
    heading = math.degrees(math.atan2(east, north)) % 360.0
    return {
        "id": VEHICLE_ID,
        "op": OPERATOR_ID,
        "lon": round(lon, 7),
        "lat": round(lat, 7),
        "heading": round(heading, 1),
    }


def telemetry_frame(
    tick: int,
    elapsed_seconds: float,
    vehicles: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """One simulator tick in the viewer's field names."""
    return {
        "type": "telemetry",
        "tick": tick,
        "sim_time": _rfc3339(datetime.now(UTC)),
        "vehicles": vehicles if vehicles is not None else [vehicle_at(elapsed_seconds)],
    }


def advisory_frame(now: datetime | None = None) -> dict[str, object]:
    """The static flood advisory, valid from the moment it is sent."""
    opened = now or datetime.now(UTC)
    return {
        "type": "advisory",
        "feature": {
            "type": "Feature",
            "id": ADVISORY_ID,
            "geometry": {"type": "Polygon", "coordinates": [FLOOD_RING]},
            "properties": {
                "id": ADVISORY_ID,
                "version": 1,
                "hazard": {
                    "id": "haz-mock-flood-1",
                    "class": "FLOOD",
                    "severity": 4,
                    "confidence": 0.9,
                    "geometry": {"type": "Polygon", "coordinates": [FLOOD_RING]},
                    "evidence": [_evidence(opened)],
                    "valid_until": _rfc3339(opened + timedelta(hours=ADVISORY_HOURS)),
                },
                "action": {
                    "type": "NO_ENTRY",
                    "applies_to": {"operators": ["*"], "vehicle_classes": ["*"]},
                },
                "valid_from": _rfc3339(opened),
                "valid_until": _rfc3339(opened + timedelta(hours=ADVISORY_HOURS)),
                "evidence": [_evidence(opened)],
                "policy_rule": "flood.no_entry.mock",
                "issuer": "headsup/mock-stream",
                # Unsigned on purpose: this never came from the compiler, and
                # the viewer should show it as unsigned rather than trusted.
                "signature": None,
            },
        },
    }


def _evidence(observed_at: datetime) -> dict[str, str]:
    return {
        "event_id": "evt-mock-flood-1",
        "source": "synthetic",
        "kind": "mock.flood",
        "observed_at": _rfc3339(observed_at),
        "raw_ref": "viz/mock_stream.py",
    }


class ScenarioRunner:
    """Which scenario the whole server is replaying, and since when.

    One run for the whole process, not one per viewer: the picker in the
    browser starts a scenario over HTTP on a connection that is gone by the
    time the first frame goes out, so the state it sets has to be somewhere
    every open viewer can see. `generation` is what the viewer loops watch —
    it moves on every start and every stop, so a loop notices a change
    without needing to compare scenario identity.
    """

    def __init__(self) -> None:
        self.scenario: dict[str, Any] | None = None
        self.generation = 0
        self.started_at: float | None = None

    def start(self, scenario: dict[str, Any], clock: float) -> None:
        self.scenario = scenario
        self.started_at = clock
        self.generation += 1

    def stop(self) -> bool:
        was_running = self.scenario is not None
        self.scenario = None
        self.started_at = None
        self.generation += 1
        return was_running


# ── The scenario API ──────────────────────────────────────────────────────

#: A browser on :4173 calls :8766, so every answer is cross-origin. The
#: scenario API is a dev-only loopback service with nothing to protect and no
#: credentials to leak, so it answers any origin rather than carrying a list
#: of ports a developer might be serving the viewer from.
CORS_HEADERS = (
    ("Access-Control-Allow-Origin", "*"),
    ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ("Access-Control-Allow-Headers", "content-type"),
    ("Access-Control-Max-Age", "600"),
)


def _response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    """One HTTP answer, built whole rather than patched after the fact.

    `ServerConnection.respond` would hand back a text/plain response whose
    headers then need editing; `Headers` is a multidict, so setting
    Content-Type again would send it twice. Building the response here keeps
    exactly one of every header.
    """
    return Response(
        status.value,
        status.phrase,
        Headers(
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
                *CORS_HEADERS,
            ]
        ),
        body,
    )


def _json_response(status: HTTPStatus, payload: object) -> Response:
    body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    return _response(status, body, "application/json; charset=utf-8")


def _text_response(status: HTTPStatus, text: str) -> Response:
    return _response(status, text.encode("utf-8"), "text/plain; charset=utf-8")


def _scenario_listing() -> list[dict[str, Any]]:
    """What `GET /scenarios` serves: the picker's rows, nothing internal."""
    return [
        {
            "id": scenario["id"],
            "code": scenario["code"],
            "name": scenario["name"],
            "city": scenario["city"],
            "hazard_class": scenario["hazard_class"],
            "what_if": scenario["what_if"],
            "speed": scenario["speed"],
            "start_view": dict(scenario["start_view"]),
            "scripted": scenario["id"] == SCRIPTED_SCENARIO_ID,
        }
        for scenario in SCENARIOS
    ]


def route_http(request: Request, runner: ScenarioRunner, clock: float) -> Response:
    """Answer one scenario-API request, or say what was expected instead."""
    path = request.path.split("?", 1)[0].rstrip("/") or "/"
    method = request.method.upper()
    if method == "OPTIONS":
        # Preflight. The browser only asks when a request is not simple, but
        # answering always costs nothing and keeps a future JSON POST working.
        return _response(HTTPStatus.NO_CONTENT, b"", "text/plain; charset=utf-8")
    if path == SCENARIOS_PATH and method == "GET":
        return _json_response(HTTPStatus.OK, {"scenarios": _scenario_listing()})
    if path == f"{SCENARIOS_PATH}/stop" and method == "POST":
        was_running = runner.stop()
        LOGGER.info("scenario stopped (was running: %s)", was_running)
        return _json_response(HTTPStatus.OK, {"state": "idle", "was_running": was_running})
    if path.startswith(f"{SCENARIOS_PATH}/") and path.endswith("/start"):
        if method != "POST":
            return _text_response(HTTPStatus.METHOD_NOT_ALLOWED, "POST /scenarios/{id}/start\n")
        scenario_id = path[len(SCENARIOS_PATH) + 1 : -len("/start")]
        scenario = SCENARIOS_BY_ID.get(scenario_id)
        if scenario is None:
            # The id came off the wire: name only the ids that exist, never
            # the one that was asked for.
            return _json_response(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "unknown scenario",
                    "known": sorted(SCENARIOS_BY_ID),
                },
            )
        runner.start(scenario, clock)
        LOGGER.info("scenario started: %s", scenario_id)
        return _json_response(
            HTTPStatus.OK,
            {
                "state": "running",
                "scenario": {
                    "id": scenario["id"],
                    "code": scenario["code"],
                    "name": scenario["name"],
                    "city": scenario["city"],
                    "speed": scenario["speed"],
                    "start_view": dict(scenario["start_view"]),
                },
            },
        )
    LOGGER.warning("refused an HTTP %s to %s", method, path)
    return _text_response(
        HTTPStatus.NOT_FOUND,
        f"expected {VIEWER_PATH} (websocket), "
        f"GET {SCENARIOS_PATH}, "
        f"POST {SCENARIOS_PATH}/<id>/start or POST {SCENARIOS_PATH}/stop\n",
    )


# ── The viewer socket ─────────────────────────────────────────────────────


async def _send(connection: ServerConnection, frame: dict[str, object]) -> None:
    await connection.send(json.dumps(frame))


async def _run_idle(
    connection: ServerConnection,
    rate_hz: float,
    runner: ScenarioRunner,
    generation: int,
) -> None:
    """The feed with no scenario running: the original one-vehicle block.

    Kept exactly as it was so `hu demo` fixtures and the D2.7 load script see
    the same idle contract they were written against.
    """
    await _send(connection, scenario_state_frame(None, state="idle"))
    await _send(connection, advisory_frame())
    interval = 1.0 / rate_hz
    started = asyncio.get_running_loop().time()
    tick = 0
    while runner.generation == generation:
        loop_now = asyncio.get_running_loop().time()
        await _send(connection, telemetry_frame(tick, loop_now - started))
        tick += 1
        # Pace against the start, not against the last send, so a slow send
        # shortens the next wait instead of drifting the whole feed late.
        target = started + tick * interval
        await asyncio.sleep(max(0.0, target - asyncio.get_running_loop().time()))


async def _run_scenario(
    connection: ServerConnection,
    rate_hz: float,
    runner: ScenarioRunner,
    generation: int,
    scenario: dict[str, Any],
) -> None:
    """Replay one scenario: the scripted frames over a continuous fleet.

    Telemetry and the script share one send loop, so the pipeline frames
    always arrive in timeline order relative to the fleet they describe. The
    script's own offsets are wall-clock seconds from the start call, which is
    what the picker's "replay speed" readout is reported against.
    """
    scripted = scenario["id"] == SCRIPTED_SCENARIO_ID
    timeline = list(S1_TIMELINE) if scripted else [(0.0, "scenario_state")]
    interval = 1.0 / rate_hz
    started = asyncio.get_running_loop().time()
    tick = 0
    next_step = 0
    while runner.generation == generation:
        elapsed = asyncio.get_running_loop().time() - started
        # Every step whose offset has passed, in order, before this tick's
        # telemetry: a viewer must never see the fleet reroute before the
        # advisory that rerouted it.
        while next_step < len(timeline) and timeline[next_step][0] <= elapsed:
            offset, step = timeline[next_step]
            next_step += 1
            frame = scripted_frame(step, offset, scenario)
            if frame is not None:
                await _send(connection, frame)
        vehicles = (
            s1_vehicles(elapsed, rerouted=elapsed >= S1_REROUTE_AT)
            if scripted
            else [vehicle_at(elapsed)]
        )
        await _send(connection, telemetry_frame(tick, elapsed, vehicles))
        # The clock readout is re-stated every second so a viewer that
        # connected mid-run converges on the right sim time and speed.
        if tick % max(1, round(rate_hz)) == 0 and next_step >= len(timeline):
            await _send(
                connection,
                scenario_state_frame(scenario, elapsed=elapsed, state="running"),
            )
        tick += 1
        target = started + tick * interval
        await asyncio.sleep(max(0.0, target - asyncio.get_running_loop().time()))


async def _drive(connection: ServerConnection, rate_hz: float, runner: ScenarioRunner) -> None:
    """Feed one viewer forever, following whatever the runner is replaying.

    Each phase runs until `runner.generation` moves, which it does on every
    start and stop. A phase therefore notices a change within one tick rather
    than holding a stale picture until it happens to finish.
    """
    while True:
        generation = runner.generation
        scenario = runner.scenario
        if scenario is None:
            await _run_idle(connection, rate_hz, runner, generation)
        else:
            await _run_scenario(connection, rate_hz, runner, generation, scenario)


async def handler(connection: ServerConnection, rate_hz: float, runner: ScenarioRunner) -> None:
    """One viewer connection, driven until it goes away."""
    LOGGER.info("viewer connected from %s", connection.remote_address)
    try:
        await _drive(connection, rate_hz, runner)
    except websockets.ConnectionClosed:
        LOGGER.info("viewer disconnected")


def make_process_request(runner: ScenarioRunner):
    """Serve the scenario API, and let only `/viewer` reach the handshake.

    Refusing a mistyped socket path at the handshake rather than closing an
    open socket keeps it an obvious failure in the browser's network panel
    instead of a connection that opens and then silently goes quiet.
    """

    def process_request(connection: ServerConnection, request: Request) -> Response | None:
        path = request.path.split("?", 1)[0].rstrip("/") or "/"
        if path == VIEWER_PATH and request.method.upper() == "GET":
            return None
        return route_http(request, runner, asyncio.get_running_loop().time())

    return process_request


async def run(host: str, port: int, rate_hz: float) -> None:
    """Serve until interrupted."""
    runner = ScenarioRunner()

    async def route(connection: ServerConnection) -> None:
        await handler(connection, rate_hz, runner)

    async with serve(route, host, port, process_request=make_process_request(runner)):
        LOGGER.info(
            "mock viewer feed on ws://%s:%d%s at %g Hz; scenarios on "
            "http://%s:%d%s (Ctrl+C to stop)",
            host,
            port,
            VIEWER_PATH,
            rate_hz,
            host,
            port,
            SCENARIOS_PATH,
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
        help="telemetry frames per second (default: 2)",
    )
    args = parser.parse_args(argv)
    if args.rate <= 0:
        parser.error("--rate must be positive")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args.host, args.port, args.rate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
