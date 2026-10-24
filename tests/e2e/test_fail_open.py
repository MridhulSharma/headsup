"""Fail-open: every agent dies mid-S1 and the fleets come back to defaults.

Needs the Docker services (`uv run hu infra up`) and a built San Antonio
(`uv run hu world build --city san_antonio`). The model is mocked, so no run
of this file spends anything.

The guardrail under test is the one nobody gets to choose at the time: when
Heads Up stops working, what the fleets are driving under has to unwind by
itself. So this test takes a real S1 run to the point where an operator is
masked by a signed advisory, kills every agent task in the process, and then
watches what the simulator does with no pipeline behind it.

Three claims, one run, because starting S1 costs a city graph:

1. **Mandatory expiry.** An advisory is a lease, not a state. With the agents
   gone nothing can renew it, and it has to lapse on its own `valid_until`
   rather than persist because the thing that would have withdrawn it is dead.
2. **Fail-open.** Once it lapses the operator holds nothing and its mask is
   `EMPTY_MASK` - the operator's own defaults, which is what the fleet drove
   under before any advice arrived - and the simulator keeps ticking.
3. **The stale-feed breaker.** `GET /healthz` reports, per feed, how long
   since it last published and whether that is past three of its own poll
   intervals. The breaker's effect on a hazard's confidence is arithmetic and
   is tested in `tests/unit/test_feed_freshness.py`; what is checked here is
   that a live server reports the verdict, in feed time, off a running replay.

**Which clock each expiry is on, because they differ.** A candidate's window
is wall-clock (`sentinels.flood`: `valid_until` is a promise to a fleet whose
clock is now), while the simulator's operators expire what they hold against
`sim_time`, which at 60x runs sixty times faster than the wall. So the
simulator reaches an advisory's `valid_until` in about a sixtieth of the
window's length, and that is the expiry this test waits out. `GET /advisories`
filters on wall-clock now and will still be listing the advisory as active at
that point; this test therefore makes no assertion about that endpoint after
the kill.

**Killing the agents.** The runner launches each worker as a task named
`agent:<name>`, so cancelling every task with that prefix is the in-process
equivalent of killing the agent processes: `_supervise` re-raises
`CancelledError` instead of restarting the worker, and nothing is left that
can propose, review, compile or withdraw. The replay and both fleets are left
running on purpose - a fail-open test where the simulator also stopped would
prove nothing.

The second-key holder is stopped *before* the kill. `POST /approvals/{id}`
puts a proposal back through the gate through the compiler service, which does
not belong to any worker, so a token granted after the kill could publish an
advisory the dead mesh never proposed and quietly falsify claim 1.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import uuid4

import httpx
import pytest
import redis.exceptions
import uvicorn

from headsup.api.app import ApiState, create_app
from headsup.bus import ALL_STREAMS, STREAM_ADVISORIES, Bus
from headsup.contracts import Advisory, Source
from headsup.ingest.freshness import STALE_FACTOR, interval_s
from headsup.sim.config import SimMode

pytestmark = pytest.mark.e2e

SCENARIO = "s1_flood"
SPEED = 60.0
REDIS_URL = "redis://localhost:6379/0"
INFRA_HINT = "run `uv run hu infra up` first"

#: Small fleet: this test watches one operator's mask, not throughput.
VEHICLES = 60
ZONES = 12

#: Seconds to wait for the start request, which loads the city world, the POI
#: index and the drive network before it answers.
START_TIMEOUT = 300.0

#: Seconds to wait for an operator to be masked by a published advisory. Feed
#: time, almost all of it: S1's first warning is uncorroborated and refused on
#: its area, and the Blanco Rd gauge does not cross its action stage until
#: 21:15Z, 145 s further into a 60x replay. Generous because it is a deadline
#: on a paced replay and not a measurement - see `tests/e2e/test_s1.py`.
MASKED_TIMEOUT = 480.0

#: Wall seconds allowed past the point `sim_time` passes `valid_until`. The
#: operator drops an expired advisory on its next tick, so this only has to
#: cover a few ticks and the poll below.
EXPIRY_GRACE_S = 60.0

#: How often the waits here look again.
POLL_S = 0.5

#: Wall seconds to watch the fleets for after the mask has dropped. Long
#: enough that a simulator which died with its last advisory cannot pass.
SETTLE_S = 10.0

OPERATOR = "e2e_fail_open"
APPROVAL_POLL_S = 0.5


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
async def state(bus: Bus, monkeypatch: pytest.MonkeyPatch) -> ApiState:
    """The server's own state, kept so the test can read the simulator.

    A mask is not public and no endpoint exposes one, which is correct: what a
    fleet is driving under is the operator's business. The claim here is about
    exactly that internal state, so this test reads the simulator in process
    rather than inferring a mask from counters.
    """
    monkeypatch.setenv("HU_LLM_MODE", "mock")
    monkeypatch.setenv("HU_CITY", "san_antonio")
    return ApiState(namespace=bus.namespace)


@pytest.fixture
async def server(state: ApiState) -> Any:
    """The real app under uvicorn, on this loop, on a port the OS picks."""
    config = uvicorn.Config(
        create_app(state), host="127.0.0.1", port=0, log_level="warning", lifespan="on"
    )
    instance = uvicorn.Server(config)
    task = asyncio.create_task(instance.serve(), name="e2e-failopen-server")
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


class SecondKey:
    """Whoever holds the second key, until this test takes it away.

    S1's narrowed hazard is a severity 4 over 7.04 km2, above both
    `two_key_severity` and `two_key_area_km2` in `playbooks/flood.yaml`, so
    nothing publishes without a token. Issued the same way the viewer's
    approve button does, and stopped before the kill.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.granted: dict[str, str] = {}
        self.refused: list[str] = []
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
            await asyncio.sleep(APPROVAL_POLL_S)

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
                    "note": f"fail-open drill approved {row.get('action')}",
                },
            )
            if response.status_code != 201:
                self.refused.append(f"{proposal_id}: {response.status_code} {response.text[:200]}")
                continue
            body = response.json()
            self.granted[proposal_id] = str(body.get("fingerprint") or "granted")


