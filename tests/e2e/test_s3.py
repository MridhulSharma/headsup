"""S3 end to end: free dispatch text, a keep-clear corridor, and V1 beside it.

Needs the Docker services (`uv run hu infra up`) and a built Austin
(`uv run hu world build --city austin`). The model is mocked: these tests
check the wiring and the two numbers the scenario exists to produce, not the
judgment, and no unit or e2e run spends money.

S1 covers the plumbing through the real server - the launcher, the 409, the
frame chain, the signature, the second key, the loop budget - and S2 covers
the assist-queue comparison. Neither is repeated here. What is new in S3 is
the *input*: a feed whose only structured field is a dispatch type string with
no vocabulary behind it, and whose first record about the scene carries no
coordinates at all. So these tests assert the three things that follow from
that and from nothing else:

1. **the scene is found from the text and placed by the graph.** The first
   scripted record is typed `Traffic Impediment` and has no position. The
   candidate the sentinel publishes has to cite it and has to be an area
   around 6th and Guadalupe, which only the intersection lookup can supply.
2. **the advisory is a corridor, not the scene.** A `KEEP_CLEAR` whose
   geometry is the hazard's own area would be the fallback path. The one this
   run must produce covers the route from the nearest fire or EMS station and
   carries the width and radius it was drawn with - and it has to be published
   within 60 s of the first scene record reaching the bus, which is the
   time-to-advisory budget.
3. **blocked corridor time is lower for the advised fleet.** The ghost fleet
   drives the same seed and dwells inside the corridor because nobody told it
   not to; the advised fleet is told, and its vehicles pick up elsewhere.

**V1 is recorded, not asserted.** The second test runs the same scenario with
`llm: false`, which is the rules-only path: keyword matching on
`issue_reported` and nothing else. It prints what that produced and asserts
only that the run did not error. Whether keyword matching finds this scene,
and how late, is the measurement the ablation is for - it is not something a
test should force in either direction.
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
from shapely.geometry import Point as ShapelyPoint
from shapely.geometry import shape as shapely_shape

from headsup.api.app import ApiState, create_app
from headsup.api.frames import (
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
from headsup.bus import ALL_STREAMS, STREAM_ADVISORIES, STREAM_AUDIT, Bus, events_stream
from headsup.contracts import Source
from headsup.scenario.incidents import SCENE_LAT, SCENE_LON

pytestmark = pytest.mark.e2e

SCENARIO = "s3_incident"
CITY = "austin"
SPEED = 60.0
REDIS_URL = "redis://localhost:6379/0"
INFRA_HINT = "run `uv run hu infra up` first"

#: The fleet `hu demo` runs. The same 300 S2 uses, and for the same reason:
#: the measurement is about vehicles meeting one block of downtown, and sixty
#: vehicles over the Austin graph almost never do.
VEHICLES = 300
ZONES = 48

#: The action the scenario exists to produce.
ACTION = "KEEP_CLEAR"

#: The feed the scene is reported on, and the stream it arrives on.
EVENTS_STREAM = events_stream(Source.AUSTIN_INCIDENTS)

#: Seconds to wait for the start request. It loads the city world, the POI
#: index and the drive network before it answers.
START_TIMEOUT = 300.0

#: Seconds to wait for the pipeline. `scenarios/s3_incident.yaml` records
#: `first_scene_offset_seconds: 20`: the first scene record is published
#: twenty minutes into the window, which is 20 s at 60x. Generous past that,
#: because a replay paces itself against wall-clock time and a loaded machine
#: makes it late for reasons that are not Heads Up's.
CHAIN_TIMEOUT = 180.0

#: The time-to-advisory budget, measured from the first scene record reaching
#: the bus to the first KEEP_CLEAR reaching `advisories`. Both are Redis entry
#: ids, which are milliseconds, so this is measured rather than inferred.
BUDGET_S = 60.0

#: Wall seconds to let the two fleets' corridor time separate. A deadline, not
#: a measurement: the assertion is the comparison, and this only bounds the
#: wait for it.
CORRIDOR_TIMEOUT = 210.0

#: Wall seconds V1 is given before its result is read. The same budget the V4
#: run is held to, plus the same slack, so the two numbers are read off runs
#: of comparable length.
V1_WATCH_S = 90.0


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
    monkeypatch.setenv("HU_CITY", CITY)
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


# --- reading the bus --------------------------------------------------------


def _entry_ms(entry_id: str) -> int:
    """The millisecond a stream entry was added at, from its id."""
    return int(str(entry_id).split("-")[0])


async def _scripted_event_ids(bus: Bus) -> dict[str, int]:
    """Every scripted incident event on the bus, to the ms it arrived at.

    The recording flags the records this repository injected, and the adapter
    carries that flag into the event payload, so "the scene the scenario is
    about" is identifiable on the bus without the test knowing anything about
    report ids.
    """
    found: dict[str, int] = {}
    async for message in bus.replay(EVENTS_STREAM, limit=5000):
        payload = message.data.get("payload") or {}
        if payload.get("scripted") is True:
            found.setdefault(str(message.data.get("id")), _entry_ms(message.id))
    return found


async def _first_action_ms(bus: Bus, action: str) -> int | None:
    """When the first advisory with this action reached `advisories`."""
    async for message in bus.replay(STREAM_ADVISORIES, limit=500):
        properties = (message.data.get("properties") or {}).get("action") or {}
        if str(properties.get("type")) == action:
            return _entry_ms(message.id)
    return None


async def _audit_types(bus: Bus) -> str:
    """What is on the audit stream, by message type, for a failure message."""
    seen: dict[str, int] = defaultdict(int)
    async for message in bus.replay(STREAM_AUDIT, limit=500):
        seen[str(message.type)] += 1
    return ", ".join(f"{name}={count}" for name, count in sorted(seen.items())) or "empty"


class CorridorTime:
    """Blocked corridor minutes per fleet, off the counters frames.

    Read as the float the simulator keeps rather than the floored integer the
    counter snapshot carries: a twelve-minute run can accumulate less than a
    whole vehicle-minute, and the comparison is still the comparison.
    """

    def __init__(self) -> None:
        self.minutes: dict[str, float] = {"live": 0.0, "ghost": 0.0}
        self.samples = 0

    def read(self, log: FrameLog) -> None:
        frames = list(log.by_type.get(FRAME_COUNTERS, ()))
        for frame in frames:
            blocked = frame.get("blocked_minutes") or {}
            for mode in self.minutes:
                by_op = blocked.get(mode) or {}
                self.minutes[mode] = max(
                    self.minutes[mode], sum(float(value) for value in by_op.values())
                )
        self.samples = len(frames)

    @property
    def separated(self) -> bool:
        return self.minutes["ghost"] > 0.0 and self.minutes["live"] < self.minutes["ghost"]

    def summary(self) -> str:
        return (
            f"ghost {self.minutes['ghost']:.2f} vehicle-minutes in the corridor, "
            f"live {self.minutes['live']:.2f}, over {self.samples} counter samples"
        )


# --- V4: the whole pipeline -------------------------------------------------


async def test_s3_reads_dispatch_text_into_a_keep_clear_corridor(server: str, bus: Bus) -> None:
    """The whole scenario, once: start, corridor, two measurements, stop."""
    log = FrameLog()
    async with (
        httpx.AsyncClient(base_url=f"http://{server}", timeout=START_TIMEOUT) as http,
        websockets.connect(f"ws://{server}/viewer") as socket,
    ):
        reader = asyncio.create_task(_read_into(socket, log), name="e2e-viewer")
        try:
            await _the_launcher_badges_s3_as_scripted(http)
            await _start_s3(http, llm=True)

            chain = await _the_chain_reaches_a_verdict(log)
            await _the_candidate_cites_the_scripted_scene(log, bus)
            advisory = await _a_keep_clear_corridor_publishes(log, chain)
            await _the_corridor_was_published_inside_the_budget(bus)
            await _the_advised_fleet_spends_less_time_in_the_corridor(log, bus)
            print(f"advisory {advisory['id']} {advisory['policy_rule']}")
            await _stop_returns_to_idle(http, log)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader


async def _the_launcher_badges_s3_as_scripted(http: httpx.AsyncClient) -> None:
    """The window is real and the scene inside it is not, so the run is badged."""
    response = await http.get("/scenarios")
    assert response.status_code == 200, response.text
    card = next(row for row in response.json()["scenarios"] if row["id"] == SCENARIO)
    assert card["city"] == CITY
    assert card["hazard_class"] == "EMERGENCY_SCENE"
    assert card["scripted"] is True
    assert card["what_if"].strip()
    assert card["default_speed"] == SPEED


async def _start_s3(http: httpx.AsyncClient, *, llm: bool) -> dict[str, Any]:
    """One POST brings up the replay, the incident sentinel and both fleets."""
    response = await http.post(
        f"/scenarios/{SCENARIO}/start",
        json={"speed": SPEED, "vehicles": VEHICLES, "zones": ZONES, "seed": 42, "llm": llm},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "running"
    assert body["scenario"]["id"] == SCENARIO
    assert body["llm_mode"] == ("mock" if llm else "off")
    # The scenario's hazard class picks the sentinel.
    assert body["sentinel"] == "sentinel.incident"
    # One clock, and both fleets on it: the comparison this test rests on.
    assert body["modes"] == ["live", "ghost"]
    return body


async def _the_chain_reaches_a_verdict(log: FrameLog) -> dict[str, dict[str, Any]]:
    """Both fleets' telemetry, then the rail as far as the critic's verdict."""
    await _require(
        log,
        lambda seen: seen.first(FRAME_TELEMETRY) and seen.first(FRAME_GHOST_TELEMETRY),
        timeout=CHAIN_TIMEOUT,
        hint="no telemetry for both fleets",
    )
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
    assert candidate["class"] == "EMERGENCY_SCENE"
    assert candidate["event_ids"], "a candidate with no evidence cannot be audited"
    assert candidate["id"] in hazard["candidate_ids"]
    assert proposal["hazard_id"] == hazard["id"]
    assert verdict["proposal_id"] == proposal["id"]
    return chain  # type: ignore[return-value]


async def _the_candidate_cites_the_scripted_scene(log: FrameLog, bus: Bus) -> None:
    """The scene the run is about was read off the records this repo injected.

    Not a tautology: the same window carries five real incidents, and a run
    that published one of those and never found the scripted scene would reach
    a verdict and satisfy everything above it.
    """
    scripted = await _scripted_event_ids(bus)
    assert scripted, f"no scripted incident events on {EVENTS_STREAM}"
    cited: set[str] = set()
    for frame in log.by_type.get(FRAME_CANDIDATE, ()):
        if frame.get("class") == "EMERGENCY_SCENE":
            cited.update(str(event_id) for event_id in frame.get("event_ids") or ())
    assert cited & set(scripted), (
        "no EMERGENCY_SCENE candidate cites a scripted record; "
        f"cited {sorted(cited)}, scripted {sorted(scripted)}"
    )


async def _a_keep_clear_corridor_publishes(
    log: FrameLog, chain: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """A KEEP_CLEAR that is the route to the scene rather than the scene.

    Three things say that it is a corridor and not the fallback: the action
    carries the width and the radius it was drawn with, the area covers the
    scene, and the area is bigger than the scene disc the sentinel published -
    because it also covers the way in.
    """

    def published(seen: FrameLog) -> bool:
        return any(
            str(frame.get("action")) == ACTION for frame in seen.by_type.get(FRAME_ADVISORY, ())
        )

    if not await _until(log, published, timeout=CHAIN_TIMEOUT):
        arrived = sorted(str(frame.get("action")) for frame in log.by_type.get(FRAME_ADVISORY, ()))
        pytest.fail(f"no {ACTION} advisory; actions published: {arrived or 'none'}")

    advisory = next(
        frame for frame in log.by_type[FRAME_ADVISORY] if str(frame.get("action")) == ACTION
    )
    assert advisory["signature_valid"] is True
    assert advisory["hazard_id"] == chain[FRAME_HAZARD]["id"]

    feature = advisory["feature"]
    params = feature["properties"]["action"]["params"]
    assert params.get("width_m") and params.get("radius_m"), (
        f"a {ACTION} without a width and a radius is the hazard's own area, "
        f"not a corridor: {params}"
    )

    area = shapely_shape(feature["geometry"])
    assert area.geom_type in {"Polygon", "MultiPolygon"}
    assert area.contains(ShapelyPoint(SCENE_LON, SCENE_LAT)), (
        "the corridor does not cover the scene it was drawn for"
    )
    scene = shapely_shape(feature["properties"]["hazard"]["geometry"])
    assert area.area > scene.area, (
        "the corridor is no larger than the scene, so no route to it was drawn"
    )
    return advisory


async def _the_corridor_was_published_inside_the_budget(bus: Bus) -> None:
    """From the first scene record on the bus to the corridor: 60 s or less.

    Measured off Redis entry ids, which are milliseconds, because there is no
    record frame on the viewer socket and the wall clock of the test process is
    not the clock either end of this was stamped with.
    """
    scripted = await _scripted_event_ids(bus)
    first_record_ms = min(scripted.values())
    published_ms = await _first_action_ms(bus, ACTION)
    assert published_ms is not None, f"no {ACTION} on the advisories stream"
    elapsed = (published_ms - first_record_ms) / 1000.0
    print(f"time to advisory: {elapsed:.1f} s (budget {BUDGET_S:g} s)")
    assert 0.0 <= elapsed <= BUDGET_S, (
        f"the {ACTION} corridor was published {elapsed:.1f} s after the first scene "
        f"record, outside the {BUDGET_S:g} s budget; audit: {await _audit_types(bus)}"
    )


async def _the_advised_fleet_spends_less_time_in_the_corridor(log: FrameLog, bus: Bus) -> None:
    """The scenario's point as a number: the ghost dwells in the corridor.

    A keep-clear corridor does not close a road, so this is not about stalling.
    It is about picking up, dropping off and idling in the one street the next
    engine is coming up: the advised fleet is told not to and goes elsewhere,
    the ghost fleet is told nothing and sits there.
    """
    corridor = CorridorTime()

    def separated(seen: FrameLog) -> bool:
        corridor.read(seen)
        return corridor.separated

    reached = await _until(log, separated, timeout=CORRIDOR_TIMEOUT)
    corridor.read(log)
    assert corridor.samples > 0, "no counters frames, so neither fleet was measured"
    assert corridor.minutes["ghost"] > 0.0, (
        "the ghost fleet never dwelt in the corridor, so the comparison proves "
        f"nothing ({corridor.summary()}); audit stream: {await _audit_types(bus)}"
    )
    if not reached:
        pytest.fail(
            f"the advised fleet did not spend less time in the corridor than the ghost "
            f"({corridor.summary()})"
        )
    print(f"corridor: {corridor.summary()}")


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
    await _require(
        log,
        lambda seen: (seen.latest(FRAME_SCENARIO_STATE) or {}).get("state") == "idle",
        timeout=30.0,
        hint="the viewer was never told the scenario stopped",
    )


# --- V1: the rules-only path, recorded --------------------------------------


async def test_s3_records_what_the_rules_only_path_makes_of_the_same_feed(
    server: str, bus: Bus
) -> None:
    """V1 over the same recording: keyword matching on the type field alone.

    Nothing here asserts that V1 succeeds or that it fails. It runs the same
    scenario with no model in the loop, waits out the same budget the V4 run
    is held to, and prints what the rules path produced. The only assertion is
    that the run itself did not error, because a crashed run would make the
    recorded number meaningless rather than informative.
    """
    log = FrameLog()
    async with (
        httpx.AsyncClient(base_url=f"http://{server}", timeout=START_TIMEOUT) as http,
        websockets.connect(f"ws://{server}/viewer") as socket,
    ):
        reader = asyncio.create_task(_read_into(socket, log), name="e2e-viewer-v1")
        try:
            await _start_s3(http, llm=False)
            await _require(
                log,
                lambda seen: seen.first(FRAME_TELEMETRY) and seen.first(FRAME_GHOST_TELEMETRY),
                timeout=CHAIN_TIMEOUT,
                hint="no telemetry for both fleets",
            )
            await asyncio.sleep(V1_WATCH_S)

            report = await _v1_report(log, bus)
            print(f"V1 (rules only): {report}")

            # The runner announces on the socket whenever a worker falls over,
            # so any `errors` on any scenario_state frame is a failed run - and
            # a failed run would make the number above meaningless rather than
            # informative.
            errors = [
                problem
                for frame in log.by_type.get(FRAME_SCENARIO_STATE, ())
                for problem in (frame.get("errors") or [])
            ]
            assert errors == [], f"the rules-only run errored: {errors}"
            listing = (await http.get("/scenarios")).json()
            assert listing["running"] == SCENARIO
            assert listing["state"] == "running"
        finally:
            with contextlib.suppress(Exception):
                await http.post("/scenarios/stop")
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader


async def _v1_report(log: FrameLog, bus: Bus) -> str:
    """What the rules-only run produced, as one line for the test output."""
    candidates: dict[str, int] = defaultdict(int)
    for frame in log.by_type.get(FRAME_CANDIDATE, ()):
        candidates[str(frame.get("class"))] += 1
    actions: dict[str, int] = defaultdict(int)
    for frame in log.by_type.get(FRAME_ADVISORY, ()):
        actions[str(frame.get("action"))] += 1

    scripted = await _scripted_event_ids(bus)
    published_ms = await _first_action_ms(bus, ACTION)
    if published_ms is not None and scripted:
        elapsed = f"{(published_ms - min(scripted.values())) / 1000.0:.1f} s"
    else:
        elapsed = "never"

    corridor = CorridorTime()
    corridor.read(log)
    return (
        f"after {V1_WATCH_S:g} s of watching: candidates {dict(candidates) or 'none'}; "
        f"advisories {dict(actions) or 'none'}; first {ACTION} after the first scene "
        f"record: {elapsed}; {corridor.summary()}"
    )
