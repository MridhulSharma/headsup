"""Signal-status payloads for the adapter and sentinel tests.

Trimmed from a real `5zpr-dehc` response: the columns it serves, in its order,
with its state codes and its naive Austin-local timestamps. The coordinates
are real downtown Austin intersections from `data/signals/austin.json`, close
enough together to cluster at 400 m, so a test that asserts on a hull is
asserting about the shape of actual city blocks.

`CLUSTER` is five of them on the Congress Avenue grid; `FAR` is one out by the
river, too far to join anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from headsup.contracts import Source
from headsup.ingest.austin_signals import SIGNAL_STATUS_DATASET, SODA_BASE
from headsup.ingest.base import RawPayload, make_ref

SIGNALS_URL = f"{SODA_BASE}/{SIGNAL_STATUS_DATASET}.json"

T0 = datetime(2026, 10, 22, 0, 45, tzinfo=UTC)

#: Feed state codes.
SCHEDULED_FLASH = 1
UNSCHEDULED_FLASH = 2
COMMUNICATION_ISSUE = 3
OTHER_UNSCHEDULED_FLASH = 7

STATE_TEXT: dict[int, str] = {
    SCHEDULED_FLASH: "Scheduled flash",
    UNSCHEDULED_FLASH: "Unscheduled (conflict) flash",
    COMMUNICATION_ISSUE: "Communication issue",
    OTHER_UNSCHEDULED_FLASH: "Unscheduled (conflict) flash",
}

#: `(signal_id, name, lon, lat)`, from the committed signal map. Within about
#: 250 m of each other: one cluster at eps 400 m.
CLUSTER: tuple[tuple[str, str, float, float], ...] = (
    ("303", "6TH ST / CONGRESS AVE", -97.742813, 30.268066),
    ("293", "7TH ST / CONGRESS AVE", -97.74247, 30.268967),
    ("302", "6TH ST / COLORADO ST", -97.744125, 30.268417),
    ("292", "7TH ST / COLORADO ST", -97.743774, 30.269348),
    ("314", "5TH ST / CONGRESS AVE", -97.743172, 30.267119),
)

#: One more on the same grid, for the rows that must not be counted.
IN_AREA: tuple[str, str, float, float] = ("283", "8TH ST / COLORADO ST", -97.743416, 30.270268)

#: Barton Springs Rd at Bouldin Ave, 1.2 km out: DBSCAN noise, not a cluster.
FAR: tuple[str, str, float, float] = (
    "233",
    "BARTON SPRINGS RD / BOULDIN AVE",
    -97.753479,
    30.259584,
)


def local(moment: datetime) -> str:
    """A UTC instant the way the feed writes it: Austin local, no offset."""
    from headsup.ingest.austin_signals import FEED_TZ

    return moment.astimezone(FEED_TZ).replace(tzinfo=None).isoformat(timespec="milliseconds")


def record(
    signal: tuple[str, str, float, float],
    state: int = UNSCHEDULED_FLASH,
    *,
    since: datetime = T0,
    at: datetime = T0,
) -> dict[str, Any]:
    """One row of the status feed."""
    signal_id, name, lon, lat = signal
    primary, _, cross = name.partition(" / ")
    return {
        "signal_id": signal_id,
        "location_name": name,
        "operation_state": str(state),
        "operation_text": STATE_TEXT[state],
        "operation_state_datetime": local(since),
        "processed_datetime": local(at),
        "plan_id": "254",
        "primary_st": primary,
        "cross_st": cross,
        "location": {"type": "Point", "coordinates": [lon, lat]},
    }


def flashing(count: int = 4, *, since: datetime = T0, at: datetime = T0) -> list[dict[str, Any]]:
    """The first `count` of the Congress Avenue grid, in unscheduled flash."""
    return [record(signal, since=since, at=at) for signal in CLUSTER[:count]]


def payload(records: list[dict[str, Any]], *, at: datetime = T0, seq: int = 0) -> RawPayload:
    """One poll of the status feed, as the recorder writes it."""
    return RawPayload(
        source=Source.AUSTIN_SIGNALS,
        ref=make_ref(Source.AUSTIN_SIGNALS, at, seq),
        url=SIGNALS_URL,
        fetched_at=at,
        body={"dataset": SIGNAL_STATUS_DATASET, "records": records},
    )


def events(records: list[dict[str, Any]], *, at: datetime = T0, seq: int = 0) -> list[Any]:
    """The `CivicEvent`s the real adapter parses out of those records."""
    from headsup.ingest.austin_signals import AustinSignalsAdapter

    return AustinSignalsAdapter().parse(payload(records, at=at, seq=seq))


#: A whole poll: four flashing signals, one scheduled flash, one comms issue
#: and one distant fault. Only the four are an outage.
MIXED_RECORDS: list[dict[str, Any]] = [
    *flashing(4),
    record(CLUSTER[4], SCHEDULED_FLASH),
    record(IN_AREA, COMMUNICATION_ISSUE),
    record(FAR, UNSCHEDULED_FLASH, since=T0 - timedelta(hours=6)),
]

MIXED_BODY: dict[str, Any] = {"dataset": SIGNAL_STATUS_DATASET, "records": MIXED_RECORDS}