async def test_killing_every_agent_mid_s1_lapses_its_advisories_and_fails_open(
    server: str, state: ApiState, bus: Bus
) -> None:
    """Start S1, get a fleet masked, kill the mesh, and watch the mask unwind."""
    async with httpx.AsyncClient(base_url=f"http://{server}", timeout=START_TIMEOUT) as http:
        await _start_s1(http)
        key = SecondKey(http)
        holder = asyncio.create_task(key.run(), name="e2e-failopen-second-key")
        try:
            advisory = await _an_operator_is_masked(http, state, key)
            feeds = await _healthz_reports_every_feeds_age(http)
        finally:
            # Before the kill, deliberately: see the module docstring.
            holder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await holder

        published = await bus.length(STREAM_ADVISORIES)
        killed = await _kill_every_agent()
        await _the_run_survived_the_kill(http, state, killed)

        await _the_advisory_lapsed_on_schedule(state, advisory)
        await _the_fleets_came_back_to_operator_defaults(state)
        await _nothing_was_published_after_the_kill(bus, published)
        await _the_staleness_verdict_is_still_consistent(http, feeds)
        await _stop_returns_to_idle(http)


# --- getting to a masked fleet ----------------------------------------------


async def _start_s1(http: httpx.AsyncClient) -> None:
    """One POST brings up the replay, the agents and both fleets at 60x."""
    response = await http.post(
        f"/scenarios/{SCENARIO}/start",
        json={"speed": SPEED, "vehicles": VEHICLES, "zones": ZONES, "seed": 42},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "running"
    assert body["llm_mode"] == "mock"
    # The mesh, named: these are the tasks the kill below has to find.
    assert body["agents"], f"no agents to kill in this variant: {body}"


def _live(state: ApiState) -> Any:
    """The live fleet's simulation, or a failure saying nothing is running."""
    runner = state.require_runner()
    # Reaching inside on purpose: no endpoint exposes a mask, and the claim
    # under test is about exactly that state.
    run = runner._run
    assert run is not None, "the scenario stopped on its own"
    return run.sim.simulations[SimMode.LIVE]


def _masked(state: ApiState) -> list[tuple[str, tuple[str, ...]]]:
    """Each live operator that is driving under something, with what."""
    return [
        (op.name, op.accepted_ids())
        for op in _live(state).operators.operators
        if op.accepted_ids() and not op.mask.empty
    ]


async def _an_operator_is_masked(
    http: httpx.AsyncClient, state: ApiState, key: SecondKey
) -> dict[str, Any]:
    """Wait until a published advisory is in force for an operator, and return it.

    Waited for in the simulator rather than on the advisories stream: an
    advisory that has been signed but not yet offered, accepted and promoted
    is not something a fleet is driving under, and only the latter can fail
    open.
    """
    deadline = asyncio.get_running_loop().time() + MASKED_TIMEOUT
    while not _masked(state):
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(
                f"no operator was masked within {MASKED_TIMEOUT:g} s. "
                f"Approvals granted: {sorted(key.granted) or 'none'}"
                + (f", refused: {key.refused}" if key.refused else "")
                + (f", errors: {key.errors}" if key.errors else "")
            )
        await asyncio.sleep(POLL_S)

    operator, held = _masked(state)[0]
    advisory_id = held[0]
    response = await http.get("/advisories", params={"active": "false"})
    assert response.status_code == 200, response.text
    features = {feature["properties"]["id"]: feature for feature in response.json()["features"]}
    assert advisory_id in features, (
        f"{operator} is masked by {advisory_id}, which GET /advisories does not list: "
        f"{sorted(features)}"
    )
    advisory = Advisory.model_validate(features[advisory_id])
    print(
        f"masked: {operator} under {advisory_id} until "
        f"{advisory.properties.valid_until.isoformat()}"
    )
    return {
        "id": advisory_id,
        "operator": operator,
        "valid_until": advisory.properties.valid_until,
    }


async def _healthz_reports_every_feeds_age(http: httpx.AsyncClient) -> list[dict[str, Any]]:
    """The staleness breaker, as an operator reads it off a running replay.

    Every row's verdict is recomputed here from its own numbers, so a `stale`
    flag that had stopped being about three poll intervals would fail rather
    than be taken on trust. The ages are feed seconds: at 60x a run that has
    been up for a couple of minutes is already hours into the recording, which
    is why they are checked against the recording's cadence and not the wall.
    """
    response = await http.get("/healthz")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["scenario"] == SCENARIO
    assert body["stale_factor"] == STALE_FACTOR

    feeds = body["feeds"]
    assert feeds, "a running replay has published events, so /healthz must know its feeds"
    for row in feeds:
        source = Source(row["source"])
        assert row["interval_s"] == interval_s(source)
        assert row["stale_after_s"] == interval_s(source) * STALE_FACTOR
        assert row["events"] > 0
        assert row["age_s"] >= 0.0
        assert row["stale"] is (row["age_s"] > row["stale_after_s"]), (
            f"{row['source']} is reported stale={row['stale']} at {row['age_s']} s "
            f"against a {row['stale_after_s']} s threshold"
        )
    assert body["stale_feeds"] == [row["source"] for row in feeds if row["stale"]]
    print(f"feeds: {[(row['source'], row['age_s'], row['stale']) for row in feeds]}")
    return feeds


# --- the kill ---------------------------------------------------------------


async def _kill_every_agent() -> list[str]:
    """Cancel every `agent:*` task and wait for all of them to be gone."""
    doomed = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("agent:") and not task.done()
    ]
    assert doomed, "no agent tasks were running, so this test killed nothing"
    names = sorted(task.get_name() for task in doomed)
    for task in doomed:
        task.cancel()
    await asyncio.gather(*doomed, return_exceptions=True)
    print(f"killed: {names}")
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("agent:")], (
        "an agent task survived the kill"
    )
    return names


