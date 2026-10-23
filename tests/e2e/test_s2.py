"""S2 end to end: a block of dark signals, and what the advice is worth.

Needs the Docker services (`uv run hu infra up`) and a built Austin
(`uv run hu world build --city austin`). The model is mocked: this test checks
the wiring and the two numbers the scenario exists to produce, not the
judgment, and no unit or e2e run spends money.

S1 already covers the pipeline's plumbing through the real server -- the
launcher, the 409, the frame chain, the signature, the second key, the loop
budget. This test does not repeat any of that. What is new in S2 is the
*comparison*, and it asserts the two measurements that make it:

1. the **assist queue**. A dark intersection does not close a road; it takes
   away the right of way, and two vehicles arriving together stop and ask a
   human what to do. The ghost fleet meets every one of the nine dark
   junctions that way for the whole run. The advised fleet is told to treat
   them as all-way stops and drains its queue. The ghost's peak has to be at
   least `QUEUE_RATIO` times the live fleet's.
2. **`op_a` enters no hazard after the advisory it accepted.** A delta, not a
   total: the signals are dark from the first tick and `op_a` drives into them
   for the fifteen seconds it takes the recording to reach a publishable
   cluster. What the advisory has to change is what happens next.

**Three actions, one hazard.** A dark junction needs the stop rule, a cap on
how many vehicles may be in it at once, and a cap on the approach speed, and
`playbooks/signal_outage.yaml` allows all three. So the proposer asks for all
three (`Proposer.propose_all`) and the compiler gates each separately. The
test asserts all three published, because a run that produced only the stop
rule would still pass the two measurements above and would not be S2.

**No second key here, and that is a property of the scenario rather than a
shortcut.** The sentinel publishes the cluster at severity 3 and fusion caps
it at 2, because the Austin signal feed is not an `AUTHORITATIVE_SOURCES` feed
and is the cluster's only source. Either way it is under the severity 4 that
`playbooks/signal_outage.yaml` asks a token for, and its 0.2 km2 is well under
the 3 km2. S1 is the scenario that exercises the two-key path, and it does.

**Why the numbers are reachable at all.** The ground truth is the padded hull
of the nine signals the recording darkens, written in
`scenarios/s2_blackout.yaml` by the same generator that wrote the recording.
So the area the sentinel publishes and the area the simulator darkens are the
same shape, and an `ALL_WAY_STOP` resolved against the signalised junctions in
it covers every junction that went dark. A hazard area invented separately
would darken intersections no advisory built from this feed could reach, and
assertion 2 would be unachievable for reasons that say nothing about Heads Up.
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

pytestmark = pytest.mark.e2e

SCENARIO = "s2_blackout"
CITY = "austin"
SPEED = 60.0
REDIS_URL = "redis://localhost:6379/0"
INFRA_HINT = "run `uv run hu infra up` first"

#: The fleet `hu demo` runs. Not reducible the way S1's is: a flood blocks an
#: edge, so one vehicle driving onto it is enough to stall, but a dark signal
#: only deadlocks when two vehicles actually *meet* at it
#: (`engine.DARK_CONTENTION`). Sixty vehicles over the Austin graph almost
#: never meet, and a run of this test at that size measured both queues at
#: zero -- a true measurement of nothing. Three hundred fills the downtown
#: block grid, which is what the scenario is about.
VEHICLES = 300
ZONES = 48

#: The action the advised fleet is judged on, and the two that go with it.
#: Every one is in `playbooks/signal_outage.yaml` allowed_actions.
PRIMARY_ACTION = "ALL_WAY_STOP"
COMPANION_ACTIONS = ("DENSITY_CAP", "SPEED_CAP")

#: Seconds to wait for the start request. It loads the city world, the POI
#: index and the drive network before it answers.
START_TIMEOUT = 300.0

#: Seconds to wait for the pipeline. `scenarios/s2_blackout.yaml` records
#: `first_candidate_offset_seconds: 15`: the cluster reaches the sentinel's
#: four-signal threshold fifteen minutes into the window, which is 15 s at
#: 60x. Generous past that, because a replay paces itself against wall-clock
#: time and a loaded machine makes it late for reasons that are not Heads Up's.
CHAIN_TIMEOUT = 180.0

#: Seconds to wait for all three advisories once the chain has a verdict. The
#: compiler decides in milliseconds; this is slack for the machine.
ADVISORY_TIMEOUT = 180.0

#: Wall seconds to watch `op_a` after it has accepted. At 60x that is fifteen
#: simulated minutes of driving under the mask.
WATCH_S = 15.0

#: How much worse the baseline has to be. The ghost fleet meets every dark
#: junction with no idea what to do at it; the advised fleet meets the same
#: junctions with a stop rule.
QUEUE_RATIO = 3.0

#: Wall seconds to let the two queues separate. A deadline, not a measurement:
#: the live fleet carries whatever it accumulated in the twenty seconds before
#: the advisory existed, so the ghost has to run on past that for the ratio to
#: appear. The assertion is the ratio; this only bounds the wait for it.
QUEUE_TIMEOUT = 150.0


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


#: The operators, for the per-operator read a failure reports.
OPERATORS = ("op_a", "op_b", "op_c")


class QueuePeaks:
    """The deepest the assist queue ever got, per fleet.

    Sampled off the counters frames rather than computed at the end, because
    `assist_open` is a gauge: it falls as vehicles are cleared, so the figure
    the demo is about is the peak and not the last reading. Both fleets are
    read from the same frames, so the two peaks are measured on one clock even
    though they are not reached at the same moment.

    The per-operator peaks are kept for the failure message only. They are
    where a disappointing ratio shows its cause: `op_c` declines any hazard
    under its confidence floor, and a declining operator's vehicles stay
    stalled exactly as the ghost's do, which puts a floor of about 3x on what
    the live fleet can ever achieve.
    """

    def __init__(self) -> None:
        self.peak: dict[str, int] = {"live": 0, "ghost": 0}
        self.by_operator: dict[str, int] = dict.fromkeys(OPERATORS, 0)
        self.latest: dict[str, int] = {"live": 0, "ghost": 0}
        self.samples = 0

    def read(self, log: FrameLog) -> None:
        frames = list(log.by_type.get(FRAME_COUNTERS, ()))
        for frame in frames:
            counters = frame.get("counters") or {}
            for mode in self.peak:
                depth = int((counters.get(mode) or {}).get("assist_open_total", 0))
                self.peak[mode] = max(self.peak[mode], depth)
            live = counters.get("live") or {}
            for op in self.by_operator:
                self.by_operator[op] = max(
                    self.by_operator[op], int(live.get(f"assist_open_{op}", 0))
                )
        if frames:
            counters = frames[-1].get("counters") or {}
            for mode in self.latest:
                self.latest[mode] = int((counters.get(mode) or {}).get("assist_open_total", 0))
        self.samples = len(frames)

    @property
    def ratio(self) -> float:
        live = self.peak["live"]
        return float("inf") if live == 0 else self.peak["ghost"] / live

    @property
    def met(self) -> bool:
        return self.peak["ghost"] > 0 and self.ratio >= QUEUE_RATIO

    def summary(self) -> str:
        per_op = ", ".join(f"{op} {self.by_operator[op]}" for op in OPERATORS)
        return (
            f"ghost peak {self.peak['ghost']}, live peak {self.peak['live']} "
            f"({self.ratio:.1f}x) over {self.samples} counter samples; "
            f"live now {self.latest['live']}, ghost now {self.latest['ghost']}; "
            f"live peaks per operator: {per_op}"
        )


async def test_s2_turns_a_block_of_dark_signals_into_an_advisory_worth_having(
    server: str, bus: Bus
) -> None:
    """The whole scenario, once: start, three advisories, two measurements, stop."""
    log = FrameLog()
    async with (
        httpx.AsyncClient(base_url=f"http://{server}", timeout=START_TIMEOUT) as http,
        websockets.connect(f"ws://{server}/viewer") as socket,
    ):
        reader = asyncio.create_task(_read_into(socket, log), name="e2e-viewer")
        try:
            await _the_launcher_badges_s2_as_scripted(http)
            await _start_s2(http)

            chain = await _the_chain_reaches_a_verdict(log)
            advisories = await _all_three_actions_publish(log, chain)
            await _op_a_enters_no_hazard_once_masked(log, advisories[PRIMARY_ACTION])
            await _the_ghost_queue_is_the_worse_one(log, bus)
            await _stop_returns_to_idle(http, log)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader


async def _the_launcher_badges_s2_as_scripted(http: httpx.AsyncClient) -> None:
    """S2's feed is generated, so the viewer has to say so."""
    response = await http.get("/scenarios")
    assert response.status_code == 200, response.text
    body = response.json()
    card = next(row for row in body["scenarios"] if row["id"] == SCENARIO)
    assert card["city"] == CITY
    assert card["hazard_class"] == "SIGNAL_OUTAGE"
    # The signal map is the city's own; which signals are dark is not. A run
    # that looked more real than it is would be the one bad outcome here.
    assert card["scripted"] is True
    assert card["what_if"].strip()
    assert card["default_speed"] == SPEED


