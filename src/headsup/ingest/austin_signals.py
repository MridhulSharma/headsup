"""Austin traffic signal status, from the city's Socrata portal.

`data.austintexas.gov/resource/5zpr-dehc.json` ("Traffic Signals Status") is
not an inventory: it lists only the signals that are *not* running normally at
the moment of the poll, so a response is a few dozen rows out of the roughly
thousand signals the city operates. The dataset's own description notes that
about 10% of signals are not covered by it at all.

What the sentinel needs out of it is `operation_state`:

| state | operation_text               | what it means here          |
| ----- | ---------------------------- | --------------------------- |
| 1     | Scheduled flash              | flashing, to a timetable    |
| 2, 7  | Unscheduled (conflict) flash | flashing, unplanned         |
| 3     | Communication issue          | state unknown, not dark     |

Only an unscheduled flash is evidence of an outage: an overnight scheduled
flash is a timetable, and a communication issue says the management centre
cannot see the signal rather than that the signal is dark. The parse records
all three and publishes `flashing` and `scheduled` as booleans in the payload,
so the judgement stays in `headsup.agents.sentinels.signals` and this module
stays a transcription of the feed.

Two feed quirks are handled here and nowhere else:

* the response is a bare JSON array, while a `RawPayload` body is an object,
  so the array is stored verbatim under `records`;
* the timestamps are Socrata floating timestamps - no offset - and they are
  Austin local time, not UTC. A reading stamped 12:05 was taken at 17:05Z in
  October. `_local_timestamp` is what keeps a dark signal from landing five
  hours in the past on the bus.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from headsup.contracts import CivicEvent, Point, Source
from headsup.ingest.base import Adapter, RawPayload, derive_id, parse_timestamp, slugify

SODA_BASE = "https://data.austintexas.gov/resource"

#: "Traffic Signals Status", City of Austin Transportation and Public Works.
SIGNAL_STATUS_DATASET = "5zpr-dehc"

#: "Traffic Signals and Pedestrian Signals": the signal map itself. Not polled
#: live - the inventory changes a few times a year - but `hu scenario
#: synth-blackout` reads a snapshot of it to place synthetic flashes on real
#: intersections, which is why the id lives beside the feed it belongs to.
SIGNAL_INVENTORY_DATASET = "p53x-x73x"

#: Socrata rows per request. One poll of the status feed is tens of rows, so
#: this is a backstop against a day when half the city is flashing.
DEFAULT_LIMIT = 2000

#: Anonymous Socrata requests share a low rate limit. A token raises it and is
#: sent as a header, never as a query parameter: `RawPayload.url` is written
#: into committed recordings and must not carry a credential.
ENV_APP_TOKEN = "HU_SOCRATA_APP_TOKEN"

#: The feed's timestamps have no offset and are Austin local time.
FEED_TZ = ZoneInfo("America/Chicago")

#: `operation_state` -> the `Kind` the event is published under.
STATE_KINDS: dict[int, str] = {
    1: "scheduled_flash",
    2: "unscheduled_flash",
    3: "communication_issue",
    7: "unscheduled_flash",
}

#: Flashing to a published timetable, usually overnight on an arterial.
SCHEDULED_FLASH_STATE = 1

#: Every state that means the signal heads are flashing rather than cycling.
FLASHING_STATES = frozenset({SCHEDULED_FLASH_STATE, 2, 7})

#: The subset of those that is unplanned, and so evidence of an outage.
UNSCHEDULED_FLASH_STATES = frozenset({2, 7})


def app_token() -> str | None:
    """The Socrata application token, if the environment holds one."""
    return os.environ.get(ENV_APP_TOKEN, "").strip() or None


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _local_timestamp(value: Any) -> datetime | None:
    """A Socrata floating timestamp read as Austin local time, in UTC.

    A value that does carry an offset is left to `parse_timestamp`, so a feed
    that starts stamping its rows properly does not have to be noticed here.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z") or "+" in text[10:] or "-" in text[10:]:
        return parse_timestamp(text)
    try:
        naive = datetime.fromisoformat(text)
    except ValueError:
        return None
    return naive.replace(tzinfo=FEED_TZ).astimezone(UTC)


