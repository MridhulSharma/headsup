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

from headsup.agents.compiler.geo import area_km2
from headsup.agents.compiler.playbooks import default_playbooks
from headsup.agents.fusion import FLOOD_REACH_RADIUS_M, Fusion
from headsup.agents.sentinels.flood import FloodSentinel
from headsup.contracts import Hazard, HazardClass, Source
from headsup.ingest import FakeClock, Recording, Replayer, parser_for
from headsup.ingest.recorder import PROJECT_ROOT

SCENARIO_FILE = PROJECT_ROOT / "scenarios" / "s1_flood.yaml"
FIXTURE_DIR = PROJECT_ROOT / "data" / "recordings" / "s1_flood" / "fixture"

#: CLAUDE.md: no committed recording over 2 MB.
MAX_FIXTURE_BYTES = 2 * 1024 * 1024

#: The caps S1's advisory has to fit inside, read from the file the compiler
#: reads them from rather than copied here.
FLOOD_PLAYBOOK = default_playbooks().require(HazardClass.FLOOD)


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


#: What the first corroborated S1 hazard measures once fusion has clipped the
#: warning polygon to the reach of the Blanco Rd gauge: pi * 1.5^2 on the
#: ellipsoid, with the whole disk inside the polygon.
NARROWED_KM2 = 7.0447

#: How far the measured area may drift before this fails. Tight on purpose.
#: The number this guards is not the area itself, it is the margin under
#: `playbooks/flood.yaml` max_area_km2: a reach radius or a polygon that crept
#: up would put S1 back over the cap, stop the advisory publishing, and the
#: only visible symptom would be `tests/e2e/test_s1.py` going quiet again.
NARROWED_TOLERANCE_KM2 = 0.05


async def _s1_hazards(recording: Recording) -> list[Hazard]:
    """Every hazard the real S1 recording fuses to, in feed order.

    The real sentinel and the real fusion, with no model: `FloodSentinel`
    answers None for its four prose fields without one, which changes a
    confidence and nothing this test looks at.
    """
    sentinel, fusion = FloodSentinel(), Fusion()
    parsed = [
        event
        for payload in recording.payloads()
        for event in parser_for(payload.source).parse(payload)
    ]
    events = sorted(parsed, key=lambda item: (item.observed_at, item.source.value, item.id))
    hazards: list[Hazard] = []
    for event in events:
        for candidate in await sentinel.on_event(event):
            hazards.append(await fusion.on_candidate(candidate))
    return hazards


async def test_the_uncorroborated_s1_warning_is_published_at_its_full_width(
    recording: Recording,
) -> None:
    """Before any gauge crosses, the office's own polygon travels on.

    1440 km2 against a 12 km2 cap, which the compiler refuses. That is the
    gate working on an uncorroborated warning and it is what attack A3 turns
    on, so it is asserted rather than left implied.
    """
    hazards = await _s1_hazards(recording)
    first = hazards[0]
    assert first.narrowing is not None
    assert first.narrowing.narrowed is False
    assert first.narrowing.area_km2 == pytest.approx(1440.25, abs=0.5)
    assert first.narrowing.area_km2 > FLOOD_PLAYBOOK.max_area_km2


async def test_the_corroborated_s1_hazard_is_narrowed_under_the_flood_area_cap(
    recording: Recording,
) -> None:
    """The Blanco Rd crossing narrows S1 to something publishable.

    The margin, not just the area: this is the assertion that keeps S1 able to
    publish at all. If it fails, read the numbers in the message before
    touching the cap - the reach radius in `fusion.FLOOD_REACH_RADIUS_M` is
    what sets this area, and `playbooks/flood.yaml` is the policy.
    """
    narrowed = [item for item in await _s1_hazards(recording) if item.narrowing is not None]
    clipped = [item for item in narrowed if item.narrowing.narrowed]
    assert clipped, "no S1 hazard was ever narrowed; the gauge citations lost their geometry"

    first = clipped[0]
    cap = FLOOD_PLAYBOOK.max_area_km2
    area = area_km2(first.geometry)
    assert area == pytest.approx(NARROWED_KM2, abs=NARROWED_TOLERANCE_KM2), (
        f"the first narrowed S1 hazard measures {area:.4f} km2, not {NARROWED_KM2:.4f}; "
        f"the {cap:g} km2 flood cap leaves {cap - area:.4f} km2 of margin"
    )
    assert area < cap, f"{area:.4f} km2 is over the {cap:g} km2 flood cap"
    assert first.narrowing.area_km2 == pytest.approx(area, abs=1e-4)
    assert first.narrowing.original_area_km2 == pytest.approx(1440.25, abs=0.5)
    # Severity 4 and over the 4 km2 two-key area: narrowing makes S1
    # publishable, it does not make it publishable without a human.
    assert first.severity >= FLOOD_PLAYBOOK.two_key_severity
    assert area > FLOOD_PLAYBOOK.two_key_area_km2
    assert FLOOD_PLAYBOOK.needs_two_keys(first.severity, area)


