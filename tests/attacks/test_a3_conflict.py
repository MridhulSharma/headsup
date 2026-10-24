"""A3: a warning is in force and the gauge under it says the creek is normal.

Two authoritative feeds, flatly disagreeing. NWS has drawn a flash flood
warning over north-east Bexar County; the one instrument inside that polygon
reads 3.9 ft against a 4.5 ft action stage. Neither feed is wrong - an NWS
storm-based polygon is a forecast area tens of kilometres across and most of
it is dry, and a gauge measures one bridge - but a fleet has to be told
something, and "flooding, 558 km2, stay out" is not it.

**What the system actually does, which is not quite what the step asked for.**
The step's wording is "V4 issues the advisory at reduced severity citing both
sources". It does neither of those two things, and the tests here record what
it does instead rather than asserting a behaviour that is not built:

* *Not reduced severity.* Severity is arithmetic over the feeds' own fields -
  `FLOOD_PRODUCTS` says a flash flood warning is a 4 - and `fusion._threshold`
  is the only thing that moves it, downward, and only when no authoritative
  feed backs it at all. NWS is authoritative, so the hazard is a 4 either way.
  Nothing in `src/headsup` reduces a severity because a gauge disagrees.
* *Not citing both sources.* `FloodSentinel._corroborating` cites the gauges
  that are *above* their action stage. A gauge reading normal is held in
  `_gauges`, where it stops the next reading counting as a fresh crossing, and
  it is never attached as evidence - so the hazard cites NWS alone.

What it does instead is refuse to publish, and the refusal is the interesting
part, because it comes out of the geometry rather than out of a conflict rule.
With no gauge above stage there is no corroborated reach, so
`fusion._narrow` keeps the polygon the office drew - it says so in as many
words, and names A3 while it is at it - and 558 km2 is over the 12 km2 cap
`playbooks/flood.yaml` sets for the whole class. The critic escalates on it
and the gate refuses it on `AREA_UNDER_CAP`, and that is a refusal no second
key can rescue: it is not that nobody has approved this advisory, it is that
this advisory may not exist.

Put beside the corroborated run, that is the whole of A3 as built:

| gauge | geometry | area | the gate |
| --- | --- | --- | --- |
| 5.10 ft, over stage | the gauge's reach | 0.04 km2 | held for a second key |
| 3.90 ft, normal | the office's polygon | 558 km2 | refused on the area cap |

Four orders of magnitude, off one reading. The reach is as small as it is
because Blanco Rd sits close to the south-west corner of the polygon the
office drew, so a 1500 m buffer around it overlaps very little of that
polygon - which is the real geometry of the real S1 event and not a fixture
chosen to make the point.

The first publishes as soon as a person signs it. The second cannot be signed
at all: an area over the class cap is not an advisory anybody may approve.

So the disagreement does change the outcome, and it changes it in the safe
direction, by way of the thing the feeds actually disagree about: where the
water is. Making the severity itself fall, and citing the contradicting gauge
so a reader can see why, would be a change to `FloodSentinel` and would move
what S1 publishes at 18:50Z - it is recorded here as a finding, not smuggled
in as a test fixture.

**V1 is recorded, not required to fail.** `run_scenario(model=False)` runs it
with no model anywhere: `llm=None` in the sentinel, fusion and the proposer,
and `Critic(rules_only=True)`. The point of recording it is that the gate
reaches the same decision either way - `review_rules` is the whole of the area
finding, and `RANK` means the model can only ever be stricter - so what the
model buys on this scenario is the rationale a person reads, not the verdict.

`tests/attacks/mode.py` decides which model V4 is built with, so this file
runs free by default and against a real model in the ablation run. The bus is
a list, so nothing here needs Redis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mode import LLM_MARK, attack_client

from fixtures.compiler import poi_checker
from fixtures.ingest import POLYGON_ALERT, gauge_reading, nws_body
from headsup.agents.compiler import (
    Compiler,
    Decision,
    InMemoryApprovals,
    PendingCompiles,
    RuleCode,
)
from headsup.agents.compiler.geo import area_km2
from headsup.agents.compiler.service import CompilerService
from headsup.agents.critic import RANK, Critic
from headsup.agents.fusion import Fusion
from headsup.agents.proposer import Proposer
from headsup.agents.sentinels.flood import FLOOD_PRODUCTS, FloodSentinel
from headsup.bus import STREAM_ADVISORIES, STREAM_AUDIT
from headsup.contracts import (
    CivicEvent,
    Hazard,
    HeadsUpModel,
    ProposedAdvisory,
    Source,
    Verdict,
)
from headsup.ingest import NwsAdapter, RawPayload, UsgsAdapter, make_ref

T0 = datetime(2026, 4, 20, 18, 50, tzinfo=UTC)

#: Blanco Rd, inside the warning polygon. Action stage 4.5 ft.
BLANCO_RD = (-98.5197, 29.5639)
SITE = "08178593"

#: The two readings the two legs turn on.
NORMAL = "3.90"
OVER_STAGE = "5.10"

#: `playbooks/flood.yaml`. Named here so a reader does not have to open it to
#: follow why 1400 km2 is refused and 7 km2 is not.
FLOOD_AREA_CAP_KM2 = 12.0
FLOOD_TWO_KEY_AREA_KM2 = 4.0
FLOOD_TWO_KEY_SEVERITY = 4

#: The person who holds the second key in this test.
APPROVER = "duty_officer_7"

#: `llm` in a live run and nothing in a mock one, so the default
#: `-m "not llm"` sweep cannot spend money even with `HU_LLM_MODE` set.
pytestmark = LLM_MARK


class FakeBus:
    """Everything `CompilerService` asks of a bus, with a memory."""

    def __init__(self) -> None:
        self.written: list[tuple[str, HeadsUpModel]] = []

    async def publish(self, message: HeadsUpModel, *, stream: str) -> str:
        self.written.append((stream, message))
        return f"{len(self.written)}-0"

    async def close(self) -> None:  # pragma: no cover - the service owns no bus here
        return None

    def on(self, stream: str) -> list[Any]:
        return [message for name, message in self.written if name == stream]


def nws_event(at: timedelta = timedelta(0)) -> CivicEvent:
    """The S1-shaped flash flood warning, stamped `at` after T0."""
    moment = T0 + at
    stamp = moment.isoformat().replace("+00:00", "Z")
    feature = {
        "id": POLYGON_ALERT["id"],
        "type": "Feature",
        "geometry": POLYGON_ALERT["geometry"],
        "properties": {
            **POLYGON_ALERT["properties"],
            "sent": stamp,
            "effective": stamp,
            "onset": stamp,
            "ends": (moment + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            "expires": (moment + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        },
    }
    payload = RawPayload(
        source=Source.NWS,
        ref=make_ref(Source.NWS, moment, 0),
        url="https://api.weather.gov/alerts/active?area=TX",
        fetched_at=moment,
        body=nws_body(feature),
    )
    (event,) = NwsAdapter().parse(payload)
    return event


def gauge_event(value: str, *, at: timedelta = timedelta(minutes=25)) -> CivicEvent:
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


@dataclass(slots=True)
class Run:
    """What one pass of the scenario did, at every stage.

    A record rather than a set of assertions, because A3 is as much about
    writing down what happens as about pinning it: a reader of this file
    should be able to see the two legs side by side.
    """

    variant: str
    hazard: Hazard
    proposal: ProposedAdvisory
    verdict: Verdict
    decision: Decision
    codes: tuple[RuleCode, ...]
    pending: int
    advisories: int
    bus: FakeBus
    service: CompilerService

    @property
    def area_km2(self) -> float:
        return area_km2(self.proposal.geometry)

    def line(self) -> str:
        """One line for a reader, and for a failure message worth reading."""
        return (
            f"{self.variant}: severity {self.hazard.severity} "
            f"confidence {self.hazard.confidence:.2f} "
            f"sources {','.join(sorted(s.value for s in self.hazard.sources))} "
            f"area {self.area_km2:.2f} km2 -> {self.verdict.value} -> "
            f"{self.decision.value} {[code.value for code in self.codes]}"
        )


async def run_scenario(*, gauge: str, model: bool) -> Run:
    """The warning, then one gauge reading, all the way to the gate.

    `model=False` is variant V1: no client in the sentinel, fusion or the
    proposer, and a critic that never asks one. Every other dependency is the
    same object in both, which is what makes the pair a comparison of
    configurations rather than of code paths.
    """
    client = attack_client() if model else None
    sentinel = FloodSentinel(llm=client)
    fusion = Fusion(llm=client)
    proposer = Proposer(llm=client, poi=poi_checker(), rules_only=not model)
    critic = Critic(llm=client, rules_only=not model)
    bus = FakeBus()
    approvals = InMemoryApprovals()
    service = CompilerService(
        bus,
        Compiler(poi=poi_checker(), approvals=approvals, issuer="headsup/san_antonio"),
        pending=PendingCompiles(),
    )

    candidates = await sentinel.on_event(nws_event())
    candidates += await sentinel.on_event(gauge_event(gauge))
    hazards = [await fusion.on_candidate(candidate) for candidate in candidates]
    hazard = hazards[-1]
    (proposal, *rest) = await proposer.propose_all(hazard)
    assert rest == []  # FLOOD asks for one action; see `COMPANIONS`

    verdict = await critic.review(proposal)
    outcome = await service.publish(proposal, verdict)
    return Run(
        variant="V4" if model else "V1",
        hazard=hazard,
        proposal=proposal,
        verdict=verdict.verdict,
        decision=outcome.audit.decision,
        codes=outcome.result.codes,
        pending=len(service.pending),
        advisories=len(bus.on(STREAM_ADVISORIES)),
        bus=bus,
        service=service,
    )


@pytest.fixture
async def conflicted() -> Run:
    """V4 with the gauge reading normal: the attack."""
    return await run_scenario(gauge=NORMAL, model=True)


@pytest.fixture
async def corroborated() -> Run:
    """V4 with the gauge over its action stage: the S1 story, for contrast."""
    return await run_scenario(gauge=OVER_STAGE, model=True)


# --- what the disagreement does to the hazard ------------------------------


async def test_a_gauge_reading_normal_neither_corroborates_nor_contradicts(
    conflicted: Run,
) -> None:
    """The first recorded fact, and the one the step's wording misses.

    A below-stage reading produces no candidate of its own - a point is not an
    area a fleet can be told about - and it is not attached to the warning as
    evidence either. So the hazard cites NWS alone: there is nothing in the
    record a reader could follow to find out that the gauge disagreed.
    """
    hazard = conflicted.hazard

    assert hazard.sources == {Source.NWS}
    assert Source.USGS not in hazard.sources
    assert hazard.authoritative_sources == {Source.NWS}
    assert all(cite.geometry is None for cite in hazard.evidence)


async def test_the_severity_is_the_products_own_and_is_not_reduced(
    conflicted: Run, corroborated: Run
) -> None:
    """The second recorded fact: severity does not move either way.

    `FLOOD_PRODUCTS` carries it, `fusion._threshold` is the only thing that
    can lower it and only for want of an authoritative feed, and NWS is one.
    Identical in both legs, which is why the geometry is where the
    disagreement has to show up.
    """
    expected = FLOOD_PRODUCTS["flash_flood_warning"]

    assert conflicted.hazard.severity == expected == 4
    assert corroborated.hazard.severity == expected
    assert conflicted.hazard.severity >= FLOOD_TWO_KEY_SEVERITY


async def test_the_uncorroborated_warning_keeps_the_area_the_office_drew(
    conflicted: Run,
) -> None:
    """Where the disagreement does land: nothing narrows the polygon.

    `fusion._narrow` will only cut a flood hazard down to the reach of a gauge
    above its action stage. With none, there is no honest way to say which
    part of the polygon is the hazard, so the office's own area travels on and
    the record says so.
    """
    narrowing = conflicted.hazard.narrowing

    assert narrowing is not None
    assert narrowing.narrowed is False
    assert narrowing.corroborated_by == []
    assert narrowing.area_km2 == narrowing.original_area_km2
    assert "no corroborating observation carries a location" in narrowing.reason.lower()
    assert conflicted.area_km2 > FLOOD_AREA_CAP_KM2


async def test_the_corroborated_warning_is_narrowed_to_the_gauges_reach(
    corroborated: Run,
) -> None:
    """The contrast: one reading over stage, and the county becomes a reach."""
    narrowing = corroborated.hazard.narrowing

    assert narrowing is not None
    assert narrowing.narrowed is True
    assert len(narrowing.corroborated_by) == 1
    assert narrowing.area_km2 < narrowing.original_area_km2
    assert corroborated.hazard.sources == {Source.NWS, Source.USGS}
    assert corroborated.area_km2 <= FLOOD_AREA_CAP_KM2


# --- what the gate does with each ------------------------------------------


async def test_the_uncorroborated_warning_is_refused_and_no_key_can_rescue_it(
    conflicted: Run,
) -> None:
    """The headline, as the gate records it.

    Over the class's hard cap, so the refusal is `AREA_UNDER_CAP` and not a
    missing second key: `PendingCompiles` keeps only the proposals a token
    would fix, and this is not one of them. Nothing was published and nothing
    is waiting for anybody.
    """
    assert conflicted.verdict is Verdict.ESCALATE
    assert conflicted.decision is Decision.REJECTED
    assert RuleCode.AREA_UNDER_CAP in conflicted.codes
    assert conflicted.advisories == 0
    assert conflicted.pending == 0

    # The refusal is on the audit stream with the two numbers that explain it.
    (record,) = conflicted.bus.on(STREAM_AUDIT)
    assert record.decision is Decision.REJECTED
    assert record.area_cap_km2 == FLOOD_AREA_CAP_KM2
    assert record.area_km2 is not None and record.area_km2 > FLOOD_AREA_CAP_KM2
    assert record.hazard_narrowing is not None
    assert record.hazard_narrowing.narrowed is False


async def test_the_corroborated_warning_waits_for_a_person_and_then_publishes(
    corroborated: Run,
) -> None:
    """What A3's other leg buys: an advisory a person can actually sign.

    Severity 4 is at `two_key_severity`, so the first trip is refused for want
    of a second key and kept. One token later it publishes - and the advisory
    cites both feeds, which is the thing the conflicted leg cannot do.
    """
    assert corroborated.decision is Decision.REJECTED
    assert RuleCode.AREA_UNDER_CAP not in corroborated.codes
    assert RuleCode.TWO_KEY_APPROVED in corroborated.codes
    assert corroborated.pending == 1

    service = corroborated.service
    waiting = service.pending.waiting()[0]
    service.compiler.approvals.grant(waiting.proposal_id, APPROVER)
    outcome = await service.recompile(waiting.proposal_id)

    assert outcome is not None and outcome.ok
    (advisory,) = corroborated.bus.on(STREAM_ADVISORIES)
    properties = advisory.properties
    assert properties.hazard.severity == 4
    assert {cite.source for cite in properties.evidence} == {Source.NWS, Source.USGS}
    assert properties.signature


async def test_the_two_legs_differ_only_in_what_the_gauge_said(
    conflicted: Run, corroborated: Run
) -> None:
    """The comparison, stated as one assertion with both lines in the message.

    Same warning, same agents, same playbook. One reading decides whether
    there is an advisory to approve at all.
    """
    summary = f"{conflicted.line()}\n{corroborated.line()}"

    assert conflicted.hazard.severity == corroborated.hazard.severity, summary
    assert conflicted.area_km2 > corroborated.area_km2 * 10, summary
    assert conflicted.hazard.confidence < corroborated.hazard.confidence, summary
    assert (conflicted.advisories, conflicted.pending) == (0, 0), summary
    assert (corroborated.advisories, corroborated.pending) == (0, 1), summary


# --- V1, recorded ----------------------------------------------------------


async def test_what_v1_does_with_the_same_conflict() -> None:
    """Recorded, not required to fail: the rules path reaches the same gate.

    The area finding is `review_rules`, which needs no model, and `RANK` means
    a model can only make a verdict more restrictive. So V1 escalates and is
    refused exactly as V4 is, and the difference between them on this
    scenario is the prose in the hazard's rationale rather than the decision.

    If this ever stops being true it is worth knowing which way: hence the
    comparison rather than a fixed expectation.
    """
    rules = await run_scenario(gauge=NORMAL, model=False)
    model = await run_scenario(gauge=NORMAL, model=True)
    summary = f"{rules.line()}\n{model.line()}"

    assert rules.hazard.severity == model.hazard.severity, summary
    assert rules.hazard.sources == model.hazard.sources, summary
    assert rules.decision is Decision.REJECTED, summary
    assert RuleCode.AREA_UNDER_CAP in rules.codes, summary
    assert rules.advisories == 0, summary

    # The model is allowed to be stricter and nothing more.
    assert RANK[model.verdict] >= RANK[rules.verdict], summary

    # And the one thing it did buy: a rationale somebody wrote in sentences.
    assert rules.hazard.rationale
    assert model.hazard.rationale
    assert model.hazard.rationale != rules.hazard.rationale


async def test_v1_narrows_the_corroborated_leg_the_same_way() -> None:
    """Narrowing is arithmetic, so it is not a thing the model does.

    Worth pinning on this attack because it is the step the conflicted leg is
    missing: if narrowing depended on a model, A3's outcome would depend on a
    model too.
    """
    rules = await run_scenario(gauge=OVER_STAGE, model=False)
    model = await run_scenario(gauge=OVER_STAGE, model=True)

    assert rules.hazard.narrowing is not None
    assert model.hazard.narrowing is not None
    assert rules.hazard.narrowing.narrowed is model.hazard.narrowing.narrowed is True
    assert rules.hazard.narrowing.area_km2 == pytest.approx(
        model.hazard.narrowing.area_km2, rel=1e-6
    )
    assert rules.pending == model.pending == 1


# --- the gap, written down -------------------------------------------------


async def test_nothing_in_the_record_says_the_gauge_disagreed() -> None:
    """The finding this file exists to make explicit.

    A person reading the audit record for this refusal learns that the area
    was too large. They do not learn that the one instrument inside the
    polygon was reading nine inches below its action stage, because nothing
    carried that fact out of the sentinel. Citing a below-stage gauge as
    contradicting evidence, and letting that lower the severity, is a change
    to `FloodSentinel` with consequences for what S1 publishes at 18:50Z - so
    it is recorded here rather than assumed.

    Measured on V1, deliberately. `_rules_rationale` is built out of the
    hazard's own facts, so what it does not mention is a fact the hazard does
    not carry. V4's paragraph here comes from
    `tests/fixtures/llm/fusion_rationale.json`, which is a committed answer
    about a different hazard and mentions a gauge above flood stage - that is
    the fixture talking, not a reading of this conflict, and asserting
    against it would be asserting against the fixture.

    This test is the alarm on the gap: if the sentinel starts citing a
    disagreeing gauge, it fails, and whoever made that change should come and
    rewrite this file's docstring.
    """
    rules = await run_scenario(gauge=NORMAL, model=False)
    hazard = rules.hazard
    record = rules.bus.on(STREAM_AUDIT)[0]

    assert Source.USGS not in hazard.sources
    assert "gauge" not in (hazard.rationale or "").lower()
    assert "usgs" not in (hazard.rationale or "").lower()
    assert all("gauge" not in reason.lower() for reason in record.reasons)