def _kind(state: int | None, operation_text: Any) -> str:
    """The `Kind` an event is published under: the state's name, or the text's.

    A state the table does not know is a state this code has not been taught,
    so the feed's own wording is slugified as a fallback and the event still
    reaches the bus for a human to look at.
    """
    known = STATE_KINDS.get(state) if state is not None else None
    return known or slugify(operation_text or "", "signal_status")


def _point(raw: Any) -> Point | None:
    """The signal's location, from either Socrata point form.

    New-style columns answer with GeoJSON; the legacy `location` type answers
    with latitude and longitude as strings. Both appear across the portal's
    datasets, and a signal with neither is dropped: a dark signal with no
    position cannot be clustered or drawn.
    """
    if not isinstance(raw, dict):
        return None
    candidate: Any = raw
    if "coordinates" not in raw and {"latitude", "longitude"} <= raw.keys():
        try:
            candidate = {
                "type": "Point",
                "coordinates": [float(raw["longitude"]), float(raw["latitude"])],
            }
        except (TypeError, ValueError):
            return None
    try:
        return Point.model_validate(candidate)
    except ValidationError:
        return None


class AustinSignalsAdapter(Adapter):
    """The signals Austin reports as flashing or unreachable, right now."""

    source: ClassVar[Source] = Source.AUSTIN_SIGNALS

    def __init__(
        self,
        *,
        dataset: str = SIGNAL_STATUS_DATASET,
        limit: int = DEFAULT_LIMIT,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.dataset = dataset
        self.limit = limit

    @property
    def url(self) -> str:
        return f"{SODA_BASE}/{self.dataset}.json"

    async def fetch(self) -> list[RawPayload]:
        fetched_at = self.clock.now()
        token = app_token()
        rows = await self.get_json_array(
            self.url,
            {"$limit": self.limit, "$order": "signal_id"},
            headers={"X-App-Token": token} if token else None,
        )
        return [
            RawPayload(
                source=self.source,
                ref=self.next_ref(fetched_at),
                url=self.url,
                fetched_at=fetched_at,
                # The body is an object by contract and Socrata answers with an
                # array, so the array goes under `records` exactly as it came.
                body={"dataset": self.dataset, "records": rows},
            )
        ]

    def parse(self, payload: RawPayload) -> list[CivicEvent]:
        records = payload.body.get("records")
        if not isinstance(records, list):
            return []

        events: list[CivicEvent] = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            signal_id = str(record.get("signal_id") or "").strip()
            if not signal_id:
                continue
            geometry = _point(record.get("location"))
            if geometry is None:
                continue

            state = _as_int(record.get("operation_state"))
            changed_at = _local_timestamp(record.get("operation_state_datetime"))
            processed_at = _local_timestamp(record.get("processed_datetime"))
            observed_at = changed_at or processed_at or payload.fetched_at

            events.append(
                CivicEvent(
                    id=derive_id("ev", self.source.value, signal_id, observed_at.isoformat()),
                    source=self.source,
                    kind=_kind(state, record.get("operation_text")),
                    geometry=geometry,
                    observed_at=observed_at,
                    raw_ref=payload.item_ref(index),
                    ingested_at=payload.fetched_at,
                    payload={
                        "signal_id": signal_id,
                        "location_name": record.get("location_name"),
                        "primary_st": record.get("primary_st"),
                        "cross_st": record.get("cross_st"),
                        "operation_state": state,
                        "operation_text": record.get("operation_text"),
                        "flashing": state in FLASHING_STATES,
                        "scheduled": state == SCHEDULED_FLASH_STATE,
                        "plan_id": record.get("plan_id"),
                        "state_changed_at": (
                            None if changed_at is None else changed_at.isoformat()
                        ),
                        "processed_at": (
                            None if processed_at is None else processed_at.isoformat()
                        ),
                        "dataset": payload.body.get("dataset", self.dataset),
                    },
                )
            )
        return events
