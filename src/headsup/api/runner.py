"""One scenario at a time: the replay, the agents, and both fleets on one clock.

This is what the viewer's Test Scene launcher drives. `start` resets the
streams and the simulator, then brings up four things together:

* the **replay**, feeding the recording onto `events.*` at the scenario speed;
* the **agents** for that scenario -- its sentinel, fusion, the proposer and
  the compiler -- as following workers, each with its own consumer group;
* the **live fleet** and the **ghost fleet**, in one `SimRunner`, which is what
  makes them one clock: the same tick loop steps both, so the comparison the
  demo rests on cannot drift apart between two runners;
* the **counters**, sampled off both fleets for the viewer's HUD.

One scenario at a time, and the lock says so: a second start while one is
running is a 409, not a second pipeline quietly sharing the first one's
streams. Everything a run owns -- the simulator, fusion's live groups, the
compiler's service, the model client -- is built in `start` and dropped in
`stop`, so a second run of the same scenario starts from the state the first
one did rather than from whatever the first one left behind.

**The speed factor is one number.** The replay's and the simulator's, both,
because a flood warning arriving at 60x feed time while vehicles drove at 1x
would be a demo of nothing. Both pace themselves against their own start
instant, and both are launched in the same event loop iteration.

**Which agents run is the scenario's own business.** A flood scenario brings up
the flood sentinel and a signal scenario the signal sentinel, from the
descriptor's `hazard_class`. Fusion and the proposer run for every scenario:
they are not per-feed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from headsup.agents.compiler.approvals import ApprovalStore
from headsup.agents.compiler.compiler import Compiler, CompilerError
from headsup.agents.compiler.service import CompilerService
from headsup.agents.fusion import Fusion
from headsup.agents.pipeline import FusionWorker, ProposerWorker
from headsup.agents.proposer import Proposer
from headsup.agents.sentinels.base import Sentinel
from headsup.agents.sentinels.flood import FloodSentinel
from headsup.agents.sentinels.signals import SignalOutageSentinel
from headsup.agents.sentinels.worker import SentinelWorker
from headsup.api.catalog import ScenarioCard
from headsup.api.frames import FRAME_GHOST_TELEMETRY, RailIndex, counters_frame
from headsup.api.hub import ViewerHub
from headsup.api.operators import OperatorRegistry
from headsup.bus import ALL_STREAMS, Bus
from headsup.bus.acl import PROTECTED_WRITER, Role, url_for
from headsup.contracts import HazardClass
from headsup.ingest import IngestError, Recording, Replayer, check_speed
from headsup.llm import LlmClient, LlmError
from headsup.sim.config import SimConfig, SimMode
from headsup.sim.ground_truth import EmptyHazardField, HazardField, ScriptedHazardField
from headsup.sim.network import DriveNetwork, NetworkError
from headsup.sim.runner import SimRunner
from headsup.sim.scenarios import scenario_regions
from headsup.world.graph import WorldError
from headsup.world.model import is_built, load_world

log = logging.getLogger(__name__)

#: Which sentinel a scenario's hazard class asks for.
SENTINEL_FOR: dict[HazardClass, type[Sentinel]] = {
    HazardClass.FLOOD: FloodSentinel,
    HazardClass.SIGNAL_OUTAGE: SignalOutageSentinel,
}

#: The sentinels that ask a model something. The signal sentinel reads
#: structured feed fields and takes no client at all.
MODEL_SENTINELS: frozenset[type[Sentinel]] = frozenset({FloodSentinel})

#: Default fleet size for a scenario run. Smaller than the 900 of the
#: throughput check: a launcher run is watched, and 300 vehicles already more
#: than fills a downtown block grid on screen.
DEFAULT_VEHICLES = 300

#: How long the ground-truth hazard areas stay in force. They are the physical
#: situation rather than an advisory, so they outlast any one run.
GROUND_TRUTH_HOURS = 24

#: Seconds between counter samples for the viewer's HUD.
COUNTERS_PERIOD_S = 1.0

#: How many times a fallen agent worker is put back on its feet, and how long
#: it waits first. A worker must not be ended by one slow read: a judgment
#: that holds the loop for several seconds -- a critical-access check over a
#: county-sized polygon will -- times out whatever Redis read was in flight,
#: and the agent has work waiting for it on the other side. Bounded, and every
#: restart is noted on the run, so a worker failing in a loop is visible
#: rather than quietly absent.
WORKER_RESTARTS = 20
WORKER_PAUSE_S = 1.0

#: The viewer wants 2-5 Hz of telemetry. Narrower than `SimConfig` allows,
#: because this is the socket's rate rather than a free run parameter.
TELEMETRY_HZ_LO = 2.0
TELEMETRY_HZ_HI = 5.0


class ScenarioState(StrEnum):
    """What the server is doing with scenarios right now."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class ScenarioError(RuntimeError):
    """A scenario cannot be started: no recording, no world, a bad speed."""