async def test_the_narrowed_s1_hazard_cites_the_gauge_that_narrowed_it(
    recording: Recording,
) -> None:
    """The audit trail: which gauge, both geometries, every citation kept."""
    clipped = [
        item
        for item in await _s1_hazards(recording)
        if item.narrowing is not None and item.narrowing.narrowed
    ]
    first = clipped[0]
    record = first.narrowing
    assert record is not None
    assert record.reach_radius_m == FLOOD_REACH_RADIUS_M
    assert len(record.corroborated_by) >= 1
    cited = {item.event_id: item for item in first.evidence}
    for event_id in record.corroborated_by:
        assert event_id in cited, "the reach was measured from a citation the hazard dropped"
        assert cited[event_id].source is Source.USGS
        assert cited[event_id].kind == "gauge_height"
        assert cited[event_id].geometry is not None
    # The NWS product is still cited, and its polygon is still on the record.
    assert any(item.source is Source.NWS for item in first.evidence)
    assert record.original_geometry.type == "Polygon"


#: When the second warning arrives and brings the Wilderness Rd gauge into
#: corroboration range, giving a second reach 8.6 km from the first.
SECOND_WARNING = datetime(2026, 4, 20, 22, 57, tzinfo=UTC)


async def test_the_s1_hazard_is_still_narrowed_and_renewable_at_the_end_of_the_window(
    recording: Recording,
) -> None:
    """The advisory survives the second warning instead of lapsing.

    From 22:57Z the etn-10 polygon brings the Wilderness Rd gauge within reach,
    so S1 corroborates two reaches 8.6 km apart. Their union is 14.09 km2,
    over the 12 km2 cap, and emitting it would have the compiler refuse every
    renewal for the last six hours of the window while the advisory already
    covering Blanco Rd quietly expired. Fusion renews against the reach in
    force instead, so every reading through to the cancellation stays
    publishable.
    """
    hazards = await _s1_hazards(recording)
    narrowed = [item for item in hazards if item.narrowing is not None and item.narrowing.narrowed]
    assert narrowed, "nothing was narrowed at all"

    cap = FLOOD_PLAYBOOK.max_area_km2
    over = [
        (item.narrowing.area_km2, item.narrowing.excluded)
        for item in narrowed
        if item.narrowing is not None and item.narrowing.area_km2 > cap
    ]
    assert not over, f"a narrowed S1 hazard is over the {cap:g} km2 cap: {over}"

    # The replay runs well past the second warning, and the last reading
    # before the cancellation is still one reach the compiler will pass.
    last = narrowed[-1]
    newest = max(cite.observed_at for cite in last.evidence)
    assert newest > SECOND_WARNING, (
        f"the last narrowed hazard cites nothing after {SECOND_WARNING.isoformat()}, "
        "so this test is not exercising the second warning at all"
    )
    assert last.geometry.type == "Polygon", "two reaches were advised on as one area"
    assert area_km2(last.geometry) == pytest.approx(NARROWED_KM2, abs=NARROWED_TOLERANCE_KM2)

    # One hazard, one reach, throughout: the same identity and the same ground
    # as the first advisory, which is what "the advisory is still in force"
    # means for a renewal.
    first = narrowed[0]
    assert last.id == first.id
    assert last.geometry.to_shapely().intersects(first.geometry.to_shapely())

    # Still a two-key advisory at the end, on both counts.
    assert last.severity >= FLOOD_PLAYBOOK.two_key_severity
    assert area_km2(last.geometry) > FLOOD_PLAYBOOK.two_key_area_km2


async def test_the_reach_s1_left_out_is_named_in_the_audit_record(
    recording: Recording,
) -> None:
    """What was excluded, and why, for a reader of the audit log."""
    excluding = [
        item
        for item in await _s1_hazards(recording)
        if item.narrowing is not None and item.narrowing.excluded
    ]
    assert excluding, "S1 corroborates two reaches, so one of them has to be excluded"

    record = excluding[-1].narrowing
    assert record is not None
    assert "disjoint" in record.reason
    assert "renewed against the reach the advisory in force already covers" in record.reason
    for event_id in record.excluded:
        assert event_id in record.reason, "an excluded reach the reason does not name"
        # Still cited as evidence for the hazard: it was left out of the
        # geometry, not disbelieved.
        assert event_id in {cite.event_id for cite in excluding[-1].evidence}
    assert not set(record.excluded) & set(record.corroborated_by)
