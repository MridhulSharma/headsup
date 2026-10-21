"""For ANY proposal, what the compiler publishes is inside the playbook.

The unit tests check one rule each on a proposal built to trip it. These check
the opposite direction: whatever a generated proposal asks for - any hazard
class, any action, a polygon from a city block to half of Texas, a window from
a minute to a fortnight, a revision, an approval or none - anything that comes
out the other side is under the area cap, under the lifetime cap, carrying an
action its class allows, and verifiable.

The strategies deliberately generate proposals that should be refused. A run
where everything was rejected would prove nothing, so `test_the_generator_
reaches_both_outcomes` keeps the generator honest.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fixtures.compiler import NOW, box
from headsup.agents.compiler import (
    BLOCKING_ACTIONS,
    Compiler,
    InMemoryApprovals,
    Poi,
    PoiKind,
    RuleCode,
    Signer,
    StubPoiChecker,
    area_km2,
    default_playbooks,
    verify,
)
from headsup.contracts import (
    Action,
    ActionType,
    AppliesTo,
    CriticVerdict,
    Hazard,
    HazardClass,
    ProposedAdvisory,
    Verdict,
)
from samples import NWS_EVIDENCE, T0, USGS_EVIDENCE

#: 500+ examples per property is the step's Done-when bar. The deadline is off
#: because the first example pays for shapely and pyproj warming up.
PROFILE = settings(
    max_examples=600,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

PLAYBOOKS = default_playbooks()
SIGNER = Signer.dev()
VERIFY_KEY = SIGNER.verify_key_b64

#: Caps are per class, so a property has to read the playbook it was judged
#: against rather than a single number.
CAPS = {book.hazard_class: book for book in PLAYBOOKS}

CENTRES = [(-98.4933, 29.4204), (-97.7431, 30.2672)]


@st.composite
def proposals(draw) -> ProposedAdvisory:
    """A proposal from anywhere in the space the compiler must survive."""
    hazard_class = draw(st.sampled_from(list(HazardClass)))
    action_type = draw(st.sampled_from(list(ActionType)))
    severity = draw(st.integers(min_value=1, max_value=5))
    # From a quarter of a city block to a county, so every area cap is
    # straddled from both sides.
    area = draw(
        st.one_of(
            st.floats(min_value=0.001, max_value=2.0),
            st.floats(min_value=2.0, max_value=400.0),
        )
    )
    centre = draw(st.sampled_from(CENTRES))
    geometry = box(area, centre=centre)

    # Windows from a minute to a fortnight, starting before or after `now`.
    offset_minutes = draw(st.integers(min_value=-240, max_value=240))
    lifetime_minutes = draw(st.integers(min_value=1, max_value=20_160))
    valid_from = NOW + timedelta(minutes=offset_minutes)

    params: dict[str, object] = {}
    if action_type is ActionType.SPEED_CAP:
        params["speed_mph"] = draw(st.integers(min_value=1, max_value=70))
    elif action_type is ActionType.DENSITY_CAP:
        params["max_vehicles"] = draw(st.integers(min_value=1, max_value=500))

    return ProposedAdvisory(
        id=draw(st.from_regex(r"\Aprop_[a-z0-9]{6,12}\Z", fullmatch=True)),
        hazard=Hazard(
            id="haz_gen_1",
            **{"class": hazard_class},
            severity=severity,
            confidence=draw(st.floats(min_value=0.0, max_value=1.0)),
            geometry=geometry,
            evidence=[NWS_EVIDENCE, USGS_EVIDENCE],
            valid_until=NOW + timedelta(hours=6),
            first_seen=T0,
            updated_at=T0,
        ),
        action=Action(
            type=action_type,
            applies_to=AppliesTo(operators=["*"], vehicle_classes=["*"]),
            params=params,
        ),
        geometry=geometry,
        valid_from=valid_from,
        valid_until=valid_from + timedelta(minutes=lifetime_minutes),
        evidence=[NWS_EVIDENCE, USGS_EVIDENCE],
        policy_rule=draw(st.sampled_from(["flood.no_entry.v1", "made.up.rule", "x"])),
        proposed_by="proposer",
        created_at=T0,
    )


@st.composite
def verdicts(draw, proposal: ProposedAdvisory) -> CriticVerdict:
    """An APPROVE, an ESCALATE, or a REVISE that may narrow or may overreach."""
    kind = draw(st.sampled_from([Verdict.APPROVE, Verdict.ESCALATE, Verdict.REVISE]))
    if kind is not Verdict.REVISE:
        return CriticVerdict(
            proposal_id=proposal.id, verdict=kind, reasons=["generated"], created_at=T0
        )
    shrink = draw(st.floats(min_value=0.1, max_value=2.0))
    cut_minutes = draw(st.integers(min_value=-120, max_value=120))
    original = area_km2(proposal.geometry)
    return CriticVerdict(
        proposal_id=proposal.id,
        verdict=Verdict.REVISE,
        reasons=["generated"],
        revised_geometry=box(max(original * shrink, 0.0005), centre=CENTRES[0]),
        revised_valid_until=proposal.valid_until + timedelta(minutes=cut_minutes),
        created_at=T0,
    )


@st.composite
def cases(draw):
    """A proposal, a verdict about it, and whether a human approved it."""
    proposal = draw(proposals())
    verdict = draw(verdicts(proposal))
    approvals = InMemoryApprovals()
    if draw(st.booleans()):
        approvals.grant(proposal.id, "duty_officer_7", now=NOW)
    pois = []
    if draw(st.booleans()):
        pois.append(
            Poi(
                id="poi_hospital_1",
                kind=PoiKind.HOSPITAL,
                name="Riverside General",
                access_points=(CENTRES[0], (CENTRES[0][0] + 0.0002, CENTRES[0][1])),
            )
        )
    return proposal, verdict, approvals, StubPoiChecker(pois=pois)


def compile_case(case, now: datetime = NOW):
    proposal, verdict, approvals, poi = case
    compiler = Compiler(
        playbooks=PLAYBOOKS,
        signer=SIGNER,
        approvals=approvals,
        poi=poi,
        clock=lambda: now,
        issuer="headsup/san_antonio",
    )
    return compiler.compile(proposal, verdict), proposal, verdict, poi


# --- the four properties the packet names -----------------------------------


@PROFILE
@given(cases())
def test_a_published_advisory_is_never_over_its_area_cap(case) -> None:
    result, *_ = compile_case(case)
    if not result.ok:
        return
    book = CAPS[result.advisory.properties.hazard.hazard_class]
    assert area_km2(result.advisory.geometry) <= book.max_area_km2


@PROFILE
@given(cases())
def test_a_published_advisory_is_never_past_its_lifetime_cap(case) -> None:
    result, *_ = compile_case(case)
    if not result.ok:
        return
    properties = result.advisory.properties
    book = CAPS[properties.hazard.hazard_class]
    lifetime = (properties.valid_until - properties.valid_from).total_seconds() / 60.0
    assert lifetime <= book.max_lifetime_minutes
    assert properties.valid_until > NOW


@PROFILE
@given(cases())
def test_a_published_advisory_never_carries_an_action_its_class_forbids(case) -> None:
    result, *_ = compile_case(case)
    if not result.ok:
        return
    properties = result.advisory.properties
    book = CAPS[properties.hazard.hazard_class]
    assert book.allows(properties.action.type)
    assert properties.policy_rule == book.rule_for(properties.action.type)


@PROFILE
@given(cases())
def test_a_published_advisory_always_verifies(case) -> None:
    result, *_ = compile_case(case)
    if not result.ok:
        return
    assert result.advisory.properties.signature
    assert verify(result.advisory, verify_key=VERIFY_KEY)
    assert not verify(result.advisory, verify_key=Signer.generate().verify_key_b64)


# --- the other guarantees ---------------------------------------------------


@PROFILE
@given(cases())
def test_a_published_advisory_never_closes_every_critical_access_edge(case) -> None:
    result, _proposal, _verdict, poi = compile_case(case)
    if not result.ok:
        return
    blocked = poi.blocked_access(result.advisory.geometry, result.advisory.properties.action.type)
    assert blocked == ()


@PROFILE
@given(cases())
def test_a_severe_or_wide_advisory_never_publishes_unapproved(case) -> None:
    result, proposal, _verdict, _poi = compile_case(case)
    if not result.ok:
        return
    book = CAPS[result.advisory.properties.hazard.hazard_class]
    area = area_km2(result.advisory.geometry)
    if proposal.hazard.severity >= book.two_key_severity or area > book.two_key_area_km2:
        assert result.context.approval is not None


@PROFILE
@given(cases())
def test_the_compiler_never_raises_and_always_answers(case) -> None:
    """Nothing goes unanswered: every proposal gets checks and a decision."""
    result, proposal, *_ = compile_case(case)
    assert result.proposal_id == proposal.id
    assert result.checks
    assert result.ok or result.reasons


@PROFILE
@given(cases())
def test_a_published_advisory_keeps_the_proposals_evidence(case) -> None:
    """An advisory nobody can trace back to a feed is not auditable."""
    result, proposal, *_ = compile_case(case)
    if not result.ok:
        return
    assert result.advisory.properties.evidence == proposal.evidence
    assert result.advisory.properties.issuer == "headsup/san_antonio"
    assert result.advisory.id == result.advisory.properties.id


@PROFILE
@given(cases())
def test_a_revision_never_grows_what_was_proposed(case) -> None:
    result, proposal, verdict, _poi = compile_case(case)
    if not result.ok or verdict.verdict is not Verdict.REVISE:
        return
    assert area_km2(result.advisory.geometry) <= area_km2(proposal.geometry) + 1e-9
    assert result.advisory.properties.valid_until <= proposal.valid_until


@PROFILE
@given(cases())
def test_compiling_twice_gives_the_same_advisory(case) -> None:
    """Determinism: the gate is a function, so a replay reproduces the run."""
    first, *_ = compile_case(case)
    second, *_ = compile_case(case)
    if not first.ok:
        assert not second.ok
        assert first.codes == second.codes
        return
    assert first.advisory.signing_bytes() == second.advisory.signing_bytes()
    assert first.advisory.properties.signature == second.advisory.properties.signature


# --- keeping the generator honest -------------------------------------------


@settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(cases())
def test_the_generator_reaches_both_outcomes(case) -> None:
    """Record what the generated space actually exercised.

    A property that only ever sees rejections proves nothing, so this test
    tallies the outcomes and the assertions below run on the totals.
    """
    result, *_ = compile_case(case)
    SEEN["published" if result.ok else "rejected"] += 1
    for code in getattr(result, "codes", ()):
        SEEN[code.value] = SEEN.get(code.value, 0) + 1


SEEN: dict[str, int] = {"published": 0, "rejected": 0}


def test_the_generated_space_covered_both_outcomes_and_every_cap() -> None:
    """Assert on the tallies the property above left behind.

    If it is run on its own (`-k covered`) the tallies are empty, so it fills
    them first: calling a `@given` function with no arguments runs it.
    """
    if not any(SEEN.values()):
        test_the_generator_reaches_both_outcomes()
    assert SEEN["published"] > 0, SEEN
    assert SEEN["rejected"] > 0, SEEN
    for code in (
        RuleCode.ACTION_ALLOWED,
        RuleCode.AREA_UNDER_CAP,
        RuleCode.LIFETIME_UNDER_CAP,
        RuleCode.WINDOW_USABLE,
        RuleCode.TWO_KEY_APPROVED,
        RuleCode.CRITICAL_ACCESS_OPEN,
        RuleCode.REVISION_NARROWS,
    ):
        assert SEEN.get(code.value, 0) > 0, f"{code.value} was never exercised: {SEEN}"


def test_every_blocking_action_is_in_the_generated_space() -> None:
    """The critical-access rule only fires on these, so they must be generated."""
    assert set(ActionType) >= BLOCKING_ACTIONS
