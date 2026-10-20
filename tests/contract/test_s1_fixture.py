"""The committed S1 fixture: small enough, real, and it says which it is.

These assertions guard the demo. If a future recording session overwrites the
fixture with something that does not replay, or quietly swaps real NWS
polygons for generated ones without flipping `scripted`, this fails.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from headsup.contracts import HazardClass, Source
from headsup.ingest import FakeClock, Recording, Replayer, parser_for
from headsup.ingest.recorder import PROJECT_ROOT

SCENARIO_FILE = PROJECT_ROOT / "scenarios" / "s1_flood.yaml"
FIXTURE_DIR = PROJECT_ROOT / "data" / "recordings" / "s1_flood" / "fixture"

#: CLAUDE.md: no committed recording over 2 MB.
MAX_FIXTURE_BYTES = 2 * 1024 * 1024


@pytest.fixture(scope="module")
def scenario() -> dict:
    return yaml.safe_load(SCENARIO_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def recording() -> Recording:
    return Recording.at(FIXTURE_DIR, scenario="s1_flood")


def test_the_fixture_is_committed_and_under_two_megabytes(recording: Recording) -> None:
    files = recording.files()
    assert [path.name for path in files] == ["nws.jsonl", "usgs.jsonl"]
    assert 0 < recording.size_bytes < MAX_FIXTURE_BYTES, (
        f"fixture is {recording.size_bytes / 1024 / 1024:.2f} MB"
    )


def test_the_scenario_declares_whether_its_data_is_scripted(scenario: dict) -> None:
    assert scenario["id"] == "s1_flood"
    assert scenario["city"] == "san_antonio"
    assert scenario["hazard_class"] == HazardClass.FLOOD.value
    assert isinstance(scenario["scripted"], bool)
    # Both feeds are real observations of the 20-21 April 2026 event, so the
    # viewer must not show a "scripted data" badge for this run.
    assert scenario["scripted"] is False
    for feed in ("nws", "usgs"):
        assert scenario["provenance"][feed]["status"] == "real"
        assert scenario["provenance"][feed]["source_url"].startswith("https://")


def test_the_scenario_counts_match_the_fixture_on_disk(
    scenario: dict, recording: Recording
) -> None:
    payloads = recording.payloads()
    assert len(payloads) == scenario["recording"]["payloads"]
    assert recording.size_bytes == scenario["recording"]["bytes"]
    by_source: dict[str, int] = {}
    for payload in payloads:
        by_source[payload.source.value] = by_source.get(payload.source.value, 0) + 1
    for feed, counts in scenario["recording"]["feeds"].items():
        assert by_source[feed] == counts["payloads"]


async def test_the_fixture_replays_in_order_at_sixty_times(
    scenario: dict, recording: Recording
) -> None:
    clock = FakeClock(datetime(2026, 10, 20, tzinfo=UTC))
    emissions, report = await Replayer(recording, clock=clock, speed=60.0).drain()

    assert report.events == scenario["recording"]["events"]
    assert report.by_source[Source.NWS] == scenario["recording"]["feeds"]["nws"]["events"]
    assert report.by_source[Source.USGS] == scenario["recording"]["feeds"]["usgs"]["events"]

    offsets = [item.replay_offset for item in emissions]
    assert offsets == sorted(offsets)
    assert report.replay_span == pytest.approx(report.feed_span / 60.0)
    # The window in the scenario file is the window on disk.
    window = scenario["window"]
    assert emissions[0].feed_time == window["start"].replace(tzinfo=UTC)
    assert report.feed_span / 60 == pytest.approx(window["duration_minutes"], abs=1)


async def test_the_first_nws_polygon_arrives_inside_the_time_to_advisory_budget(
    scenario: dict, recording: Recording
) -> None:
    emissions, _ = await Replayer(
        recording, clock=FakeClock(datetime(2026, 10, 20, tzinfo=UTC)), speed=60.0
    ).drain()
    first = next(item for item in emissions if item.source is Source.NWS)

    assert first.event.kind == "flash_flood_warning"
    assert first.feed_time == scenario["window"]["first_warning_at"].replace(tzinfo=UTC)
    assert first.replay_offset == pytest.approx(
        scenario["window"]["first_warning_offset_seconds"] / 60.0
    )
    # Gate 1 measures time to advisory from here, with 60 s to spend.
    assert first.replay_offset < 60.0


def test_every_nws_payload_carries_one_valid_warning_polygon(recording: Recording) -> None:
    adapter = parser_for(Source.NWS)
    events = [
        event
        for payload in recording.payloads()
        if payload.source is Source.NWS
        for event in adapter.parse(payload)
    ]
    assert len(events) == 9
    for event in events:
        assert event.kind == "flash_flood_warning"
        assert event.geometry.type == "Polygon"
        assert event.geometry.to_shapely().is_valid
        assert event.payload["ugc"] == ["TXC029", "TXC091", "TXC187"]
        assert event.payload["sender_name"] == "NWS Austin/San Antonio TX"


def test_the_gauges_behave_the_way_the_scenario_file_says(
    scenario: dict, recording: Recording
) -> None:
    adapter = parser_for(Source.USGS)
    readings: dict[str, list[tuple[datetime, float]]] = {}
    for payload in recording.payloads():
        if payload.source is not Source.USGS:
            continue
        for event in adapter.parse(payload):
            readings.setdefault(event.payload["site"], []).append(
                (event.observed_at, event.payload["value"])
            )

    for gauge in scenario["feeds"]["usgs"]["gauges"]:
        series = sorted(readings[gauge["site"]])
        assert series[0][1] == pytest.approx(gauge["start_ft"])
        peak_at, peak = max(series, key=lambda row: row[1])
        assert peak == pytest.approx(gauge["peak_ft"])
        assert peak_at == gauge["peak_at"].replace(tzinfo=UTC)

        crossed = [at for at, value in series if value >= gauge["action_stage_ft"]]
        expected = gauge["crossed_action_stage_at"]
        if expected is None:
            assert not crossed, f"{gauge['site']} was not supposed to reach its action stage"
        else:
            assert min(crossed) == expected.replace(tzinfo=UTC)


def test_the_warning_polygons_cover_the_salado_creek_gauges(recording: Recording) -> None:
    """A flood warning the gauges sit outside of would corroborate nothing."""
    nws, usgs = parser_for(Source.NWS), parser_for(Source.USGS)
    payloads = recording.payloads()
    warnings = [
        event.geometry.to_shapely()
        for payload in payloads
        if payload.source is Source.NWS
        for event in nws.parse(payload)
    ]
    gauges = {
        event.payload["site"]: event.geometry.to_shapely()
        for payload in payloads
        if payload.source is Source.USGS
        for event in usgs.parse(payload)
    }

    covered = {
        site for site, point in gauges.items() if any(area.contains(point) for area in warnings)
    }
    assert covered, "no gauge falls inside any warning polygon"
    assert "08178593" in covered, "Blanco Rd is the corroborating gauge for S1"


def test_the_fixture_path_is_the_one_the_scenario_file_names(scenario: dict) -> None:
    named = PROJECT_ROOT / Path(scenario["recording"]["fixture"])
    assert named.resolve() == FIXTURE_DIR.resolve()
