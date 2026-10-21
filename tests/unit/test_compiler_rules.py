"""One test per rule the compiler enforces, plus the shape of what it returns.

Every test builds the smallest proposal that trips exactly one rule, so a
failure here names the rule that broke rather than "the compiler".
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from fixtures.compiler import (
    CENTRE,
    NOW,
    approve,
    box,
    clock,
    escalate,
    hospital,
    poi_checker,
    proposal,
    revise,
)
from headsup.agents.compiler import (
    Compiler,
    InMemoryApprovals,
    Poi,
    PoiKind,
    RuleCode,
    verify,
)
from headsup.contracts import ActionType, HazardClass, Verdict
from samples import PROPOSAL, VERDICT

FLOOD_AREA_CAP = 12.0
FLOOD_LIFETIME_CAP = 180.0


@pytest.fixture
def compiler() -> Compiler:
    """A compiler on a fixed clock, with no POI data and no approvals."""
    return Compiler(clock=clock())


def codes(result) -> tuple[RuleCode, ...]:
    return result.codes if not result.ok else ()


# --- the happy path ---------------------------------------------------------


def test_a_proposal_inside_its_playbook_is_published(compiler: Compiler) -> None:
    result = compiler.compile(proposal(), approve())
    assert result.ok, result.reasons
    assert verify(result.advisory)
    assert result.advisory.properties.policy_rule == "flood.no_entry.v1"
    assert result.advisory.properties.issuer == "headsup/san_antonio"
    assert all(check.ok for check in result.checks)


def test_every_rule_is_recorded_even_when_they_all_pass(compiler: Compiler) -> None:
    """The audit record is a checklist, so a pass is recorded as loudly as a fail."""
    result = compiler.compile(proposal(), approve())
    recorded = [check.code for check in result.checks]
    for code in (
        RuleCode.SCHEMA_VALID,
        RuleCode.GEOMETRY_VALID,
        RuleCode.PLAYBOOK_KNOWN,
        RuleCode.ACTION_ALLOWED,
        RuleCode.AREA_UNDER_CAP,
        RuleCode.LIFETIME_UNDER_CAP,
        RuleCode.WINDOW_USABLE,
        RuleCode.TWO_KEY_APPROVED,
        RuleCode.CRITICAL_ACCESS_OPEN,
        RuleCode.ADVISORY_VALID,
    ):
        assert code in recorded


def test_the_advisory_id_is_derived_from_the_proposal(compiler: Compiler) -> None:
    """Same proposal, same advisory id: two replays of a scenario can be compared."""
    first = compiler.compile(proposal(proposal_id="prop_abc123"), approve("prop_abc123"))
    second = compiler.compile(proposal(proposal_id="prop_abc123"), approve("prop_abc123"))
    assert first.advisory.properties.id == "adv_abc123"
    assert second.advisory.properties.id == "adv_abc123"
    assert first.advisory.signing_bytes() == second.advisory.signing_bytes()


# --- schema and verdict -----------------------------------------------------


def test_a_payload_that_is_not_a_proposal_is_rejected(compiler: Compiler) -> None:
    result = compiler.compile({"id": "prop_junk", "hazard": "a flood, probably"}, approve())
    assert not result.ok
    assert codes(result) == (RuleCode.SCHEMA_VALID,)
    assert result.proposal_id == "prop_junk"


def test_a_proposal_mapping_is_validated_not_trusted(compiler: Compiler) -> None:
    """JSON from a file or the API goes through the contract, not around it."""
    payload = proposal().to_wire()
    payload["hazard"]["severity"] = 9
    result = compiler.compile(payload, approve())
    assert not result.ok
    assert codes(result) == (RuleCode.SCHEMA_VALID,)


def test_a_verdict_about_another_proposal_is_rejected(compiler: Compiler) -> None:
    result = compiler.compile(proposal(), approve("prop_someone_else"))
    assert not result.ok
    assert codes(result) == (RuleCode.VERDICT_MATCHES_PROPOSAL,)


def test_a_malformed_verdict_is_rejected(compiler: Compiler) -> None:
    result = compiler.compile(proposal(), {"verdict": "APPROVE"})
    assert not result.ok
    assert codes(result) == (RuleCode.VERDICT_VALID,)


# --- geometry and playbook --------------------------------------------------


def test_a_hazard_class_with_no_playbook_publishes_nothing() -> None:
    """No caps, no advisory. An empty playbook set is a closed gate, not an open one."""
    from headsup.agents.compiler import PlaybookSet

    compiler = Compiler(playbooks=PlaybookSet({}), clock=clock())
    result = compiler.compile(proposal(), approve())
    assert not result.ok
    assert RuleCode.PLAYBOOK_KNOWN in codes(result)


def test_a_bow_tie_polygon_never_reaches_a_signature(compiler: Compiler) -> None:
    """The compiler re-checks the geometry it is about to sign.

    The contracts already refuse a self-intersecting polygon on construction,
    so getting one this far needs `model_construct` to skip validation - which
    is exactly the case this rule exists for: the compiler is the last gate
    before a signature and does not assume what reached it was validated.
    """
    from headsup.contracts import CriticVerdict, Polygon

    good = proposal()
    bow_tie = Polygon.model_construct(
        type="Polygon",
        coordinates=[
            [
                [-98.50, 29.41],
                [-98.49, 29.42],
                [-98.50, 29.42],
                [-98.49, 29.41],
                [-98.50, 29.41],
            ]
        ],
    )
    unchecked = CriticVerdict.model_construct(
        proposal_id=good.id,
        verdict=Verdict.REVISE,
        reasons=["narrower"],
        revised_geometry=bow_tie,
        revised_valid_until=None,
        reviewed_by="critic",
        created_at=NOW,
    )
    result = compiler.compile(good, unchecked)
    assert not result.ok
    assert RuleCode.GEOMETRY_VALID in codes(result)


# --- the playbook caps ------------------------------------------------------


def test_an_action_the_class_does_not_allow_is_rejected(compiler: Compiler) -> None:
    """A school zone slows traffic; it never closes a street."""
    result = compiler.compile(
        proposal(hazard_class=HazardClass.SCHOOL_ZONE, action_type=ActionType.NO_ENTRY),
        approve(),
    )
    assert not result.ok
    assert RuleCode.ACTION_ALLOWED in codes(result)
    assert "SPEED_CAP" in result.reasons[0]


def test_an_area_over_the_cap_is_rejected(compiler: Compiler) -> None:
    result = compiler.compile(proposal(area_km2=FLOOD_AREA_CAP * 3), approve())
    assert not result.ok
    assert RuleCode.AREA_UNDER_CAP in codes(result)


def test_an_area_just_under_the_cap_is_published(compiler: Compiler) -> None:
    approvals = InMemoryApprovals()
    approvals.grant("prop_test_1", "duty_officer_7", now=NOW)
    result = Compiler(clock=clock(), approvals=approvals).compile(
        proposal(area_km2=FLOOD_AREA_CAP * 0.8), approve()
    )
    assert result.ok, result.reasons
    from headsup.agents.compiler import area_km2

    assert area_km2(result.advisory.geometry) <= FLOOD_AREA_CAP


def test_a_lifetime_over_the_cap_is_rejected(compiler: Compiler) -> None:
    result = compiler.compile(proposal(lifetime_minutes=FLOOD_LIFETIME_CAP + 1), approve())
    assert not result.ok
    assert RuleCode.LIFETIME_UNDER_CAP in codes(result)


def test_each_class_keeps_its_own_caps(compiler: Compiler) -> None:
    """A work zone's 2 km2 cap refuses what a flood's 12 km2 cap allows.

    3 km2 is also under the flood playbook's 4 km2 two-key limit, so the only
    thing that differs between these two calls is the playbook.
    """
    wide = proposal(area_km2=3.0)
    assert compiler.compile(wide, approve()).ok

    work_zone = proposal(
        hazard_class=HazardClass.WORK_ZONE, action_type=ActionType.SPEED_CAP, area_km2=3.0
    )
    result = compiler.compile(work_zone, approve())
    assert not result.ok
    assert RuleCode.AREA_UNDER_CAP in codes(result)


# --- the window -------------------------------------------------------------


def test_an_advisory_that_already_expired_is_rejected(compiler: Compiler) -> None:
    stale = proposal(valid_from=NOW - timedelta(hours=4), lifetime_minutes=60)
    result = compiler.compile(stale, approve())
    assert not result.ok
    assert RuleCode.WINDOW_USABLE in codes(result)


# --- critic revisions -------------------------------------------------------


def test_a_revision_that_narrows_the_polygon_is_applied(compiler: Compiler) -> None:
    wide = proposal(area_km2=FLOOD_AREA_CAP * 3)
    narrow = box(1.0)
    result = compiler.compile(wide, revise(wide, geometry=narrow))
    assert result.ok, result.reasons
    assert result.advisory.geometry.coordinates == narrow.coordinates


def test_a_revision_that_shortens_the_window_is_applied(compiler: Compiler) -> None:
    long_run = proposal(lifetime_minutes=FLOOD_LIFETIME_CAP * 2)
    cut = NOW + timedelta(minutes=30)
    result = compiler.compile(long_run, revise(long_run, valid_until=cut))
    assert result.ok, result.reasons
    assert result.advisory.properties.valid_until == cut


def test_a_revision_may_not_widen_the_polygon(compiler: Compiler) -> None:
    """REVISE is the critic's scalpel, not a second proposal."""
    small = proposal(area_km2=0.5)
    result = compiler.compile(small, revise(small, geometry=box(4.0)))
    assert not result.ok
    assert RuleCode.REVISION_NARROWS in codes(result)


