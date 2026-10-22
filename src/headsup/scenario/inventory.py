"""The real Austin signal map, as a committed snapshot.

`hu scenario synth-blackout` puts synthetic flashing signals on real
intersections, so it needs the city's signal inventory - which is a different
dataset from the status feed the adapter polls. "Traffic Signals and
Pedestrian Signals" (`p53x-x73x`) is the inventory; it changes a few times a
year, so it is fetched once and committed rather than polled.

The snapshot is trimmed on the way in: traffic signals that are turned on,
inside the city's built bbox, with only the five columns the synth needs. That
is about 180 intersections and 30 KB, small enough to read in a diff - which
matters, because every synthetic scenario's geometry comes from this file.

    uv run hu scenario signals --refresh     # re-fetch and rewrite it
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import orjson

from headsup.ingest.austin_signals import SIGNAL_INVENTORY_DATASET, SODA_BASE, app_token
from headsup.ingest.base import DEFAULT_TIMEOUT, IngestError, user_agent
from headsup.world.bbox import BBox, box

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Where the committed snapshots live, one file per city.
SIGNALS_DIR = PROJECT_ROOT / "data" / "signals"

#: Only signals that control traffic, and only the ones actually in service.
SIGNAL_TYPE = "TRAFFIC"
SIGNAL_STATUS = "TURNED_ON"

#: Columns kept. Everything else in the dataset - cabinet ids, retiming zones,
#: bus stops - is irrelevant to where an intersection is.
COLUMNS = ("signal_id", "location_name", "primary_st", "cross_st", "location")


@dataclass(frozen=True, slots=True)
class Signal:
    """One real intersection, as the city maps it."""

    signal_id: str
    name: str
    lon: float
    lat: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "location_name": self.name,
            "location": {"type": "Point", "coordinates": [self.lon, self.lat]},
        }


@dataclass(frozen=True, slots=True)
class SignalInventory:
    """A snapshot of one city's signal map, in signal-id order."""

    city: str
    dataset: str
    source_url: str
    retrieved_at: str
    bbox: tuple[float, float, float, float]
    signals: tuple[Signal, ...]

    def __len__(self) -> int:
        return len(self.signals)

    def __iter__(self) -> Iterator[Signal]:
        return iter(self.signals)

    @classmethod
    def path_for(cls, city: str, *, root: Path | None = None) -> Path:
        return (root or SIGNALS_DIR) / f"{city}.json"

    @classmethod
    def load(cls, city: str = "austin", *, root: Path | None = None) -> SignalInventory:
        """Read the committed snapshot, or say how to make one."""
        path = cls.path_for(city, root=root)
        if not path.is_file():
            raise IngestError(
                f"no signal map for {city} at {path.as_posix()}; "
                f"run `hu scenario signals --city {city} --refresh` to fetch one"
            )
        try:
            document = orjson.loads(path.read_bytes())
        except orjson.JSONDecodeError as exc:
            raise IngestError(f"{path.as_posix()} is not readable JSON: {exc}") from exc
        return cls(
            city=str(document.get("city", city)),
            dataset=str(document.get("dataset", SIGNAL_INVENTORY_DATASET)),
            source_url=str(document.get("source_url", "")),
            retrieved_at=str(document.get("retrieved_at", "")),
            bbox=tuple(document.get("bbox", (0.0, 0.0, 0.0, 0.0))),  # type: ignore[arg-type]
            signals=tuple(_signal(record) for record in document.get("signals", [])),
        )

    def write(self, *, root: Path | None = None) -> Path:
        """Write the snapshot, newest fetch wins, one signal per line's worth."""
        path = self.path_for(self.city, root=root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            orjson.dumps(
                {
                    "city": self.city,
                    "dataset": self.dataset,
                    "source_url": self.source_url,
                    "retrieved_at": self.retrieved_at,
                    "bbox": list(self.bbox),
                    "signal_type": SIGNAL_TYPE,
                    "signal_status": SIGNAL_STATUS,
                    "count": len(self.signals),
                    "signals": [signal.to_dict() for signal in self.signals],
                },
                option=orjson.OPT_INDENT_2,
            )
        )
        return path


def _signal(record: Any) -> Signal:
    """One inventory row, from the portal or from the snapshot."""
    if not isinstance(record, dict):
        raise IngestError(f"a signal must be an object, got {type(record).__name__}")
    location = record.get("location")
    if not isinstance(location, dict) or "coordinates" not in location:
        raise IngestError(f"signal {record.get('signal_id')!r} has no point location")
    lon, lat = location["coordinates"][0], location["coordinates"][1]
    name = str(record.get("location_name") or "").strip()
    if not name:
        primary = str(record.get("primary_st") or "").strip()
        cross = str(record.get("cross_st") or "").strip()
        name = f"{primary} / {cross}".strip(" /")
    return Signal(
        signal_id=str(record.get("signal_id") or "").strip(),
        name=" ".join(name.split()),
        lon=float(lon),
        lat=float(lat),
    )


def soda_url(dataset: str = SIGNAL_INVENTORY_DATASET) -> str:
    return f"{SODA_BASE}/{dataset}.json"


def _where(bounds: BBox) -> str:
    """Socrata's `within_box` takes the north-west corner, then the south-east."""
    return (
        f"within_box(location, {bounds.north}, {bounds.west}, {bounds.south}, {bounds.east})"
        f" AND signal_type='{SIGNAL_TYPE}' AND signal_status='{SIGNAL_STATUS}'"
    )


async def fetch(city: str = "austin", *, limit: int = 2000) -> SignalInventory:
    """Fetch the signal map for a city's bbox from the Socrata portal.

    The only networked call in this package. `hu scenario synth-blackout`
    itself never reaches the network: it reads the snapshot this writes.
    """
    import httpx

    bounds = box(city)
    url = soda_url()
    params = {
        "$select": ",".join(COLUMNS),
        "$where": _where(bounds),
        "$order": "signal_id",
        "$limit": limit,
    }
    token = app_token()
    headers = {"User-Agent": user_agent()}
    if token:
        headers["X-App-Token"] = token

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        try:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            rows = response.json()
        except httpx.HTTPError as exc:
            raise IngestError(f"signal map: GET {url} failed: {exc}") from exc
        except ValueError as exc:
            raise IngestError(f"signal map: GET {url} did not return JSON") from exc
    if not isinstance(rows, list):
        raise IngestError(f"signal map: GET {url} returned {type(rows).__name__}")

    signals = sorted(
        (_signal(row) for row in rows),
        key=lambda item: (int(item.signal_id) if item.signal_id.isdigit() else 0, item.signal_id),
    )
    return SignalInventory(
        city=city,
        dataset=SIGNAL_INVENTORY_DATASET,
        source_url=url,
        retrieved_at=datetime.now().astimezone().date().isoformat(),
        bbox=bounds.as_tuple,
        signals=tuple(signals),
    )
