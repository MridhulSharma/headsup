"""Redis ACL users: the compiler writes `advisories`, nobody else does.

`CLAUDE.md` says only `src/headsup/agents/compiler` may write to the
`advisories` stream. Python can promise that (`Bus(writer=...)` refuses), but a
promise in the same process as the code it constrains is not a control. This
module moves the constraint into Redis, where a buggy or talked-into agent
cannot route around it:

* **compiler** - read and write every stream, including `advisories`;
* **agent** - read and write every stream except `advisories`, which it may
  read and consume but never append to, delete from or trim.

Redis has no "deny one key" rule, so the agent's access is granted key by key.
`advisories` is the interesting one, because "read it" is not the same as "may
only use read commands": `XREADGROUP`, `XACK` and `XGROUP` are flagged *write*
in Redis (they move the group cursor), so a read-only `%R~advisories` would
lock an operator out of consuming the stream it is supposed to act on.

So `advisories` is granted through an **ACL selector** instead - an extra
permission set in parentheses that pairs its own key patterns with its own
command list:

    (~advisories ~*:advisories +xread +xrange +xreadgroup +xack +xgroup ...)

The selector grants full key access, and then names every command it allows.
Anything it does not name is refused, so `XADD`, `XDEL`, `XTRIM` and `XSETID`
on `advisories` all fail with `NOPERM` while a consumer group works normally.
The allow-list is the safe direction: a stream command added in a future Redis
is denied until someone adds it here on purpose.

Persistence: the rules live in memory until `ACL SAVE` writes them to the
`aclfile` configured in `docker-compose.yml`, so `apply(persist=True)` (what
`hu bus acl apply` does) is what makes them survive `docker compose restart`.

    import redis.asyncio as aioredis
    client = aioredis.Redis.from_url("redis://localhost:6379/0")
    await apply(client, persist=True)        # or `hu bus acl apply`
    bus = Bus(url_for(Role.COMPILER), writer="compiler")
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote, urlsplit, urlunsplit

from redis import asyncio as aioredis
from redis.exceptions import ResponseError

from headsup.bus.streams import (
    CORE_STREAMS,
    EVENTS_PREFIX,
    SINGLE_WRITER_STREAMS,
    STREAM_ADVISORIES,
)


class AclError(RuntimeError):
    """An ACL operation failed, or the server is not configured for one."""


class Role(StrEnum):
    """The two ACL users Heads Up connects as."""

    COMPILER = "compiler"
    AGENT = "agent"


#: Per-role password environment variables. `url_for` only adds credentials to
#: a URL when the role's variable is set, so a dev box with no ACLs applied
#: keeps connecting as `default` and nothing changes for it.
ENV_PASSWORD: dict[Role, str] = {
    Role.COMPILER: "HU_REDIS_COMPILER_PASSWORD",
    Role.AGENT: "HU_REDIS_AGENT_PASSWORD",
}

#: Development passwords, used by `hu bus acl apply` when the environment is
#: silent. Published on purpose, like the development signing key: the ACL is
#: here to stop an agent writing the wrong stream, not to keep a secret from
#: anyone who can already reach the Redis port.
DEFAULT_PASSWORD: dict[Role, str] = {
    Role.COMPILER: "compiler-dev",
    Role.AGENT: "agent-dev",
}

#: Commands both users get: everything a stream client needs, and nothing
#: administrative. `client|setinfo` is added back because redis-py sends it on
#: every connect (it tolerates a refusal, but there is no reason to refuse).
COMMAND_RULES = ("+@all", "-@dangerous", "+client|setinfo")

#: Redis factory settings for the `default` user, used to undo `lock_default`.
DEFAULT_USER_RULES = ("on", "nopass", "sanitize-payload", "~*", "&*", "+@all")

#: What a reader of `advisories` may do: read the log, and run a consumer
#: group on it. `XREADGROUP`, `XACK`, `XGROUP` and the claim commands are
#: write-flagged in Redis because they move the group cursor, so they have to
#: be named here rather than covered by a read-only key pattern.
CONSUMER_COMMANDS = (
    "+xread",
    "+xrange",
    "+xrevrange",
    "+xlen",
    "+xinfo",
    "+xgroup",
    "+xreadgroup",
    "+xack",
    "+xpending",
    "+xclaim",
    "+xautoclaim",
)

#: Stream commands that mutate the log itself, listed so a test can assert
#: they are absent from `CONSUMER_COMMANDS`. Only the compiler may run these
#: against `advisories`.
APPEND_COMMANDS = ("xadd", "xdel", "xtrim", "xsetid")


def _patterns(stream: str, *, prefix: str = "~") -> list[str]:
    """Key patterns for one logical stream, bare and namespaced.

    `Bus` prefixes a namespace (`test_ab12:advisories`) for tests and parallel
    replays, so every stream needs both forms.
    """
    return [f"{prefix}{stream}", f"{prefix}*:{stream}"]


def key_rules(role: Role) -> list[str]:
    """The root key patterns for one role, in the order they are applied.

    The single-writer streams are deliberately absent from the agent's root
    set: they are granted through `selector_rules` instead, which is the only
    way to allow consuming a stream without also allowing appends to it.
    """
    if role is Role.COMPILER:
        # The privileged writer: every stream, including the single-writer one.
        return ["~*"]
    rules: list[str] = [*_patterns(f"{EVENTS_PREFIX}.*")]
    for stream in CORE_STREAMS:
        if stream in SINGLE_WRITER_STREAMS:
            continue
        rules.extend(_patterns(stream))
    return rules


def selector_rules(role: Role) -> list[str]:
    """Extra ACL selectors for one role, each already wrapped in parentheses.

    One selector per single-writer stream: full key access paired with the
    consumer command allow-list, so the stream can be read and consumed but
    not appended to, deleted from or trimmed.
    """
    if role is Role.COMPILER:
        return []
    selectors: list[str] = []
    for stream in SINGLE_WRITER_STREAMS:
        rules = [*_patterns(stream), *CONSUMER_COMMANDS]
        selectors.append(f"({' '.join(rules)})")
    return selectors


def setuser_args(role: Role, password: str) -> list[str]:
    """The full `ACL SETUSER` argument list for one role.

    Pure, so `tests/unit/test_bus_acl.py` can assert on the rules without a
    Redis to apply them to.
    """
    return [
        role.value,
        "reset",
        "on",
        f">{password}",
        "resetchannels",
        *key_rules(role),
        *COMMAND_RULES,
        *selector_rules(role),
    ]


def lock_default_args() -> list[str]:
    """`ACL SETUSER` arguments that take `advisories` away from `default`.

    Opt-in, because it is the one rule with a cost: `default` ends up with the
    same stream-only key access as `agent`, so a bare `redis-cli` can read
    every stream and write all but `advisories`. Applying it makes `compiler`
    the only user on the server that can write the stream; leaving it off
    keeps the open `default` user the other integration tests use.

    Two things this deliberately does *not* do. It does not grant a wider key
    pattern alongside, because there is no "allow everything except this key"
    rule in Redis and `~*:*` would hand namespaced advisories straight back as
    writable. And it does not restrict `default`'s commands: it keeps `+@all`,
    so `ACL` and `CONFIG` stay available and this lock can always be undone
    from `redis-cli`. Taking `@admin` from the last administrative user is a
    locked door with the key inside - and now that the ACL is persisted to an
    aclfile, even restarting the server would not undo it.
    """
    return [
        "default",
        "reset",
        "on",
        "nopass",
        "sanitize-payload",
        "&*",
        *key_rules(Role.AGENT),
        "+@all",
        *selector_rules(Role.AGENT),
    ]


def password_for(role: Role) -> str:
    """The role's password: its environment variable, else the dev default."""
    return os.environ.get(ENV_PASSWORD[role], "").strip() or DEFAULT_PASSWORD[role]


