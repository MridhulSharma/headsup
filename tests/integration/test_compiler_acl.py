"""The single-writer rule, enforced by Redis rather than promised by Python.

Needs `uv run hu infra up`. The ACL users are created at the start and deleted
afterwards, and every test works inside its own stream namespace, so a demo
running on the same Redis is untouched.

The headline test is `test_the_agent_user_gets_noperm_writing_to_advisories`:
a process that claims `writer="compiler"` but connects as the `agent` user is
still refused by the server. That is the difference between a convention and a
control.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import redis.exceptions
from redis import asyncio as aioredis

from headsup.agents.compiler import Compiler, CompilerService, Decision, InMemoryApprovals
from headsup.bus import ALL_STREAMS, STREAM_ADVISORIES, STREAM_AUDIT, Bus, BusError
from headsup.bus.acl import Role, apply, describe, lock_default_args, remove, restore, snapshot
from headsup.contracts import Advisory, CriticVerdict, Verdict
from samples import ADVISORY, HAZARD, PROPOSAL

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
HINT = "redis unreachable - run `uv run hu infra up`"

PASSWORDS = {Role.COMPILER: "compiler-test-pw", Role.AGENT: "agent-test-pw"}

#: The S1 proposal's own clock. The approval has to be granted at the same
#: instant the compiler judges at, or its TTL has already run out.
COMPILE_NOW = PROPOSAL.valid_from


def role_url(role: Role) -> str:
    """REDIS_URL with one ACL user's credentials spliced in."""
    _scheme, _, rest = REDIS_URL.partition("://")
    host_and_db = rest.rpartition("@")[2]
    return f"redis://{role.value}:{PASSWORDS[role]}@{host_and_db}"


@pytest.fixture
async def admin() -> AsyncIterator[aioredis.Redis]:
    """A client as the `default` user, used to apply and remove the ACL."""
    client = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except redis.exceptions.ConnectionError as exc:
        await client.aclose()
        pytest.fail(f"{HINT}: {REDIS_URL} ({exc})")
    await apply(client, passwords=PASSWORDS)
    try:
        yield client
    finally:
        await remove(client)
        await client.aclose()


@pytest.fixture
def namespace() -> str:
    return f"test_{uuid4().hex[:8]}"


@pytest.fixture
async def compiler_bus(admin: aioredis.Redis, namespace: str) -> AsyncIterator[Bus]:
    """A bus as the `compiler` user: allowed to write advisories."""
    bus = Bus(role_url(Role.COMPILER), namespace=namespace, writer="compiler")
    try:
        yield bus
    finally:
        await admin.delete(*[bus.key(name) for name in ALL_STREAMS])
        await bus.close()


@pytest.fixture
async def agent_bus(admin: aioredis.Redis, namespace: str) -> AsyncIterator[Bus]:
    """A bus as the `agent` user, claiming to be the compiler.

    The client-side guard is deliberately satisfied here so that what the test
    observes is the server's answer, not `Bus`'s own check.
    """
    bus = Bus(role_url(Role.AGENT), namespace=namespace, writer="compiler")
    try:
        yield bus
    finally:
        await bus.close()


def approve(proposal_id: str) -> CriticVerdict:
    return CriticVerdict(
        proposal_id=proposal_id, verdict=Verdict.APPROVE, reasons=["integration test"]
    )


# --- the control ------------------------------------------------------------


async def test_the_agent_user_gets_noperm_writing_to_advisories(agent_bus: Bus) -> None:
    """redis-py maps the server's `NOPERM` reply to NoPermissionError."""
    with pytest.raises(redis.exceptions.NoPermissionError) as caught:
        await agent_bus.client.xadd(agent_bus.key(STREAM_ADVISORIES), {"type": "probe"})
    assert "permission" in str(caught.value).lower()


