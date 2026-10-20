"""Stream naming and message routing, with no Redis involved."""

from __future__ import annotations

import pytest

from headsup.bus import (
    ALL_STREAMS,
    CORE_STREAMS,
    EVENT_STREAMS,
    SINGLE_WRITER_STREAMS,
    STREAM_ADVISORIES,
    STREAM_HAZARDS,
    STREAM_TELEMETRY,
    Bus,
    events_stream,
    stream_for,
)
from headsup.contracts import Source
from samples import ADVISORY, HAZARD, KEEP_CLEAR, NWS_EVENT, TELEMETRY, USGS_EVENT


def test_the_packet_stream_names_all_exist() -> None:
    expected = {
        "candidates",
        "hazards",
        "proposals",
        "verdicts",
        "advisories",
        "acks",
        "telemetry",
        "assist",
        "audit",
    }
    assert set(CORE_STREAMS) == expected
    assert "events.nws" in EVENT_STREAMS
    assert len(set(ALL_STREAMS)) == len(ALL_STREAMS)


def test_events_stream_is_one_per_feed() -> None:
    assert events_stream(Source.NWS) == "events.nws"
    assert events_stream("austin_signals") == "events.austin_signals"
    assert set(EVENT_STREAMS) == {f"events.{source.value}" for source in Source}


@pytest.mark.parametrize("bad", ["NWS", "has space", "", "nws;flushall"])
def test_events_stream_refuses_a_name_that_is_not_a_feed(bad: str) -> None:
    with pytest.raises(ValueError, match="not a usable feed name"):
        events_stream(bad)


def test_messages_route_to_their_stream_by_type() -> None:
    assert stream_for(NWS_EVENT) == "events.nws"
    assert stream_for(USGS_EVENT) == "events.usgs"
    assert stream_for(HAZARD) == STREAM_HAZARDS
    assert stream_for(ADVISORY) == STREAM_ADVISORIES
    assert stream_for(TELEMETRY) == STREAM_TELEMETRY


def test_a_message_with_no_stream_of_its_own_says_so() -> None:
    with pytest.raises(ValueError, match="no default stream"):
        stream_for(KEEP_CLEAR)


def test_namespace_prefixes_every_key() -> None:
    bus = Bus("redis://localhost:6379/0", namespace="test42", client=object())  # type: ignore[arg-type]
    assert bus.key(STREAM_ADVISORIES) == "test42:advisories"
    assert bus.key("events.nws") == "test42:events.nws"


def test_no_namespace_leaves_the_key_alone() -> None:
    bus = Bus("redis://localhost:6379/0", client=object())  # type: ignore[arg-type]
    assert bus.key(STREAM_ADVISORIES) == STREAM_ADVISORIES


def test_advisories_has_exactly_one_writer() -> None:
    """CLAUDE.md hard rule: only the compiler writes advisories."""
    assert SINGLE_WRITER_STREAMS == {STREAM_ADVISORIES: "compiler"}
