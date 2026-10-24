"""The route responders take to a scene, as a keep-clear area.

Runs on the committed 4x4 grid, which has one fire station on the middle
block, so no city build is needed and the expected route length is a number
of 100 m blocks.
"""

from __future__ import annotations

import pytest

from fixtures.world import FIRE_STATION, at, grid_graph, grid_world
from headsup.agents.compiler.geo import area_km2, to_shapely
from headsup.agents.compiler.playbooks import default_playbooks
from headsup.contracts import HazardClass, Point
from headsup.world.responders import (
    NullSceneRouter,
    WorldSceneRouter,
    nearest_responders,
    route_line,
)

#: The caps the real playbook carries, so the test measures what a run would.
CAPS = default_playbooks().require(HazardClass.EMERGENCY_SCENE).keep_clear


def scene_at(east_blocks: float, north_blocks: float) -> Point:
    return Point(coordinates=list(at(east_blocks, north_blocks)))


def corridor(scene: Point, *, max_route_m: float | None = None):
    assert CAPS is not None
    return WorldSceneRouter(grid_world()).corridor(
        scene,
        width_m=CAPS.corridor_width_m,
        radius_m=CAPS.no_stop_radius_m,
        max_route_m=max_route_m if max_route_m is not None else CAPS.max_route_m,
    )


def test_the_playbook_is_where_the_three_numbers_come_from() -> None:
    """Nothing in the router has a width or a radius of its own."""
    assert CAPS is not None
    assert (CAPS.corridor_width_m, CAPS.no_stop_radius_m) == (30.0, 120.0)


def test_the_corridor_runs_from_the_nearest_station_to_the_scene() -> None:
    found = corridor(scene_at(3, 0))
    assert found is not None
    assert found.station_id == FIRE_STATION
    # Four 100 m blocks from the station's corner to the scene corner.
    assert found.route_m == pytest.approx(400.0, abs=1.0)
    assert found.geometry.type == "Polygon"


def test_the_area_covers_the_scene_and_the_route() -> None:
    scene = scene_at(3, 0)
    found = corridor(scene)
    assert found is not None
    area = to_shapely(found.geometry)
    assert area.contains(to_shapely(scene))
    # A point a third of the way along the route is inside the corridor.
    assert area.contains(found.route.interpolate(0.33, normalized=True))


def test_the_area_stays_inside_the_playbook_cap() -> None:
    """A corridor the compiler would refuse on area is not worth drawing."""
    playbook = default_playbooks().require(HazardClass.EMERGENCY_SCENE)
    found = corridor(scene_at(3, 0))
    assert found is not None
    assert area_km2(found.geometry) < playbook.max_area_km2


def test_a_route_longer_than_the_cap_draws_nothing() -> None:
    """A station further than the cap is not the one responding."""
    assert corridor(scene_at(3, 0), max_route_m=100.0) is None


def test_the_nearest_station_is_the_first_one_tried() -> None:
    lon, lat = at(3, 0)
    stations = nearest_responders(grid_world().pois, lon, lat)
    assert [poi.id for poi in stations] == [FIRE_STATION]


def test_a_route_to_where_you_already_are_is_not_a_route() -> None:
    graph = grid_graph()
    line, length = route_line(graph, 0, 0)
    assert line is None
    assert length == 0.0


def test_the_null_router_says_it_knows_no_city() -> None:
    """The default, so a proposer runs before a world is built - and says so."""
    router = NullSceneRouter()
    assert router.corridor(scene_at(1, 1), width_m=30, radius_m=120, max_route_m=3000) is None
    assert "no road graph" in router.describes_itself()
