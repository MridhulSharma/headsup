"""The `roads` and `pois` tables, against the real PostGIS from compose.

Marked `integration`: the default `-m "not llm"` run includes these, so they
skip rather than fail when Docker is not up, the same way `test_infra` does.

The fixture grid is written under a city name no real build uses, so running
these never disturbs a city a demo has loaded.
"""

from __future__ import annotations

import pytest
import sqlalchemy

from fixtures.world import HOSPITAL, at, grid_graph, grid_world
from headsup.world.db import (
    POIS_TABLE,
    ROADS_TABLE,
    DbError,
    critical_pois_near,
    database_url,
    engine,
    table_counts,
    write_world,
)
from headsup.world.model import World

pytestmark = pytest.mark.integration

HINT = "PostGIS is not up - run `hu infra up`"

#: Not `austin` or `san_antonio`: these tests delete and rewrite their city.
TEST_CITY = "test_grid"


@pytest.fixture(scope="module")
def db():
    """A live engine, or skip the module."""
    try:
        target = engine()
        with target.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT PostGIS_Version()"))
    except sqlalchemy.exc.SQLAlchemyError as exc:
        pytest.skip(f"{HINT} ({exc})")
    return target


@pytest.fixture(scope="module")
def loaded(db):
    """The fixture grid written into PostGIS under `test_grid`."""
    world = grid_world()
    under_test = World(city=TEST_CITY, graph=world.graph, pois=world.pois, bbox=world.bbox)
    report = write_world(under_test, bind=db)
    yield report
    with db.begin() as conn:
        for table in (ROADS_TABLE, POIS_TABLE):
            conn.execute(
                sqlalchemy.text(f"DELETE FROM {table} WHERE city = :city"), {"city": TEST_CITY}
            )


def test_a_write_reports_what_it_loaded(loaded) -> None:
    assert loaded.roads == grid_graph().edge_count
    assert loaded.pois == len(grid_world().pois)
    assert TEST_CITY in str(loaded)


def test_rows_are_written_under_the_worlds_city_not_the_graphs(loaded, db) -> None:
    """The city column is the key rows are deleted by, so it has to be the
    world's name - a world built from another city's graph still writes its
    own rows, and a rewrite replaces them instead of colliding."""
    with db.connect() as conn:
        cities = (
            conn.execute(sqlalchemy.text(f"SELECT DISTINCT city FROM {ROADS_TABLE}"))
            .scalars()
            .all()
        )
    assert TEST_CITY in cities
    assert grid_graph().city not in cities


def test_both_tables_hold_the_city(loaded, db) -> None:
    rows = table_counts(TEST_CITY)
    assert rows[ROADS_TABLE] == grid_graph().edge_count
    assert rows[POIS_TABLE] == len(grid_world().pois)


def test_road_geometry_arrives_as_4326_linestrings(loaded, db) -> None:
    with db.connect() as conn:
        srid, kind, length = conn.execute(
            sqlalchemy.text(
                f"""
                SELECT ST_SRID(geom), ST_GeometryType(geom),
                       ST_Length(geom::geography)
                FROM {ROADS_TABLE} WHERE city = :city AND edge_id = 0
                """
            ),
            {"city": TEST_CITY},
        ).one()
    assert srid == 4326
    assert kind == "ST_LineString"
    assert length == pytest.approx(100.0, abs=1.0)


def test_pois_keep_their_access_edge_ids_as_an_array(loaded, db) -> None:
    with db.connect() as conn:
        ids, critical = conn.execute(
            sqlalchemy.text(
                f"SELECT access_edge_ids, critical FROM {POIS_TABLE} "
                "WHERE city = :city AND poi_id = :poi"
            ),
            {"city": TEST_CITY, "poi": HOSPITAL},
        ).one()
    assert sorted(ids) == list(grid_world().poi(HOSPITAL).access_edge_ids)
    assert critical is True


def test_a_spatial_query_finds_the_roads_under_a_polygon(loaded, db) -> None:
    """The reason the geometry is in PostGIS and not just in the cache."""
    west, south = at(1.2, 0.98)
    east, north = at(1.8, 1.02)
    with db.connect() as conn:
        count = conn.execute(
            sqlalchemy.text(
                f"""
                SELECT count(*) FROM {ROADS_TABLE}
                WHERE city = :city
                  AND ST_Intersects(
                          geom,
                          ST_MakeEnvelope(:west, :south, :east, :north, 4326)
                      )
                """
            ),
            {"city": TEST_CITY, "west": west, "south": south, "east": east, "north": north},
        ).scalar_one()
    assert count == 2


def test_critical_pois_near_a_point_come_back_in_distance_order(loaded) -> None:
    lon, lat = at(1.5, 1.5)
    found = critical_pois_near(lon, lat, 500.0, city=TEST_CITY)
    assert next(poi_id for poi_id, _name, _m in found) == HOSPITAL
    assert len(found) == 2
    assert all(metres < 500.0 for _id, _name, metres in found)
    assert found == sorted(found, key=lambda item: item[2])


def test_a_school_is_not_a_critical_poi_in_the_query(loaded) -> None:
    lon, lat = at(0.5, 2.5)
    found = critical_pois_near(lon, lat, 20.0, city=TEST_CITY)
    assert found == []


def test_a_rewrite_replaces_rather_than_duplicates(loaded, db) -> None:
    world = grid_world()
    again = write_world(
        World(city=TEST_CITY, graph=world.graph, pois=world.pois, bbox=world.bbox), bind=db
    )
    assert again.roads == loaded.roads
    assert table_counts(TEST_CITY)[ROADS_TABLE] == loaded.roads


def test_an_unreachable_database_says_to_start_it() -> None:
    world = grid_world()
    with pytest.raises(DbError, match="hu infra up"):
        write_world(world, url="postgresql+psycopg://headsup:headsup@127.0.0.1:1/headsup")


def test_the_default_url_is_the_compose_one() -> None:
    assert "headsup" in database_url()