async def test_the_agent_user_gets_noperm_through_the_bus_wrapper(agent_bus: Bus) -> None:
    """Even a bus that claims writer="compiler" is refused by the server."""
    with pytest.raises(BusError, match="XADD"):
        await agent_bus.publish(HAZARD, stream=STREAM_ADVISORIES)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("xdel", id="XDEL"),
        pytest.param("xtrim", id="XTRIM"),
        pytest.param("xsetid", id="XSETID"),
    ],
)
async def test_the_agent_user_may_not_mutate_the_advisory_log(
    compiler_bus: Bus, agent_bus: Bus, command: str
) -> None:
    """Appending is not the only way to rewrite history.

    A reader that could `XDEL` or `XTRIM` could make a published advisory
    disappear, and `XSETID` could make the next one look older than it is.
    The selector names the commands it allows, so all three are refused.
    """
    entry_id = await compiler_bus.publish(HAZARD, stream=STREAM_ADVISORIES)
    key = agent_bus.key(STREAM_ADVISORIES)
    calls = {
        "xdel": lambda: agent_bus.client.xdel(key, entry_id),
        "xtrim": lambda: agent_bus.client.xtrim(key, maxlen=0),
        "xsetid": lambda: agent_bus.client.execute_command("XSETID", key, "9999999-0"),
    }
    with pytest.raises(redis.exceptions.NoPermissionError):
        await calls[command]()
    assert await compiler_bus.length(STREAM_ADVISORIES) == 1


async def test_the_agent_user_may_run_a_consumer_group_on_advisories(
    compiler_bus: Bus, agent_bus: Bus
) -> None:
    """An operator has to consume the stream it is supposed to act on.

    `XREADGROUP`, `XACK` and `XGROUP` are write-flagged in Redis because they
    move the group cursor, so a plain read-only grant would lock operators out
    of the advisories they exist to answer. The selector allows exactly these.
    """
    await compiler_bus.publish(ADVISORY, stream=STREAM_ADVISORIES)

    received = [
        message
        async for message in agent_bus.consume(STREAM_ADVISORIES, "operators", "op_a", limit=1)
    ]
    assert len(received) == 1
    assert received[0].type == "Advisory"
    assert received[0].parse(Advisory).properties.id == ADVISORY.properties.id

    # And the group bookkeeping an at-least-once consumer needs.
    assert await agent_bus.ack(received[0]) == 1
    assert await agent_bus.pending_count(STREAM_ADVISORIES, "operators") == 0


async def test_the_agent_user_may_read_advisories_without_a_group(
    compiler_bus: Bus, agent_bus: Bus
) -> None:
    """XRANGE and XREAD still work, for a reader that wants no cursor."""
    await compiler_bus.publish(ADVISORY, stream=STREAM_ADVISORIES)
    assert await agent_bus.length(STREAM_ADVISORIES) == 1
    assert await agent_bus.last_id(STREAM_ADVISORIES)
    seen = [message async for message in agent_bus.replay(STREAM_ADVISORIES)]
    assert len(seen) == 1
    raw = await agent_bus.client.xread({agent_bus.key(STREAM_ADVISORIES): "0"})
    assert raw


async def test_the_compiler_user_may_write_advisories(compiler_bus: Bus) -> None:
    entry_id = await compiler_bus.publish(HAZARD, stream=STREAM_ADVISORIES)
    assert "-" in entry_id
    assert await compiler_bus.length(STREAM_ADVISORIES) == 1


async def test_the_agent_user_may_write_every_other_stream(agent_bus: Bus) -> None:
    for stream in ("candidates", "hazards", "proposals", "verdicts", "acks", "audit"):
        assert await agent_bus.publish(HAZARD, stream=stream)
    assert await agent_bus.publish(HAZARD, stream="events.nws")


async def test_neither_user_may_reconfigure_the_server(agent_bus: Bus) -> None:
    """`-@dangerous`: an agent cannot rewrite the ACL that constrains it."""
    with pytest.raises(redis.exceptions.NoPermissionError):
        await agent_bus.client.execute_command("ACL", "SETUSER", "agent", "~*")
    with pytest.raises(redis.exceptions.NoPermissionError):
        await agent_bus.client.execute_command("CONFIG", "SET", "maxmemory", "0")


async def test_applying_the_acl_twice_leaves_the_same_rules(admin: aioredis.Redis) -> None:
    """Every user is `reset` first, so `hu bus acl apply` is idempotent."""
    before = sorted(line for line in await describe(admin) if "compiler" in line)
    await apply(admin, passwords=PASSWORDS)
    assert sorted(line for line in await describe(admin) if "compiler" in line) == before