async def _the_run_survived_the_kill(
    http: httpx.AsyncClient, state: ApiState, killed: list[str]
) -> None:
    """The server is still up and the scenario is still running, with no errors.

    A cancelled worker is a worker that was taken away, not one that fell
    over, so the runner must not report it as a failure - an `errors` entry
    here would mean the kill had been mistaken for a crash and a restart
    attempted.
    """
    runner = state.require_runner()
    assert runner.state.value == "running"
    assert runner.scenario_id == SCENARIO

    listing = await http.get("/scenarios")
    assert listing.status_code == 200, listing.text
    assert listing.json()["running"] == SCENARIO

    health = await http.get("/healthz")
    assert health.status_code == 200, health.text
    assert health.json()["ok"] is True

    assert runner.status()["errors"] == [], (
        f"killing {killed} was reported as a failure: {runner.status()['errors']}"
    )


# --- what has to unwind by itself -------------------------------------------


async def _the_advisory_lapsed_on_schedule(state: ApiState, advisory: dict[str, Any]) -> None:
    """With nobody left to renew it, the advisory lapses on its own window.

    The wait is bounded by the window itself, read off the advisory and
    divided by the replay speed, because the operators expire what they hold
    against `sim_time`. A simulator that kept a dead mesh's advisory past that
    point is one that would keep a stale closure in front of a fleet forever.
    """
    live = _live(state)
    valid_until = advisory["valid_until"]
    remaining = (valid_until - live.sim_time).total_seconds()
    assert remaining > 0, (
        f"advisory {advisory['id']} had already expired in sim time before the kill; "
        f"this run proved nothing about expiry"
    )
    budget = remaining / SPEED + EXPIRY_GRACE_S
    print(
        f"waiting out {remaining / 60:.1f} sim minutes of advisory "
        f"{advisory['id']} (<= {budget:.0f} s wall)"
    )

    deadline = asyncio.get_running_loop().time() + budget
    while advisory["id"] in _held_ids(state):
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(
                f"advisory {advisory['id']} was still in force {budget:.0f} s after the "
                f"kill, with sim time {live.sim_time.isoformat()} past its "
                f"valid_until {valid_until.isoformat()}"
            )
        await asyncio.sleep(POLL_S)
    assert live.sim_time >= valid_until, (
        f"advisory {advisory['id']} was dropped at {live.sim_time.isoformat()}, before "
        f"its valid_until {valid_until.isoformat()}: an expiry is not a withdrawal"
    )


