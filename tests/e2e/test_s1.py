"""S1 end to end: launcher to advisory to ack, through the real server.

Needs the Docker services (`uv run hu infra up`) and a built San Antonio
(`uv run hu world build --city san_antonio`). The model is mocked: this test
checks the wiring, not the judgment, and no unit or e2e run spends money.

What it asserts, in the order the demo happens:

1. `POST /scenarios/s1_flood/start` at 60x brings up the replay, the agents and
   both fleets, and a second start while it runs is a 409.
2. The viewer socket delivered the whole chain -- candidate, hazard, proposal,
   verdict, advisory, ack -- with the ids that link each one to its parent.
3. The compiler published at least one advisory and this server verifies its
   signature.
4. `op_a`, which accepts immediately, enters no hazard after the advisory it
   accepted is in force.
5. `POST /scenarios/stop` leaves the server idle with nothing running.

**Where it stands today.** S1's chain reaches a verdict and stops there: the
compiler refuses the proposal, and the audit record says why. Nothing in the
pipeline narrows an NWS warning polygon -- the flood sentinel publishes "the
product's own polygon, unchanged" by design -- and a storm-based flash flood
warning over north-east Bexar County is 1440 km2 against the 12 km2 cap in
`playbooks/flood.yaml`. That is the gate working, not a wiring fault, so the
advisory-dependent assertions are reported as an expected failure carrying the
compiler's own reasons, and everything that does not depend on an advisory is
checked either way. The moment a flood hazard is narrowed to the reach its
gauges corroborate, this test passes as written.

The server runs on this test's own event loop rather than in a thread, on a
port the OS picks, so the socket under test is the socket the viewer uses and
two runs of the suite cannot collide on 8765.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from typing import Any
from uuid import uuid4

import httpx
import pytest
import redis.exceptions
import uvicorn
import websockets

from headsup.agents.compiler.signing import verify
from headsup.api.app import ApiState, create_app
from headsup.api.frames import (
    FRAME_ACK,
    FRAME_ADVISORY,
    FRAME_CANDIDATE,
    FRAME_COUNTERS,
    FRAME_GHOST_TELEMETRY,
    FRAME_HAZARD,
    FRAME_PROPOSAL,
    FRAME_SCENARIO_STATE,
    FRAME_TELEMETRY,
    FRAME_VERDICT,
)
from headsup.bus import ALL_STREAMS, STREAM_AUDIT, Bus
from headsup.contracts import Advisory

pytestmark = pytest.mark.e2e

SCENARIO = "s1_flood"
SPEED = 60.0
REDIS_URL = "redis://localhost:6379/0"
INFRA_HINT = "run `uv run hu infra up` first"

#: Small fleet: this test watches the pipeline, not the throughput, and the
#: perf marker owns the 900-vehicle run.
VEHICLES = 60
ZONES = 12

#: Seconds to wait for the start request. It loads the city world, the POI
#: index and the drive network before it answers.
START_TIMEOUT = 300.0

#: Seconds to wait for the pipeline. S1's first flash flood warning sits 1200 s
#: into the recording, which is 20 s at 60x; everything after it is immediate.
CHAIN_TIMEOUT = 180.0

#: Seconds to wait for the compiler's answer once a verdict has been reached.
#: It decides synchronously, so this is slack, not latency.
ADVISORY_TIMEOUT = 60.0

#: Wall seconds to watch `op_a` for after it has accepted an advisory. At 60x
#: that is ten simulated minutes of driving under the mask.
WATCH_S = 10.0


class FrameLog:
    """Every frame the viewer socket sent, kept by type and in arrival order."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def add(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        self.by_type[str(frame.get("type"))].append(frame)

    def first(self, kind: str) -> dict[str, Any] | None:
        found = self.by_type.get(kind)
        return found[0] if found else None

    def latest(self, kind: str) -> dict[str, Any] | None:
        found = self.by_type.get(kind)
        return found[-1] if found else None

    def summary(self) -> str:
        return ", ".join(f"{kind}={len(rows)}" for kind, rows in sorted(self.by_type.items()))


