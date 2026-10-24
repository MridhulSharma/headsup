"""GeoJSON geometries, validated with shapely.

Three things are checked on every geometry, because a bad polygon that reaches
the compiler becomes a bad road closure:

1. every position is finite and inside the WGS84 bounds (lon -180..180, lat -90..90);
2. polygon rings are closed and have at least four positions;
3. shapely agrees the geometry is valid and not empty (no bow ties, no empty sets).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator
from shapely.geometry import base as shapely_base
from shapely.geometry import shape as shapely_shape
from shapely.validation import explain_validity

from headsup.contracts.base import HeadsUpModel

LON_MIN, LON_MAX = -180.0, 180.0
LAT_MIN, LAT_MAX = -90.0, 90.0

#: A GeoJSON position: [lon, lat] or [lon, lat, altitude].
Position = Annotated[list[float], Field(min_length=2, max_length=3)]

#: A line needs two positions; a closed ring needs four (the last repeats the first).
Line = Annotated[list[Position], Field(min_length=2)]
Ring = Annotated[list[Position], Field(min_length=4)]
PolygonCoords = Annotated[list[Ring], Field(min_length=1)]


def _walk_positions(coords: Any) -> Iterator[list[float]]:
    """Yield every position in an arbitrarily nested coordinate array."""
    if coords and isinstance(coords[0], (int, float)):
        yield coords
        return
    for item in coords:
        yield from _walk_positions(item)


def _check_bounds(coords: Any) -> None:
    for position in _walk_positions(coords):
        if not all(math.isfinite(value) for value in position):
            raise ValueError(f"position {position} is not finite")
        lon, lat = position[0], position[1]
        if not LON_MIN <= lon <= LON_MAX:
            raise ValueError(f"longitude {lon} is outside WGS84 bounds [{LON_MIN}, {LON_MAX}]")
        if not LAT_MIN <= lat <= LAT_MAX:
            raise ValueError(f"latitude {lat} is outside WGS84 bounds [{LAT_MIN}, {LAT_MAX}]")


def _check_ring(ring: list[list[float]], where: str) -> None:
    if len(ring) < 4:
        raise ValueError(f"{where}: a polygon ring needs at least 4 positions, got {len(ring)}")
    if ring[0] != ring[-1]:
        raise ValueError(f"{where}: ring is not closed, {ring[0]} != {ring[-1]}")


class _GeometryBase(HeadsUpModel):
    """Common validation for every geometry type."""

    type: str
    coordinates: Any

    @property
    def __geo_interface__(self) -> dict[str, Any]:
        return {"type": self.type, "coordinates": self.coordinates}

    def _check_rings(self) -> None:
        """Hook for the polygon types; a no-op elsewhere."""

    @model_validator(mode="after")
    def _validate_geometry(self) -> _GeometryBase:
        _check_bounds(self.coordinates)
        self._check_rings()
        geom = shapely_shape(self)
        if geom.is_empty:
            raise ValueError(f"{self.type} geometry is empty")
        if not geom.is_valid:
            raise ValueError(f"invalid {self.type}: {explain_validity(geom)}")
        return self

    def to_shapely(self) -> shapely_base.BaseGeometry:
        """This geometry as a shapely object, in WGS84 degrees."""
        return shapely_shape(self)


class Point(_GeometryBase):
    """A single location, e.g. a stream gauge or a stalled vehicle."""

    type: Literal["Point"] = "Point"
    coordinates: Position


class MultiPoint(_GeometryBase):
    """Several locations, e.g. the dark signals in one outage region."""

    type: Literal["MultiPoint"] = "MultiPoint"
    coordinates: Annotated[list[Position], Field(min_length=1)]


class LineString(_GeometryBase):
    """A road segment or a keep-clear corridor centre line."""

    type: Literal["LineString"] = "LineString"
    coordinates: Line


class MultiLineString(_GeometryBase):
    """Several road segments, e.g. every approach to one intersection."""

    type: Literal["MultiLineString"] = "MultiLineString"
    coordinates: Annotated[list[Line], Field(min_length=1)]


class Polygon(_GeometryBase):
    """An area: the first ring is the exterior, the rest are holes."""

    type: Literal["Polygon"] = "Polygon"
    coordinates: PolygonCoords

    def _check_rings(self) -> None:
        for index, ring in enumerate(self.coordinates):
            _check_ring(ring, f"ring {index}")


class MultiPolygon(_GeometryBase):
    """Several areas, e.g. two low-water crossings under one warning."""

    type: Literal["MultiPolygon"] = "MultiPolygon"
    coordinates: Annotated[list[PolygonCoords], Field(min_length=1)]

    def _check_rings(self) -> None:
        for p_index, polygon in enumerate(self.coordinates):
            for r_index, ring in enumerate(polygon):
                _check_ring(ring, f"polygon {p_index} ring {r_index}")


#: Any geometry a contract may carry, discriminated on the GeoJSON `type`.
Geometry = Annotated[
    Point | MultiPoint | LineString | MultiLineString | Polygon | MultiPolygon,
    Field(discriminator="type"),
]

#: Geometries that can describe a keep-clear corridor.
Corridor = Annotated[LineString | Polygon, Field(discriminator="type")]

GEOMETRY_TYPES: tuple[type[_GeometryBase], ...] = (
    Point,
    MultiPoint,
    LineString,
    MultiLineString,
    Polygon,
    MultiPolygon,
)


#: Decimal places a geometry built by code is rounded to. 1e-7 degrees is
#: about a centimetre: a buffer or a union produces float noise per vertex, and
#: a geometry that travels over the bus into an audit record carries none of
#: it.
COORD_DIGITS = 7


def rings_of(
    area: shapely_base.BaseGeometry, digits: int = COORD_DIGITS
) -> list[list[list[float]]]:
    """One shapely polygon's rings as rounded GeoJSON coordinates."""
    exterior = getattr(area, "exterior", None)
    if exterior is None:
        raise ValueError(f"cannot take rings from {type(area).__name__}")
    rings = [exterior.coords, *(hole.coords for hole in area.interiors)]
    return [
        [[round(float(x), digits), round(float(y), digits)] for x, y in ring]
        for ring in rings
        if len(ring) >= 4
    ]


def area_geometry(
    area: shapely_base.BaseGeometry, digits: int = COORD_DIGITS
) -> Polygon | MultiPolygon | None:
    """A shapely area as a contract geometry, or None if it has no usable area.

    Everything that is not an area somebody could be told about - empty,
    invalid, a bare line where two shapes touched, a ring with too few points
    to close - comes back None, and the caller keeps whatever it had.

    `agents.fusion` and `agents.sentinels.signals` each predate this and keep
    their own private copy; new callers use this one.
    """
    if area.is_empty or not area.is_valid:
        return None
    try:
        if area.geom_type == "Polygon":
            rings = rings_of(area, digits)
            return Polygon(coordinates=rings) if rings else None
        if area.geom_type == "MultiPolygon":
            parts = [rings for part in area.geoms if (rings := rings_of(part, digits))]
            return MultiPolygon(coordinates=parts) if parts else None
    except ValueError:
        return None
    return None
