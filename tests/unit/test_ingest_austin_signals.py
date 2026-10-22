"""The Austin signal-status adapter, against a mocked Socrata endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from fixtures.signals import (
    CLUSTER,
    COMMUNICATION_ISSUE,
    MIXED_BODY,
    MIXED_RECORDS,
    OTHER_UNSCHEDULED_FLASH,
    SCHEDULED_FLASH,
    SIGNALS_URL,
    UNSCHEDULED_FLASH,
    local,
    payload,
    record,
)
from headsup.contracts import Source
from headsup.ingest import (
    ENV_APP_TOKEN,
    SIGNAL_STATUS_DATASET,
    AustinSignalsAdapter,
    FakeClock,
    IngestError,
    adapter_class,
    parser_for,
)

T0 = datetime(2026, 10, 22, 0, 45, tzinfo=UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


# --- fetching ---------------------------------------------------------------


@respx.mock
async def test_fetch_wraps_the_bare_array_the_portal_answers_with(clock: FakeClock) -> None:
    """A RawPayload body is an object; SODA answers with a list."""
    respx.get(SIGNALS_URL).mock(return_value=httpx.Response(200, json=MIXED_RECORDS))
    async with AustinSignalsAdapter(clock=clock) as adapter:
        payloads = await adapter.fetch()

    assert len(payloads) == 1
    body = payloads[0].body
    assert body["dataset"] == SIGNAL_STATUS_DATASET
    assert body["records"] == MIXED_RECORDS
    assert payloads[0].source is Source.AUSTIN_SIGNALS
    assert payloads[0].ref == "austin_signals/20261022T004500Z/0"


@respx.mock
async def test_fetch_asks_for_every_row_in_a_stable_order(clock: FakeClock) -> None:
    route = respx.get(SIGNALS_URL).mock(return_value=httpx.Response(200, json=[]))
    async with AustinSignalsAdapter(clock=clock) as adapter:
        await adapter.fetch()

    params = route.calls.last.request.url.params
    assert params["$order"] == "signal_id"
    assert int(params["$limit"]) >= 1000


@respx.mock
async def test_an_app_token_travels_as_a_header_not_in_the_url(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded payload's url is committed, so it must carry no credential."""
    monkeypatch.setenv(ENV_APP_TOKEN, "s3cret-token")
    route = respx.get(SIGNALS_URL).mock(return_value=httpx.Response(200, json=[]))
    async with AustinSignalsAdapter(clock=clock) as adapter:
        payloads = await adapter.fetch()

    request = route.calls.last.request
    assert request.headers["X-App-Token"] == "s3cret-token"
    assert "s3cret-token" not in str(request.url)
    assert "s3cret-token" not in payloads[0].url


@respx.mock
async def test_an_object_where_an_array_belongs_is_a_feed_error(clock: FakeClock) -> None:
    respx.get(SIGNALS_URL).mock(return_value=httpx.Response(200, json={"error": True}))
    async with AustinSignalsAdapter(clock=clock) as adapter:
        with pytest.raises(IngestError, match="returned dict"):
            await adapter.fetch()


# --- parsing ----------------------------------------------------------------


def test_parse_reads_the_state_into_a_kind_and_two_booleans() -> None:
    events = AustinSignalsAdapter().parse(payload(MIXED_RECORDS))
    by_id = {event.payload["signal_id"]: event for event in events}

    assert len(events) == len(MIXED_RECORDS)
    flash = by_id["303"]
    assert flash.kind == "unscheduled_flash"
    assert flash.payload["flashing"] is True
    assert flash.payload["scheduled"] is False

    scheduled = by_id["314"]
    assert scheduled.kind == "scheduled_flash"
    assert scheduled.payload["flashing"] is True
    assert scheduled.payload["scheduled"] is True

    comms = by_id["283"]
    assert comms.kind == "communication_issue"
    assert comms.payload["flashing"] is False