async def _start_s2(http: httpx.AsyncClient) -> None:
    """One POST brings up the replay, the signal sentinel and both fleets."""
    response = await http.post(
        f"/scenarios/{SCENARIO}/start",
        json={"speed": SPEED, "vehicles": VEHICLES, "zones": ZONES, "seed": 42},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "running"
    assert body["scenario"]["id"] == SCENARIO
    assert body["llm_mode"] == "mock"
    # The scenario's hazard class picks the sentinel, and S2's reads the
    # feed's own operation_state rather than asking a model anything.
    assert body["sentinel"] == "sentinel.signal_outage"
    # One clock, and both fleets on it: the comparison this test rests on.
    assert body["modes"] == ["live", "ghost"]


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
    assert candidate["event_ids"], "a candidate with no evidence cannot be audited"
    assert candidate["class"] == "SIGNAL_OUTAGE"
    # Four flashing signals is the sentinel's threshold; the recording's first
    # three are deliberately under it and must not have published anything.
    assert len(candidate["event_ids"]) >= 4, (
        f"a SIGNAL_OUTAGE cluster needs four signals, this one cites {len(candidate['event_ids'])}"
    )
    assert candidate["id"] in hazard["candidate_ids"]
    assert proposal["hazard_id"] == hazard["id"]
    assert verdict["proposal_id"] == proposal["id"]
    assert verdict["hazard_id"] == hazard["id"]
    return chain  # type: ignore[return-value]


async def _all_three_actions_publish(
    log: FrameLog, chain: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """The stop rule, the queue cap and the approach cap, all for one hazard.

    One hazard, three proposals, three advisories: `Proposer.propose_all` asks
    for everything `playbooks/signal_outage.yaml` allows, and the compiler
    gates each one on its own. Keyed by action here so a failure says which
    one is missing rather than how many arrived.
    """
    wanted = (PRIMARY_ACTION, *COMPANION_ACTIONS)

    def published(seen: FrameLog) -> bool:
        actions = {str(frame.get("action")) for frame in seen.by_type.get(FRAME_ADVISORY, ())}
        return all(action in actions for action in wanted)

    if not await _until(log, published, timeout=ADVISORY_TIMEOUT):
        arrived = sorted(str(frame.get("action")) for frame in log.by_type.get(FRAME_ADVISORY, ()))
        pytest.fail(
            f"expected advisories for {', '.join(wanted)}; got {arrived or 'none'}. "
            f"Compile records: {await _compile_records(log)}"
        )

    by_action: dict[str, dict[str, Any]] = {}
    for frame in log.by_type[FRAME_ADVISORY]:
        by_action.setdefault(str(frame.get("action")), frame)

    hazard_id = chain[FRAME_HAZARD]["id"]
    proposals = {frame["id"] for frame in log.by_type.get(FRAME_PROPOSAL, ())}
    for action in wanted:
        advisory = by_action[action]
        assert advisory["signature_valid"] is True, f"{action} advisory does not verify"
        assert advisory["hazard_id"] == hazard_id, (
            f"{action} advisory is about {advisory['hazard_id']}, not the hazard the "
            f"chain reached ({hazard_id})"
        )
        assert advisory["proposal_id"] in proposals, (
            f"{action} advisory names proposal {advisory['proposal_id']}, which the "
            f"viewer never saw; proposals delivered: {sorted(proposals)}"
        )
    # Three separate asks, not one advisory counted three times.
    assert len({by_action[action]["id"] for action in wanted}) == len(wanted)
    return by_action


async def _compile_records(log: FrameLog) -> str:
    """What the gate said about each proposal, for a failure message.

    Read off the viewer's own verdict and advisory frames rather than the
    audit stream, because this runs before the scenario is stopped and the
    streams are still the run's -- but the frames are already in hand.
    """
    verdicts = [str(frame.get("proposal_id")) for frame in log.by_type.get(FRAME_VERDICT, ())]
    advisories = [
        f"{frame.get('action')} from {frame.get('proposal_id')}"
        for frame in log.by_type.get(FRAME_ADVISORY, ())
    ]
    return (
        f"verdicts for {verdicts or 'nothing'}; advisories {advisories or 'none'}; "
        f"frames {log.summary()}"
    )


async def _op_a_enters_no_hazard_once_masked(log: FrameLog, advisory: dict[str, Any]) -> None:
    """`op_a` accepts the stop rule, and stops meeting dark signals.

    A delta rather than a total: the signals are dark from the first tick and
    `op_a` has been driving into them for the fifteen seconds it took the
    recording to reach a publishable cluster. What the advisory has to change
    is what happens next.
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
        hint=f"op_a never accepted the {PRIMARY_ACTION} advisory",
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
        f"op_a met a dark signal {after - before} more time(s) while masked by "
        f"advisory {advisory['id']}; every junction the outage darkened should be in "
        "its all-way-stop set"
    )


async def _the_ghost_queue_is_the_worse_one(log: FrameLog, bus: Bus) -> None:
    """The ghost fleet's assist queue peaks at least `QUEUE_RATIO` times deeper.

    This is the scenario's point as a number. The ghost fleet drives the same
    seed into the same dark junctions with no idea what to do at them, and
    every pair that arrives together stops and asks a human. The advised fleet
    is told to treat them as all-way stops, so its queue drains instead.

    Polled to a deadline rather than read once after a fixed sleep. The live
    fleet's peak is mostly what it accumulated in the twenty seconds before
    any advisory existed -- the signals are dark from the first tick -- so the
    separation takes a while to appear and there is no honest fixed duration
    to wait. `QUEUE_TIMEOUT` bounds the wait; the ratio is the assertion.
    """
    peaks = QueuePeaks()

    def separated(seen: FrameLog) -> bool:
        peaks.read(seen)
        return peaks.met

    reached = await _until(log, separated, timeout=QUEUE_TIMEOUT)
    peaks.read(log)
    assert peaks.samples > 0, "no counters frames, so neither queue was measured"
    # A baseline that met nothing would make the ratio meaningless whatever it
    # came out as.
    assert peaks.peak["ghost"] > 0, (
        "the ghost fleet never queued for assistance, so the comparison proves "
        f"nothing ({peaks.summary()}); audit stream: {await _audit_types(bus)}"
    )
    if not reached:
        pytest.fail(
            f"the ghost queue peaked at only {peaks.ratio:.1f}x the advised fleet's in "
            f"{QUEUE_TIMEOUT:g} s, under the {QUEUE_RATIO:g}x this scenario is supposed "
            f"to show ({peaks.summary()}); acks: {_ack_summary(log)}"
        )
    # The live fleet's queue has to have *drained*, not merely grown slower:
    # clearing the vehicles already stuck at the dark junctions is what the
    # all-way stop buys, and a live queue sitting at its own peak would mean
    # the advisory only helped vehicles that had not arrived yet.
    assert peaks.latest["live"] < peaks.peak["live"], (
        f"the advised fleet's queue never came down from its peak ({peaks.summary()})"
    )
    print(f"assist queue: {peaks.summary()}, floor {QUEUE_RATIO:g}x")


def _ack_summary(log: FrameLog) -> str:
    """How each operator answered each advisory, for a failure message.

    `op_c` declines a hazard under its confidence floor, and its vehicles then
    stay stalled exactly as the ghost's do. That is the first thing to look at
    when the ratio comes in near 3x rather than above it.
    """
    answers: dict[str, set[str]] = defaultdict(set)
    for frame in log.by_type.get(FRAME_ACK, ()):
        answers[str(frame.get("operator"))].add(str(frame.get("status")))
    rows = [f"{op} {sorted(statuses)}" for op, statuses in sorted(answers.items())]
    return "; ".join(rows) or "none"


async def _audit_types(bus: Bus) -> str:
    """What is on the audit stream, by message type, for a failure message.

    A queue of zero has two very different causes -- the pipeline never got
    far enough to advise anything, or it advised and the fleets never met the
    hazard -- and the audit stream is what tells them apart.
    """
    seen: dict[str, int] = defaultdict(int)
    async for message in bus.replay(STREAM_AUDIT, limit=500):
        seen[str(message.type)] += 1
    return ", ".join(f"{name}={count}" for name, count in sorted(seen.items())) or "empty"


def _entries(log: FrameLog, operator: str) -> int:
    """One operator's live hazard entries, from the newest counters frame."""
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

    advisories = await http.get("/advisories")
    assert advisories.json()["features"] == []

    await _require(
        log,
        lambda seen: (seen.latest(FRAME_SCENARIO_STATE) or {}).get("state") == "idle",
        timeout=30.0,
        hint="the viewer was never told the scenario stopped",
    )
