"""What every feed adapter shares: a clock, a raw payload, and one parse path.

The important rule here is that `fetch` and `parse` are separate. `fetch` does
the I/O and hands back `RawPayload` objects; `parse` is a pure function from
one payload to `CivicEvent`s. Live mode records what `fetch` returned and then
parses it; replay mode reads those same recorded payloads and calls the same
`parse`. That is what makes a replayed event identical to the live one.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx

from headsup.contracts import CivicEvent, HeadsUpModel, RawRef, Source, UtcDatetime

#: Sent on every outbound request unless `HU_HTTP_USER_AGENT` overrides it. NWS
#: turns away anonymous clients and asks for a contact, so say who we are.
DEFAULT_USER_AGENT = (
    "headsup-research/0.1 (robotaxi advisory research prototype; "
    "https://github.com/MridhulSharma/headsup)"
)

DEFAULT_TIMEOUT = 20.0

_SLUG_SPLIT = re.compile(r"[^a-z0-9]+")


class IngestError(RuntimeError):
    """A feed could not be fetched, or a payload could not be parsed."""


def user_agent() -> str:
    """The descriptive User-Agent this process identifies itself with."""
    return os.environ.get("HU_HTTP_USER_AGENT", "").strip() or DEFAULT_USER_AGENT


def slugify(text: str, fallback: str) -> str:
    """Feed type text to a `Kind`: `"Flash Flood Warning"` -> `flash_flood_warning`.

    Feed text is data, so this is deliberately lossy: whatever comes back is
    forced into the narrow `Kind` alphabet or replaced by `fallback`.
    """
    slug = _SLUG_SPLIT.sub("_", str(text).strip().lower()).strip("_")[:64].rstrip("_")
    return slug if slug and slug[0].isalnum() else fallback


def derive_id(prefix: str, *parts: str) -> str:
    """A stable id for the same observation, however often it is parsed.

    Replay has to produce the identifier live mode produced, so ids are hashed
    from the payload's own natural key instead of being drawn at random.
    """
    digest = hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=6).hexdigest()
    return f"{prefix}_{digest}"


def parse_timestamp(value: Any) -> datetime | None:
    """An ISO-8601 timestamp from a feed, normalised to aware UTC, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


@runtime_checkable
class Clock(Protocol):
    """Time and waiting, injected so replay can run without really waiting."""

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """Wall-clock time and real `asyncio.sleep`."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class FakeClock:
    """A clock that only moves when something sleeps.

    `slept` keeps every requested duration, which is how the replay tests check
    that a 60x run asked for the right gaps without taking an hour.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start.astimezone(UTC)
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if seconds > 0:
            self._now += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        """Move time on without recording a sleep."""
        self._now += timedelta(seconds=seconds)


class RawPayload(HeadsUpModel):
    """One response body exactly as a feed returned it, plus when we asked.

    This is the unit the recorder writes and the replayer reads. `ref` is a
    pointer the parsed events cite; it is minted at fetch time and travels with
    the payload, so an event's `raw_ref` is the same live and on replay.
    """

    source: Source
    ref: RawRef
    url: str
    fetched_at: UtcDatetime
    body: dict[str, Any]

    def item_ref(self, index: int) -> str:
        """Pointer to one item inside this payload, e.g. `nws/...#3`."""
        return f"{self.ref}#{index}"


def make_ref(source: Source, fetched_at: datetime, seq: int) -> str:
    """`nws/20260420T185000Z/0`: feed, fetch time, and this fetch's number."""
    stamp = fetched_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{source.value}/{stamp}/{seq}"


class Adapter(ABC):
    """One public feed: fetch raw payloads, parse them into `CivicEvent`s."""

    source: ClassVar[Source]

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Clock | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.clock: Clock = clock or SystemClock()
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._seq = 0

    @property
    def client(self) -> httpx.AsyncClient:
        """The shared HTTP client, created on first use with our User-Agent."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": user_agent(), "Accept": "application/geo+json"},
                follow_redirects=True,
            )
        return self._client

    async def __aenter__(self) -> Adapter:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def next_ref(self, fetched_at: datetime) -> str:
        """Mint the pointer for the payload being fetched right now."""
        ref = make_ref(self.source, fetched_at, self._seq)
        self._seq += 1
        return ref

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One GET returning a JSON object, with feed errors wrapped."""
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise IngestError(f"{self.source.value}: GET {url} failed: {exc}") from exc
        except ValueError as exc:
            raise IngestError(f"{self.source.value}: GET {url} did not return JSON") from exc
        if not isinstance(body, dict):
            raise IngestError(f"{self.source.value}: GET {url} returned {type(body).__name__}")
        return body

    @abstractmethod
    async def fetch(self) -> list[RawPayload]:
        """Ask the feed for its current state. The only I/O in an adapter."""

    @abstractmethod
    def parse(self, payload: RawPayload) -> list[CivicEvent]:
        """Turn one raw payload into events. Pure: same input, same output."""

    async def poll(self) -> tuple[list[RawPayload], list[CivicEvent]]:
        """Fetch and parse in one step: what live mode and the recorder want."""
        payloads = await self.fetch()
        events: list[CivicEvent] = []
        for payload in payloads:
            events.extend(self.parse(payload))
        return payloads, events