def test_a_revision_may_not_extend_the_window(compiler: Compiler) -> None:
    short = proposal(lifetime_minutes=30)
    result = compiler.compile(
        short, revise(short, valid_until=short.valid_until + timedelta(minutes=30))
    )
    assert not result.ok
    assert RuleCode.REVISION_NARROWS in codes(result)


# --- the two-key rule -------------------------------------------------------


def test_severity_four_needs_a_human_approval_token(compiler: Compiler) -> None:
    result = compiler.compile(proposal(severity=4), approve())
    assert not result.ok
    assert codes(result) == (RuleCode.TWO_KEY_APPROVED,)
    assert result.needs_approval


def test_severity_four_publishes_once_approved() -> None:
    approvals = InMemoryApprovals()
    granted = approvals.grant("prop_test_1", "duty_officer_7", now=NOW)
    result = Compiler(clock=clock(), approvals=approvals).compile(proposal(severity=4), approve())
    assert result.ok, result.reasons
    assert granted.approved_by in result.checks[-3].detail


def test_an_area_over_the_two_key_limit_needs_approval(compiler: Compiler) -> None:
    """Severity 3, but 6 km2 is past the flood playbook's 4 km2 two-key limit."""
    result = compiler.compile(proposal(severity=3, area_km2=6.0), approve())
    assert not result.ok
    assert codes(result) == (RuleCode.TWO_KEY_APPROVED,)