def url_for(role: Role | str, base_url: str | None = None) -> str:
    """`base_url` with this role's credentials, if the environment set any.

    With no password in the environment the URL comes back untouched, so an
    agent on a box with no ACLs applied still connects as `default`.
    """
    chosen = Role(role)
    url = base_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    password = os.environ.get(ENV_PASSWORD[chosen], "").strip()
    if not password:
        return url
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    netloc = f"{quote(chosen.value)}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True, slots=True)
class Applied:
    """What one `apply` call did, for the CLI to print and tests to assert on."""

    rules: dict[str, list[str]]
    #: The `aclfile` the server is configured with, or "" if it has none.
    aclfile: str = ""
    #: True once `ACL SAVE` has written these rules to that file.
    persisted: bool = False

    @property
    def users(self) -> tuple[str, ...]:
        return tuple(self.rules)

    def __len__(self) -> int:
        return len(self.rules)

    def __contains__(self, user: str) -> bool:
        return user in self.rules


async def apply(
    client: aioredis.Redis,
    *,
    passwords: Mapping[Role, str] | None = None,
    lock_default: bool = False,
    persist: bool = False,
) -> Applied:
    """Create or update the `compiler` and `agent` users.

    Idempotent: every user is `reset` first, so applying twice leaves the same
    ACL. `persist` additionally runs `ACL SAVE`, which is what makes the rules
    survive a restart; it is off by default so that a test applying throwaway
    passwords cannot overwrite the operator's saved aclfile.
    """
    applied: dict[str, list[str]] = {}
    for role in Role:
        password = (passwords or {}).get(role) or password_for(role)
        args = setuser_args(role, password)
        await _setuser(client, args)
        applied[role.value] = args
    if lock_default:
        args = lock_default_args()
        await _setuser(client, args)
        applied["default"] = args

    path = await acl_file(client)
    if not persist:
        return Applied(rules=applied, aclfile=path)
    return Applied(rules=applied, aclfile=await save(client), persisted=True)


