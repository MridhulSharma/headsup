"""The NWS and USGS adapters, against mocked HTTP and a fake clock."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from fixtures.ingest import NWS_BODY, USGS_BODY, nws_body
from headsup.contracts import Source
from headsup.ingest import (
    ALERTS_URL,
    DEFAULT_USER_AGENT,
    FakeClock,
    IngestError,
    NwsAdapter,
    UsgsAdapter,
    derive_id,
    parse_timestamp,
    slugify,
)
from headsup.ingest.usgs import COLLECTION_HISTORY, COLLECTION_LATEST, collection_url

T0 = datetime(2026, 4, 20, 18, 55, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


# --- helpers ----------------------------------------------------------------


def test_slugify_forces_feed_text_into_the_kind_alphabet() -> None:
    assert slugify("Flash Flood Warning", "x") == "flash_flood_warning"
    assert slugify("  Winter Storm / Ice  ", "x") == "winter_storm_ice"
    assert slugify("!!!", "weather_alert") == "weather_alert"
    assert slugify("", "weather_alert") == "weather_alert"


def test_derive_id_is_stable_and_distinct() -> None:
    assert derive_id("ev", "a", "b") == derive_id("ev", "a", "b")
    assert derive_id("ev", "a", "b") != derive_id("ev", "a", "c")
    assert derive_id("ev", "a").startswith("ev_")


def test_parse_timestamp_normalises_to_utc_and_tolerates_junk() -> None:
    assert parse_timestamp("2026-04-20T13:50:00-05:00") == datetime(2026, 4, 20, 18, 50, tzinfo=UTC)
    assert parse_timestamp("2026-04-20T18:50:00Z") == datetime(2026, 4, 20, 18, 50, tzinfo=UTC)
    # Naive input is read as UTC rather than rejected: a feed that omits the
    # offset is still worth ingesting.
    assert parse_timestamp("2026-04-20T18:50:00") == datetime(2026, 4, 20, 18, 50, tzinfo=UTC)
    assert parse_timestamp("not a time") is None
    assert parse_timestamp(None) is None


# --- NWS --------------------------------------------------------------------


@respx.mock
async def test_nws_fetch_sends_a_descriptive_user_agent(clock: FakeClock) -> None:
    route = respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    async with NwsAdapter(clock=clock) as adapter:
        payloads = await adapter.fetch()

    request = route.calls.last.request
    assert request.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert "headsup" in request.headers["User-Agent"]
    assert request.url.params["area"] == "TX"
    assert len(payloads) == 1
    assert payloads[0].source is Source.NWS
    assert payloads[0].fetched_at == T0
    assert payloads[0].ref == "nws/20260420T185500Z/0"


@respx.mock
async def test_nws_user_agent_comes_from_the_environment(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = "headsup-test/9.9 (someone@example.com)"
    monkeypatch.setenv("HU_HTTP_USER_AGENT", chosen)
    route = respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    async with NwsAdapter(clock=clock) as adapter:
        await adapter.fetch()
    assert route.calls.last.request.headers["User-Agent"] == chosen


@respx.mock
async def test_nws_keeps_only_alerts_with_a_usable_polygon(clock: FakeClock) -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    async with NwsAdapter(clock=clock) as adapter:
        _, events = await adapter.poll()

    # Three alerts in, one event out: the zone-only alert has no geometry and
    # the bow-tie polygon is not a shape anything can be closed around.
    assert len(events) == 1
    event = events[0]
    assert event.source is Source.NWS
    assert event.kind == "flash_flood_warning"
    assert event.geometry.type == "Polygon"
    assert event.observed_at == datetime(2026, 4, 20, 18, 50, tzinfo=UTC)
    assert event.ingested_at == T0
    assert event.raw_ref == "nws/20260420T185500Z/0#0"


@respx.mock
async def test_nws_payload_carries_the_prose_and_drops_the_routing_noise(
    clock: FakeClock,
) -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    async with NwsAdapter(clock=clock) as adapter:
        _, events = await adapter.poll()

    payload = events[0].payload
    assert payload["event"] == "Flash Flood Warning"
    assert payload["severity"] == "Severe"
    assert payload["ugc"] == ["TXC029", "TXC091"]
    assert "Turn around" in payload["instruction"]
    assert payload["parameters"]["flashFloodDamageThreat"] == ["CONSIDERABLE"]
    assert "BLOCKCHANNEL" not in payload["parameters"]


@respx.mock
async def test_nws_same_alert_parses_to_the_same_event_id(clock: FakeClock) -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    async with NwsAdapter(clock=clock) as adapter:
        first, _ = await adapter.poll()
        clock.advance(60)
        second, _ = await adapter.poll()

    ids = [event.id for payload in (first[0], second[0]) for event in adapter.parse(payload)]
    assert ids[0] == ids[1], "re-issuing the same alert must not mint a new event id"


@respx.mock
async def test_nws_empty_collection_yields_nothing(clock: FakeClock) -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=nws_body()))
    async with NwsAdapter(clock=clock) as adapter:
        _, events = await adapter.poll()
    assert events == []


@respx.mock
async def test_nws_http_error_is_an_ingest_error(clock: FakeClock) -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(503))
    async with NwsAdapter(clock=clock) as adapter:
        with pytest.raises(IngestError, match="nws"):
            await adapter.fetch()


# --- USGS -------------------------------------------------------------------


@respx.mock
async def test_usgs_fetch_asks_latest_continuous_for_the_configured_sites(
    clock: FakeClock,
) -> None:
    route = respx.get(collection_url(COLLECTION_LATEST)).mock(
        return_value=httpx.Response(200, json=USGS_BODY)
    )
    async with UsgsAdapter(sites=("USGS-08178593", "USGS-08178800"), clock=clock) as adapter:
        payloads = await adapter.fetch()

    params = route.calls.last.request.url.params
    assert params["monitoring_location_id"] == "USGS-08178593,USGS-08178800"
    assert params["parameter_code"] == "00065"
    assert payloads[0].source is Source.USGS


@respx.mock
async def test_usgs_fetch_window_uses_the_history_collection(clock: FakeClock) -> None:
    route = respx.get(collection_url(COLLECTION_HISTORY)).mock(
        return_value=httpx.Response(200, json=USGS_BODY)
    )
    async with UsgsAdapter(sites=("USGS-08178593",), clock=clock) as adapter:
        await adapter.fetch_window(
            datetime(2026, 4, 20, 18, tzinfo=UTC), datetime(2026, 4, 21, 5, tzinfo=UTC)
        )
    assert (
        route.calls.last.request.url.params["datetime"]
        == "2026-04-20T18:00:00Z/2026-04-21T05:00:00Z"
    )


@respx.mock
async def test_usgs_parses_readings_and_flags_the_action_stage(clock: FakeClock) -> None:
    respx.get(collection_url(COLLECTION_LATEST)).mock(
        return_value=httpx.Response(200, json=USGS_BODY)
    )
    async with UsgsAdapter(sites=("USGS-08178593",), clock=clock) as adapter:
        _, events = await adapter.poll()

    # Three features in, two out: the offline gauge reported no value.
    assert len(events) == 2
    assert [event.payload["value"] for event in events] == [4.21, 4.77]
    assert [event.payload["above_action_stage"] for event in events] == [False, True]
    assert all(event.kind == "gauge_height" for event in events)
    assert all(event.geometry.type == "Point" for event in events)
    assert events[0].payload["site"] == "08178593"
    assert events[0].ingested_at == T0
    assert events[0].observed_at == datetime(2026, 4, 20, 21, tzinfo=UTC)


@respx.mock
async def test_usgs_event_ids_are_distinct_per_reading(clock: FakeClock) -> None:
    respx.get(collection_url(COLLECTION_LATEST)).mock(
        return_value=httpx.Response(200, json=USGS_BODY)
    )
    async with UsgsAdapter(sites=("USGS-08178593",), clock=clock) as adapter:
        payloads, events = await adapter.poll()

    assert len({event.id for event in events}) == 2
    assert [event.id for event in adapter.parse(payloads[0])] == [e.id for e in events]


async def test_usgs_without_sites_can_only_parse() -> None:
    adapter = UsgsAdapter()
    with pytest.raises(IngestError, match="no sites configured"):
        await adapter.fetch()