async def test_locking_the_default_user_closes_the_last_way_in(
    admin: aioredis.Redis, namespace: str
) -> None:
    """With `--lock-default`, `compiler` is the only writer on the server.

    The `default` rules are snapshotted and restored, because the other
    integration tests connect as `default` and expect it open.
    """
    original = await snapshot(admin, "default")
    assert original is not None, "no default user to snapshot"
    try:
        await admin.execute_command("ACL", "SETUSER", *lock_default_args())
        with pytest.raises(redis.exceptions.NoPermissionError):
            await admin.xadd(f"{namespace}:{STREAM_ADVISORIES}", {"type": "probe"})
        # Still able to read it, and to write every other stream.
        assert await admin.xadd(f"{namespace}:{STREAM_AUDIT}", {"type": "probe"})
    finally:
        await restore(admin, original)
        await admin.delete(f"{namespace}:{STREAM_AUDIT}")
    assert await admin.xadd(f"{namespace}:{STREAM_ADVISORIES}", {"type": "probe"})
    await admin.delete(f"{namespace}:{STREAM_ADVISORIES}")


# --- the service on top of it ----------------------------------------------


async def test_the_service_publishes_an_advisory_and_an_audit_record(
    compiler_bus: Bus,
) -> None:
    approvals = InMemoryApprovals()
    approvals.grant(PROPOSAL.id, "duty_officer_7", now=COMPILE_NOW)
    service = CompilerService(
        compiler_bus, Compiler(approvals=approvals, clock=lambda: COMPILE_NOW)
    )
    outcome = await service.publish(PROPOSAL, approve(PROPOSAL.id))

    assert outcome.ok, outcome.result.reasons
    assert outcome.audit.decision is Decision.PUBLISHED
    assert outcome.audit.stream_id == outcome.stream_id
    assert await compiler_bus.length(STREAM_ADVISORIES) == 1
    assert await compiler_bus.length(STREAM_AUDIT) == 1

    published = [message async for message in compiler_bus.replay(STREAM_ADVISORIES)]
    assert published[0].type == "Advisory"
    assert published[0].data["properties"]["id"] == "adv_flood_1"


async def test_a_rejected_proposal_writes_audit_and_nothing_else(
    compiler_bus: Bus,
) -> None:
    """A refusal leaves as much evidence as a publication."""
    service = CompilerService(compiler_bus, Compiler(clock=lambda: COMPILE_NOW))
    outcome = await service.publish(PROPOSAL, approve(PROPOSAL.id))

    assert not outcome.ok
    assert outcome.stream_id is None
    assert outcome.audit.decision is Decision.REJECTED
    assert await compiler_bus.length(STREAM_ADVISORIES) == 0
    assert await compiler_bus.length(STREAM_AUDIT) == 1

    from headsup.agents.compiler import CompileAudit

    records = [message async for message in compiler_bus.replay(STREAM_AUDIT)]
    record = records[0].parse(CompileAudit)
    assert record.proposal_id == PROPOSAL.id
    assert record.two_key_required
    assert record.advisory_id is None


async def test_the_service_audits_a_write_it_could_not_make(
    admin: aioredis.Redis, namespace: str
) -> None:
    """An ACL-refused write is recorded as PUBLISH_FAILED, never as published."""
    approvals = InMemoryApprovals()
    approvals.grant(PROPOSAL.id, "duty_officer_7", now=COMPILE_NOW)
    # The agent user, lying about being the compiler: the client-side guard
    # passes and the server refuses.
    bus = Bus(role_url(Role.AGENT), namespace=namespace, writer="compiler")
    service = CompilerService(bus, Compiler(approvals=approvals, clock=lambda: COMPILE_NOW))
    try:
        with pytest.raises(BusError):
            await service.publish(PROPOSAL, approve(PROPOSAL.id))

        from headsup.agents.compiler import CompileAudit

        records = [message async for message in bus.replay(STREAM_AUDIT)]
        assert len(records) == 1
        record = records[0].parse(CompileAudit)
        assert record.decision is Decision.PUBLISH_FAILED
        assert record.stream_id is None
        assert await bus.length(STREAM_ADVISORIES) == 0
    finally:
        await admin.delete(*[bus.key(name) for name in ALL_STREAMS])
        await bus.close()
