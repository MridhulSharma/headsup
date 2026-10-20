"""National Weather Service active alerts.

`api.weather.gov/alerts/active?area=TX` returns every active alert for Texas as
a GeoJSON FeatureCollection. Only the storm-based ones carry a polygon; the
rest describe whole forecast zones through `affectedZones` and are far too
coarse to close a road with, so this adapter keeps the polygons and drops the
rest.

Everything the alert says in prose (`headline`, `description`, `instruction`)
is copied into `CivicEvent.payload` untouched. It is data for a sentinel to
read, never text that reaches an instruction slot.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ValidationError

from headsup.contracts import CivicEvent, MultiPolygon, Polygon, Source
from headsup.ingest.base import Adapter, RawPayload, derive_id, parse_timestamp, slugify

ALERTS_URL = "https://api.weather.gov/alerts/active"

#: Alerts without one of these geometry types cover a whole forecast zone.
POLYGON_TYPES = frozenset({"Polygon", "MultiPolygon"})

#: `properties.parameters` entries worth carrying. The rest is routing noise
#: (EAS channel flags, WMO headers) that no sentinel reads.
KEPT_PARAMETERS = (
    "NWSheadline",
    "flashFloodDamageThreat",
    "flashFloodDetection",
    "eventMotionDescription",
    "AWIPSidentifier",
    "WMOidentifier",
)


def _geometry(raw: Any) -> Polygon | MultiPolygon | None:
    """Validate an alert geometry, or None if it is absent or unusable."""
    if not isinstance(raw, dict) or raw.get("type") not in POLYGON_TYPES:
        return None
    model = Polygon if raw["type"] == "Polygon" else MultiPolygon
    try:
        return model.model_validate(raw)
    except ValidationError:
        # A warning polygon that shapely rejects cannot become a road closure,
        # so it is dropped here rather than carried to the compiler.
        return None


def _parameters(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in KEPT_PARAMETERS if key in raw}


class NwsAdapter(Adapter):
    """Active NWS alerts for one state, polygons only."""

    source: ClassVar[Source] = Source.NWS

    def __init__(self, *, area: str = "TX", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.area = area

    async def fetch(self) -> list[RawPayload]:
        fetched_at = self.clock.now()
        params = {"area": self.area}
        body = await self.get_json(ALERTS_URL, params)
        return [
            RawPayload(
                source=self.source,
                ref=self.next_ref(fetched_at),
                url=f"{ALERTS_URL}?area={self.area}",
                fetched_at=fetched_at,
                body=body,
            )
        ]

    def parse(self, payload: RawPayload) -> list[CivicEvent]:
        features = payload.body.get("features")
        if not isinstance(features, list):
            return []

        events: list[CivicEvent] = []
        for index, feature in enumerate(features):
            if not isinstance(feature, dict):
                continue
            geometry = _geometry(feature.get("geometry"))
            if geometry is None:
                continue
            props = feature.get("properties")
            if not isinstance(props, dict):
                continue

            alert_id = str(props.get("id") or feature.get("id") or payload.item_ref(index))
            observed_at = (
                parse_timestamp(props.get("onset"))
                or parse_timestamp(props.get("effective"))
                or parse_timestamp(props.get("sent"))
                or payload.fetched_at
            )
            geocode = props.get("geocode") if isinstance(props.get("geocode"), dict) else {}

            events.append(
                CivicEvent(
                    id=derive_id("ev", alert_id),
                    source=self.source,
                    kind=slugify(props.get("event", ""), "weather_alert"),
                    geometry=geometry,
                    observed_at=observed_at,
                    raw_ref=payload.item_ref(index),
                    ingested_at=payload.fetched_at,
                    payload={
                        "nws_id": alert_id,
                        "event": props.get("event"),
                        "headline": props.get("headline"),
                        "description": props.get("description"),
                        "instruction": props.get("instruction"),
                        "area_desc": props.get("areaDesc"),
                        "severity": props.get("severity"),
                        "certainty": props.get("certainty"),
                        "urgency": props.get("urgency"),
                        "response": props.get("response"),
                        "status": props.get("status"),
                        "message_type": props.get("messageType"),
                        "sender_name": props.get("senderName"),
                        "sent": props.get("sent"),
                        "effective": props.get("effective"),
                        "onset": props.get("onset"),
                        "expires": props.get("expires"),
                        "ends": props.get("ends"),
                        "ugc": geocode.get("UGC", []),
                        "same": geocode.get("SAME", []),
                        "parameters": _parameters(props.get("parameters")),
                    },
                )
            )
        return events