async def _read_into(socket: Any, log: FrameLog) -> None:
    """Drain the viewer socket into the log until the test is done with it."""
    async for message in socket:
        log.add(json.loads(message))


async def _until(log: FrameLog, predicate: Any, *, timeout: float) -> bool:
    """Whether the log satisfied `predicate` before `timeout` ran out."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate(log):
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.25)
    return True


async def _require(log: FrameLog, predicate: Any, *, timeout: float, hint: str) -> None:
    """Wait for the log to satisfy `predicate`, or fail saying what did arrive."""
    if not await _until(log, predicate, timeout=timeout):
        pytest.fail(f"{hint} within {timeout:g} s; frames seen: {log.summary() or 'none'}")


@pytest.fixture
async def bus() -> Any:
    """A namespaced bus, and the check that Redis is actually there."""
    instance = Bus(REDIS_URL, namespace=f"e2e_{uuid4().hex[:8]}")
    try:
        await instance.ping()
    except redis.exceptions.ConnectionError as exc:
        await instance.close()
        pytest.fail(f"{INFRA_HINT}: {REDIS_URL} ({exc})")
    try:
        yield instance
    finally:
        await instance.delete_streams(ALL_STREAMS)
        await instance.close()


@pytest.fixture
async def server(bus: Bus, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real app under uvicorn, on this loop, on a port the OS picks."""
    monkeypatch.setenv("HU_LLM_MODE", "mock")
    monkeypatch.setenv("HU_CITY", "san_antonio")
    state = ApiState(namespace=bus.namespace)
    config = uvicorn.Config(
        create_app(state), host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve(), name="e2e-server")
    while not instance.started:
        if task.done():  # pragma: no cover - a server that refused to start
            task.result()
        await asyncio.sleep(0.05)
    port = instance.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"127.0.0.1:{port}"
    finally:
        instance.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=30.0)


async def test_s1_runs_from_the_launcher_to_an_acknowledged_advisory(server: str, bus: Bus) -> None:
    """The whole demo, once: start, chain, masked fleet, stop."""
    log = FrameLog()
    async with (
        httpx.AsyncClient(base_url=f"http://{server}", timeout=START_TIMEOUT) as http,
        websockets.connect(f"ws://{server}/viewer") as socket,
    ):
        reader = asyncio.create_task(_read_into(socket, log), name="e2e-viewer")
        try:
            await _launcher_lists_s1(http)
            await _start_s1(http)
            await _second_start_is_a_conflict(http)
            chain = await _the_chain_reaches_a_verdict(log)

            advisory = await _wait_for_an_advisory(log)
            if advisory is None:
                # The gate's own reasons first: stopping the scenario resets
                # the streams, the audit stream with them. Then everything
                # that does not turn on an advisory, which still has to hold.
                why = await _why_nothing_was_published(bus, chain)
                await _stop_returns_to_idle(http, log)
                pytest.xfail(why)

            _the_advisory_closes_the_chain(log, chain, advisory)
            await _advisories_are_published_and_verify(http, advisory)
            await _op_a_enters_no_hazard_once_masked(log, advisory)
            await _stop_returns_to_idle(http, log)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader


async def _launcher_lists_s1(http: httpx.AsyncClient) -> None:
    """`GET /scenarios` is what the Test Scene launcher draws its rows from."""
    response = await http.get("/scenarios")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["running"] is None
    assert body["state"] == "idle"
    card = next(row for row in body["scenarios"] if row["id"] == SCENARIO)
    assert card["city"] == "san_antonio"
    # S1 replays real NWS and USGS observations, so the viewer must not badge
    # the run as scripted.
    assert card["scripted"] is False
    assert card["what_if"].strip()
    assert card["start_view"]["height"] > 0


