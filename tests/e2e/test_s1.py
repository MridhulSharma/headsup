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

**The second key, and who holds it here.** S1's advisory is a severity 4, so
`playbooks/flood.yaml` two_key_severity means it cannot publish on the agents'
word alone, and its 7.04 km2 footprint is over the 4 km2 two-key area as well.
This test is the person who approves it: `SecondKey` watches `GET /approvals`
for what the gate is waiting on and issues a token through
`POST /approvals/{proposal_id}`, the same request the viewer's approve button
makes. The rule is not disabled or lowered anywhere -- the one path that
exercises it end to end is this one, and a test that bypassed it would leave
it untested. The advisory that publishes is asserted to be one this test
approved.

**Two warnings and two clocks.** The first flash flood warning lands 20 s into
a 60x replay, but no gauge is above its action stage yet, so fusion narrows
nothing and the compiler refuses 1440 km2 against the 12 km2 cap -- correctly.
Blanco Rd crosses at 21:15Z, which is 145 s further into the replay, and from
there the hazard is the gauge's reach rather than the county. That is why
`ADVISORY_TIMEOUT` is minutes rather than seconds: it is a feed-time wait, not
latency.

**Nothing may block the loop.** One replay, two fleets and this socket share
an event loop, and a replay that stops being serviced fires every overdue
event at once. `LoopLag` samples a short sleep for the whole run, charges each
gap against the loop thread's own CPU to tell blocking apart from a busy
machine, and `MAX_BLOCK_MS` is the budget. Two things used to break it:
resolving a county-sized polygon to road edges, about 12 s per hazard across
the proposer and the compiler, and the scenario start reading the city graph
out of graphml with scipy loading behind it, about 10 s more. All of it runs
in a worker thread now.

The server runs on this test's own event loop rather than in a thread, on a
port the OS picks, so the socket under test is the socket the viewer uses and
two runs of the suite cannot collide on 8765.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
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

#: Seconds to wait for an advisory once a verdict has been reached. Almost all
#: of it is feed time: the first warning is uncorroborated and is refused on
#: its area, and the Blanco Rd gauge does not cross its action stage until
#: 21:15Z, which is 145 s further into a 60x replay. The compiler decides in
#: milliseconds and the approval round-trip is one poll.
#:
#: Generous on purpose, because this is a deadline and not a measurement. A
#: replay paces itself against wall-clock time, so on a machine busy enough to
#: deschedule this process it falls behind, and a tight deadline here fails the
#: whole run for being on a loaded box rather than for anything Heads Up did.
#: What must not be loosened is `MAX_BLOCK_MS` and the gate assertions, and
#: those have not been: 165 s is the nominal wait, so this allows about 3x.
ADVISORY_TIMEOUT = 480.0

#: Wall seconds the loop may be blocked by any one tick. A replay paces itself
#: against wall-clock time, so a loop that stops being serviced for longer
#: than this is a run whose timing means nothing.
MAX_BLOCK_MS = 500.0

#: How often `LoopLag` samples. Short enough to catch a block, long enough not
#: to be the load it is measuring.
LAG_INTERVAL_S = 0.05

#: Who this test approves as, and how often it looks for something to approve.
OPERATOR = "e2e_second_key"
APPROVAL_POLL_S = 0.5

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


