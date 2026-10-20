"""The Docker services are reachable and PostGIS is installed.

Needs `uv run hu infra up` locally; CI supplies both as service containers.
"""

from __future__ import annotations

import os

import pytest
import redis
import sqlalchemy

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://headsup:headsup@localhost:5432/headsup"
)

HINT = "service unreachable - run `uv run hu infra up`"


def test_redis_ping() -> None:
    client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=5)
    try:
        assert client.ping() is True
    except redis.exceptions.ConnectionError as exc:
        pytest.fail(f"{HINT}: redis at {REDIS_URL} ({exc})")
    finally:
        client.close()


def test_redis_streams_round_trip() -> None:
    """XADD/XRANGE work, which is all the bus will need from Redis."""
    client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=5, decode_responses=True)
    stream = "headsup:test:infra"
    try:
        client.delete(stream)
        entry_id = client.xadd(stream, {"hello": "headsup"})
        entries = client.xrange(stream)
        assert [(entry_id, {"hello": "headsup"})] == entries
    except redis.exceptions.ConnectionError as exc:
        pytest.fail(f"{HINT}: redis at {REDIS_URL} ({exc})")
    finally:
        client.delete(stream)
        client.close()


def test_postgis_version() -> None:
    engine = sqlalchemy.create_engine(DATABASE_URL, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as conn:
            postgis_version = conn.execute(sqlalchemy.text("SELECT PostGIS_Version()")).scalar_one()
    except sqlalchemy.exc.OperationalError as exc:
        pytest.fail(f"{HINT}: postgis at {DATABASE_URL} ({exc})")
    finally:
        engine.dispose()
    assert postgis_version
    assert postgis_version.startswith("3."), postgis_version
