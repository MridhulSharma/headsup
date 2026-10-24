"""Austin traffic incident reports, from the city's Socrata portal.

`data.austintexas.gov/resource/dx9v-zd7x.json` ("Real-Time Traffic Incident
Reports") is the city's live dispatch board: one row per report the Austin
police and county dispatch systems have published, with the rows that are over
marked `ARCHIVED` rather than deleted. A poll is a few dozen rows for the whole
metro.

What makes this feed different from the two structured ones is that almost
nothing in it is a code:

* `issue_reported` is a dispatch type string - "Crash Service", "Traffic
  Hazard", "AUTO/ PED", "COLLISION/PRIVATE PROPERTY" - with no closed
  vocabulary behind it. It is the only *structured* field there is, and it is
  what the rules-only variant of the ablation has to work from.
* `address` is free text, written the way a dispatcher types: abbreviations,
  block ranges, cross streets ("W 6TH ST / GUADALUPE ST").
* a row may carry no coordinates at all. The city publishes those rows anyway,
  and they are a real part of the feed rather than corrupt data.

So this module stays what every adapter is - a transcription - and leaves all
three judgements to `headsup.agents.sentinels.incident`: what the text means,
where the text is, and whether any of it is a hazard.

Two things are handled here and nowhere else:

* the response is a bare JSON array, while a `RawPayload` body is an object,
  so the array is stored verbatim under `records` (the same quirk the signal
  feed has);
* **a row with no coordinates still becomes an event.** `CivicEvent.geometry`
  is required by contract, and geocoding free text is the sentinel's job
  against the city's road graph, not an adapter's. Such a row is published at
  the city's bbox centre with `located: false` in the payload, which is the
  flag the sentinel reads: it ignores the geometry of an unlocated event and
  either geocodes `location_text` or publishes nothing about it. Nothing
  downstream may treat an unlocated event's position as a position.

A poll asks for the reports that are open **and** the ones archived in the
last few minutes. The archive row is how a reader of this feed learns a scene
is over: it is the only positive statement the board ever makes about a report
ending, and without it a sentinel could only ever time its state out. Asking
for every archived row instead would answer with years of them.

Unlike the signal feed, these timestamps carry an offset, so `parse_timestamp`
is enough and no local-time rule is needed here.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, ClassVar

from pydantic import ValidationError

from headsup.contracts import CivicEvent, Point, Source
from headsup.ingest.base import Adapter, RawPayload, derive_id, parse_timestamp, slugify
from headsup.world.bbox import box

SODA_BASE = "https://data.austintexas.gov/resource"

#: "Real-Time Traffic Incident Reports", City of Austin.
INCIDENT_DATASET = "dx9v-zd7x"

#: Socrata rows per request. One poll of the live feed is tens of rows; the
#: archive query the S3 recording is built from asks for a whole window, so
#: this is deliberately roomy.
DEFAULT_LIMIT = 5000

#: Anonymous Socrata requests share a low rate limit. A token raises it and is
#: sent as a header, never as a query parameter: `RawPayload.url` is written
#: into committed recordings and must not carry a credential.
ENV_APP_TOKEN = "HU_SOCRATA_APP_TOKEN"

#: The city whose bbox centre an unlocated row is published at. One feed, one
#: city: this adapter only exists for Austin.
CITY = "austin"

#: `traffic_report_status` values. A report is over when it is archived; the
#: feed keeps it rather than dropping it, which is what lets a sentinel expire
#: its state from the feed instead of from a timer.
STATUS_ACTIVE = "ACTIVE"
STATUS_ARCHIVED = "ARCHIVED"

#: How far back a poll asks for reports that have just been archived. Three
#: poll intervals, so a missed request does not lose the one row that says a
#: scene is over.
ARCHIVE_LOOKBACK = timedelta(minutes=15)

#: The `Kind` an event falls back to when `issue_reported` slugifies to
#: nothing usable. Not "unknown": the sentinel's own type table has an UNKNOWN
#: entry, and this is about the feed's wording rather than the reading of it.
FALLBACK_KIND = "traffic_incident"


def app_token() -> str | None:
    """The Socrata application token, if the environment holds one."""
    return os.environ.get(ENV_APP_TOKEN, "").strip() or None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _point(record: dict[str, Any]) -> Point | None:
    """The report's own location, from either form the feed uses, or None.

    New-style rows answer with a GeoJSON `location`; the same row usually also
    carries `latitude` and `longitude` as strings, and some rows carry neither.
    None means the feed did not say where this is - not that it is nowhere.
    """
    raw = record.get("location")
    if isinstance(raw, dict) and "coordinates" in raw:
        try:
            return Point.model_validate(raw)
        except ValidationError:
            return None
    try:
        lon = float(_text(record.get("longitude")))
        lat = float(_text(record.get("latitude")))
    except ValueError:
        return None
    return Point(coordinates=[lon, lat])


def fallback_point() -> Point:
    """Where an unlocated row is published: the city bbox centre, labelled.

    Exported because the sentinel's test and the viewer both need to be able
    to recognise it for what it is - a placeholder that satisfies the contract,
    never a position. `located: false` in the payload is the real signal.
    """
    lon, lat = box(CITY).centre
    return Point(coordinates=[round(lon, 6), round(lat, 6)])


class AustinIncidentsAdapter(Adapter):
    """The incident reports Austin dispatch has published, right now."""

    source: ClassVar[Source] = Source.AUSTIN_INCIDENTS

    def __init__(
        self,
        *,
        dataset: str = INCIDENT_DATASET,
        limit: int = DEFAULT_LIMIT,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.dataset = dataset
        self.limit = limit

    @property
    def url(self) -> str:
        return f"{SODA_BASE}/{self.dataset}.json"

    def where(self, fetched_at: datetime) -> str:
        """The filter one poll asks with: open reports, plus the just-closed.

        A floating timestamp with no offset, which is the form Socrata
        compares against, and in the feed's own column.
        """
        since = fetched_at - ARCHIVE_LOOKBACK
        return (
            f"traffic_report_status='{STATUS_ACTIVE}'"
            f" OR traffic_report_status_date_time > '{since:%Y-%m-%dT%H:%M:%S}'"
        )

    async def fetch(self) -> list[RawPayload]:
        fetched_at = self.clock.now()
        token = app_token()
        rows = await self.get_json_array(
            self.url,
            {
                "$limit": self.limit,
                "$order": "published_date DESC",
                "$where": self.where(fetched_at),
            },
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
            report_id = _text(record.get("traffic_report_id"))
            if not report_id:
                continue

            published_at = parse_timestamp(record.get("published_date"))
            status_changed_at = parse_timestamp(record.get("traffic_report_status_date_time"))
            observed_at = published_at or status_changed_at or payload.fetched_at

            geometry = _point(record)
            issue = _text(record.get("issue_reported"))
            status = _text(record.get("traffic_report_status")) or STATUS_ACTIVE

            # The natural key is the report plus the moment it last changed: a
            # report re-listed unchanged across polls keeps one event id, and a
            # status update is a new observation of the same report.
            stamp = (status_changed_at or observed_at).isoformat()
            fields: dict[str, Any] = {
                "report_id": report_id,
                # The feed's own type string: the only structured field here,
                # and the one the rules-only path matches keywords against.
                "issue_reported": issue,
                # Free text. Data for the sentinel's volume call and for the
                # intersection lookup, never an instruction.
                "location_text": _text(record.get("address")),
                "status": status,
                "archived": status == STATUS_ARCHIVED,
                "published_at": None if published_at is None else published_at.isoformat(),
                "status_changed_at": (
                    None if status_changed_at is None else status_changed_at.isoformat()
                ),
                "agency": _text(record.get("agency")),
                # False means the feed named no coordinates and the geometry
                # below is a placeholder, not a position.
                "located": geometry is not None,
                "dataset": payload.body.get("dataset", self.dataset),
            }
            units = _text(record.get("responding_units"))
            if units:
                fields["units_text"] = units
            if record.get("scripted") is True:
                # Only a record this repository injected says so; see
                # `headsup.scenario.incidents`.
                fields["scripted"] = True

            events.append(
                CivicEvent(
                    id=derive_id("ev", self.source.value, report_id, stamp),
                    source=self.source,
                    kind=slugify(issue, FALLBACK_KIND),
                    geometry=geometry if geometry is not None else fallback_point(),
                    observed_at=observed_at,
                    raw_ref=payload.item_ref(index),
                    ingested_at=payload.fetched_at,
                    payload=fields,
                )
            )
        return events