class LoopLag:
    """How long any one tick held the event loop, in milliseconds.

    A task that sleeps `interval_s` and measures how late it was woken. The
    overshoot alone cannot answer the question, because it is just as large
    when the OS has not scheduled this process at all - and on a loaded
    machine that happens for a few hundred milliseconds at a time. Watching
    the real server with a stack-dumping watchdog, every such gap had the loop
    sitting in the IOCP selector with nothing to run.

    So each gap is also charged against `time.thread_time()`, the CPU this
    thread actually burned. Synchronous work on the loop - a graphml parse, an
    H3 cover of a county - burns the loop thread's own CPU and is `blocking`.
    A gap the loop spent descheduled or waiting on a socket burns none of it
    and is only `starved`. `blocking` is what a replay's pacing turns on and
    what `MAX_BLOCK_MS` is the budget for; `starved` is reported so a run on a
    busy machine is visibly a run on a busy machine.
    """

    #: How much of a gap has to be this thread's own CPU before the gap counts
    #: as the loop being blocked rather than descheduled.
    BLOCKING_SHARE = 0.5

    def __init__(self, interval_s: float = LAG_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self.samples = 0
        self.worst_blocking_s = 0.0
        self.worst_gap_s = 0.0
        self.total_gap_s = 0.0

    @property
    def worst_ms(self) -> float:
        """The worst gap this thread burned its own CPU through."""
        return self.worst_blocking_s * 1000.0

    @property
    def worst_gap_ms(self) -> float:
        """The worst gap of any kind, blocking or descheduled."""
        return self.worst_gap_s * 1000.0

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before, cpu_before = loop.time(), time.thread_time()
            await asyncio.sleep(self.interval_s)
            gap = max(0.0, loop.time() - before - self.interval_s)
            burned = max(0.0, time.thread_time() - cpu_before)
            self.samples += 1
            self.total_gap_s += gap
            self.worst_gap_s = max(self.worst_gap_s, gap)
            if gap > 0.0 and burned >= gap * self.BLOCKING_SHARE:
                self.worst_blocking_s = max(self.worst_blocking_s, gap)

    def summary(self) -> str:
        mean = (self.total_gap_s / self.samples * 1000.0) if self.samples else 0.0
        return (
            f"{self.samples} ticks, worst blocking {self.worst_ms:.0f} ms, "
            f"worst gap {self.worst_gap_ms:.0f} ms (starved or blocked), mean {mean:.1f} ms"
        )


class SecondKey:
    """Whoever holds the second key, for as long as this test is running.

    `GET /approvals` says which proposals the gate refused with
    `two_key_approved` as their only failed rule; each one gets a token
    through `POST /approvals/{proposal_id}`. Granted once per proposal, and
    `granted` is what the advisory is checked against afterwards.

    Errors are swallowed and retried on the next poll. This runs alongside a
    scenario that is starting and stopping, and a lost race here must not be
    the reason the test fails.
    """

    def __init__(self, http: httpx.AsyncClient, *, poll_s: float = APPROVAL_POLL_S) -> None:
        self.http = http
        self.poll_s = poll_s
        #: proposal id -> the token fingerprint the server answered with.
        self.granted: dict[str, str] = {}
        self.refused: list[str] = []
        #: Anything that went wrong in a poll. Reported, not swallowed: a
        #: silent hole here looks exactly like a two-key rule that was never
        #: needed, which is the one thing this class exists to disprove.
        self.errors: list[str] = []

    async def run(self) -> None:
        while True:
            try:
                await self._one_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A lost race against a scenario starting or stopping is not a
                # failure; the next poll picks the proposal up again.
                self.errors.append(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self.poll_s)

    async def _one_pass(self) -> None:
        listing = await self.http.get("/approvals")
        if listing.status_code != 200:
            return
        for row in listing.json().get("awaiting_approval", []):
            proposal_id = str(row.get("proposal_id") or "")
            if not proposal_id or proposal_id in self.granted:
                continue
            response = await self.http.post(
                f"/approvals/{proposal_id}",
                json={
                    "approved_by": OPERATOR,
                    "minutes": 10,
                    "note": f"e2e approved {row.get('action')} over {row.get('area_km2')} km2",
                },
            )
            if response.status_code != 201:
                self.refused.append(f"{proposal_id}: {response.status_code} {response.text[:200]}")
                continue
            # Recorded before anything else is read off the body: a token
            # issued twice for one proposal is a second compile nobody asked
            # for, so the grant is remembered the moment the server took it.
            body = response.json()
            self.granted[proposal_id] = str(body.get("fingerprint") or "granted")


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

            # Both of these start once the scenario is up, not before. `start`
            # loads the city world, the POI index and the drive network
            # synchronously and deliberately, and that is a startup cost
            # rather than a tick of the run `MAX_BLOCK_MS` is about.
            lag = LoopLag()
            key = SecondKey(http)
            watchers = [
                asyncio.create_task(lag.run(), name="e2e-loop-lag"),
                asyncio.create_task(key.run(), name="e2e-second-key"),
            ]
            try:
                chain = await _the_chain_reaches_a_verdict(log)

                advisory = await _wait_for_an_advisory(log)
                if advisory is None:
                    # The gate's own reasons, read before stopping: stopping
                    # the scenario resets the streams, the audit stream with
                    # them.
                    pytest.fail(await _why_nothing_was_published(bus, chain, key))

                await _the_second_key_is_what_let_it_publish(bus, key, advisory)
                await _the_advisory_closes_the_chain(log, chain, advisory)
                await _advisories_are_published_and_verify(http, advisory)
                await _the_approval_is_on_the_record(http, advisory)
                await _op_a_enters_no_hazard_once_masked(log, advisory)
                _no_tick_blocked_the_loop(lag)
                await _stop_returns_to_idle(http, log)
            finally:
                for task in watchers:
                    task.cancel()
                for task in watchers:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
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


async def _the_advisory_closes_the_chain(
    log: FrameLog, chain: dict[str, dict[str, Any]], advisory: dict[str, Any]
) -> None:
    """The last two links: the advisory to its proposal, and the ack to it.

    Against the proposal the advisory names rather than the first one the
    socket delivered. S1 reaches several: the office's uncorroborated warning
    is proposed on and refused on its area, and the advisory comes from a
    later proposal built on the narrowed hazard. The chain is still one chain
    -- the hazard id is the group's and does not change as readings land under
    it -- so what has to hold is that the advisory's proposal is one the
    viewer was shown, and that it was about this hazard.
    """
    proposals = {frame["id"]: frame for frame in log.by_type.get(FRAME_PROPOSAL, ())}
    assert advisory["proposal_id"] in proposals, (
        f"advisory {advisory['id']} names proposal {advisory['proposal_id']}, which the "
        f"viewer never saw; proposals delivered: {sorted(proposals)}"
    )
    assert proposals[advisory["proposal_id"]]["hazard_id"] == advisory["hazard_id"]
    assert advisory["hazard_id"] == chain[FRAME_HAZARD]["id"]
    assert advisory["signature_valid"] is True

    # Waited for, not assumed: the fleets read `advisories` once a tick, and
    # an advisory that has only just been signed has not been answered yet.
    await _require(
        log,
        lambda seen: any(
            frame["advisory_id"] == advisory["id"] for frame in seen.by_type.get(FRAME_ACK, ())
        ),
        timeout=CHAIN_TIMEOUT,
        hint=f"no fleet answered advisory {advisory['id']}",
    )
    ack = next(frame for frame in log.by_type[FRAME_ACK] if frame["advisory_id"] == advisory["id"])
    assert ack["proposal_id"] == advisory["proposal_id"]


async def _compile_records(bus: Bus) -> str:
    """Every compile record on the audit stream, one line each.

    The audit record is the point of having an audit stream: a rejection
    leaves as much evidence as a publication, so any failure around the gate
    should report what the gate actually said rather than guess. Read before
    stopping the scenario, which resets the streams.
    """
    lines: list[str] = []
    async for message in bus.replay(STREAM_AUDIT, limit=500):
        if message.type != "CompileAudit":
            continue
        reasons = "; ".join(str(item) for item in message.data.get("reasons", ())) or "no reasons"
        record = message.data.get("hazard_narrowing") or {}
        narrowing = (
            f"{record.get('original_area_km2')}->{record.get('area_km2')} km2"
            if record.get("narrowed")
            else f"unnarrowed ({record.get('reason', 'no record')})"
        )
        lines.append(
            f"{message.data.get('decision')} {message.data.get('proposal_id')} "
            f"sev={message.data.get('severity')} {message.data.get('action')} "
            f"{message.data.get('area_km2')} km2 of {message.data.get('area_cap_km2')} "
            f"two_key={message.data.get('two_key_required')}"
            f"({message.data.get('two_key_reason')}) "
            f"narrowing={narrowing}: {reasons}"
        )
    return " | ".join(lines) or "no compile records"


async def _why_nothing_was_published(
    bus: Bus, chain: dict[str, dict[str, Any]], key: SecondKey
) -> str:
    """The compiler's own reasons, read out of the audit stream.

    The audit record is the point of having an audit stream: a rejection is
    supposed to leave as much evidence as a publication, and a failure here
    should say what the gate actually said rather than guess. Every compile
    record is reported, not just the first proposal's, because S1 reaches
    several: the uncorroborated warning is refused on its area by design and
    the narrowed one is the one that was supposed to publish.
    """
    return (
        f"no advisory was published. Verdict reached for {chain[FRAME_PROPOSAL]['id']}. "
        f"Approvals granted: {sorted(key.granted) or 'none'}"
        + (f", refused: {key.refused}" if key.refused else "")
        + ". Compile records: "
        + await _compile_records(bus)
    )


async def _the_second_key_is_what_let_it_publish(
    bus: Bus, key: SecondKey, advisory: dict[str, Any]
) -> None:
    """The advisory that published is one this test approved by hand.

    Not a formality. S1's narrowed hazard is severity 4 over 7.04 km2, which
    is above `two_key_severity` and above `two_key_area_km2`, so an advisory
    that appeared without a token in `granted` would mean the two-key rule had
    been weakened somewhere rather than satisfied.
    """
    assert not key.refused, f"the server refused a token this test issued: {key.refused}"
    assert not key.errors, f"the second-key holder could not do its job: {key.errors}"
    if advisory["proposal_id"] not in key.granted:
        pytest.fail(
            f"advisory {advisory['id']} published from proposal {advisory['proposal_id']}, "
            f"which this test never approved. Approved: {sorted(key.granted) or 'nothing'}. "
            f"Advisory frame: {advisory}. Compile records: {await _compile_records(bus)}"
        )


async def _the_approval_is_on_the_record(http: httpx.AsyncClient, advisory: dict[str, Any]) -> None:
    """`GET /approvals` shows the token's fingerprint, and never the token."""
    response = await http.get("/approvals")
    assert response.status_code == 200, response.text
    body = response.json()
    issued = {row["proposal_id"]: row for row in body["approvals"]}
    row = issued.get(advisory["proposal_id"])
    assert row is not None, f"no approval on record for {advisory['proposal_id']}"
    assert row["approved_by"] == OPERATOR
    assert row["fingerprint"]
    assert "token" not in row, "a token anyone could GET is not a second key"


def _no_tick_blocked_the_loop(lag: LoopLag) -> None:
    """No single tick of the run held the event loop past the budget.

    Judged on `worst_ms`, the worst gap the loop thread burned its own CPU
    through, because that is the gap Heads Up caused. `worst_gap_ms` is
    printed beside it: when that is large and `worst_ms` is not, the machine
    was busy and this run's timing should not be read too closely.
    """
    print(f"event-loop lag: {lag.summary()}, budget {MAX_BLOCK_MS:.0f} ms")
    assert lag.samples > 0, "the lag monitor never ran"
    assert lag.worst_ms <= MAX_BLOCK_MS, (
        f"the event loop was blocked for {lag.worst_ms:.0f} ms by one tick, over the "
        f"{MAX_BLOCK_MS:.0f} ms budget ({lag.summary()}); something synchronous is "
        "running on the loop thread"
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