class AlreadyRunning(RuntimeError):
    """A scenario is already running, and only one may be."""


class StartOptions(BaseModel):
    """What the launcher may choose about a run."""

    model_config = ConfigDict(extra="forbid")

    #: Feed seconds per wall second, for the replay and the fleets alike.
    #: Unset means the descriptor's own `replay.default_speed`.
    speed: float | None = Field(default=None, ge=1.0, le=60.0)
    vehicles: int = Field(default=DEFAULT_VEHICLES, ge=3, le=900)
    fleets: int = Field(default=3, ge=1, le=3)
    seed: int = 42
    #: Simulated minutes before the fleets stop. 0 runs until stopped.
    minutes: float = Field(default=0.0, ge=0.0, le=600.0)
    telemetry_hz: float = Field(default=TELEMETRY_HZ_LO, ge=TELEMETRY_HZ_LO, le=TELEMETRY_HZ_HI)
    zones: int = Field(default=48, ge=2, le=512)
    #: Let the agents that ask a model ask it. Off means the rules-only path.
    llm: bool = True


@dataclass(slots=True)
class _Run:
    """Everything one running scenario owns."""

    card: ScenarioCard
    options: StartOptions
    cfg: SimConfig
    speed: float
    started_at: datetime
    sim: SimRunner
    replayer: Replayer
    service: CompilerService
    compiler_bus: Bus
    client: LlmClient | None
    sentinel: Sentinel
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    replay_complete: bool = False
    fleets_complete: bool = False
    errors: list[str] = field(default_factory=list)

    def note(self, problem: str) -> None:
        log.warning("scenario %s: %s", self.card.id, problem)
        if len(self.errors) < 20:
            self.errors.append(problem)


