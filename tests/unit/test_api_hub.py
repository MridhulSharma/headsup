"""Fan-out to the viewers, and the snapshot a late joiner is given.

The hub is the one place a slow socket could stall the simulator, so what is
pinned here is that it cannot: a subscriber that never reads loses its oldest
frames and the publisher returns either way.
"""

from __future__ import annotations

from typing import Any

from headsup.api.frames import (
    FRAME_ADVISORY,
    FRAME_CANDIDATE,
    FRAME_COUNTERS,
    FRAME_SCENARIO_STATE,
    FRAME_TELEMETRY,
)
from headsup.api.hub import ViewerHub


def _frame(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


STATE = _frame(FRAME_SCENARIO_STATE, state="running")
ADVISORY = _frame(FRAME_ADVISORY, id="adv_1", feature={})
CANDIDATE = _frame(FRAME_CANDIDATE, id="cand_1")
COUNTERS = _frame(FRAME_COUNTERS, counters={})


async def _drain(subscriber: Any, count: int) -> list[dict[str, Any]]:
    """The next `count` frames, which are already queued."""
    frames = []
    stream = subscriber.frames()
    for _ in range(count):
        frames.append(await anext(stream))
    return frames


async def test_every_subscriber_gets_every_frame() -> None:
    hub = ViewerHub()
    one, two = hub.subscribe(), hub.subscribe()
    hub.publish(CANDIDATE)
    assert await _drain(one, 1) == [CANDIDATE]
    assert await _drain(two, 1) == [CANDIDATE]
    assert hub.viewers == 2


async def test_a_late_joiner_gets_the_state_the_advisories_and_the_rail() -> None:
    """A viewer connecting mid-run sees the map before the next advisory."""
    hub = ViewerHub()
    hub.publish(STATE)
    hub.publish(CANDIDATE)
    hub.publish(ADVISORY)
    hub.publish(COUNTERS)

    late = hub.subscribe()
    # Advisories before the rail: the map is drawn first and the rail fills in
    # behind it. The advisory is in the rail tail too, and is sent once.
    assert await _drain(late, 4) == [STATE, ADVISORY, CANDIDATE, COUNTERS]
    assert hub.snapshot() == [STATE, ADVISORY, CANDIDATE, COUNTERS]


async def test_the_snapshot_keeps_one_state_and_one_counter_frame() -> None:
    """Retained state is the current one, not a log of every one."""
    hub = ViewerHub()
    hub.publish(STATE)
    hub.publish(_frame(FRAME_SCENARIO_STATE, state="idle"))
    hub.publish(COUNTERS)
    hub.publish(_frame(FRAME_COUNTERS, counters={"live": {}}))
    snapshot = hub.snapshot()
    assert [frame["type"] for frame in snapshot] == [FRAME_SCENARIO_STATE, FRAME_COUNTERS]
    assert snapshot[0]["state"] == "idle"
    assert snapshot[1]["counters"] == {"live": {}}


async def test_telemetry_is_not_retained() -> None:
    """A late joiner wants the next tick, not the last one before it arrived."""
    hub = ViewerHub()
    hub.publish(_frame(FRAME_TELEMETRY, tick=1, vehicles=[]))
    assert hub.snapshot() == []


async def test_a_subscriber_that_never_reads_loses_its_oldest_frames() -> None:
    """Back-pressure must never reach the tick loop."""
    hub = ViewerHub(queue_limit=4)
    subscriber = hub.subscribe()
    for tick in range(10):
        hub.publish(_frame(FRAME_TELEMETRY, tick=tick))
    assert subscriber.dropped == 6
    kept = await _drain(subscriber, 4)
    assert [frame["tick"] for frame in kept] == [6, 7, 8, 9]


async def test_reset_drops_the_run_and_may_set_the_new_state() -> None:
    hub = ViewerHub()
    hub.publish(STATE)
    hub.publish(ADVISORY)
    idle = _frame(FRAME_SCENARIO_STATE, state="idle", scenario=None)
    hub.reset(idle)
    assert hub.snapshot() == [idle]


async def test_a_closed_subscriber_ends_its_own_iterator() -> None:
    hub = ViewerHub()
    subscriber = hub.subscribe()
    hub.publish(CANDIDATE)
    hub.unsubscribe(subscriber)
    assert hub.viewers == 0
    seen = [frame async for frame in subscriber.frames()]
    assert seen == [CANDIDATE]


async def test_publishing_with_no_viewers_still_retains_the_snapshot() -> None:
    """A server nobody is watching is still a server with a current picture."""
    hub = ViewerHub()
    hub.publish(STATE)
    hub.publish(CANDIDATE)
    assert hub.viewers == 0
    assert hub.published == 2
    assert hub.snapshot() == [STATE, CANDIDATE]
