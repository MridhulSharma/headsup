"""A2: the gauge stops reporting mid-storm, and the hazard does not vanish.

A stream gauge is a box on a bridge. In the middle of the event that matters
it is the single most likely thing in this system to go quiet: the power goes,
the telemetry backhaul floods, the site is simply not polled for an hour. The
failure to avoid is the one where that silence reads as good news - the water
has gone down, the hazard is over, the crossing is open - because nothing
observed the water going down. Nothing observed anything.

So the claim is three things, and they pull in different directions: the
confidence has to fall, because the corroboration really is gone; the hazard
has to stay alive, because the warning is still in force; and USGS has to go
on being cited, because the last thing the gauge said is still the only
observation of water anybody has.

The timeline, in feed time, and what each step is for:

| at | event | what it establishes |
| --- | --- | --- |
| T0 | flash flood warning issued | the hazard, NWS alone |
| T0+25 | Blanco Rd gauge at 5.1 ft, over its 4.5 ft action stage | corroboration |
| T0+40 | continuation statement | the gauge is inside `GAUGE_WINDOW`, so it still counts |
| T0+100 | continuation statement | 75 minutes stale: dropped, and the confidence falls |
| T0+160 | continuation statement | it stays fallen, and the hazard is still here |

The corroboration at T0+25 is three things at once: `GAUGE_WEIGHT` on the
confidence, the gauge cited with its own point, and - in fusion - the polygon
narrowed to the reach that point covers.

Two separate mechanisms are being measured, which is why the tests split:

* **The sentinel drops a stale gauge.** `FloodSentinel.prune` forgets a
  reading older than `GAUGE_WINDOW` on *event* time, so each later
  continuation is read with less corroboration than the last. That is the
  confidence falling.
* **Fusion decays a vote rather than dropping it.** A reading's weight halves
  every `HALF_LIFE` of feed time and never goes below `STALE_FLOOR`, and
  `GROUP_WINDOW` is three hours, so a group whose feeds have all gone quiet
  is still there with a confidence somebody can see is low. That is the
  hazard staying alive.
* **The stale-feed breaker cuts what the floor left.** Once the gauge has
  missed three of its own poll intervals (`ingest.freshness`) it is not a
  slow feed any more, and `STALE_BREAKER` takes half of the floored vote.
  This is the attack's own failure mode being priced in: the floor alone
  would leave a silent sensor holding a quarter of its weight indefinitely.

Everything the hazard cites it keeps: `_Group.evidence` accumulates, so the
gauge's citation - and with it the narrowed geometry, which is measured off
that citation's own point - survives the gauge going silent. The hazard
therefore goes on pointing at the reach where water was actually observed
while saying it is less sure, which is the behaviour this attack is about.

`tests/attacks/mode.py` decides which model answers the one volume call per
product: the committed `sentinel_flood_fields.json` by default, a real model
in the ablation run. Nothing here needs Redis.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mode import LLM_MARK, attack_client

from fixtures.ingest import POLYGON_ALERT, WARNING_RING, gauge_reading, nws_body
from headsup.agents.fusion import (
    GROUP_WINDOW,
    HALF_LIFE,
    STALE_BREAKER,
    STALE_FLOOR,
    Fusion,
)
from headsup.agents.sentinels.flood import (
    GAUGE_WEIGHT,
    GAUGE_WINDOW,
    FloodSentinel,
)
from headsup.contracts import CandidateHazard, CivicEvent, HazardClass, Source
from headsup.ingest import NwsAdapter, RawPayload, UsgsAdapter, make_ref

#: The S1 warning's issue time.
T0 = datetime(2026, 4, 20, 18, 50, tzinfo=UTC)

#: Blanco Rd, inside the warning polygon. Its action stage is 4.5 ft.
BLANCO_RD = (-98.5197, 29.5639)
SITE = "08178593"
ABOVE_STAGE = "5.10"

#: How long after T0 each product and reading is stamped.
GAUGE_AT = timedelta(minutes=25)
EARLY_CON = timedelta(minutes=40)
LATE_CON = timedelta(minutes=100)
LATEST_CON = timedelta(minutes=160)

#: Far enough from Bexar County to be a different hazard in a different group:
#: the clock has to be able to move without anything renewing the first one.
FAR_WEST = 4.0

#: `llm` in a live run and nothing in a mock one, so the default
#: `-m "not llm"` sweep cannot spend money even with `HU_LLM_MODE` set.
pytestmark = LLM_MARK


def alert(
    *,
    at: timedelta = timedelta(0),
    nws_id: str = "urn:oid:2.49.0.1.840.0.ffw.001.1",
    shift_lon: float = 0.0,
    continuation: bool = False,
) -> dict[str, Any]:
    """One flood product, stamped `at` after T0.

    A continuation carries no `onset` of its own, which is how the recording
    stamps the statements that follow a warning: the adapter then reads
    `effective`, so a reissue moves the sentinel's event clock the way a real
    one does.
    """
    moment = T0 + at
    stamp = moment.isoformat().replace("+00:00", "Z")
    ring = [[lon + shift_lon, lat] for lon, lat in WARNING_RING]
    properties = {
        **POLYGON_ALERT["properties"],
        "id": nws_id,
        "sent": stamp,
        "effective": stamp,
        "onset": None if continuation else stamp,
        # The product's own window, which is where the candidate's lifetime
        # comes from. Well clear of the last continuation either way.
        "ends": (T0 + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
        "expires": (T0 + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
        "messageType": "Update" if continuation else "Alert",
    }
    return {
        "id": f"https://api.weather.gov/alerts/{nws_id}",
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": properties,
    }


def nws_event(feature: dict[str, Any], *, at: timedelta) -> CivicEvent:
    payload = RawPayload(
        source=Source.NWS,
        ref=make_ref(Source.NWS, T0 + at, 0),
        url="https://api.weather.gov/alerts/active?area=TX",
        fetched_at=T0 + at,
        body=nws_body(feature),
    )
    (event,) = NwsAdapter().parse(payload)
    return event


def gauge_event(value: str = ABOVE_STAGE, *, at: timedelta = GAUGE_AT) -> CivicEvent:
    """One gauge-height reading from Blanco Rd."""
    moment = T0 + at
    lon, lat = BLANCO_RD
    payload = RawPayload(
        source=Source.USGS,
        ref=make_ref(Source.USGS, moment, 0),
        url="https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous/items",
        fetched_at=moment,
        body={
            "type": "FeatureCollection",
            "features": [
                gauge_reading(SITE, moment.isoformat().replace("+00:00", "Z"), value, lon, lat)
            ],
        },
    )
    (event,) = UsgsAdapter().parse(payload)
    return event


@pytest.fixture
def sentinel() -> FloodSentinel:
    return FloodSentinel(llm=attack_client())


async def storm(sentinel: FloodSentinel) -> dict[str, CandidateHazard]:
    """Run the whole timeline through one sentinel, keyed by step.

    Every step emits exactly one candidate, which is itself part of the claim:
    a feed going quiet must not make the sentinel stop answering.
    """
    steps: dict[str, CandidateHazard] = {}
    (steps["warning"],) = await sentinel.on_event(nws_event(alert(), at=timedelta(0)))
    (steps["gauge"],) = await sentinel.on_event(gauge_event())
    for name, offset in (
        ("early_con", EARLY_CON),
        ("late_con", LATE_CON),
        ("latest_con", LATEST_CON),
    ):
        (steps[name],) = await sentinel.on_event(
            nws_event(alert(at=offset, continuation=True), at=offset)
        )
    return steps


# --- the sentinel: a stale gauge stops corroborating -----------------------


async def test_the_gauge_corroborates_until_its_window_passes_and_then_does_not(
    sentinel: FloodSentinel,
) -> None:
    """The headline on the sentinel's side, step by step.

    The reading at T0+25 is 15 minutes old at the early continuation and 75
    at the late one, and `GAUGE_WINDOW` is 45: the first still counts and the
    second has been forgotten, on feed time, so a recording replayed at 60x
    forgets it 60 times faster.
    """
    steps = await storm(sentinel)

    assert GAUGE_AT < EARLY_CON < GAUGE_AT + GAUGE_WINDOW < LATE_CON
    assert [item.hazard_class for item in steps.values()] == [HazardClass.FLOOD] * 5

    cited = {name: {cite.source for cite in item.evidence} for name, item in steps.items()}
    assert cited["warning"] == {Source.NWS}
    assert cited["gauge"] == {Source.NWS, Source.USGS}
    assert cited["early_con"] == {Source.NWS, Source.USGS}
    # The gauge has gone quiet and been forgotten: nothing observed the water
    # going down, and nothing is pretending it did.
    assert cited["late_con"] == {Source.NWS}
    assert cited["latest_con"] == {Source.NWS}
    assert sentinel.gauges_above_stage == 0
    assert sentinel.live_alerts == 1


async def test_the_confidence_falls_when_the_corroboration_is_lost(
    sentinel: FloodSentinel,
) -> None:
    """Confidence decays, which is the first third of the claim.

    It falls by exactly the one weight that was withdrawn, and it stops
    falling: the product's own fields still support what they supported, so
    the floor here is the warning on its own and not zero.
    """
    steps = await storm(sentinel)

    uncorroborated = steps["warning"].confidence
    corroborated = steps["early_con"].confidence
    lost = steps["late_con"].confidence

    assert corroborated > lost
    assert lost == pytest.approx(uncorroborated, abs=1e-4)
    assert steps["latest_con"].confidence == pytest.approx(lost, abs=1e-4)
    # The gap is the gauge's weight, folded the way `noisy_or` folds it.
    assert corroborated == pytest.approx(lost + (1 - lost) * GAUGE_WEIGHT, abs=1e-4)


async def test_the_sentinel_keeps_answering_while_the_gauge_is_silent(
    sentinel: FloodSentinel,
) -> None:
    """The warning is still in force, so every product still yields a hazard.

    The silence costs corroboration. It does not cost the candidate, the
    polygon or the window: a vehicle routed off that crossing stays routed
    off it.
    """
    steps = await storm(sentinel)
    latest = steps["latest_con"]

    assert latest.severity == steps["warning"].severity
    assert latest.geometry == steps["warning"].geometry
    assert latest.valid_until > latest.detected_at
    assert sentinel.llm_failures == 0


async def test_a_gauge_that_comes_back_corroborates_again(
    sentinel: FloodSentinel,
) -> None:
    """The silence is not a verdict: the site reports again and is believed.

    A gauge crossing its action stage re-emits the live warning, so the
    recovery needs no new product - which matters, because the office may not
    issue one for half an hour.
    """
    steps = await storm(sentinel)
    (recovered,) = await sentinel.on_event(gauge_event(at=LATEST_CON + timedelta(minutes=10)))

    assert {cite.source for cite in recovered.evidence} == {Source.NWS, Source.USGS}
    assert recovered.confidence > steps["latest_con"].confidence
    assert sentinel.gauges_above_stage == 1


# --- fusion: the hazard stays alive ----------------------------------------


async def test_nws_and_usgs_keep_the_hazard_alive_through_the_silence(
    sentinel: FloodSentinel,
) -> None:
    """The other two thirds: one hazard, both sources, all the way through.

    Fusion's group accumulates citations, so the gauge's reading is still part
    of why this hazard is believed long after the site stopped reporting - and
    the geometry, which was measured off that reading's own point, is still
    the reach where water was actually observed rather than the county the
    office drew.
    """
    fusion = Fusion()
    steps = await storm(sentinel)
    hazards = [await fusion.on_candidate(candidate) for candidate in steps.values()]

    first, latest = hazards[0], hazards[-1]
    assert fusion.live_groups == 1
    assert {hazard.id for hazard in hazards} == {first.id}  # one hazard, kept

    assert latest.sources == {Source.NWS, Source.USGS}
    assert any(cite.source is Source.USGS for cite in latest.evidence)
    assert latest.severity == first.severity
    assert latest.valid_until > latest.updated_at

    # Narrowed, still, to the reach the gauge cited - the only part of the
    # polygon anything ever observed water in.
    narrowing = latest.narrowing
    assert narrowing is not None
    assert narrowing.narrowed is True
    assert narrowing.corroborated_by  # by event id, so a reader can follow it
    assert narrowing.area_km2 < narrowing.original_area_km2


async def test_the_fused_confidence_follows_the_sentinel_down(
    sentinel: FloodSentinel,
) -> None:
    """What a viewer and an audit record see: the number falling.

    One vote per detector, so the flood sentinel's best live reading is the
    whole of this hazard's confidence - and when that reading loses the gauge,
    so does the hazard.
    """
    fusion = Fusion()
    steps = await storm(sentinel)
    hazards = {name: await fusion.on_candidate(candidate) for name, candidate in steps.items()}

    assert hazards["early_con"].confidence > hazards["late_con"].confidence
    assert hazards["late_con"].confidence > 0.0
    assert hazards["late_con"].confidence == pytest.approx(steps["late_con"].confidence, abs=1e-3)


async def test_a_quiet_feed_decays_to_the_floor_and_no_further() -> None:
    """A feed that has gone quiet is old, not wrong - and never weightless.

    This is the half of the decay the timeline above cannot show, because
    there the office keeps reissuing and every reissue renews the vote. Here
    the clock is moved by a hazard on the other side of the state, the first
    group is left untouched for 155 minutes, and then its own reading is
    offered again - which is what a replayer does with a re-listed product.
    `STALE_FLOOR` is where the half-life stops, `STALE_BREAKER` is the cut a
    feed takes for having missed three polls, and `GROUP_WINDOW` is what stops
    the hazard being dropped. Together they are what keeps the confidence off
    zero while making it plainly low.
    """
    sentinel = FloodSentinel(llm=attack_client())
    (warning,) = await sentinel.on_event(nws_event(alert(), at=timedelta(0)))
    (corroborated,) = await sentinel.on_event(gauge_event())

    elsewhere = FloodSentinel(llm=attack_client())
    quiet_hours = timedelta(minutes=180)
    (far_away,) = await elsewhere.on_event(
        nws_event(
            alert(at=quiet_hours, nws_id="urn:oid:2.49.0.1.840.0.ffw.009.1", shift_lon=FAR_WEST),
            at=quiet_hours,
        )
    )

    fusion = Fusion()
    fresh = await fusion.on_candidate(corroborated)
    await fusion.on_candidate(far_away)
    stale = await fusion.on_candidate(corroborated)

    age = quiet_hours - GAUGE_AT
    assert age < GROUP_WINDOW  # so the group is still there to be asked
    assert age > 3 * HALF_LIFE  # and stale enough to be on the floor
    assert fusion.live_groups == 2

    assert stale.id == fresh.id
    assert stale.confidence < fresh.confidence
    assert stale.confidence == pytest.approx(
        STALE_FLOOR * STALE_BREAKER * corroborated.confidence, abs=1e-3
    )
    assert stale.confidence > 0.0
    assert stale.sources == {Source.NWS, Source.USGS}
    assert warning.geometry == corroborated.geometry  # the office's own polygon
