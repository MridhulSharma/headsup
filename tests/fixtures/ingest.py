"""Small hand-written feed payloads for the ingest unit tests.

Trimmed from real responses: one NWS alert with a polygon, one without (an
alert that only names forecast zones), and two USGS gauge-height readings.
Three vertices plus the closing one is the smallest ring a polygon can have,
which keeps these literals readable.
"""

from __future__ import annotations

from typing import Any

NWS_URL = "https://api.weather.gov/alerts/active?area=TX"

#: A polygon over north-east San Antonio, the shape of the S1 warning area.
WARNING_RING = [
    [-98.60, 29.56],
    [-98.38, 29.73],
    [-98.14, 29.78],
    [-97.99, 29.69],
    [-98.60, 29.56],
]

POLYGON_ALERT: dict[str, Any] = {
    "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.ffw.001.1",
    "type": "Feature",
    "geometry": {"type": "Polygon", "coordinates": [WARNING_RING]},
    "properties": {
        "id": "urn:oid:2.49.0.1.840.0.ffw.001.1",
        "areaDesc": "Bexar, TX; Comal, TX",
        "geocode": {"UGC": ["TXC029", "TXC091"], "SAME": ["048029", "048091"]},
        "sent": "2026-04-20T13:50:00-05:00",
        "effective": "2026-04-20T13:50:00-05:00",
        "onset": "2026-04-20T13:50:00-05:00",
        "expires": "2026-04-20T17:00:00-05:00",
        "ends": "2026-04-20T17:00:00-05:00",
        "status": "Actual",
        "messageType": "Alert",
        "severity": "Severe",
        "certainty": "Observed",
        "urgency": "Immediate",
        "event": "Flash Flood Warning",
        "senderName": "NWS Austin/San Antonio TX",
        "headline": "Flash Flood Warning issued April 20 at 1:50PM CDT",
        "description": "Thunderstorms producing heavy rain across the warned area.",
        "instruction": "Turn around, don't drown when encountering flooded roads.",
        "response": "Avoid",
        "parameters": {
            "flashFloodDamageThreat": ["CONSIDERABLE"],
            "AWIPSidentifier": ["FFWEWX"],
            "BLOCKCHANNEL": ["EAS", "NWEM"],
        },
    },
}

#: Zone-only: no geometry at all, so the adapter must drop it.
ZONE_ONLY_ALERT: dict[str, Any] = {
    "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.fla.002.1",
    "type": "Feature",
    "geometry": None,
    "properties": {
        "id": "urn:oid:2.49.0.1.840.0.fla.002.1",
        "areaDesc": "Travis, TX",
        "affectedZones": ["https://api.weather.gov/zones/county/TXC453"],
        "sent": "2026-04-20T13:55:00-05:00",
        "event": "Flood Watch",
        "severity": "Moderate",
        "headline": "Flood Watch issued April 20 at 1:55PM CDT",
    },
}

#: A bow tie: the ring is closed but self-intersecting, so shapely refuses it
#: and it must never reach the compiler as a road closure.
INVALID_POLYGON_ALERT: dict[str, Any] = {
    "id": "https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.ffw.003.1",
    "type": "Feature",
    "geometry": {
        "type": "Polygon",
        "coordinates": [
            [[-98.5, 29.4], [-98.4, 29.5], [-98.5, 29.5], [-98.4, 29.4], [-98.5, 29.4]]
        ],
    },
    "properties": {
        "id": "urn:oid:2.49.0.1.840.0.ffw.003.1",
        "sent": "2026-04-20T14:00:00-05:00",
        "event": "Flash Flood Warning",
        "severity": "Severe",
    },
}


def nws_body(*features: dict[str, Any]) -> dict[str, Any]:
    return {
        "@context": {"@version": "1.1"},
        "type": "FeatureCollection",
        "features": list(features),
        "title": "Current watches, warnings, and advisories for Texas",
        "updated": "2026-04-20T18:55:00+00:00",
    }


NWS_BODY = nws_body(POLYGON_ALERT, ZONE_ONLY_ALERT, INVALID_POLYGON_ALERT)


def gauge_reading(
    site: str, time: str, value: str, lon: float, lat: float, series: str = "ts-1"
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": f"{site}-{time}",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "time_series_id": series,
            "monitoring_location_id": f"USGS-{site}",
            "parameter_code": "00065",
            "statistic_id": "00011",
            "time": time,
            "value": value,
            "unit_of_measure": "ft",
            "approval_status": "Approved",
            "qualifier": None,
        },
    }


#: Blanco Rd crosses its 4.5 ft action stage between these two readings.
USGS_BODY: dict[str, Any] = {
    "type": "FeatureCollection",
    "features": [
        gauge_reading("08178593", "2026-04-20T21:00:00+00:00", "4.21", -98.5197, 29.5639),
        gauge_reading("08178593", "2026-04-20T21:15:00+00:00", "4.77", -98.5197, 29.5639),
        # No numeric value: the gauge was offline, so there is nothing to emit.
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-98.4128, 29.3572]},
            "properties": {
                "time_series_id": "ts-2",
                "monitoring_location_id": "USGS-08178800",
                "parameter_code": "00065",
                "time": "2026-04-20T21:15:00+00:00",
                "value": None,
                "unit_of_measure": "ft",
            },
        },
    ],
    "numberReturned": 3,
    "links": [],
    "timeStamp": "2026-04-20T21:20:00+00:00",
}
