"""The live polling loop: recording sits in the same path as publishing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from fixtures.ingest import NWS_BODY, USGS_BODY
from headsup.contracts import CivicEvent, Source
from headsup.ingest import (
    ALERTS_URL,
    AUSTIN,
    CITIES,
    SAN_ANTONIO,
    FakeClock,
    IngestError,
    LiveIngest,
    NwsAdapter,
    Recorder,
    Recording,
    UsgsAdapter,
    city,
    city_adapters,
)
from headsup.ingest.usgs import COLLECTION_LATEST, collection_url

T0 = datetime(2026, 4, 20, 18, 55, tzinfo=UTC)


def mock_feeds() -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(200, json=NWS_BODY))
    respx.get(collection_url(COLLECTION_LATEST)).mock(
        return_value=httpx.Response(200, json=USGS_BODY)
    )


def both_adapters(clock: FakeClock) -> list[tuple[object, float]]:
    return [
        (NwsAdapter(clock=clock), 60.0),
        (UsgsAdapter(sites=SAN_ANTONIO.gauge_ids, clock=clock), 300.0),
    ]


# --- cities -----------------------------------------------------------------


def test_the_two_cities_are_configured_with_gauges() -> None:
    assert set(CITIES) == {"austin", "san_antonio"}
    assert city("austin") is AUSTIN
    assert AUSTIN.county_name == "Travis"
    assert SAN_ANTONIO.county_name == "Bexar"
    for target in CITIES.values():
        assert target.nws_area == "TX"
        assert target.gauge_sites
        assert all(site.startswith("USGS-") for site in target.gauge_ids)


def test_an_unknown_city_lists_the_known_ones() -> None:
    with pytest.raises(ValueError, match="austin, san_antonio"):
        city("atlantis")


def test_a_city_is_polled_through_both_feeds() -> None:
    feeds = city_adapters(SAN_ANTONIO)
    assert [adapter.source for adapter, _ in feeds] == [Source.NWS, Source.USGS]
    assert [interval for _, interval in feeds] == [60.0, 300.0]


# --- the loop ---------------------------------------------------------------


@respx.mock
async def test_one_round_records_every_payload_and_parses_every_event(
    tmp_path: Path,
) -> None:
    mock_feeds()
    clock = FakeClock(T0)
    seen: list[CivicEvent] = []
    with Recorder("s_live", root=tmp_path) as recorder:
        ingest = LiveIngest(
            both_adapters(clock),
            recorder=recorder,
            clock=clock,
            on_event=seen.append,
        )
        await ingest.run(polls=1)
        await ingest.aclose()

    # One polygon alert from NWS, two readings from USGS.
    assert ingest.total_payloads == 2
    assert ingest.total_events == 3
    assert len(seen) == 3
    assert ingest.runs[Source.NWS].polls == 1

    recorded = Recording.locate("s_live", root=tmp_path).payloads()
    assert [item.source for item in recorded] == [Source.NWS, Source.USGS]


@respx.mock
async def test_polling_twice_waits_the_feed_interval(tmp_path: Path) -> None:
    mock_feeds()
    clock = FakeClock(T0)
    ingest = LiveIngest([(NwsAdapter(clock=clock), 60.0)], clock=clock)
    await ingest.run(polls=2)
    await ingest.aclose()

    assert ingest.runs[Source.NWS].polls == 2
    # One wait between the two polls, and none after the last.
    assert clock.slept == [60.0]


@respx.mock
async def test_a_feed_that_fails_does_not_stop_the_others() -> None:
    respx.get(ALERTS_URL).mock(return_value=httpx.Response(500))
    respx.get(collection_url(COLLECTION_LATEST)).mock(
        return_value=httpx.Response(200, json=USGS_BODY)
    )
    clock = FakeClock(T0)
    errors: list[tuple[Source, Exception]] = []
    ingest = LiveIngest(
        both_adapters(clock),
        clock=clock,
        on_error=lambda source, exc: errors.append((source, exc)),
    )
    await ingest.run(polls=1)
    await ingest.aclose()

    assert [source for source, _ in errors] == [Source.NWS]
    assert ingest.runs[Source.NWS].events == 0
    assert len(ingest.runs[Source.NWS].errors) == 1
    assert ingest.runs[Source.USGS].events == 2


@respx.mock
async def test_the_minutes_budget_ends_the_session(tmp_path: Path) -> None:
    mock_feeds()
    clock = FakeClock(T0)
    ingest = LiveIngest([(NwsAdapter(clock=clock), 60.0)], clock=clock)
    # Two and a half minutes buys three polls at a one-minute interval.
    await ingest.run(duration_s=150)
    await ingest.aclose()
    assert ingest.runs[Source.NWS].polls == 3


def test_live_ingest_needs_a_feed() -> None:
    with pytest.raises(IngestError, match="at least one feed"):
        LiveIngest([])
