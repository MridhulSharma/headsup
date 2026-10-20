"""Geometry validation: valid, closed, inside WGS84.

A bad polygon that gets past this file becomes a bad road closure, so the
rejections matter more than the acceptances.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from headsup.contracts import Geometry, LineString, MultiPolygon, Point, Polygon
from samples import CROSSING_RING

geometry_adapter: TypeAdapter[Geometry] = TypeAdapter(Geometry)

SQUARE = [[-98.4946, 29.4195], [-98.4921, 29.4195], [-98.4921, 29.4213], [-98.4946, 29.4213]]


def closed(ring: list[list[float]]) -> list[list[float]]:
    return [*ring, ring[0]]


def test_valid_polygon_is_accepted_and_reaches_shapely() -> None:
    polygon = Polygon(coordinates=[closed(SQUARE)])
    assert polygon.to_shapely().is_valid
    assert polygon.to_shapely().area > 0


def test_polygon_with_a_hole_is_accepted() -> None:
    hole = [[-98.4940, 29.4200], [-98.4930, 29.4200], [-98.4930, 29.4208], [-98.4940, 29.4208]]
    polygon = Polygon(coordinates=[closed(SQUARE), closed(hole)])
    assert len(polygon.coordinates) == 2


def test_geometry_union_discriminates_on_type() -> None:
    parsed = geometry_adapter.validate_python({"type": "Point", "coordinates": [-98.49, 29.42]})
    assert isinstance(parsed, Point)


def test_unknown_geometry_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        geometry_adapter.validate_python({"type": "Pyramid", "coordinates": [0, 0]})


def test_unclosed_ring_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not closed"):
        Polygon(coordinates=[SQUARE])


def test_ring_shorter_than_four_positions_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 4"):
        Polygon(coordinates=[[[-98.49, 29.42], [-98.48, 29.42], [-98.49, 29.42]]])


def test_self_intersecting_polygon_is_rejected() -> None:
    bowtie = [
        [-98.4946, 29.4195],
        [-98.4921, 29.4213],
        [-98.4921, 29.4195],
        [-98.4946, 29.4213],
        [-98.4946, 29.4195],
    ]
    with pytest.raises(ValidationError, match="Self-intersection"):
        Polygon(coordinates=[bowtie])


def test_empty_coordinates_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Polygon(coordinates=[])


def test_zero_area_polygon_is_rejected() -> None:
    """Three collinear corners is not an area, however well formed it looks."""
    line = [[-98.49, 29.42], [-98.48, 29.42], [-98.47, 29.42], [-98.49, 29.42]]
    with pytest.raises(ValidationError):
        Polygon(coordinates=[line])


@pytest.mark.parametrize(
    ("lon", "lat"),
    [(-180.1, 29.42), (180.1, 29.42), (-98.49, 90.5), (-98.49, -90.5), (999.0, 999.0)],
)
def test_positions_outside_wgs84_bounds_are_rejected(lon: float, lat: float) -> None:
    with pytest.raises(ValidationError, match="outside WGS84 bounds"):
        Point(coordinates=[lon, lat])


def test_non_finite_positions_are_rejected() -> None:
    with pytest.raises(ValidationError, match="not finite"):
        Point(coordinates=[float("nan"), 29.42])


def test_a_position_needs_two_numbers() -> None:
    with pytest.raises(ValidationError):
        Point(coordinates=[-98.49])


def test_altitude_is_allowed_but_a_fourth_number_is_not() -> None:
    assert Point(coordinates=[-98.49, 29.42, 198.0]).coordinates[2] == 198.0
    with pytest.raises(ValidationError):
        Point(coordinates=[-98.49, 29.42, 198.0, 1.0])


def test_linestring_needs_two_positions() -> None:
    with pytest.raises(ValidationError):
        LineString(coordinates=[[-98.49, 29.42]])


def test_multipolygon_rings_are_checked_too() -> None:
    assert MultiPolygon(coordinates=[[CROSSING_RING]]).to_shapely().is_valid
    with pytest.raises(ValidationError, match="polygon 0 ring 0"):
        MultiPolygon(coordinates=[[SQUARE]])


def test_overlapping_multipolygon_is_rejected() -> None:
    """Shapely calls overlapping parts invalid, and so should an advisory."""
    shifted = [[lon + 0.0005, lat] for lon, lat in CROSSING_RING]
    with pytest.raises(ValidationError):
        MultiPolygon(coordinates=[[CROSSING_RING], [shifted]])


def test_geometry_mutation_is_revalidated() -> None:
    polygon = Polygon(coordinates=[closed(SQUARE)])
    with pytest.raises(ValidationError, match="not closed"):
        polygon.coordinates = [SQUARE]