def test_state_seven_is_an_unscheduled_flash_like_state_two() -> None:
    events = AustinSignalsAdapter().parse(payload([record(CLUSTER[0], OTHER_UNSCHEDULED_FLASH)]))
    assert events[0].kind == "unscheduled_flash"
    assert events[0].payload["flashing"] is True
    assert events[0].payload["scheduled"] is False


def test_an_unknown_state_still_reaches_the_bus_under_the_feeds_own_wording() -> None:
    row = record(CLUSTER[0])
    row["operation_state"] = "42"
    row["operation_text"] = "Cabinet door open"
    event = AustinSignalsAdapter().parse(payload([row]))[0]
    assert event.kind == "cabinet_door_open"
    assert event.payload["operation_state"] == 42
    assert event.payload["flashing"] is False


def test_the_feeds_naive_timestamps_are_read_as_austin_local_time() -> None:
    """12:05 in this feed is 17:05Z in October, not 12:05Z."""
    noon_local = datetime(2026, 10, 22, 17, 5, tzinfo=UTC)
    row = record(CLUSTER[0], since=noon_local, at=noon_local)
    assert row["operation_state_datetime"] == "2026-10-22T12:05:00.000"

    event = AustinSignalsAdapter().parse(payload([row]))[0]
    assert event.observed_at == noon_local


def test_a_timestamp_that_does_carry_an_offset_is_believed() -> None:
    row = record(CLUSTER[0])
    row["operation_state_datetime"] = "2026-10-22T00:45:00Z"
    event = AustinSignalsAdapter().parse(payload([row]))[0]
    assert event.observed_at == datetime(2026, 10, 22, 0, 45, tzinfo=UTC)


def test_ids_are_stable_while_a_signal_stays_in_the_same_state() -> None:
    """The feed restamps `processed_datetime` every poll; the id must not move."""
    first = AustinSignalsAdapter().parse(payload([record(CLUSTER[0], at=T0)]))[0]
    later = T0.replace(minute=50)
    second = AustinSignalsAdapter().parse(payload([record(CLUSTER[0], at=later)], at=later))[0]
    assert first.id == second.id

    changed = AustinSignalsAdapter().parse(
        payload([record(CLUSTER[0], since=later, at=later)], at=later)
    )[0]
    assert changed.id != first.id


def test_a_row_without_a_location_or_an_id_is_dropped() -> None:
    no_location = record(CLUSTER[0])
    no_location["location"] = None
    no_id = record(CLUSTER[1])
    no_id["signal_id"] = ""
    events = AustinSignalsAdapter().parse(payload([no_location, no_id, record(CLUSTER[2])]))
    assert [event.payload["signal_id"] for event in events] == ["302"]


def test_the_legacy_socrata_location_shape_is_read_too() -> None:
    row = record(CLUSTER[0])
    row["location"] = {"latitude": "30.268066", "longitude": "-97.742813"}
    event = AustinSignalsAdapter().parse(payload([row]))[0]
    assert event.geometry.coordinates == [-97.742813, 30.268066]


def test_a_payload_that_is_not_a_recording_of_this_feed_parses_to_nothing() -> None:
    assert AustinSignalsAdapter().parse(payload([])) == []
    empty = payload([])
    empty.body.pop("records")
    assert AustinSignalsAdapter().parse(empty) == []


# --- registration -----------------------------------------------------------


def test_the_registry_can_build_a_parse_only_adapter_for_this_feed() -> None:
    assert adapter_class(Source.AUSTIN_SIGNALS) is AustinSignalsAdapter
    assert isinstance(parser_for(Source.AUSTIN_SIGNALS), AustinSignalsAdapter)


def test_local_matches_the_feeds_own_format() -> None:
    assert local(T0) == "2026-10-21T19:45:00.000"
    assert COMMUNICATION_ISSUE == 3
    assert SCHEDULED_FLASH == 1
    assert UNSCHEDULED_FLASH == 2
    assert MIXED_BODY["records"] is MIXED_RECORDS
