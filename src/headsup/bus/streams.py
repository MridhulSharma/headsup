"""Stream names, and which stream each message belongs on.

Names are flat and readable so `redis-cli XINFO STREAM advisories` is a useful
debugging tool during the demo. One stream per feed on the way in
(`events.nws`), one stream per stage after that.
"""

from __future__ import annotations

import re

from headsup.contracts import (
    Ack,
    Advisory,
    AssistRequest,
    CandidateHazard,
    CivicEvent,
    CriticVerdict,
    Hazard,
    HeadsUpModel,
    ProposedAdvisory,
    Source,
    Telemetry,
)

EVENTS_PREFIX = "events"

STREAM_CANDIDATES = "candidates"
STREAM_HAZARDS = "hazards"
STREAM_PROPOSALS = "proposals"
STREAM_VERDICTS = "verdicts"
STREAM_ADVISORIES = "advisories"
STREAM_ACKS = "acks"
STREAM_TELEMETRY = "telemetry"
STREAM_ASSIST = "assist"
STREAM_AUDIT = "audit"

_SOURCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def events_stream(source: Source | str) -> str:
    """Name of one feed's ingest stream, e.g. `events.nws`."""
    name = source.value if isinstance(source, Source) else str(source)
    if not _SOURCE_PATTERN.fullmatch(name):
        raise ValueError(f"{name!r} is not a usable feed name for an events.* stream")
    return f"{EVENTS_PREFIX}.{name}"


#: One ingest stream per known feed.
EVENT_STREAMS: tuple[str, ...] = tuple(events_stream(source) for source in Source)

#: Every stream after ingest, in pipeline order.
CORE_STREAMS: tuple[str, ...] = (
    STREAM_CANDIDATES,
    STREAM_HAZARDS,
    STREAM_PROPOSALS,
    STREAM_VERDICTS,
    STREAM_ADVISORIES,
    STREAM_ACKS,
    STREAM_TELEMETRY,
    STREAM_ASSIST,
    STREAM_AUDIT,
)

ALL_STREAMS: tuple[str, ...] = EVENT_STREAMS + CORE_STREAMS

#: Streams with a single legitimate writer. The Redis ACL in D1.3 enforces this
#: server-side; `Bus(writer=...)` refuses it client-side as a second lock.
SINGLE_WRITER_STREAMS: dict[str, str] = {STREAM_ADVISORIES: "compiler"}

#: Where each message type goes when the caller does not name a stream.
STREAM_BY_MODEL: dict[str, str] = {
    CandidateHazard.__name__: STREAM_CANDIDATES,
    Hazard.__name__: STREAM_HAZARDS,
    ProposedAdvisory.__name__: STREAM_PROPOSALS,
    CriticVerdict.__name__: STREAM_VERDICTS,
    Advisory.__name__: STREAM_ADVISORIES,
    Ack.__name__: STREAM_ACKS,
    Telemetry.__name__: STREAM_TELEMETRY,
    AssistRequest.__name__: STREAM_ASSIST,
}


def stream_for(message: HeadsUpModel) -> str:
    """The stream a message belongs on, by its type.

    `MoveOrder` and `KeepClear` have no stream of their own: they reach the
    agents through the API and travel on inside a proposal or an advisory.
    """
    if isinstance(message, CivicEvent):
        return events_stream(message.source)
    name = type(message).__name__
    try:
        return STREAM_BY_MODEL[name]
    except KeyError:
        raise ValueError(f"{name} has no default stream; pass stream=... to publish") from None