def _held_ids(state: ApiState) -> set[str]:
    """Every advisory any live operator is currently driving under."""
    return {
        advisory_id for op in _live(state).operators.operators for advisory_id in op.accepted_ids()
    }


async def _the_fleets_came_back_to_operator_defaults(state: ApiState) -> None:
    """Every mask is the empty one again, and the simulator is still ticking.

    `EMPTY_MASK` is the operator's defaults: the graph the fleet drove on
    before any advice arrived. Checked after a settling window rather than
    once, because a simulator that stopped on the same tick that dropped the
    last advisory would otherwise look identical to one that recovered.
    """
    live = _live(state)
    before_tick, before_time = live.tick, live.sim_time
    await asyncio.sleep(SETTLE_S)

    assert live.tick > before_tick, (
        f"the simulator stopped ticking at {before_tick} when its advisories lapsed"
    )
    assert live.sim_time > before_time
    assert _held_ids(state) == set(), (
        f"operators still hold {sorted(_held_ids(state))} with every agent dead"
    )
    for op in live.operators.operators:
        assert op.mask.empty, (
            f"{op.name} is still masked after everything it accepted lapsed: "
            f"no_entry={len(op.mask.no_entry)}, speed_cap={len(op.mask.speed_cap_mm_s)}, "
            f"all_way_stop={len(op.mask.all_way_stop)}"
        )
    print(f"fail-open: ticked {before_tick} -> {live.tick} with every mask empty")


async def _nothing_was_published_after_the_kill(bus: Bus, published: int) -> None:
    """A dead mesh proposes nothing, so the advisories stream stops growing."""
    now = await bus.length(STREAM_ADVISORIES)
    assert now == published, (
        f"{now - published} advisory(s) were published after every agent was killed"
    )


async def _the_staleness_verdict_is_still_consistent(
    http: httpx.AsyncClient, before: list[dict[str, Any]]
) -> None:
    """The breaker still reports, and no feed has gone backwards in feed time.

    The replay is still running with nothing consuming it, so feeds keep
    reporting and their ages keep being measured in feed time. What must hold
    is that the endpoint survived the kill and that a feed's last event never
    moves earlier - the breaker has to be monotonic or an operator watching it
    would see feeds flap in and out of staleness.
    """
    response = await http.get("/healthz")
    assert response.status_code == 200, response.text
    body = response.json()
    was = {row["source"]: row["last_event_at"] for row in before}
    for row in body["feeds"]:
        assert row["stale"] is (row["age_s"] > row["stale_after_s"])
        if row["source"] in was:
            assert row["last_event_at"] >= was[row["source"]], (
                f"{row['source']} moved backwards in feed time"
            )
    assert body["stale_feeds"] == [row["source"] for row in body["feeds"] if row["stale"]]


async def _stop_returns_to_idle(http: httpx.AsyncClient) -> None:
    """Stop still works on a run whose agents are gone, and resets the streams."""
    response = await http.post("/scenarios/stop")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "type": "scenario_state",
        "state": "idle",
        "scenario": None,
        "errors": [],
    }
    assert (await http.get("/advisories")).json()["features"] == []
    # The feed times went with the run: an idle server reports no feeds rather
    # than the last run's, which would open the next one with every breaker
    # tripped.
    assert (await http.get("/healthz")).json()["feeds"] == []
