"""The audit record: one per compile, published or not, with the whole checklist.

D3.4 hash-chains whatever is on the `audit` stream, so anything the chain
should cover has to be in the record at write time. These tests pin that down.
"""

from __future__ import annotations

from datetime import timedelta

from fixtures.compiler import NOW, approve, clock, hospital, poi_checker, proposal
from headsup.agents.compiler import (
    CompileAudit,
    Compiler,
    Decision,
    InMemoryApprovals,
    RuleCode,
    Signer,
)
from headsup.contracts import ActionType, HazardClass


def record(result, **kwargs) -> CompileAudit:
    signer = Signer.dev()
    return CompileAudit.from_result(
        result, key_fingerprint=signer.fingerprint, key_label=signer.label, **kwargs
    )


def approved_compiler(proposal_id: str = "prop_test_1") -> Compiler:
    approvals = InMemoryApprovals()
    approvals.grant(proposal_id, "duty_officer_7", now=NOW)
    return Compiler(clock=clock(), approvals=approvals)


# --- a published advisory ---------------------------------------------------


def test_a_published_advisory_is_recorded_with_its_measurements() -> None:
    result = Compiler(clock=clock()).compile(proposal(area_km2=1.0), approve())
    audit = record(result)
    assert audit.decision is Decision.PUBLISHED
    assert audit.published
    assert audit.proposal_id == "prop_test_1"
    assert audit.advisory_id == "adv_test_1"
    assert audit.advisory_version == 1
    assert audit.hazard_class is HazardClass.FLOOD
    assert audit.action is ActionType.NO_ENTRY
    assert audit.policy_rule == "flood.no_entry.v1"
    assert audit.signature == result.advisory.properties.signature
    assert audit.reasons == []


def test_the_record_carries_the_cap_the_measurement_was_judged_against() -> None:
    """An area alone means nothing a year later; the cap it was under does."""
    result = Compiler(clock=clock()).compile(proposal(area_km2=1.0), approve())
    audit = record(result)
    assert audit.area_cap_km2 == 12.0
    assert audit.lifetime_cap_minutes == 180.0
    assert 0 < audit.area_km2 <= audit.area_cap_km2
    assert 0 < audit.lifetime_minutes <= audit.lifetime_cap_minutes


def test_the_record_names_the_key_that_signed() -> None:
    result = Compiler(clock=clock(), signer=Signer.dev()).compile(proposal(), approve())
    audit = record(result)
    assert audit.key_fingerprint == Signer.dev().fingerprint
    assert audit.key_label == "dev"


def test_the_record_names_who_answered_the_access_question() -> None:
    """A run with no POI data says so, so an unguarded rule is visible."""
    audit = record(Compiler(clock=clock()).compile(proposal(), approve()))
    assert "null-poi-checker" in audit.poi_checker

    guarded = Compiler(clock=clock(), poi=poi_checker(hospital(inside=1, outside=1)))
    audit = record(guarded.compile(proposal(area_km2=1.0), approve()))
    assert "stub-poi-checker" in audit.poi_checker


def test_every_check_is_recorded_in_order() -> None:
    result = Compiler(clock=clock()).compile(proposal(), approve())
    audit = record(result)
    assert [check.code for check in audit.checks] == [check.code.value for check in result.checks]
    assert all(check.ok for check in audit.checks)


# --- the second key ---------------------------------------------------------


def test_an_approval_is_recorded_without_replaying_the_token() -> None:
    """The log is read by people not entitled to re-use the approval."""
    approvals = InMemoryApprovals()
    granted = approvals.grant("prop_test_1", "duty_officer_7", now=NOW)
    result = Compiler(clock=clock(), approvals=approvals).compile(proposal(severity=4), approve())
    audit = record(result)
    assert audit.two_key_required
    assert "severity 4" in audit.two_key_reason
    assert audit.approved_by == "duty_officer_7"
    assert audit.approval_token == granted.fingerprint
    assert granted.token not in (audit.approval_token or "")
    assert granted.token not in audit.model_dump_json()


def test_an_advisory_that_needed_no_second_key_says_so() -> None:
    audit = record(Compiler(clock=clock()).compile(proposal(severity=2), approve()))
    assert not audit.two_key_required
    assert audit.two_key_reason is None
    assert audit.approved_by is None


# --- a rejection ------------------------------------------------------------


def test_a_rejection_is_recorded_with_its_reasons() -> None:
    result = Compiler(clock=clock()).compile(proposal(area_km2=40.0), approve())
    audit = record(result)
    assert audit.decision is Decision.REJECTED
    assert not audit.published
    assert audit.advisory_id is None
    assert audit.signature is None
    assert any("area_under_cap" in reason for reason in audit.reasons)
    assert any(check.code == RuleCode.AREA_UNDER_CAP.value for check in audit.checks)


def test_a_rejection_still_records_what_was_asked_for() -> None:
    """The ablation table counts refused advisories, so the ask must survive."""
    result = Compiler(clock=clock()).compile(
        proposal(hazard_class=HazardClass.SCHOOL_ZONE, action_type=ActionType.NO_ENTRY), approve()
    )
    audit = record(result)
    assert audit.hazard_class is HazardClass.SCHOOL_ZONE
    assert audit.action is ActionType.NO_ENTRY
    assert audit.area_km2 is not None


def test_a_payload_too_broken_to_parse_still_produces_a_record() -> None:
    """Nothing goes unanswered, even a proposal with no usable fields."""
    result = Compiler(clock=clock()).compile({"id": "prop_junk"}, approve())
    audit = record(result)
    assert audit.decision is Decision.REJECTED
    assert audit.proposal_id == "prop_junk"
    assert audit.hazard_class is None
    assert audit.reasons


def test_a_failed_write_is_not_recorded_as_published() -> None:
    """The log never claims an advisory a reader cannot find."""
    result = approved_compiler().compile(proposal(severity=4), approve())
    audit = record(result, decision=Decision.PUBLISH_FAILED)
    assert audit.decision is Decision.PUBLISH_FAILED
    assert not audit.published


# --- the wire form ----------------------------------------------------------


def test_the_record_round_trips_through_json() -> None:
    result = Compiler(clock=clock()).compile(proposal(), approve())
    audit = record(result, stream_id="1792529063577-0")
    restored = CompileAudit.model_validate_json(audit.to_json())
    assert restored == audit
    assert restored.stream_id == "1792529063577-0"


def test_long_rule_detail_is_capped() -> None:
    """Feed and model text is data: the record caps it rather than trusting it."""
    result = Compiler(clock=clock()).compile(proposal(), approve())
    audit = record(result)
    assert all(len(check.detail) <= 1000 for check in audit.checks)


def test_the_summary_is_one_line() -> None:
    published = record(Compiler(clock=clock()).compile(proposal(), approve())).summary()
    assert published.startswith("PUBLISHED prop_test_1 -> adv_test_1")

    rejected = record(
        Compiler(clock=clock()).compile(
            proposal(valid_from=NOW - timedelta(days=2), lifetime_minutes=30), approve()
        )
    ).summary()
    assert rejected.startswith("REJECTED prop_test_1")
    assert "window_usable" in rejected
