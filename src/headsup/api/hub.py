"""Fan-out to every connected viewer, and the snapshot a late joiner gets.

One hub per server. Publishers never await a slow socket: each subscriber owns
a bounded queue and its own writer task, and a queue that fills drops its
oldest frame rather than back-pressuring the simulator tick loop. Telemetry is
the only frame that arrives fast enough for that to happen, and a dropped
telemetry frame costs the viewer one position update out of several per second.

**What is retained, and why.** A viewer that connects halfway through a run
would otherwise see an empty map until the next advisory, which may be minutes
away at 1x. So the hub keeps the current `scenario_state`, every advisory still
in force, the latest counters, and a bounded tail of rail events, and replays
them on connect in pipeline order. The retained set is the server's own view of
what it has published, held per run and cleared when a scenario is reset.

The hub holds no sockets of its own: `Subscriber` is an async iterator of
frames, and whoever owns the WebSocket decides how to write them. That keeps
this module testable with no FastAPI and no network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Iterable
from typing import Any

from headsup.api.frames import (
    FRAME_ADVISORY,
    FRAME_COUNTERS,
    FRAME_SCENARIO_STATE,
    RAIL_STAGES,
)

log = logging.getLogger(__name__)

#: Frames one subscriber may fall behind by before the oldest is dropped.
QUEUE_LIMIT = 512

#: Rail events replayed to a late joiner. Two hundred rows is more than the
#: rail shows and well under what a scenario run produces.
RAIL_BACKLOG = 200

#: Advisories kept in the snapshot. A run that somehow published more than
#: this has long since stopped being a demo.
ADVISORY_BACKLOG = 64


class Subscriber:
    """One viewer's queue of frames, oldest-dropped when it cannot keep up."""

    def __init__(self, limit: int = QUEUE_LIMIT) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=limit)
        self._closed = False
        #: Frames thrown away because this subscriber was behind.
        self.dropped = 0

    def offer(self, frame: dict[str, Any]) -> None:
        """Queue a frame, making room by dropping the oldest if need be."""
        if self._closed:
            return
        while True:
            try:
                self._queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
                    self.dropped += 1

    def close(self) -> None:
        self._closed = True
        # A sentinel rather than a cancel: the writer task gets to finish the
        # frame it is sending before the iterator ends.
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait({})

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        """Yield frames until this subscriber is closed."""
        while True:
            frame = await self._queue.get()
            if self._closed and not frame:
                return
            yield frame


class ViewerHub:
    """Every connected viewer, plus the snapshot a new one starts from."""

    def __init__(
        self,
        *,
        queue_limit: int = QUEUE_LIMIT,
        rail_backlog: int = RAIL_BACKLOG,
    ) -> None:
        self.queue_limit = queue_limit
        self._subscribers: set[Subscriber] = set()
        self._state: dict[str, Any] | None = None
        self._counters: dict[str, Any] | None = None
        self._advisories: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._rail: deque[dict[str, Any]] = deque(maxlen=rail_backlog)
        #: Frames published since the server started, for `GET /healthz`.
        self.published = 0

    # --- subscriptions ------------------------------------------------------

    @property
    def viewers(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> Subscriber:
        """A new subscriber, already holding the snapshot."""
        subscriber = Subscriber(self.queue_limit)
        for frame in self.snapshot():
            subscriber.offer(frame)
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)
        subscriber.close()

    def close_all(self) -> None:
        """Let every subscriber's iterator end: server shutdown."""
        for subscriber in tuple(self._subscribers):
            self.unsubscribe(subscriber)

    # --- publishing ---------------------------------------------------------

    def publish(self, frame: dict[str, Any]) -> None:
        """Retain what a late joiner needs, then hand the frame to everyone."""
        self._retain(frame)
        self.published += 1
        for subscriber in tuple(self._subscribers):
            subscriber.offer(frame)

    def publish_all(self, frames: Iterable[dict[str, Any]]) -> None:
        for frame in frames:
            self.publish(frame)

    def _retain(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == FRAME_SCENARIO_STATE:
            self._state = frame
            return
        if kind == FRAME_COUNTERS:
            self._counters = frame
            return
        if kind == FRAME_ADVISORY:
            advisory_id = str(frame.get("id") or "")
            if advisory_id:
                self._advisories[advisory_id] = frame
                self._advisories.move_to_end(advisory_id)
                while len(self._advisories) > ADVISORY_BACKLOG:
                    self._advisories.popitem(last=False)
        if kind in RAIL_STAGES:
            self._rail.append(frame)

    def reset(self, state: dict[str, Any] | None = None) -> None:
        """Drop the retained run. Called when a scenario starts or stops."""
        self._advisories.clear()
        self._rail.clear()
        self._counters = None
        self._state = state

    # --- what a late joiner sees -------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        """The current picture: state, advisories in force, the rail, counters.

        Advisories come before the rail events that produced them so a viewer
        can draw the map first and fill the rail behind it, and an advisory
        that is in both the live set and the rail tail is sent once: the rail
        replay skips the frames the map has already been given.
        """
        frames: list[dict[str, Any]] = []
        if self._state is not None:
            frames.append(self._state)
        live = list(self._advisories.values())
        sent = {id(frame) for frame in live}
        frames.extend(live)
        frames.extend(frame for frame in self._rail if id(frame) not in sent)
        if self._counters is not None:
            frames.append(self._counters)
        return frames