def test_an_escalation_needs_approval(compiler: Compiler) -> None:
    result = compiler.compile(proposal(), escalate())
    assert not result.ok
    assert codes(result) == (RuleCode.TWO_KEY_APPROVED,)
    assert "escalated" in result.reasons[0]


def test_an_expired_approval_does_not_count() -> None:
    approvals = InMemoryApprovals()
    approvals.grant(
        "prop_test_1", "duty_officer_7", ttl=timedelta(minutes=5), now=NOW - timedelta(hours=1)
    )
    result = Compiler(clock=clock(), approvals=approvals).compile(proposal(severity=4), approve())
    assert not result.ok
    assert codes(result) == (RuleCode.TWO_KEY_APPROVED,)


def test_an_approval_for_another_proposal_does_not_count() -> None:
    approvals = InMemoryApprovals()
    approvals.grant("prop_something_else", "duty_officer_7", now=NOW)
    result = Compiler(clock=clock(), approvals=approvals).compile(proposal(severity=4), approve())
    assert not result.ok
    assert codes(result) == (RuleCode.TWO_KEY_APPROVED,)


def test_a_store_that_raises_rejects_rather_than_publishes() -> None:
    """Fail closed: a broken approval store must not become an open gate."""

    class Broken:
        def approval_for(self, proposal_id: str):
            raise RuntimeError("the approvals database is down")

    result = Compiler(clock=clock(), approvals=Broken()).compile(proposal(severity=4), approve())
    assert not result.ok
    assert codes(result) == (RuleCode.TWO_KEY_APPROVED,)


