"""Fake viewer socket: one vehicle, one flood polygon, no Heads Up services.

Serves the same `ws://localhost:8765/viewer` contract the real viewer bridge
will, so the God's Eye View `headsup` layer can be developed and demonstrated
without Redis, PostGIS, the compiler or an API key:

* `{"type": "telemetry", "vehicles": [{id, op, lon, lat, heading}]}` at 2 Hz,
  one vehicle driving clockwise around a downtown Austin block.
* `{"type": "advisory", "feature": <Advisory GeoJSON>}` once per connection, a
  static flood polygon over the next block east.

Run it from the repository root::

    uv run python viz/mock_stream.py
    uv run python viz/mock_stream.py --host 0.0.0.0 --port 8765 --rate 2

Everything here is synthetic and says so: the advisory's evidence cites the
`synthetic` source and its issuer is `headsup/mock-stream`, never a real
sentinel, and it carries no signature — nothing downstream may mistake this
for a published advisory.
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

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

LOGGER = logging.getLogger("viz.mock_stream")

#: The path the God's Eye View layer connects to.
VIEWER_PATH = "/viewer"

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
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def telemetry_frame(tick: int, elapsed_seconds: float) -> dict[str, object]:
    """One simulator tick in the viewer's field names."""
    return {
        "type": "telemetry",
        "tick": tick,
        "sim_time": _rfc3339(datetime.now(UTC)),
        "vehicles": [vehicle_at(elapsed_seconds)],
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


async def _drive(connection: ServerConnection, rate_hz: float) -> None:
    """Send the advisory once, then telemetry forever at `rate_hz`."""
    await connection.send(json.dumps(advisory_frame()))
    interval = 1.0 / rate_hz
    started = asyncio.get_running_loop().time()
    tick = 0
    while True:
        loop_now = asyncio.get_running_loop().time()
        await connection.send(json.dumps(telemetry_frame(tick, loop_now - started)))
        tick += 1
        # Pace against the start, not against the last send, so a slow send
        # shortens the next wait instead of drifting the whole feed late.
        target = started + tick * interval
        await asyncio.sleep(max(0.0, target - asyncio.get_running_loop().time()))


def reject_other_paths(connection: ServerConnection, request: Request) -> Response | None:
    """Answer anything but `/viewer` with a 404 before the handshake.

    Refusing at the handshake rather than closing an open socket keeps a
    mistyped URL an obvious failure in the browser's network panel instead of
    a connection that opens and then silently goes quiet.
    """
    path = request.path.split("?", 1)[0].rstrip("/") or "/"
    if path == VIEWER_PATH:
        return None
    LOGGER.warning("refused a connection to %s", path)
    return connection.respond(HTTPStatus.NOT_FOUND, f"expected {VIEWER_PATH}\n")


async def handler(connection: ServerConnection, rate_hz: float) -> None:
    """One viewer connection, driven until it goes away."""
    LOGGER.info("viewer connected from %s", connection.remote_address)
    try:
        await _drive(connection, rate_hz)
    except websockets.ConnectionClosed:
        LOGGER.info("viewer disconnected")


async def run(host: str, port: int, rate_hz: float) -> None:
    """Serve until interrupted."""

    async def route(connection: ServerConnection) -> None:
        await handler(connection, rate_hz)

    async with serve(route, host, port, process_request=reject_other_paths):
        LOGGER.info(
            "mock viewer feed on ws://%s:%d%s at %g Hz (Ctrl+C to stop)",
            host,
            port,
            VIEWER_PATH,
            rate_hz,
        )
        await asyncio.Future()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
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