async def acl_file(client: aioredis.Redis) -> str:
    """The `aclfile` this server is configured with, or "" if it has none.

    Without one, every `ACL SETUSER` lives in memory only and is lost on
    restart, so this is the difference between a control and a session.
    """
    try:
        config = await client.config_get("aclfile")
    except ResponseError:  # pragma: no cover - needs a user without @admin
        return ""
    return str(config.get("aclfile", "") or "")


async def save(client: aioredis.Redis) -> str:
    """`ACL SAVE`: write the in-memory rules to the aclfile. Returns its path."""
    path = await acl_file(client)
    if not path:
        raise AclError(
            "this Redis has no aclfile, so ACL users would be lost on restart. "
            "Set one in docker-compose.yml (redis-server --aclfile /data/users.acl) "
            "and run `hu infra up` to recreate the container."
        )
    try:
        await client.execute_command("ACL", "SAVE")
    except ResponseError as exc:
        raise AclError(f"ACL SAVE failed: {exc}") from exc
    return path


async def load(client: aioredis.Redis) -> str:
    """`ACL LOAD`: discard the in-memory rules and re-read the aclfile.

    This is the restart path without the restart, which is how
    `tests/integration/test_bus_acl_persistence.py` proves the users survive
    one without stopping the container other tests are using.
    """
    path = await acl_file(client)
    if not path:
        raise AclError("this Redis has no aclfile to load from")
    try:
        await client.execute_command("ACL", "LOAD")
    except ResponseError as exc:
        raise AclError(f"ACL LOAD failed: {exc}") from exc
    return path


async def remove(client: aioredis.Redis, *, roles: Sequence[Role] | None = None) -> int:
    """Delete the Heads Up ACL users. Returns how many were removed."""
    names = [role.value for role in (roles or tuple(Role))]
    try:
        return int(await client.acl_deluser(*names))
    except ResponseError as exc:  # pragma: no cover - needs an ACL-less server
        raise AclError(f"ACL DELUSER {' '.join(names)} failed: {exc}") from exc


async def describe(client: aioredis.Redis) -> list[str]:
    """`ACL LIST`: one line per user, as the server has it."""
    lines = await client.execute_command("ACL", "LIST")
    return [line if isinstance(line, str) else line.decode() for line in lines]


async def snapshot(client: aioredis.Redis, user: str) -> list[str] | None:
    """The `ACL LIST` rules for one user, ready to pass back to `restore`."""
    for line in await describe(client):
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "user" and parts[1] == user:
            return parts[1:]
    return None


async def restore(client: aioredis.Redis, rules: Sequence[str]) -> None:
    """Re-apply a snapshot taken by `snapshot`."""
    user, *rest = rules
    await _setuser(client, [user, "reset", *rest])


async def _setuser(client: aioredis.Redis, args: Sequence[str]) -> None:
    try:
        await client.execute_command("ACL", "SETUSER", *args)
    except ResponseError as exc:
        raise AclError(f"ACL SETUSER {' '.join(args)} failed: {exc}") from exc


#: The stream the ACL exists to protect, and the only user allowed to write it.
PROTECTED_STREAM = STREAM_ADVISORIES
PROTECTED_WRITER = SINGLE_WRITER_STREAMS[STREAM_ADVISORIES]
