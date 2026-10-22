"""The simulator on the real bus: reads advisories, writes acks and telemetry.

Needs the Docker services (`uv run hu infra up`). The deterministic core is
tested without Redis everywhere else; what is checked here is only the wiring,
including the rule that the simulator must not be able to publish an advisory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import redis.exceptions

from fixtures.sim import grid_net, scripted_hazards, tiny_config
from fixtures.world import block_box
from headsup.agents.compiler.signing import Signer
from headsup.bus import Bus, BusError
from headsup.bus.streams import (
    ALL_STREAMS,
    STREAM_ACKS,
    STREAM_ADVISORIES,
    STREAM_ASSIST,
    STREAM_TELEMETRY,
)
from headsup.contracts import (
    Ack,
    Action,
    ActionType,
    Advisory,
    AdvisoryProperties,
    AppliesTo,
    AssistRequest,
    Evidence,
    Hazard,
    HazardClass,
    Source,
    Telemetry,
)
from headsup.sim.config import SimMode
from headsup.sim.runner import SimRunner

pytestmark = pytest.mark.integration

REDIS_URL = "redis://localhost:6379/0"
HINT = "run `uv run hu infra up` first"

T0 = datetime(2026, 10, 20, 23, 15, tzinfo=UTC)
UNTIL = T0 + timedelta(hours=3)
BARRIER = block_box(0.9, 0.0, 1.1, 3.0)


@pytest.fixture
async def bus():
    instance = Bus(REDIS_URL, namespace=f"test_{uuid4().hex[:8]}")
    try:
        await instance.ping()
    except redis.exceptions.ConnectionError as exc:
        await instance.close()
        pytest.fail(f"{HINT}: {REDIS_URL} ({exc})")
    try:
        yield instance
    finally:
        await instance.delete_streams(ALL_STREAMS)
        await instance.close()


@pytest.fixture
async def compiler_bus(bus: Bus):
    instance = Bus(REDIS_URL, namespace=bus.namespace, writer="compiler", client=bus.client)
    yield instance
    await instance.close()


def _signed_advisory() -> Advisory:
    geometry = BARRIER.__geo_interface__
    evidence = [
        Evidence(
            event_id="ev-bus",
            source=Source.NWS,
            kind="flood_warning",
            observed_at=T0,
            raw_ref="data/recordings/s1_flood/fixture/nws.jsonl",
        )
    ]
    hazard = Hazard(
        id="haz-bus",
        **{"class": HazardClass.FLOOD},
        severity=4,
        confidence=0.95,
        geometry=geometry,
        evidence=evidence,
        valid_until=UNTIL,
        first_seen=T0,
        updated_at=T0,
    )
    advisory = Advisory(
        geometry=geometry,
        properties=AdvisoryProperties(
            id="adv-bus",
            version=1,
            hazard=hazard,
            action=Action(type=ActionType.NO_ENTRY, applies_to=AppliesTo()),
            valid_from=T0,
            valid_until=UNTIL,
            evidence=evidence,
            policy_rule="flood.no_entry.v1",
            issuer="headsup/fixture",
        ),
    )
    return Signer.dev().sign(advisory)


def _runner(bus: Bus, **kwargs) -> SimRunner:
    net = grid_net()
    overrides = {"speed": 0.0, **kwargs.pop("config", {})}
    cfg = tiny_config(**overrides)
    return SimRunner(
        net.world,
        cfg,
        modes=kwargs.pop("modes", (SimMode.LIVE,)),
        bus=bus,
        hazards=scripted_hazards(net),
        start=T0,
        net=net,
        **kwargs,
    )


async def test_the_simulator_may_not_publish_an_advisory(bus: Bus) -> None:
    """The CLAUDE.md hard rule, enforced client-side as well as by the ACL."""
    sim_bus = Bus(REDIS_URL, namespace=bus.namespace, writer=None, client=bus.client)
    with pytest.raises(BusError, match="only compiler may publish to advisories"):
        await sim_bus.publish(_signed_advisory())


async def test_telemetry_reaches_the_stream_and_is_capped(bus: Bus) -> None:
    runner = _runner(bus, config={"speed": 0.0})
    await runner.run(ticks=20)

    length = await bus.length(STREAM_TELEMETRY)
    assert length > 0
    messages = [message async for message in bus.replay(STREAM_TELEMETRY, limit=1)]
    frame = messages[0].parse(Telemetry)
    assert frame.type == "telemetry"
    assert frame.vehicles
    assert "hazard_entries_total" in frame.counters


async def test_an_advisory_on_the_stream_is_consumed_and_acked(bus: Bus, compiler_bus: Bus) -> None:
    await compiler_bus.publish(_signed_advisory(), stream=STREAM_ADVISORIES)

    runner = _runner(bus)
    await runner.run(ticks=150)

    acks = [message.parse(Ack) async for message in bus.replay(STREAM_ACKS)]
    assert acks, "the operators should have answered the advisory"
    assert {ack.advisory_id for ack in acks} == {"adv-bus"}
    assert {ack.operator for ack in acks} <= {"op_a", "op_b", "op_c"}
    assert any(ack.status.value == "ACCEPTED" for ack in acks)


async def test_an_accepted_advisory_keeps_op_a_out_of_the_hazard(
    bus: Bus, compiler_bus: Bus
) -> None:
    await compiler_bus.publish(_signed_advisory(), stream=STREAM_ADVISORIES)
    runner = _runner(bus)
    report = await runner.run(ticks=400)
    counters = report.counters_by_mode["live"]
    assert counters["hazard_entries_op_a"] == 0


async def test_assist_requests_reach_the_stream(bus: Bus) -> None:
    runner = _runner(bus, config={"stall_to_assist_s": 3})
    await runner.run(ticks=200)

    requests = [message.parse(AssistRequest) async for message in bus.replay(STREAM_ASSIST)]
    assert requests, "an unadvised fleet on a flooded barrier should ask for help"
    assert all(request.operator in {"op_a", "op_b", "op_c"} for request in requests)


async def test_an_unsigned_advisory_is_rejected_rather_than_applied(
    bus: Bus, compiler_bus: Bus
) -> None:
    """A signature that does not verify means the pipeline is wrong.

    Acting on it anyway would defeat the point of signing it.
    """
    advisory = _signed_advisory()
    tampered = advisory.model_copy(
        update={
            "properties": advisory.properties.model_copy(update={"signature": None}),
        }
    )
    await compiler_bus.publish(tampered, stream=STREAM_ADVISORIES)

    runner = _runner(bus)
    report = await runner.run(ticks=200)
    counters = report.counters_by_mode["live"]
    assert counters["advisories_rejected"] >= 1
    assert counters["advisories_seen"] == 0
    assert counters["hazard_entries_total"] > 0, "the fleet was never actually advised"


async def test_the_ghost_fleet_puts_no_ack_on_the_wire(bus: Bus, compiler_bus: Bus) -> None:
    await compiler_bus.publish(_signed_advisory(), stream=STREAM_ADVISORIES)
    runner = _runner(bus, modes=(SimMode.GHOST,))
    await runner.run(ticks=150)
    acks = [message async for message in bus.replay(STREAM_ACKS)]
    assert acks == []


async def test_both_fleets_publish_telemetry_distinguishable_by_the_ghost_flag(
    bus: Bus,
) -> None:
    runner = _runner(bus, modes=(SimMode.LIVE, SimMode.GHOST))
    await runner.run(ticks=20)
    frames = [message.parse(Telemetry) async for message in bus.replay(STREAM_TELEMETRY)]
    assert any(frame.ghost for frame in frames)
    assert any(not frame.ghost for frame in frames)
