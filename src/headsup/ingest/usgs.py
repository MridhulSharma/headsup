"""USGS stream gauges through the Water Data OGC API.

Two collections of the same shape are used. `latest-continuous` is the live
feed: one feature per site holding its most recent reading. `continuous` is
the same data with a `datetime` window, which is how a past event such as S1
is recorded after the fact. Both return GeoJSON features with the reading in
`properties`, so one `parse` covers live, recorded and replayed alike.

A gauge height on its own means nothing without a threshold, so each site may
carry an `action_stage_ft`. These are operating thresholds Heads Up uses to
decide when a crossing is worth looking at; they are not official NWS flood
stages, and the flood sentinel treats them as a hint, not a verdict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from pydantic import ValidationError

from headsup.contracts import CivicEvent, Point, Source
from headsup.ingest.base import (
    Adapter,
    IngestError,
    RawPayload,
    derive_id,
    parse_timestamp,
)
from headsup.ingest.cities import GAUGE_HEIGHT, City

OGC_BASE = "https://api.waterdata.usgs.gov/ogcapi/v0"

#: Most recent reading per site: what `hu ingest live` polls.
COLLECTION_LATEST = "latest-continuous"
#: The same values with a `datetime` window: what a recording of a past event uses.
COLLECTION_HISTORY = "continuous"

#: Readings per request. The OGC API caps a page well above this.
DEFAULT_LIMIT = 500

#: Stage in feet above which a Salado Creek low-water crossing is worth a look.
#: Chosen from the April 2026 hydrographs, not published NWS flood stages.
ACTION_STAGE_FT: dict[str, float] = {
    "08178585": 2.0,
    "08178593": 4.5,
    "08178700": 6.0,
    "08178740": 8.0,
    "08178800": 9.0,
}


def collection_url(collection: str) -> str:
    return f"{OGC_BASE}/collections/{collection}/items"


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _site_number(location_id: Any) -> str:
    """`USGS-08178800` -> `08178800`; anything else comes back unchanged."""
    text = str(location_id or "")
    return text.removeprefix("USGS-")


class UsgsAdapter(Adapter):
    """Gauge height for a configured set of monitoring locations."""

    source: ClassVar[Source] = Source.USGS

    def __init__(
        self,
        *,
        sites: tuple[str, ...] | list[str] = (),
        parameter_code: str = GAUGE_HEIGHT,
        limit: int = DEFAULT_LIMIT,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Sites are only needed to fetch. Replay builds an adapter purely to
        # reach `parse`, and has no business naming a gauge list.
        self.sites = tuple(sites)
        self.parameter_code = parameter_code
        self.limit = limit

    @classmethod
    def for_city(cls, city: City, **kwargs: Any) -> UsgsAdapter:
        """An adapter over the gauges configured for one city."""
        return cls(sites=city.gauge_ids, **kwargs)

    def _params(self) -> dict[str, Any]:
        if not self.sites:
            raise IngestError("this UsgsAdapter has no sites configured; it can only parse")
        return {
            "monitoring_location_id": ",".join(self.sites),
            "parameter_code": self.parameter_code,
            "limit": self.limit,
            "f": "json",
        }

    async def fetch(self) -> list[RawPayload]:
        """The latest reading at every configured site."""
        return [await self._fetch(COLLECTION_LATEST, self._params())]

    async def fetch_window(self, start: datetime, end: datetime) -> list[RawPayload]:
        """Every reading in a past window: how an event like S1 is recorded."""
        params = self._params()
        params["datetime"] = f"{start.isoformat().replace('+00:00', 'Z')}/" + end.isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        return [await self._fetch(COLLECTION_HISTORY, params)]

    async def _fetch(self, collection: str, params: dict[str, Any]) -> RawPayload:
        fetched_at = self.clock.now()
        url = collection_url(collection)
        body = await self.get_json(url, params)
        return RawPayload(
            source=self.source,
            ref=self.next_ref(fetched_at),
            url=url,
            fetched_at=fetched_at,
            body=body,
        )

    def parse(self, payload: RawPayload) -> list[CivicEvent]:
        features = payload.body.get("features")
        if not isinstance(features, list):
            return []

        events: list[CivicEvent] = []
        for index, feature in enumerate(features):
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties")
            if not isinstance(props, dict):
                continue
            value = _as_float(props.get("value"))
            if value is None:
                continue
            try:
                geometry = Point.model_validate(feature.get("geometry"))
            except ValidationError:
                continue

            site = _site_number(props.get("monitoring_location_id"))
            parameter = str(props.get("parameter_code", self.parameter_code))
            observed_at = parse_timestamp(props.get("time")) or payload.fetched_at
            series = str(props.get("time_series_id", ""))
            action_stage = ACTION_STAGE_FT.get(site)

            events.append(
                CivicEvent(
                    id=derive_id("ev", site, parameter, series, observed_at.isoformat()),
                    source=self.source,
                    kind="gauge_height" if parameter == GAUGE_HEIGHT else f"usgs_{parameter}",
                    geometry=geometry,
                    observed_at=observed_at,
                    raw_ref=payload.item_ref(index),
                    ingested_at=payload.fetched_at,
                    payload={
                        "site": site,
                        "monitoring_location_id": props.get("monitoring_location_id"),
                        "parameter_code": parameter,
                        "time_series_id": series or None,
                        "value": value,
                        "unit": props.get("unit_of_measure"),
                        "approval_status": props.get("approval_status"),
                        "qualifier": props.get("qualifier"),
                        "action_stage_ft": action_stage,
                        "above_action_stage": (
                            None if action_stage is None else value >= action_stage
                        ),
                    },
                )
            )
        return events
