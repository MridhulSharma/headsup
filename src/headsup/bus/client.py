"""Async Redis Streams wrapper: publish, consume with groups, ack, replay.

Every entry is two fields: `type` (the contract class name) and `data` (its
wire JSON), plus the publish timestamp. That keeps `redis-cli XRANGE` readable
and lets a consumer parse an entry without being told what it is.

    bus = Bus.from_env()
    await bus.publish(hazard, stream=STREAM_HAZARDS)
    async for message in bus.consume(STREAM_HAZARDS, "fusion", "worker-1"):
        hazard = message.parse(Hazard)
        await bus.ack(message)
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, TypeVar

import orjson
from pydantic import BaseModel, ValidationError
from redis import asyncio as aioredis
from redis.exceptions import ResponseError

from headsup.bus.streams import SINGLE_WRITER_STREAMS, stream_for
from headsup.contracts import MODEL_BY_NAME, HeadsUpModel, utcnow

DEFAULT_REDIS_URL = "redis://localhost:6379/0"

FIELD_TYPE = "type"
FIELD_DATA = "data"
FIELD_PUBLISHED_AT = "published_at"

#: Start a consumer group at the beginning of the stream's history.
FROM_START = "0"
#: Start a consumer group at whatever arrives next.
FROM_NEW = "$"
#: XREADGROUP cursor for this consumer's delivered-but-unacked backlog.
PENDING = "0"
#: XREADGROUP cursor for entries no consumer in the group has seen.
NEW = ">"

M = TypeVar("M", bound=BaseModel)


class BusError(RuntimeError):
    """A stream operation failed, or an entry could not be parsed."""


@dataclass(frozen=True, slots=True)
class BusMessage:
    """One entry read off a stream."""

    stream: str
    id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    published_at: str | None = None
    group: str | None = None

    def parse(self, model: type[M] | None = None) -> M:
        """Validate the payload into its contract model.

        With no `model`, the entry's own `type` field picks it, so a consumer
        reading a mixed stream still gets typed objects.
        """
        target = model or MODEL_BY_NAME.get(self.type)
        if target is None:
            raise BusError(
                f"{self.stream} {self.id}: unknown message type {self.type!r}; "
                "pass model=... to parse it"
            )
        try:
            return target.model_validate(self.data)  # type: ignore[return-value]
        except ValidationError as exc:
            raise BusError(f"{self.stream} {self.id}: does not match {target.__name__}") from exc


def _decode(stream: str, entry_id: str, fields: Mapping[str, str], group: str | None) -> BusMessage:
    raw = fields.get(FIELD_DATA, "")
    try:
        data = orjson.loads(raw) if raw else {}
    except orjson.JSONDecodeError as exc:
        raise BusError(f"{stream} {entry_id}: payload is not JSON") from exc
    return BusMessage(
        stream=stream,
        id=entry_id,
        type=fields.get(FIELD_TYPE, ""),
        data=data,
        published_at=fields.get(FIELD_PUBLISHED_AT),
        group=group,
    )


class Bus:
    """Thin async wrapper over the Redis Streams the agents talk through.

    `namespace` prefixes every stream name (`test42:advisories`), which keeps
    an integration test or a parallel replay off the live streams. `writer`
    names this process for the single-writer streams: only
    `Bus(writer="compiler")` may publish to `advisories`.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        namespace: str = "",
        writer: str | None = None,
        client: aioredis.Redis | None = None,
    ) -> None:
        self.url = url or os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
        self.namespace = namespace
        self.writer = writer
        self._client = client or aioredis.Redis.from_url(self.url, decode_responses=True)
        self._owns_client = client is None

    @classmethod
    def from_env(cls, *, namespace: str | None = None, writer: str | None = None) -> Bus:
        """A bus from `REDIS_URL` and `HU_STREAM_NAMESPACE`."""
        return cls(
            os.environ.get("REDIS_URL", DEFAULT_REDIS_URL),
            namespace=os.environ.get("HU_STREAM_NAMESPACE", "") if namespace is None else namespace,
            writer=writer,
        )

    @property
    def client(self) -> aioredis.Redis:
        """The underlying redis client, for the few things this wrapper omits."""
        return self._client

    def key(self, stream: str) -> str:
        """Redis key for a logical stream name, with the namespace applied."""
        return f"{self.namespace}:{stream}" if self.namespace else stream

    async def __aenter__(self) -> Bus:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Release the connection pool, if this bus created it."""
        if self._owns_client:
            await self._client.aclose()

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    # --- publishing ---------------------------------------------------------

    def _check_writer(self, stream: str) -> None:
        required = SINGLE_WRITER_STREAMS.get(stream)
        if required is not None and self.writer != required:
            raise BusError(
                f"only {required} may publish to {stream}; "
                f"this bus is writer={self.writer!r} (see CLAUDE.md hard rules)"
            )

    async def publish(
        self,
        message: HeadsUpModel | BaseModel | Mapping[str, Any],
        *,
        stream: str | None = None,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        """Append one message to a stream and return its entry id.

        `maxlen` caps the stream, which telemetry needs on a long run.
        Trimming is approximate by default because that is what Redis does
        cheaply; pass `approximate=False` when the exact length matters.
        """
        if isinstance(message, BaseModel):
            type_name = type(message).__name__
            payload: dict[str, Any] = (
                message.to_wire()
                if isinstance(message, HeadsUpModel)
                else message.model_dump(mode="json", by_alias=True)
            )
            target = stream or stream_for(message)  # type: ignore[arg-type]
        elif isinstance(message, Mapping):
            if stream is None:
                raise BusError("publishing a plain mapping needs an explicit stream")
            type_name = str(message.get(FIELD_TYPE, "dict"))
            payload = dict(message)
            target = stream
        else:
            raise BusError(f"cannot publish {type(message).__name__}")

        self._check_writer(target)
        fields = {
            FIELD_TYPE: type_name,
            FIELD_DATA: orjson.dumps(payload).decode(),
            FIELD_PUBLISHED_AT: utcnow().isoformat(),
        }
        try:
            entry_id = await self._client.xadd(
                self.key(target), fields, maxlen=maxlen, approximate=approximate
            )
        except ResponseError as exc:
            raise BusError(f"XADD {self.key(target)} failed: {exc}") from exc
        return str(entry_id)

    # --- consumer groups ----------------------------------------------------

    async def ensure_group(
        self, stream: str, group: str, *, start_id: str = FROM_START, mkstream: bool = True
    ) -> bool:
        """Create a consumer group unless it exists. True if this call created it."""
        try:
            await self._client.xgroup_create(
                self.key(stream), group, id=start_id, mkstream=mkstream
            )
            return True
        except ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                return False
            raise BusError(f"XGROUP CREATE {self.key(stream)} {group} failed: {exc}") from exc

    async def read_once(
        self,
        streams: str | Sequence[str],
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 0,
        pending: bool = False,
    ) -> list[BusMessage]:
        """One XREADGROUP: new entries, or this consumer's unacked backlog.

        A group that does not exist yet is created from the start of the
        stream and the read is retried once, so an agent that starts before
        its producer does not have to care about the order.
        """
        names = [streams] if isinstance(streams, str) else list(streams)
        cursor = PENDING if pending else NEW
        request = {self.key(name): cursor for name in names}
        for attempt in (1, 2):
            try:
                response = await self._client.xreadgroup(
                    group, consumer, request, count=count, block=block_ms or None
                )
                break
            except ResponseError as exc:
                if attempt == 1 and "NOGROUP" in str(exc):
                    for name in names:
                        await self.ensure_group(name, group)
                    continue
                raise BusError(f"XREADGROUP {group}/{consumer} failed: {exc}") from exc

        by_key = {self.key(name): name for name in names}
        messages: list[BusMessage] = []
        for key, entries in response or []:
            logical = by_key.get(key, key)
            for entry_id, fields in entries:
                messages.append(_decode(logical, entry_id, fields, group))
        return messages

    async def consume(
        self,
        streams: str | Sequence[str],
        group: str,
        consumer: str,
        *,
        count: int = 10,
        block_ms: int = 1000,
        limit: int | None = None,
        claim_pending: bool = True,
        auto_ack: bool = False,
        stop_when_idle: bool = False,
    ) -> AsyncIterator[BusMessage]:
        """Yield entries for a consumer group, forever by default.

        The consumer's own unacked backlog is replayed first (`claim_pending`),
        so a worker that died mid-message picks it up again on restart. Pass
        `limit` or `stop_when_idle` to make the loop finite, which is what the
        tests and one-shot CLI commands want.
        """
        names = [streams] if isinstance(streams, str) else list(streams)
        for name in names:
            await self.ensure_group(name, group)

        yielded = 0
        pending = claim_pending
        while True:
            batch = await self.read_once(
                names,
                group,
                consumer,
                count=count,
                block_ms=0 if pending else block_ms,
                pending=pending,
            )
            if pending and not batch:
                pending = False
                continue
            if not batch:
                if stop_when_idle:
                    return
                continue
            for message in batch:
                yield message
                if auto_ack:
                    await self.ack(message)
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    async def ack(
        self, message: BusMessage | str, *ids: str, stream: str = "", group: str = ""
    ) -> int:
        """Acknowledge entries: pass a BusMessage, or ids plus stream= and group=."""
        if isinstance(message, BusMessage):
            if message.group is None:
                raise BusError(f"{message.stream} {message.id} was not read through a group")
            stream, group, entry_ids = message.stream, message.group, [message.id, *ids]
        else:
            if not stream or not group:
                raise BusError("acking by id needs stream= and group=")
            entry_ids = [message, *ids]
        try:
            return int(await self._client.xack(self.key(stream), group, *entry_ids))
        except ResponseError as exc:
            raise BusError(f"XACK {self.key(stream)} {group} failed: {exc}") from exc

    async def pending_count(self, stream: str, group: str) -> int:
        """How many entries this group has been delivered but has not acked."""
        try:
            summary = await self._client.xpending(self.key(stream), group)
        except ResponseError as exc:
            if "NOGROUP" in str(exc):
                return 0
            raise BusError(f"XPENDING {self.key(stream)} {group} failed: {exc}") from exc
        if isinstance(summary, Mapping):
            return int(summary.get("pending", 0))
        return int(summary or 0)

    # --- replay -------------------------------------------------------------

    async def replay(
        self,
        stream: str,
        *,
        start: str = "-",
        end: str = "+",
        limit: int | None = None,
        batch: int = 200,
        exclusive: bool = False,
    ) -> AsyncIterator[BusMessage]:
        """Re-read a stream from a stream ID, oldest first, outside any group.

        `start` is a stream ID (`1700000000000-0`) or `-` for the beginning.
        Set `exclusive` to skip the entry at `start`, which is how a consumer
        resumes from the last id it finished.
        """
        key = self.key(stream)
        cursor = f"({start}" if exclusive and start != "-" else start
        seen = 0
        while True:
            size = batch if limit is None else min(batch, limit - seen)
            if size <= 0:
                return
            try:
                entries = await self._client.xrange(key, min=cursor, max=end, count=size)
            except ResponseError as exc:
                raise BusError(f"XRANGE {key} failed: {exc}") from exc
            if not entries:
                return
            for entry_id, fields in entries:
                yield _decode(stream, entry_id, fields, None)
                seen += 1
                if limit is not None and seen >= limit:
                    return
            cursor = f"({entries[-1][0]}"

    async def last_id(self, stream: str) -> str | None:
        """Entry id of the newest entry, or None if the stream is empty or absent."""
        entries = await self._client.xrevrange(self.key(stream), count=1)
        return str(entries[0][0]) if entries else None

    async def length(self, stream: str) -> int:
        return int(await self._client.xlen(self.key(stream)))

    async def delete_streams(self, streams: Iterable[str]) -> int:
        """Delete streams outright: test teardown and scenario resets."""
        keys = [self.key(name) for name in streams]
        return int(await self._client.delete(*keys)) if keys else 0
