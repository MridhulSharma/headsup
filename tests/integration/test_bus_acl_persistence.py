"""The ACL users survive a Redis restart, because they are on disk.

Needs `uv run hu infra up`. An ACL that only lives in memory is a session, not
a control: one `docker compose restart redis` and the `agent` user is gone,
which means every agent falls back to the open `default` user and the
single-writer rule quietly stops existing.

`docker-compose.yml` configures `--aclfile /data/users.acl` in the named
volume, and `hu bus acl apply` runs `ACL SAVE`. These tests use `ACL LOAD` -
"discard what is in memory and re-read the file" - which is the restart path
without stopping the container the other integration tests are using. The
`docker compose restart` itself is in the step's manual output.

Everything here applies the *development* passwords, which is exactly what
`hu bus acl apply` writes, so the file is left in the documented state however
the run ends.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import redis.exceptions
from redis import asyncio as aioredis

from headsup.bus import STREAM_ADVISORIES, Bus
from headsup.bus.acl import (
    DEFAULT_PASSWORD,
    Role,
    acl_file,
    apply,
    describe,
    load,
    remove,
    save,
)

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
HINT = "redis unreachable - run `uv run hu infra up`"
NAMESPACE = "test_acl_persistence"


def dev_url(role: Role) -> str:
    _scheme, _, rest = REDIS_URL.partition("://")
    host_and_db = rest.rpartition("@")[2]
    return f"redis://{role.value}:{DEFAULT_PASSWORD[role]}@{host_and_db}"


@pytest.fixture
async def admin() -> AsyncIterator[aioredis.Redis]:
    """A client as `default`, with the dev-password users saved to disk.

    Both the setup and the teardown leave the aclfile holding exactly what
    `hu bus acl apply` would write.
    """
    client = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except redis.exceptions.ConnectionError as exc:
        await client.aclose()
        pytest.fail(f"{HINT}: {REDIS_URL} ({exc})")
    if not await acl_file(client):
        await client.aclose()
        pytest.fail(
            "this Redis has no aclfile: recreate it with `uv run hu infra up` "
            "so docker-compose.yml's --aclfile takes effect"
        )
    await apply(client, passwords=DEFAULT_PASSWORD, persist=True)
    try:
        yield client
    finally:
        await apply(client, passwords=DEFAULT_PASSWORD, persist=True)
        await client.delete(f"{NAMESPACE}:{STREAM_ADVISORIES}")
        await client.aclose()


async def test_the_server_is_configured_with_an_aclfile(admin: aioredis.Redis) -> None:
    assert (await acl_file(admin)).endswith("users.acl")


async def test_applying_with_persist_reports_where_it_saved(admin: aioredis.Redis) -> None:
    applied = await apply(admin, passwords=DEFAULT_PASSWORD, persist=True)
    assert applied.persisted
    assert applied.aclfile.endswith("users.acl")
    assert set(applied.users) == {role.value for role in Role}


async def test_applying_without_persist_does_not_touch_the_file(
    admin: aioredis.Redis,
) -> None:
    """A test with throwaway passwords must not overwrite the saved ACL."""
    applied = await apply(
        admin, passwords={Role.AGENT: "throwaway-pw", Role.COMPILER: "throwaway-pw"}
    )
    assert not applied.persisted

    # The file still holds the dev passwords, so reloading restores them.
    await load(admin)
    bus = Bus(dev_url(Role.AGENT), namespace=NAMESPACE)
    try:
        assert await bus.ping()
    finally:
        await bus.close()


async def test_the_users_come_back_after_the_in_memory_acl_is_discarded(
    admin: aioredis.Redis,
) -> None:
    """`ACL LOAD` is the restart path: memory is dropped, the file is re-read."""
    assert await remove(admin) == len(Role)
    assert not [line for line in await describe(admin) if line.split()[1] in {"agent", "compiler"}]

    await load(admin)

    users = {line.split()[1] for line in await describe(admin)}
    assert {"agent", "compiler"} <= users


async def test_the_agent_still_gets_noperm_after_a_reload(admin: aioredis.Redis) -> None:
    """The headline guarantee: the single-writer rule survives the round trip.

    This is what `docker compose restart redis` does to the ACL, minus the
    container stop.
    """
    await save(admin)
    await remove(admin)
    await load(admin)

    agent = Bus(dev_url(Role.AGENT), namespace=NAMESPACE, writer="compiler")
    compiler = Bus(dev_url(Role.COMPILER), namespace=NAMESPACE, writer="compiler")
    try:
        with pytest.raises(redis.exceptions.NoPermissionError):
            await agent.client.xadd(agent.key(STREAM_ADVISORIES), {"type": "probe"})

        # ...and the compiler still may, so the reload restored both users.
        assert await compiler.client.xadd(compiler.key(STREAM_ADVISORIES), {"type": "probe"})
    finally:
        await agent.close()
        await compiler.close()


async def test_the_selector_survives_the_round_trip(admin: aioredis.Redis) -> None:
    """A reloaded `agent` can still consume advisories, not just be refused.

    The selector is the part most likely to be lost in a save-and-reload, so
    it is checked on the far side rather than assumed.
    """
    await save(admin)
    await remove(admin)
    await load(admin)

    key = f"{NAMESPACE}:{STREAM_ADVISORIES}"
    await admin.xadd(key, {"type": "probe"})
    agent = Bus(dev_url(Role.AGENT), namespace=NAMESPACE)
    try:
        assert await agent.client.xrange(key)
        await agent.client.xgroup_create(key, "operators", id="0")
        assert await agent.client.xreadgroup("operators", "op_a", {key: ">"})
        with pytest.raises(redis.exceptions.NoPermissionError):
            await agent.client.xadd(key, {"type": "probe"})
    finally:
        await agent.close()