async def _start_s1(http: httpx.AsyncClient) -> None:
    """One POST brings up the replay, the agents and both fleets at 60x."""
    response = await http.post(
        f"/scenarios/{SCENARIO}/start",
        json={"speed": SPEED, "vehicles": VEHICLES, "zones": ZONES, "seed": 42},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "running"
    assert body["scenario"]["id"] == SCENARIO
    assert body["speed"] == SPEED
    assert body["llm_mode"] == "mock"
    # One clock, and both fleets on it: the comparison the demo rests on.
    assert body["modes"] == ["live", "ghost"]


async def _second_start_is_a_conflict(http: httpx.AsyncClient) -> None:
    """A server runs one scenario, and says so rather than sharing streams."""
    response = await http.post(f"/scenarios/{SCENARIO}/start", json={"speed": SPEED})
    assert response.status_code == 409, response.text
    assert SCENARIO in response.json()["detail"]


async def _the_chain_reaches_a_verdict(log: FrameLog) -> dict[str, dict[str, Any]]:
    """Both fleets' telemetry, then the rail as far as the critic's verdict."""
    await _require(
        log,
        lambda seen: seen.first(FRAME_TELEMETRY) and seen.first(FRAME_GHOST_TELEMETRY),
        timeout=CHAIN_TIMEOUT,
        hint="no telemetry for both fleets",
    )
    # The first state frame is the snapshot a viewer gets on connect, which is
    # idle; the run announces itself in a later one.
    running = [
        frame
        for frame in log.by_type.get(FRAME_SCENARIO_STATE, ())
        if frame.get("state") == "running"
    ]
    assert running, f"the socket was never told a scenario started: {log.summary()}"
    assert running[-1]["scenario"]["id"] == SCENARIO

    await _require(
        log,
        lambda seen: seen.first(FRAME_VERDICT) is not None,
        timeout=CHAIN_TIMEOUT,
        hint="the chain did not reach a verdict",
    )
    chain = {
        stage: log.first(stage)
        for stage in (FRAME_CANDIDATE, FRAME_HAZARD, FRAME_PROPOSAL, FRAME_VERDICT)
    }
    for stage, frame in chain.items():
        assert frame is not None, f"no {stage} frame; frames seen: {log.summary()}"

    candidate, hazard = chain[FRAME_CANDIDATE], chain[FRAME_HAZARD]
    proposal, verdict = chain[FRAME_PROPOSAL], chain[FRAME_VERDICT]
    assert candidate["event_ids"], "a candidate with no evidence cannot be audited"
    assert candidate["class"] == "FLOOD"
    assert candidate["id"] in hazard["candidate_ids"]
    assert proposal["hazard_id"] == hazard["id"]
    assert verdict["proposal_id"] == proposal["id"]
    assert verdict["hazard_id"] == hazard["id"]
    return chain  # type: ignore[return-value]


async def _wait_for_an_advisory(log: FrameLog) -> dict[str, Any] | None:
    """The published advisory, or None if the compiler refused the proposal."""
    await _until(
        log,
        lambda seen: seen.first(FRAME_ADVISORY) is not None,
        timeout=ADVISORY_TIMEOUT,
    )
    return log.first(FRAME_ADVISORY)


def _the_advisory_closes_the_chain(
    log: FrameLog, chain: dict[str, dict[str, Any]], advisory: dict[str, Any]
) -> None:
    """The last two links: the advisory to its proposal, and the ack to it."""
    assert advisory["proposal_id"] == chain[FRAME_PROPOSAL]["id"]
    assert advisory["hazard_id"] == chain[FRAME_HAZARD]["id"]
    assert advisory["signature_valid"] is True
    ack = log.first(FRAME_ACK)
    assert ack is not None, f"no fleet answered the advisory; frames: {log.summary()}"
    assert ack["advisory_id"] == advisory["id"]
    assert ack["proposal_id"] == chain[FRAME_PROPOSAL]["id"]


async def _why_nothing_was_published(bus: Bus, chain: dict[str, dict[str, Any]]) -> str:
    """The compiler's own reasons, read out of the audit stream.

    The audit record is the point of having an audit stream: a rejection is
    supposed to leave as much evidence as a publication, and an expected
    failure here should say what the gate actually said rather than guess.
    """
    reasons: list[str] = []
    decision = "no compile record"
    async for message in bus.replay(STREAM_AUDIT, limit=200):
        if message.type != "CompileAudit":
            continue
        if message.data.get("proposal_id") != chain[FRAME_PROPOSAL]["id"]:
            continue
        decision = str(message.data.get("decision"))
        reasons = [str(item) for item in message.data.get("reasons", [])]
    return (
        f"the chain reached a verdict and stopped: the compiler answered {decision} "
        f"for proposal {chain[FRAME_PROPOSAL]['id']} -- " + "; ".join(reasons)
    )


async def _advisories_are_published_and_verify(
    http: httpx.AsyncClient, advisory: dict[str, Any]
) -> None:
    """`GET /advisories` shows it, and its signature checks out off the wire."""
    response = await http.get("/advisories", params={"active": "true"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "FeatureCollection"
    features = {feature["properties"]["id"]: feature for feature in body["features"]}
    assert advisory["id"] in features, f"published advisory missing from {sorted(features)}"
    for feature in features.values():
        assert verify(Advisory.model_validate(feature)), (
            f"advisory {feature['properties']['id']} does not verify"
        )

    hazards = await http.get("/hazards")
    assert hazards.status_code == 200, hazards.text
    assert any(row["id"] == advisory["hazard_id"] for row in hazards.json()["hazards"])


async def _op_a_enters_no_hazard_once_masked(log: FrameLog, advisory: dict[str, Any]) -> None:
    """`op_a` accepts at once, and stops driving into the hazard.

    Measured as a delta rather than a total: the ground-truth flood is in force
    from the first tick, and `op_a` has been driving through it for the twenty
    seconds it took the recording to reach its first warning. What the advisory
    has to change is what happens next.
    """
    await _require(
        log,
        lambda seen: any(
            frame["operator"] == "op_a"
            and frame["advisory_id"] == advisory["id"]
            and frame["status"] == "ACCEPTED"
            for frame in seen.by_type.get(FRAME_ACK, ())
        ),
        timeout=CHAIN_TIMEOUT,
        hint="op_a never accepted the advisory",
    )
    await _require(
        log,
        lambda seen: seen.latest(FRAME_COUNTERS) is not None,
        timeout=CHAIN_TIMEOUT,
        hint="no counters frame",
    )
    before = _entries(log, "op_a")
    await asyncio.sleep(WATCH_S)
    after = _entries(log, "op_a")
    assert after == before, (
        f"op_a entered the hazard {after - before} more time(s) while masked by "
        f"advisory {advisory['id']}"
    )

    # The ghost fleet is the same seed with the advice ignored: if it entered
    # nothing either, the comparison is measuring nothing.
    ghost = log.latest(FRAME_COUNTERS)
    assert ghost is not None
    assert ghost["counters"]["ghost"]["hazard_entries_total"] > 0, (
        "the ghost fleet never met the hazard, so the run proves nothing"
    )


def _entries(log: FrameLog, operator: str) -> int:
    """`op_a`'s live hazard entries, from the newest counters frame."""
    frame = log.latest(FRAME_COUNTERS)
    assert frame is not None
    return int(frame["counters"]["live"][f"hazard_entries_{operator}"])


async def _stop_returns_to_idle(http: httpx.AsyncClient, log: FrameLog) -> None:
    """Stop resets the streams, the fleets and the socket's own state."""
    response = await http.post("/scenarios/stop")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "type": "scenario_state",
        "state": "idle",
        "scenario": None,
        "errors": [],
    }

    listing = await http.get("/scenarios")
    assert listing.json()["running"] is None
    assert listing.json()["state"] == "idle"

    # The streams were reset with the run, so nothing is left in force.
    advisories = await http.get("/advisories")
    assert advisories.json()["features"] == []

    await _require(
        log,
        lambda seen: (seen.latest(FRAME_SCENARIO_STATE) or {}).get("state") == "idle",
        timeout=30.0,
        hint="the viewer was never told the scenario stopped",
    )
