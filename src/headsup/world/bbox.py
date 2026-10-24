"""The patch of each city Heads Up builds a road graph for.

A bbox, not a city boundary: the graph is the stage the simulator drives on,
and it has to download, simplify and index in seconds rather than minutes. So
each city is one rectangle chosen around the scenario it hosts.

* Austin is downtown, the grid the S2 signal outage and the D1.5 viewer demo
  sit on.
* San Antonio is the Salado Creek corridor through the north-east of the city,
  from the Loop 1604 crossing down to Wurzbach Parkway, under the S1 flash
  flood warning polygon. The warning itself covers most of three counties -
  73 by 45 km - which is a download, not a stage, so the bbox is the corridor
  the fleet would actually route over.

The keys match `headsup.ingest.cities.CITIES`, and a test holds the two
registries to that.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.affinity import scale as affine_scale
from shapely.geometry import Polygon as ShapelyPolygon
from shapely.geometry import base as shapely_base

#: Metres per degree of latitude on the WGS84 ellipsoid, near enough to size a
#: bbox with. Exact measurement is `headsup.agents.compiler.geo.area_km2`.
M_PER_DEG_LAT = 111_132.0
M_PER_DEG_LON_EQUATOR = 111_320.0


@dataclass(frozen=True, slots=True)
class BBox:
    """One city's download window, in WGS84 degrees.

    Stored as west/south/east/north because that is the order OSMnx 2.x takes
    a bbox in, and a transposed bbox downloads the wrong city in silence.
    """

    west: float
    south: float
    east: float
    north: float

    def __post_init__(self) -> None:
        if self.west >= self.east:
            raise ValueError(f"west {self.west} is not west of east {self.east}")
        if self.south >= self.north:
            raise ValueError(f"south {self.south} is not south of north {self.north}")

    @property
    def as_tuple(self) -> tuple[float, float, float, float]:
        """`(west, south, east, north)`, ready for `osmnx.graph_from_bbox`."""
        return (self.west, self.south, self.east, self.north)

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.west + self.east) / 2, (self.south + self.north) / 2)

    @property
    def width_km(self) -> float:
        _lon, lat = self.centre
        metres_per_deg = M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat))
        return (self.east - self.west) * metres_per_deg / 1000.0

    @property
    def height_km(self) -> float:
        return (self.north - self.south) * M_PER_DEG_LAT / 1000.0

    @property
    def area_km2(self) -> float:
        return self.width_km * self.height_km

    def contains(self, lon: float, lat: float) -> bool:
        return self.west <= lon <= self.east and self.south <= lat <= self.north

    def to_shapely(self) -> ShapelyPolygon:
        return ShapelyPolygon(
            [
                (self.west, self.south),
                (self.east, self.south),
                (self.east, self.north),
                (self.west, self.north),
                (self.west, self.south),
            ]
        )

    def __str__(self) -> str:
        return (
            f"{self.west:.4f},{self.south:.4f},{self.east:.4f},{self.north:.4f} "
            f"({self.width_km:.1f}x{self.height_km:.1f} km)"
        )


#: Downtown Austin: the Capitol down to the river, Lamar across to I-35.
AUSTIN_BOX = BBox(west=-97.7600, south=30.2580, east=-97.7280, north=30.2820)

#: North-east San Antonio along Salado Creek: the Loop 1604 crossing down to
#: Wurzbach Parkway. Both of the upstream gauges the S1 recording replays are
#: inside it - Salado Ck at Wilderness Rd, by the 1604 crossing, and at Blanco
#: Rd - so a gauge crossing its action stage has roads near it to be about.
SAN_ANTONIO_BOX = BBox(west=-98.5800, south=29.5400, east=-98.4900, north=29.6400)

BOXES: dict[str, BBox] = {
    "austin": AUSTIN_BOX,
    "san_antonio": SAN_ANTONIO_BOX,
}


def box(city: str) -> BBox:
    """Look up a city's bbox by key, with the available keys in the error."""
    try:
        return BOXES[city]
    except KeyError:
        known = ", ".join(sorted(BOXES))
        raise ValueError(f"no bbox for city {city!r}; known cities: {known}") from None


def buffer_m(geometry: shapely_base.BaseGeometry, metres: float) -> shapely_base.BaseGeometry:
    """Grow a geometry by a fixed number of metres on the ground.

    One buffer radius in degrees cannot be right in both axes at once - at
    30 degrees north a degree of longitude is about 0.87 of a degree of
    latitude - so the shape is scaled into a local metric frame, buffered
    there and scaled back.

    `agents.sentinels.base.pad_m` and `sim.masks.corridor_polygon` do the same
    thing for their own layers. This copy exists because the world layer
    cannot import either of them: `agents.sentinels.base` imports the
    constants above, so reaching back into it from here is an import cycle.
    """
    if geometry.is_empty or metres <= 0:
        return geometry
    cos_lat = max(math.cos(math.radians(float(geometry.centroid.y))), 1e-6)
    sx = M_PER_DEG_LON_EQUATOR * cos_lat
    sy = M_PER_DEG_LAT
    metric = affine_scale(geometry, xfact=sx, yfact=sy, origin=(0.0, 0.0))
    return affine_scale(metric.buffer(metres), xfact=1 / sx, yfact=1 / sy, origin=(0.0, 0.0))
