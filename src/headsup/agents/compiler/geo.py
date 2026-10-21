"""Measuring an advisory's footprint, and re-checking its geometry.

The area cap is in square kilometres, so the compiler measures on the WGS84
ellipsoid with `pyproj.Geod` rather than in square degrees: a degree of
longitude is 97 km in Bexar County and 111 km at the equator, and a cap that
drifted with latitude would not be a cap.

`sound` re-validates a geometry the compiler is about to sign. The contracts
already validate on construction, but the compiler is the last gate before a
signature and does not assume the object in front of it came through
validation.
"""

from __future__ import annotations

from pyproj import Geod
from shapely.errors import ShapelyError
from shapely.geometry import base as shapely_base
from shapely.validation import explain_validity

from headsup.contracts import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, Geometry

#: Areas and distances are measured on the WGS84 ellipsoid.
GEOD = Geod(ellps="WGS84")

#: Geometry types with no area of their own. A keep-clear line covers a
#: corridor on the ground, but its width lives in the action params, so the
#: area cap treats it as zero and the action's own limits apply.
ZERO_AREA_TYPES = frozenset({"Point", "MultiPoint", "LineString", "MultiLineString"})

SQ_M_PER_SQ_KM = 1_000_000.0


class GeometryError(ValueError):
    """A geometry cannot be measured or is not fit to sign."""


def to_shapely(geometry: Geometry | shapely_base.BaseGeometry) -> shapely_base.BaseGeometry:
    """Whatever came in, as a shapely geometry."""
    if isinstance(geometry, shapely_base.BaseGeometry):
        return geometry
    try:
        return geometry.to_shapely()
    except (ShapelyError, ValueError, TypeError, AttributeError) as exc:
        raise GeometryError(f"not a usable geometry: {exc}") from exc


def area_km2(geometry: Geometry | shapely_base.BaseGeometry) -> float:
    """Geodesic area in square kilometres; 0.0 for points and lines.

    The sign `Geod` returns depends on ring winding order, which GeoJSON does
    not fix, so the magnitude is what counts.
    """
    shape = to_shapely(geometry)
    if shape.geom_type in ZERO_AREA_TYPES:
        return 0.0
    try:
        area_m2, _perimeter = GEOD.geometry_area_perimeter(shape)
    except (ShapelyError, ValueError) as exc:
        raise GeometryError(f"cannot measure {shape.geom_type}: {exc}") from exc
    return abs(area_m2) / SQ_M_PER_SQ_KM


def sound(geometry: Geometry | shapely_base.BaseGeometry) -> str | None:
    """None if the geometry is fit to sign, else why it is not.

    Checks the three things that turn a bad polygon into a bad road closure:
    it is non-empty, shapely-valid (no bow ties), and inside the WGS84 bounds.
    """
    try:
        shape = to_shapely(geometry)
    except GeometryError as exc:
        return str(exc)
    if shape.is_empty:
        return f"{shape.geom_type} geometry is empty"
    if not shape.is_valid:
        return f"invalid {shape.geom_type}: {explain_validity(shape)}"
    lon_min, lat_min, lon_max, lat_max = shape.bounds
    if not (lon_min >= LON_MIN and lon_max <= LON_MAX):
        return f"longitude {lon_min}..{lon_max} is outside WGS84 bounds"
    if not (lat_min >= LAT_MIN and lat_max <= LAT_MAX):
        return f"latitude {lat_min}..{lat_max} is outside WGS84 bounds"
    return None


def covers(outer: Geometry, inner: Geometry) -> bool:
    """True if `outer` contains every point of `inner`.

    Used on a critic revision: a REVISE may narrow an advisory, never grow it.
    """
    return to_shapely(outer).buffer(0).covers(to_shapely(inner))
