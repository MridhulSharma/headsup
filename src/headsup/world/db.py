"""The world in PostGIS: a `roads` table and a `pois` table.

The cache under `data/cache/` is what the Python side loads; these tables are
for the questions SQL answers better than a dict - what crosses this creek,
which hospitals are within 2 km of a gauge, give the viewer every arterial in
this tile. D2.4's flood sentinel needs the second one, so the geometry goes in
with a GiST index on it rather than being derived again later.

Writes are per city and idempotent: a rebuild deletes that city's rows and
inserts the new ones in one transaction, so a failed build leaves the previous
world in place rather than half of a new one.

Nothing else in Heads Up requires these tables. `hu world build --no-db`
skips them, and `World.load` never touches PostGIS, which keeps the unit tests
and a laptop with no Docker running working.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import Engine

from headsup.world.model import World

DEFAULT_DATABASE_URL = "postgresql+psycopg://headsup:headsup@localhost:5432/headsup"

ROADS_TABLE = "roads"
POIS_TABLE = "pois"

#: Rows per executemany round trip. A 40 000 edge city in 2 000 row batches is
#: twenty round trips instead of one statement PostgreSQL has to buffer whole.
BATCH_ROWS = 2_000

#: SRID 4326 throughout: every geometry in this system is WGS84 lon/lat, the
#: same as the GeoJSON the advisories carry.
SRID = 4326

_DDL = (
    "CREATE EXTENSION IF NOT EXISTS postgis",
    f"""
    CREATE TABLE IF NOT EXISTS {ROADS_TABLE} (
        city        text    NOT NULL,
        edge_id     integer NOT NULL,
        osmid       text    NOT NULL DEFAULT '',
        name        text    NOT NULL DEFAULT '',
        highway     text    NOT NULL DEFAULT '',
        oneway      boolean NOT NULL DEFAULT false,
        length_m    double precision NOT NULL,
        speed_kph   double precision NOT NULL,
        free_flow_s double precision NOT NULL,
        from_node   text    NOT NULL,
        to_node     text    NOT NULL,
        geom        geometry(LineString, {SRID}) NOT NULL,
        PRIMARY KEY (city, edge_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {POIS_TABLE} (
        city            text    NOT NULL,
        poi_id          text    NOT NULL,
        kind            text    NOT NULL,
        name            text    NOT NULL DEFAULT '',
        osm_id          text    NOT NULL DEFAULT '',
        critical        boolean NOT NULL DEFAULT false,
        access_edge_ids integer[] NOT NULL DEFAULT '{{}}',
        geom            geometry(Point, {SRID}) NOT NULL,
        PRIMARY KEY (city, poi_id)
    )
    """,
    f"CREATE INDEX IF NOT EXISTS {ROADS_TABLE}_geom_idx ON {ROADS_TABLE} USING GIST (geom)",
    f"CREATE INDEX IF NOT EXISTS {POIS_TABLE}_geom_idx ON {POIS_TABLE} USING GIST (geom)",
    f"CREATE INDEX IF NOT EXISTS {POIS_TABLE}_kind_idx ON {POIS_TABLE} (city, kind)",
)

_INSERT_ROAD = text(
    f"""
    INSERT INTO {ROADS_TABLE}
        (city, edge_id, osmid, name, highway, oneway, length_m, speed_kph,
         free_flow_s, from_node, to_node, geom)
    VALUES
        (:city, :edge_id, :osmid, :name, :highway, :oneway, :length_m, :speed_kph,
         :free_flow_s, :from_node, :to_node, ST_GeomFromText(:wkt, {SRID}))
    """
)

_INSERT_POI = text(
    f"""
    INSERT INTO {POIS_TABLE}
        (city, poi_id, kind, name, osm_id, critical, access_edge_ids, geom)
    VALUES
        (:city, :poi_id, :kind, :name, :osm_id, :critical, :access_edge_ids,
         ST_GeomFromText(:wkt, {SRID}))
    """
)


class DbError(RuntimeError):
    """PostGIS is unreachable, or a write failed."""


def database_url() -> str:
    """`DATABASE_URL`, or the docker-compose default."""
    return os.environ.get("DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL


def engine(url: str | None = None) -> Engine:
    """A SQLAlchemy engine with a short connect timeout.

    Short on purpose: when PostGIS is not up, the answer should be an error
    with `hu infra up` in it, not a two-minute wait.
    """
    return sqlalchemy.create_engine(
        url or database_url(), connect_args={"connect_timeout": 5}, future=True
    )


def _batched(rows: Iterable[Mapping[str, Any]], size: int) -> Iterable[list[Mapping[str, Any]]]:
    batch: list[Mapping[str, Any]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


@dataclass(frozen=True, slots=True)
class WriteReport:
    """What one PostGIS write put where."""

    city: str
    url: str
    roads: int
    pois: int

    def __str__(self) -> str:
        where = self.url.rsplit("@", 1)[-1]
        return f"{self.city}: {self.roads} roads, {self.pois} pois -> {where}"


def create_tables(url: str | None = None, *, bind: Engine | None = None) -> None:
    """Create the extension, the tables and the indexes if they are missing."""
    target = bind or engine(url)
    try:
        with target.begin() as conn:
            for statement in _DDL:
                conn.execute(text(statement))
    except sqlalchemy.exc.SQLAlchemyError as exc:
        raise DbError(f"cannot prepare the world tables: {_hint(exc)}") from exc


def write_world(
    world: World,
    *,
    url: str | None = None,
    bind: Engine | None = None,
    batch: int = BATCH_ROWS,
) -> WriteReport:
    """Replace one city's roads and POIs in PostGIS, in one transaction."""
    from headsup.world.pois import poi_rows

    resolved = url or database_url()
    target = bind or engine(resolved)
    create_tables(bind=target)
    road_rows = list(world.graph.edge_rows(city=world.city))
    poi_records = list(poi_rows(world.city, world.pois))
    try:
        with target.begin() as conn:
            conn.execute(
                text(f"DELETE FROM {ROADS_TABLE} WHERE city = :city"), {"city": world.city}
            )
            conn.execute(text(f"DELETE FROM {POIS_TABLE} WHERE city = :city"), {"city": world.city})
            for chunk in _batched(road_rows, batch):
                conn.execute(_INSERT_ROAD, chunk)
            for chunk in _batched(poi_records, batch):
                conn.execute(_INSERT_POI, chunk)
    except sqlalchemy.exc.SQLAlchemyError as exc:
        raise DbError(f"cannot write the world for {world.city}: {_hint(exc)}") from exc
    return WriteReport(city=world.city, url=resolved, roads=len(road_rows), pois=len(poi_records))


def table_counts(city: str | None = None, *, url: str | None = None) -> dict[str, int]:
    """Row counts per table, for one city or for all of them."""
    target = engine(url)
    where = "" if city is None else " WHERE city = :city"
    params = {} if city is None else {"city": city}
    try:
        with target.connect() as conn:
            return {
                table: int(
                    conn.execute(text(f"SELECT count(*) FROM {table}{where}"), params).scalar_one()
                )
                for table in (ROADS_TABLE, POIS_TABLE)
            }
    except sqlalchemy.exc.SQLAlchemyError as exc:
        raise DbError(f"cannot count the world tables: {_hint(exc)}") from exc


def critical_pois_near(
    lon: float, lat: float, metres: float, *, city: str | None = None, url: str | None = None
) -> Sequence[tuple[str, str, float]]:
    """Critical POIs within `metres` of a point: (poi_id, name, distance).

    The one query that justifies the GiST index, and what D2.4's flood
    sentinel will ask when a gauge crosses its action stage.
    """
    target = engine(url)
    # Every parameter is cast with CAST, not `::`: `ST_DWithin` and
    # `ST_MakePoint` are both overloaded, so an untyped bind leaves
    # PostgreSQL unable to choose between the geometry and geography
    # forms, and `::` inside a `text()` construct is read as a bind.
    statement = text(
        f"""
        WITH here AS (
            SELECT ST_SetSRID(
                       ST_MakePoint(CAST(:lon AS float8), CAST(:lat AS float8)), {SRID}
                   )::geography AS point
        )
        SELECT poi_id,
               name,
               ST_Distance(geom::geography, here.point) AS metres
        FROM {POIS_TABLE}, here
        WHERE critical
          AND (CAST(:city AS text) IS NULL OR city = CAST(:city AS text))
          AND ST_DWithin(geom::geography, here.point, CAST(:metres AS float8))
        ORDER BY metres
        """
    )
    try:
        with target.connect() as conn:
            rows = conn.execute(
                statement, {"lon": lon, "lat": lat, "metres": metres, "city": city}
            ).all()
    except sqlalchemy.exc.SQLAlchemyError as exc:
        raise DbError(f"cannot query POIs near {lon},{lat}: {_hint(exc)}") from exc
    return [(str(row[0]), str(row[1]), float(row[2])) for row in rows]


def _hint(exc: Exception) -> str:
    """The driver's message, with the fix appended when it is a connection."""
    message = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
    if isinstance(exc, sqlalchemy.exc.OperationalError):
        return f"{message} - is PostGIS up? run `hu infra up`"
    return message
