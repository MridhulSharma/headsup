"""The only module here that touches Redis, asyncio or the wall clock.

Everything deterministic lives in `engine.py`; this wraps it in pacing and
I/O. Keeping the split sharp is what lets the unit tests build a `Simulation`
and step it with no Docker running, which matters because this repo has no
fake Redis and is not getting one.

Pacing follows `ingest/replayer.py`: the target for tick N is measured from
the run's start instant, not from the previous tick, so a slow tick makes the
run late but never lets the schedule drift.

Advisory intake uses `read_once(block_ms=0)` rather than `consume`. A blocking
read inside the tick loop would stall the simulator for up to a full second
per poll, which at 10x is ten ticks of simulated time lost to waiting.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime

from headsup.bus import Bus
from headsup.bus.streams import STREAM_ACKS, STREAM_ADVISORIES, STREAM_ASSIST, STREAM_TELEMETRY
from headsup.contracts import Advisory
from headsup.ingest import Clock, SystemClock
from headsup.sim.config import UNPACED, SimConfig, SimMode
from headsup.sim.engine import SimReport, Simulation, TickResult
from headsup.sim.ground_truth import EmptyHazardField, HazardField
from headsup.sim.network import DriveNetwork
from headsup.sim.rng import SeedTree
from headsup.sim.router import Router
from headsup.world.model import World

#: Telemetry frames kept on the stream. A long run must not grow Redis
#: without bound, and the viewer only ever wants the latest.
TELEMETRY_MAXLEN = 600

#: Consumer group for the simulator's read of `advisories`.
ADVISORY_GROUP = "sim"


class SimRunner:
    """Runs one or both fleet sets on a single clock, optionally on the bus."""

    __slots__ = (
        "_bus",
        "_cfg",
        "_clock",
        "_consumer",
        "_group_ready",
        "_net",
        "_sims",
        "_start",
        "_verify",
    )

    def __init__(
        self,
        world: World,
        cfg: SimConfig,
        *,
        modes: Sequence[SimMode] = (SimMode.LIVE,),
        bus: Bus | None = None,
        clock: Clock | None = None,
        hazards: HazardField | None = None,
        start: datetime | None = None,
        anchors: Sequence[object] = (),
        verify_signatures: bool = True,
        net: DriveNetwork | None = None,
    ) -> None:
        cfg.validate()
        self._cfg = cfg
        self._bus = bus
        self._clock: Clock = clock or SystemClock()
        self._start = start or datetime.now(UTC).replace(microsecond=0)
        self._verify = verify_signatures
        self._consumer = f"sim-{cfg.seed}-{int(time.time())}"
        self._group_ready = False

        self._net = net or DriveNetwork.build(world, cfg, anchors=anchors)  # type: ignore[arg-type]
        field = hazards or EmptyHazardField()
        # One router, one seed tree, shared by every mode: the ghost fleet's
        # mask is the empty one, so it reuses labels rather than paying for
        # its own, and a shared seed tree is what makes the two fleets start
        # from byte-identical positions.
        router = Router(self._net, cfg)
        seeds = SeedTree(cfg.seed)
        self._sims: dict[SimMode, Simulation] = {
            mode: Simulation(
                self._net,
                cfg,
                mode=mode,
                hazards=field,
                start=self._start,
                router=router,
                seeds=seeds,
            )
            for mode in modes
        }

    @property
    def simulations(self) -> dict[SimMode, Simulation]:
        return self._sims

    @property
    def network(self) -> DriveNetwork:
        return self._net

    def offer(self, advisory: Advisory) -> None:
        """Hand an advisory to every fleet set, live and ghost alike.

        The ghost receives it too, and ignores it behaviourally -- but a
        KEEP_CLEAR corridor still has to be *measured* against the ghost, or
        there is nothing to compare the advised fleet's corridor time to.
        """
        for sim in self._sims.values():
            sim.offer(advisory)

    async def run(self, *, ticks: int | None = None) -> SimReport:
        """Step every fleet set together until `ticks` is reached or forever."""
        cfg = self._cfg
        report = SimReport()
        wall_start = time.perf_counter()
        sim_start = self._clock.now()
        step_index = 0

        try:
            while ticks is None or step_index < ticks:
                if self._bus is not None and step_index % cfg.telemetry_stride == 0:
                    await self._intake()

                for mode, sim in self._sims.items():
                    result = sim.step()
                    if self._bus is not None:
                        await self._publish(result, mode)

                step_index += 1
                if cfg.speed != UNPACED:
                    target = (step_index * cfg.tick_s) / cfg.speed
                    elapsed = (self._clock.now() - sim_start).total_seconds()
                    await self._clock.sleep(max(0.0, target - elapsed))
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            pass

        report.ticks = step_index
        report.sim_span_s = float(step_index * cfg.tick_s)
        report.wall_span_s = time.perf_counter() - wall_start
        for mode, sim in self._sims.items():
            report.counters_by_mode[mode.value] = sim.counters()
            report.hashes_by_mode[mode.value] = sim.trajectory_hash()
            report.blocked_minutes_by_mode[mode.value] = sim.blocked_minutes()
        return report

    async def _intake(self) -> None:
        """Drain whatever advisories are waiting, without blocking."""
        bus = self._bus
        if bus is None:
            return
        if not self._group_ready:
            await bus.ensure_group(STREAM_ADVISORIES, ADVISORY_GROUP)
            self._group_ready = True
        messages = await bus.read_once(
            STREAM_ADVISORIES, ADVISORY_GROUP, self._consumer, count=32, block_ms=0
        )
        for message in messages:
            try:
                advisory = message.parse(Advisory)
            except Exception:
                await bus.ack(message)
                continue
            if self._verify and not self._signed(advisory):
                for sim in self._sims.values():
                    sim.note_rejected_advisory()
                await bus.ack(message)
                continue
            self.offer(advisory)
            await bus.ack(message)

    def _signed(self, advisory: Advisory) -> bool:
        """An advisory that does not verify is dropped, never applied.

        The simulator is downstream of the only process allowed to sign, so a
        failure here means something is wrong with the pipeline, not with the
        advice -- but acting on it anyway would defeat the signature.
        """
        from headsup.agents.compiler.signing import verify

        try:
            return verify(advisory)
        except Exception:
            return False

    async def _publish(self, result: TickResult, mode: SimMode) -> None:
        bus = self._bus
        if bus is None:
            return
        if result.telemetry is not None:
            await bus.publish(result.telemetry, stream=STREAM_TELEMETRY, maxlen=TELEMETRY_MAXLEN)
        for ack in result.acks:
            if mode is SimMode.GHOST:
                # A ghost fleet is a counterfactual, not a bus participant: it
                # must never put an ack on the wire that an operator would be
                # held to.
                continue
            await bus.publish(ack, stream=STREAM_ACKS)
        for assist in result.assists:
            await bus.publish(assist, stream=STREAM_ASSIST)


def modes_for(ghost: bool, both: bool) -> tuple[SimMode, ...]:
    """Resolve the CLI's mode flags into the fleet sets to run."""
    if both:
        return (SimMode.LIVE, SimMode.GHOST)
    return (SimMode.GHOST,) if ghost else (SimMode.LIVE,)


def config_from_options(
    *,
    city: str,
    seed: int,
    vehicles: int,
    fleets: int,
    speed: float,
    zones: int,
    telemetry_hz: float,
    any_vertex_destinations: bool,
) -> SimConfig:
    """Build and validate a config from CLI options."""
    cfg = SimConfig(
        city=city,
        seed=seed,
        vehicles=vehicles,
        fleets=fleets,
        speed=speed,
        zones=zones,
        telemetry_hz=telemetry_hz,
        any_vertex_destinations=any_vertex_destinations,
    )
    cfg.validate()
    return cfg
