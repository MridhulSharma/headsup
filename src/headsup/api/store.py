"""Reading the streams back: what is in force, and what is believed.

`GET /advisories` and `GET /hazards` answer off the streams rather than off a
cache the server keeps, because the streams are the record and a cache is a
second version of the truth. A scenario run writes a few dozen advisories and
a few hundred hazards, so reading the range is cheaper than keeping either in
step would be.

Both endpoints collapse the stream to one entry per id. Fusion re-emits the
same hazard id as evidence accumulates, and the compiler re-issues an advisory
id with a higher `version`, so the newest entry for an id is the current one
and the older ones are history a reader of the stream can still see.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import datetime

from shapely.geometry import box as shapely_box

from headsup.agents.compiler.geo import to_shapely
from headsup.bus import STREAM_ADVISORIES, STREAM_HAZARDS, Bus, BusError
from headsup.contracts import Advisory, Hazard, utcnow

log = logging.getLogger(__name__)

#: Entries read back per request. Well above what one scenario run writes.
READ_LIMIT = 2000

#: A bbox query: `west,south,east,north` in degrees.
BBOX_PARTS = 4


class QueryError(ValueError):
    """A query parameter the server will not guess at."""


def parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """`west,south,east,north` as floats, or None when the query omits it."""
    if raw is None or not raw.strip():
        return None
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != BBOX_PARTS:
        raise QueryError(f"bbox needs {BBOX_PARTS} comma-separated degrees: west,south,east,north")
    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError as exc:
        raise QueryError(f"bbox is not four numbers: {raw!r}") from exc
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise QueryError(f"bbox longitudes must be within -180..180, got {west},{east}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise QueryError(f"bbox latitudes must be within -90..90, got {south},{north}")
    if west >= east or south >= north:
        raise QueryError(f"bbox must be west<east and south<north, got {raw!r}")
    return west, south, east, north


def intersects(geometry: object, bbox: tuple[float, float, float, float] | None) -> bool:
    """True when a geometry reaches into the bbox, or when there is no bbox."""
    if bbox is None:
        return True
    try:
        return to_shapely(geometry).intersects(shapely_box(*bbox))  # type: ignore[arg-type]
    except Exception:  # a geometry that will not parse matches no query
        return False


async def _newest_by_id(bus: Bus, stream: str, model: type, limit: int) -> list:
    """Every entry on a stream that parses, newest kept per id, oldest first."""
    newest: dict[str, object] = {}
    try:
        async for message in bus.replay(stream, limit=limit):
            try:
                parsed = message.parse(model)
            except BusError as exc:
                log.warning("%s %s does not parse: %s", stream, message.id, exc)
                continue
            newest[_identity(parsed)] = parsed
    except BusError as exc:
        raise QueryError(f"cannot read {stream}: {exc}") from exc
    return list(newest.values())


def _identity(parsed: object) -> str:
    if isinstance(parsed, Advisory):
        return parsed.properties.id
    return str(getattr(parsed, "id", id(parsed)))


async def read_advisories(
    bus: Bus,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    active: bool = True,
    at: datetime | None = None,
    limit: int = READ_LIMIT,
) -> list[Advisory]:
    """Published advisories, newest version per id, filtered as asked."""
    moment = at or utcnow()
    items: Iterable[Advisory] = await _newest_by_id(bus, STREAM_ADVISORIES, Advisory, limit)
    return sorted(
        (
            advisory
            for advisory in items
            if (not active or advisory.is_active(moment)) and intersects(advisory.geometry, bbox)
        ),
        key=lambda advisory: advisory.properties.valid_from,
    )


async def read_hazards(
    bus: Bus,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    active: bool = True,
    at: datetime | None = None,
    limit: int = READ_LIMIT,
) -> list[Hazard]:
    """Fused hazards, newest per id, filtered as asked.

    `active` here means not yet expired. A hazard has no start instant: it is
    believed from the moment it is first fused.
    """
    moment = at or utcnow()
    items: Iterator[Hazard] = iter(await _newest_by_id(bus, STREAM_HAZARDS, Hazard, limit))
    return sorted(
        (
            hazard
            for hazard in items
            if (not active or hazard.valid_until > moment) and intersects(hazard.geometry, bbox)
        ),
        key=lambda hazard: hazard.updated_at,
    )