class ScenarioRunner:
    """The one scenario a server may have running, and how it is reset."""

    def __init__(
        self,
        bus: Bus,
        hub: ViewerHub,
        index: RailIndex,
        registry: OperatorRegistry,
        *,
        approvals: ApprovalStore | None = None,
        namespace: str = "",
    ) -> None:
        self.bus = bus
        self.hub = hub
        self.index = index
        self.registry = registry
        self.approvals = approvals
        self.namespace = namespace
        self._run: _Run | None = None
        self._state = ScenarioState.IDLE
        self._lock = asyncio.Lock()

    # --- what the API reports ----------------------------------------------

    @property
    def state(self) -> ScenarioState:
        return self._state

    @property
    def running(self) -> bool:
        return self._run is not None

    @property
    def scenario_id(self) -> str | None:
        return self._run.card.id if self._run is not None else None

    def status(self) -> dict[str, Any]:
        """The `scenario_state` frame: one answer, for HTTP and for the socket."""
        run = self._run
        if run is None:
            return {
                "type": "scenario_state",
                "state": self._state.value,
                "scenario": None,
                "errors": [],
            }
        return {
            "type": "scenario_state",
            "state": self._state.value,
            "scenario": run.card.model_dump(mode="json"),
            "speed": run.speed,
            "started_at": run.started_at.isoformat(),
            "vehicles": run.cfg.vehicles,
            "fleets": run.cfg.fleets,
            "seed": run.cfg.seed,
            "modes": [mode.value for mode in run.sim.simulations],
            "sentinel": run.sentinel.name,
            "llm_mode": run.client.mode.value if run.client is not None else "off",
            "replay_complete": run.replay_complete,
            "fleets_complete": run.fleets_complete,
            "errors": list(run.errors),
        }

    def _announce(self) -> None:
        self.hub.publish(self.status())

    # --- starting -----------------------------------------------------------

    async def start(self, card: ScenarioCard, options: StartOptions) -> dict[str, Any]:
        """Reset everything, then run this scenario. Raises if one is running."""
        async with self._lock:
            if self._run is not None:
                raise AlreadyRunning(
                    f"scenario {self._run.card.id} is already running; "
                    "POST /scenarios/stop before starting another"
                )
            self._state = ScenarioState.STARTING
            try:
                run = await self._build(card, options)
            except Exception:
                self._state = ScenarioState.IDLE
                raise
            self._run = run
            self._state = ScenarioState.RUNNING
            run.tasks = self._launch(run)
            self._announce()
            return self.status()

    async def _build(self, card: ScenarioCard, options: StartOptions) -> _Run:
        """Everything that can refuse a start, before anything is launched."""
        speed = check_speed(options.speed or card.default_speed)
        if not card.city:
            raise ScenarioError(f"scenario {card.id} names no city")
        if not is_built(card.city):
            raise ScenarioError(
                f"{card.city} is not built - run `hu world build --city {card.city}`"
            )
        hazard_class = _hazard_class(card)
        try:
            recording = Recording.locate(card.id)
        except IngestError as exc:
            raise ScenarioError(str(exc)) from exc

        cfg = SimConfig(
            city=card.city,
            seed=options.seed,
            vehicles=options.vehicles - options.vehicles % options.fleets,
            fleets=options.fleets,
            speed=speed,
            zones=min(options.zones, options.vehicles),
            telemetry_hz=options.telemetry_hz,
        )
        try:
            cfg.validate()
            world = load_world(card.city)
        except (ValueError, WorldError) as exc:
            raise ScenarioError(str(exc)) from exc

        # Cleared before anything reads them, so the agents' consumer groups
        # and the viewer's are created over an empty history and the entry
        # counts of one run mean something.
        await self.bus.delete_streams(ALL_STREAMS)
        self.index.clear()
        self.hub.reset()
        self.registry.set_city(card.city)

        start = datetime.now(UTC).replace(microsecond=0)
        hazards, net = self._ground_truth(card, cfg, world, start)

        client = LlmClient.from_env(bus=self.bus) if options.llm else None
        compiler_bus = Bus(
            url_for(Role.COMPILER), namespace=self.namespace, writer=PROTECTED_WRITER
        )
        try:
            service = self._compiler_service(card, compiler_bus)
        except (CompilerError, LlmError, WorldError) as exc:
            await compiler_bus.close()
            if client is not None:
                await client.aclose()
            raise ScenarioError(str(exc)) from exc

        sentinel_class = SENTINEL_FOR[hazard_class]
        sentinel = (
            sentinel_class(llm=client)  # type: ignore[call-arg]
            if sentinel_class in MODEL_SENTINELS
            else sentinel_class()  # type: ignore[call-arg]
        )
        return _Run(
            card=card,
            options=options,
            cfg=cfg,
            speed=speed,
            started_at=start,
            sim=SimRunner(
                world,
                cfg,
                modes=(SimMode.LIVE, SimMode.GHOST),
                bus=self.bus,
                hazards=hazards,
                start=start,
                net=net,
                tap=self._tap,
            ),
            replayer=Replayer(recording, bus=self.bus, speed=speed),
            service=service,
            compiler_bus=compiler_bus,
            client=client,
            sentinel=sentinel,
        )

    def _ground_truth(
        self, card: ScenarioCard, cfg: SimConfig, world: Any, start: datetime
    ) -> tuple[HazardField, DriveNetwork]:
        """The scenario's physical hazards, and a network anchored on them.

        What is physically true is independent of any advisory: without it
        there is nothing for an unadvised vehicle to stall on and no baseline
        to compare the advised fleet against.
        """
        regions = list(
            scenario_regions(
                card.id,
                valid_from=start,
                valid_until=start + timedelta(hours=GROUND_TRUTH_HOURS),
            )
        )
        try:
            net = DriveNetwork.build(world, cfg, anchors=[region.geometry for region in regions])
        except NetworkError as exc:
            raise ScenarioError(str(exc)) from exc
        field_ = ScriptedHazardField.from_regions(regions, net) if regions else EmptyHazardField()
        return field_, net

    def _compiler_service(self, card: ScenarioCard, bus: Bus) -> CompilerService:
        """The gate, on this city's road graph and this server's approvals.

        The POI checker is the city's own, so the critical-access rule is
        answered off the same graph the fleets drive on; `CompilerService`
        refuses to start if it is a null one.
        """
        from headsup.world.checker import default_poi_checker

        compiler = Compiler(
            poi=default_poi_checker(card.city),
            approvals=self.approvals,
            issuer=f"headsup/{card.city}",
        )
        return CompilerService(bus, compiler)

    def _agents(self, run: _Run) -> list[SentinelWorker | FusionWorker | ProposerWorker]:
        """The scenario's sentinel, fusion and the proposer, as workers.

        The proposer borrows the compiler's POI checker, so both ends of the
        pipeline answer the critical-access question off one road graph.
        """
        return [
            SentinelWorker(run.sentinel, self.bus),
            FusionWorker(Fusion(llm=run.client), self.bus),
            ProposerWorker(
                Proposer(llm=run.client, poi=run.service.compiler.poi),
                self.bus,
                compiler=run.service,
            ),
        ]

    def _launch(self, run: _Run) -> list[asyncio.Task[Any]]:
        """Start the replay, the agents, the fleets and the counter sampler."""
        tasks = [
            asyncio.create_task(self._replay(run), name=f"replay:{run.card.id}"),
            asyncio.create_task(self._fleets(run), name=f"fleets:{run.card.id}"),
            asyncio.create_task(self._counters(run), name=f"counters:{run.card.id}"),
        ]
        tasks.extend(
            asyncio.create_task(
                self._supervise(run, worker), name=f"agent:{worker.report.sentinel}"
            )
            for worker in self._agents(run)
        )
        for task in tasks:
            task.add_done_callback(lambda done, owner=run: self._finished(owner, done))
        return tasks

    def _finished(self, run: _Run, task: asyncio.Task[Any]) -> None:
        """Record a task that ended on its own, successfully or not."""
        if task.cancelled() or self._run is not run:
            return
        exc = task.exception()
        if exc is not None:
            run.note(f"{task.get_name()} failed: {exc}")
            self._announce()

    # --- the four things a run does ----------------------------------------

    async def _supervise(self, run: _Run, worker: Any) -> None:
        """Follow one agent's stream, putting the worker back if it falls over.

        The worker object is reused rather than rebuilt: `Bus.consume` recreates
        a missing consumer group and replays this consumer's own unacked
        backlog, so restarting loses no message, while a fresh worker would
        lose what the agent knows -- fusion's live groups above all.
        """
        name = worker.report.sentinel
        for attempt in range(1, WORKER_RESTARTS + 1):
            try:
                await worker.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                run.note(f"{name} fell over ({attempt}/{WORKER_RESTARTS}) and was restarted: {exc}")
                await asyncio.sleep(WORKER_PAUSE_S)
        run.note(f"{name} failed {WORKER_RESTARTS} times and was given up on")

    async def _replay(self, run: _Run) -> None:
        """Feed the recording onto `events.*` with its recorded spacing."""
        count = 0
        async for _emission in run.replayer.run():
            count += 1
        run.replay_complete = True
        log.info("scenario %s: replay finished, %d events", run.card.id, count)
        self._announce()

    async def _fleets(self, run: _Run) -> None:
        """Step the live and the ghost fleet together."""
        ticks = round(run.options.minutes * 60) if run.options.minutes > 0 else None
        await run.sim.run(ticks=ticks)
        run.fleets_complete = True
        self._announce()

    async def _counters(self, run: _Run) -> None:
        """Sample both fleets' counters for the HUD until cancelled."""
        while True:
            await asyncio.sleep(COUNTERS_PERIOD_S)
            self.hub.publish(self.counters_frame(run))

    def counters_frame(self, run: _Run | None = None) -> dict[str, Any]:
        """Both fleets' counters side by side, or empty sets when idle."""
        target = run or self._run
        if target is None:
            return counters_frame({}, {})
        by_mode = {mode.value: sim.counters() for mode, sim in target.sim.simulations.items()}
        blocked = {
            mode.value: sim.blocked_minutes() for mode, sim in target.sim.simulations.items()
        }
        return counters_frame(by_mode, blocked)

    def _tap(self, mode: SimMode, result: Any) -> None:
        """One tick, straight to the viewer. Called inside the tick loop.

        Queues and returns: the hub never awaits a socket, so a viewer that
        has stopped reading cannot slow the fleets down.
        """
        if result.telemetry is None:
            return
        frame = result.telemetry.to_wire()
        if mode is SimMode.GHOST:
            frame["type"] = FRAME_GHOST_TELEMETRY
        self.hub.publish(frame)

    # --- stopping -----------------------------------------------------------

    async def stop(self) -> dict[str, Any]:
        """Stop whatever is running and reset to idle. Safe when already idle."""
        async with self._lock:
            run, self._run = self._run, None
            if run is None:
                self._state = ScenarioState.IDLE
                idle = self.status()
                self.hub.reset(idle)
                return idle

            self._state = ScenarioState.STOPPING
            for task in run.tasks:
                task.cancel()
            await asyncio.gather(*run.tasks, return_exceptions=True)
            await run.service.close()
            await run.compiler_bus.close()
            if run.client is not None:
                with contextlib.suppress(Exception):
                    await run.client.aclose()
            await self.bus.delete_streams(ALL_STREAMS)

            self._state = ScenarioState.IDLE
            self.index.clear()
            idle = self.status()
            self.hub.reset(idle)
            self.hub.publish(idle)
            log.info("scenario %s stopped", run.card.id)
            return idle


def _hazard_class(card: ScenarioCard) -> HazardClass:
    """The hazard class this scenario's sentinel is chosen by."""
    known = ", ".join(cls.value for cls in SENTINEL_FOR)
    if not card.hazard_class:
        raise ScenarioError(f"scenario {card.id} names no hazard_class; expected one of: {known}")
    try:
        hazard_class = HazardClass(card.hazard_class)
    except ValueError as exc:
        raise ScenarioError(f"{card.hazard_class!r} is not a hazard class") from exc
    if hazard_class not in SENTINEL_FOR:
        raise ScenarioError(f"no sentinel reads {hazard_class.value}; have: {known}")
    return hazard_class
