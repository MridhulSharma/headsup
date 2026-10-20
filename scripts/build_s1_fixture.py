"""Rebuild the committed S1 recording fixture from the public archives.

    uv run python scripts/build_s1_fixture.py

S1 is the San Antonio flash flood of 20-21 April 2026. Everything the fixture
replays was observed by NWS or USGS; nothing in it is generated. Every source
URL, site id, VTEC event and retrieval window this script uses is read from
`scenarios/s1_flood.yaml`, so that file stays the single description of the
scenario and this script stays a way of carrying it out.

NWS, envelope reconstructed
    The Flash Flood Warning products for KEWX events 0009 and 0010 come from
    the IEM VTEC archive. Their polygons, timestamps, VTEC strings, county
    codes and narratives are used verbatim: the storm-based warning polygon is
    decoded from each product's `LAT...LON` block, where pairs are hundredths
    of a degree, latitude first and longitude positive west.

    The surrounding api.weather.gov alert JSON is *rebuilt* around that
    content. It has to be: `/alerts/active` serves only currently active
    alerts, and the archive behind it does not reach back to April 2026. So
    the values are real and the envelope carrying them is reconstructed, which
    is what `provenance.nws.envelope: reconstructed` in the scenario file
    records. The envelope is shaped to match what `NwsAdapter` parses live.

USGS, envelope verbatim
    Gauge-height features exactly as the Water Data OGC API `continuous`
    collection returns them, for the Salado Creek sites named in the scenario
    file. No reshaping at all.

Both feeds are then stitched into poll-shaped snapshots, so the recording
looks like a session that polled through the event: one NWS fetch each time a
product was issued, one USGS fetch per `feeds.usgs.poll_minutes` carrying the
readings due in that window.

The archives are stable for an event this old, so a rebuild is byte-identical
to the committed fixture. If it is not, something upstream was revised and the
difference is worth reading before committing it.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import yaml

from headsup.ingest import ALERTS_URL, user_agent
from headsup.ingest.usgs import COLLECTION_HISTORY, collection_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_FILE = PROJECT_ROOT / "scenarios" / "s1_flood.yaml"

VTEC_RE = re.compile(r"/O\.(\w+)\.(K\w+)\.(\w\w)\.(\w)\.(\d{4})\.(\w+)-(\w+)/")
LATLON_RE = re.compile(r"LAT\.\.\.LON(.*?)(?:\n\n|\$\$)", re.S)
UGC_RE = re.compile(r"^([A-Z]{2}C[\d>\-]+)-(\d{6})-$", re.M)
#: `2026.KEWX.FF.W.0009` as the scenario file writes a VTEC event.
EVENT_RE = re.compile(r"^(\d{4})\.K(\w{3})\.(\w\w)\.(\w)\.(\d{4})$")

#: UGC county codes to the names and SAME codes the alerts API publishes.
COUNTIES = {
    "TXC029": ("Bexar", "048029"),
    "TXC091": ("Comal", "048091"),
    "TXC187": ("Guadalupe", "048187"),
}

CENTRAL = ZoneInfo("America/Chicago")


# --- scenario file ----------------------------------------------------------


def load_scenario() -> dict[str, Any]:
    return yaml.safe_load(SCENARIO_FILE.read_text(encoding="utf-8"))


def base_url(url: str) -> str:
    """A source URL with any example query string dropped."""
    return url.split("?", 1)[0]


def as_utc(value: Any) -> datetime:
    """A timestamp from the scenario file as an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def vtec_events(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """`feeds.nws.vtec_events` parsed into IEM query parameters."""
    parsed = []
    for name in scenario["feeds"]["nws"]["vtec_events"]:
        match = EVENT_RE.match(str(name))
        if not match:
            raise SystemExit(f"cannot read VTEC event {name!r} from the scenario file")
        year, office, phenomena, significance, etn = match.groups()
        parsed.append(
            {
                "wfo": office,
                "year": int(year),
                "phenomena": phenomena,
                "significance": significance,
                "etn": int(etn),
            }
        )
    return parsed


# --- NWS product text -------------------------------------------------------


def vtec_time(text: str) -> datetime:
    """`260420T1850Z` -> aware UTC datetime."""
    return datetime.strptime(text, "%y%m%dT%H%MZ").replace(tzinfo=UTC)


def polygon(text: str) -> list[list[float]]:
    """The storm-based warning polygon from a product's LAT...LON block."""
    block = LATLON_RE.search(text)
    if not block:
        raise ValueError("no LAT...LON block")
    nums = [int(n) for n in re.findall(r"\d{3,5}", block.group(1))]
    ring = [[-nums[i + 1] / 100.0, nums[i] / 100.0] for i in range(0, len(nums) - 1, 2)]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    return ring


def section(text: str, start: str, stop: str | None) -> str:
    begin = text.find(start)
    if begin < 0:
        return ""
    begin += len(start)
    end = text.find(stop, begin) if stop else -1
    return text[begin : end if end > 0 else len(text)].strip()


def narrative(text: str) -> tuple[str, str]:
    """The description and the preparedness instruction, as issued."""
    body = section(text, "* Flash Flood Warning for...", "PRECAUTIONARY/PREPAREDNESS ACTIONS")
    if not body:
        body = section(text, "Flash Flood Warning", "PRECAUTIONARY/PREPAREDNESS ACTIONS")
    instruction = section(text, "PRECAUTIONARY/PREPAREDNESS ACTIONS...", "&&")
    return body.strip(), instruction.strip()


def local(moment: datetime) -> str:
    """`April 20 at 1:50PM CDT`, the way the alerts API writes a headline."""
    stamp = moment.astimezone(CENTRAL)
    if sys.platform != "win32":
        return stamp.strftime("%B %-d at %-I:%M%p %Z")
    return (
        f"{stamp.strftime('%B')} {stamp.day} at "
        f"{stamp.strftime('%I:%M%p').lstrip('0')} {stamp.strftime('%Z')}"
    )


def headline_text(onset: datetime, expires: datetime) -> str:
    return (
        f"Flash Flood Warning issued {local(onset)} until {local(expires)} "
        "by NWS Austin/San Antonio TX"
    )


def ugc_codes(text: str) -> list[str]:
    match = UGC_RE.search(text)
    if not match:
        return []
    state, codes = match.group(1)[:3], match.group(1)[3:]
    out: list[str] = []
    for part in codes.split("-"):
        if ">" in part:
            lo, hi = part.split(">")
            out.extend(f"{state}{n:03d}" for n in range(int(lo), int(hi) + 1))
        elif part:
            out.append(f"{state}{part}")
    return out


def alert_feature(product: dict[str, Any], text: str) -> dict[str, Any]:
    """One archived product as an api.weather.gov alert feature."""
    vtec = VTEC_RE.search(text)
    if not vtec:
        raise ValueError("no VTEC string")
    action, office, phenom, sig, etn, begins, ends = vtec.groups()
    sent = datetime.strptime(product["valid"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    onset = vtec_time(begins) if begins != "000000T0000Z" else sent
    expires = vtec_time(ends)
    description, instruction = narrative(text)
    ugc = ugc_codes(text)
    headline = headline_text(onset, expires)
    damage = section(text, "FLASH FLOOD DAMAGE THREAT...", "\n")
    alert_id = f"urn:oid:2.49.0.1.840.0.iem.{product['product_id']}"

    return {
        "id": f"https://api.weather.gov/alerts/{alert_id}",
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [polygon(text)]},
        "properties": {
            "@id": f"https://api.weather.gov/alerts/{alert_id}",
            "@type": "wx:Alert",
            "id": alert_id,
            "areaDesc": "; ".join(f"{COUNTIES[c][0]}, TX" for c in ugc if c in COUNTIES),
            "geocode": {
                "UGC": ugc,
                "SAME": [COUNTIES[c][1] for c in ugc if c in COUNTIES],
            },
            "affectedZones": [],
            "references": [],
            "sent": iso(sent),
            "effective": iso(sent),
            "onset": iso(onset),
            "expires": iso(expires),
            "ends": iso(expires),
            "status": "Actual",
            "messageType": {"NEW": "Alert", "CAN": "Cancel", "EXP": "Cancel"}.get(action, "Update"),
            "category": "Met",
            "severity": "Severe",
            "certainty": "Observed",
            "urgency": "Immediate",
            "event": "Flash Flood Warning",
            "sender": "w-nws.webmaster@noaa.gov",
            "senderName": "NWS Austin/San Antonio TX",
            "headline": headline,
            "description": description,
            "instruction": instruction,
            "response": "Avoid",
            "parameters": {
                "AWIPSidentifier": [product["product_id"].split("-")[-1]],
                "VTEC": [f"/O.{action}.{office}.{phenom}.{sig}.{etn}.{begins}-{ends}/"],
                "flashFloodDamageThreat": [damage] if damage else [],
                "NWSheadline": [headline.upper()],
            },
            "scope": "Public",
            "code": "IPAWSv1.0",
            "language": "en-US",
            "web": "http://www.weather.gov",
            "eventCode": {"SAME": ["FFW"], "NationalWeatherService": ["FFW"]},
        },
    }


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def line(payload: dict[str, Any]) -> bytes:
    """One recorder-shaped JSONL line, compact so the fixture stays small."""
    return json.dumps(payload, separators=(",", ":")).encode()


# --- building ---------------------------------------------------------------


def build_nws(client: httpx.Client, scenario: dict[str, Any]) -> list[bytes]:
    """One recorded poll per issued product, in issue order."""
    url = base_url(scenario["provenance"]["nws"]["source_url"])
    alerts_url = f"{ALERTS_URL}?area=TX"

    products: list[tuple[datetime, dict[str, Any]]] = []
    for params in vtec_events(scenario):
        event = client.get(url, params=params).json()
        for product in [event["report"], *event.get("svs", [])]:
            valid = datetime.strptime(product["valid"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            products.append((valid, product))
    products.sort(key=lambda item: item[0])
    print(f"NWS products from the IEM VTEC archive: {len(products)}")

    lines: list[bytes] = []
    for seq, (valid, product) in enumerate(products):
        feature = alert_feature(product, product["text"])
        lines.append(
            line(
                {
                    "source": "nws",
                    "ref": f"nws/{valid.strftime('%Y%m%dT%H%M%SZ')}/{seq}",
                    "url": alerts_url,
                    "fetched_at": iso(valid),
                    "body": {
                        "@context": {"@version": "1.1"},
                        "type": "FeatureCollection",
                        "features": [feature],
                        "title": "Current watches, warnings, and advisories for Texas",
                        "updated": iso(valid),
                    },
                }
            )
        )
        points = len(feature["geometry"]["coordinates"][0]) - 1
        print(f"  {iso(valid)}  {feature['properties']['messageType']:7s} {points} pt polygon")
    return lines


def build_usgs(client: httpx.Client, scenario: dict[str, Any]) -> list[bytes]:
    """Readings grouped into fixed polls, each stamped at the end of its window."""
    feed = scenario["feeds"]["usgs"]
    source = scenario["provenance"]["usgs"]
    url = base_url(source["source_url"])
    expected = collection_url(COLLECTION_HISTORY)
    if url != expected:
        print(f"  note: scenario URL {url} is not the adapter's {expected}")

    start = as_utc(source["retrieval_window"]["start"])
    end = as_utc(source["retrieval_window"]["end"])
    step = timedelta(minutes=feed["poll_minutes"])

    readings: list[dict[str, Any]] = []
    for gauge in feed["gauges"]:
        site = f"USGS-{gauge['site']}"
        page = client.get(
            url,
            params={
                "monitoring_location_id": site,
                "parameter_code": feed["parameter"],
                "datetime": f"{iso(start)}/{iso(end)}",
                "limit": 500,
                "f": "json",
            },
        ).json()
        features = page.get("features", [])
        print(f"USGS {site}: {len(features)} gauge-height readings ({gauge['name']})")
        readings.extend(features)

    def at(feature: dict[str, Any]) -> datetime:
        return datetime.fromisoformat(feature["properties"]["time"])

    readings.sort(key=at)
    lines: list[bytes] = []
    seq = 0
    cursor = start
    while cursor < end:
        nxt = cursor + step
        batch = [item for item in readings if cursor <= at(item) < nxt]
        if batch:
            lines.append(
                line(
                    {
                        "source": "usgs",
                        "ref": f"usgs/{nxt.strftime('%Y%m%dT%H%M%SZ')}/{seq}",
                        "url": url,
                        "fetched_at": iso(nxt),
                        "body": {
                            "type": "FeatureCollection",
                            "features": batch,
                            "numberReturned": len(batch),
                            "links": [],
                            "timeStamp": iso(nxt),
                        },
                    }
                )
            )
            seq += 1
        cursor = nxt
    return lines


def main() -> int:
    scenario = load_scenario()
    out = PROJECT_ROOT / scenario["recording"]["fixture"]
    out.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(timeout=90.0, headers={"User-Agent": user_agent()}, follow_redirects=True)
    try:
        nws_lines = build_nws(client, scenario)
        usgs_lines = build_usgs(client, scenario)
    finally:
        client.close()

    (out / "nws.jsonl").write_bytes(b"\n".join(nws_lines) + b"\n")
    (out / "usgs.jsonl").write_bytes(b"\n".join(usgs_lines) + b"\n")

    total = 0
    for path in sorted(out.glob("*.jsonl")):
        size = path.stat().st_size
        total += size
        payloads = len(path.read_bytes().splitlines())
        print(f"{path.relative_to(PROJECT_ROOT).as_posix()}: {payloads} payloads, {size} bytes")
    print(f"total {total} bytes; scenario file says {scenario['recording']['bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