# --- critical access --------------------------------------------------------


def test_closing_every_hospital_access_edge_is_rejected() -> None:
    compiler = Compiler(clock=clock(), poi=poi_checker(hospital(inside=2)))
    result = compiler.compile(proposal(area_km2=1.0), approve())
    assert not result.ok
    assert RuleCode.CRITICAL_ACCESS_OPEN in codes(result)
    assert "Riverside General" in result.reasons[0]


def test_leaving_one_access_edge_open_is_allowed() -> None:
    compiler = Compiler(clock=clock(), poi=poi_checker(hospital(inside=1, outside=1)))
    result = compiler.compile(proposal(area_km2=1.0), approve())
    assert result.ok, result.reasons


def test_a_speed_cap_over_a_hospital_is_not_a_closure() -> None:
    """Slowing traffic to a hospital does not cut the hospital off."""
    compiler = Compiler(clock=clock(), poi=poi_checker(hospital(inside=2)))
    result = compiler.compile(proposal(action_type=ActionType.SPEED_CAP, area_km2=1.0), approve())
    assert result.ok, result.reasons


def test_a_school_is_not_critical_access() -> None:
    """Closing the street outside a school is sometimes the right advisory."""
    school = Poi(
        id="poi_school_1",
        kind=PoiKind.SCHOOL,
        name="Travis Elementary",
        access_points=(CENTRE,),
    )
    compiler = Compiler(clock=clock(), poi=poi_checker(school))
    assert compiler.compile(proposal(area_km2=1.0), approve()).ok


def test_a_poi_checker_that_raises_rejects_rather_than_publishes() -> None:
    class Broken:
        def blocked_access(self, geometry, action):
            raise RuntimeError("PostGIS is down")

        def describes_itself(self) -> str:
            return "broken"

    result = Compiler(clock=clock(), poi=Broken()).compile(proposal(), approve())
    assert not result.ok
    assert RuleCode.CRITICAL_ACCESS_OPEN in codes(result)


# --- several failures at once -----------------------------------------------


def test_a_rejection_names_every_rule_that_failed(compiler: Compiler) -> None:
    """One trip through the gate, not one rule per round trip."""
    bad = proposal(
        hazard_class=HazardClass.SCHOOL_ZONE,
        action_type=ActionType.NO_ENTRY,
        area_km2=40.0,
        lifetime_minutes=600,
    )
    result = compiler.compile(bad, approve())
    assert not result.ok
    assert RuleCode.ACTION_ALLOWED in codes(result)
    assert RuleCode.AREA_UNDER_CAP in codes(result)
    assert RuleCode.LIFETIME_UNDER_CAP in codes(result)
    assert not result.needs_approval


def test_the_s1_sample_proposal_needs_a_second_key() -> None:
    """The packet's own S1 proposal is severity 4, so it waits for a human."""
    result = Compiler(clock=lambda: PROPOSAL.valid_from).compile(PROPOSAL, VERDICT)
    assert not result.ok
    assert result.needs_approval
