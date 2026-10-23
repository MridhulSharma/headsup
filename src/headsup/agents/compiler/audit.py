"""The audit record every compile writes, published or rejected.

One `CompileAudit` per call to `Compiler.compile`, on the `audit` stream. It is
the record that answers, months later, *why* a given advisory existed or did
not: which playbook and rule id applied, how large the area was against the
cap, who supplied the second key, which key signed it, and the outcome of
every single rule in order.

D3.4 hash-chains these into `data/audit/<run_id>.jsonl`; the chain covers
whatever is on the stream, so everything that matters belongs in this record
and nothing here is derived later.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from headsup.agents.compiler.compiler import CompileResult
from headsup.agents.compiler.rules import two_key_reason
from headsup.contracts import (
    ActionType,
    GeometryNarrowing,
    HazardClass,
    HeadsUpModel,
    Identifier,
    Severity,
    UtcDatetime,
    Verdict,
    new_id,
    utcnow,
)

#: Rule detail and rejection text: feed and model text is data, so it is
#: length-capped here rather than trusted.
AuditText = Annotated[str, StringConstraints(max_length=1000)]


class Decision(StrEnum):
    """What became of one proposal."""

    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    #: Passed every rule, then the write to `advisories` failed.
    PUBLISH_FAILED = "PUBLISH_FAILED"


class AuditCheck(HeadsUpModel):
    """One rule's outcome, as the audit log carries it."""

    code: str
    ok: bool
    detail: AuditText = ""


class CompileAudit(HeadsUpModel):
    """Everything the compiler decided about one proposal."""

    id: Identifier = Field(default_factory=lambda: new_id("aud"))
    proposal_id: Identifier
    decision: Decision
    checks: list[AuditCheck] = Field(default_factory=list)
    reasons: list[AuditText] = Field(default_factory=list)

    # what was asked for
    hazard_class: HazardClass | None = None
    severity: Severity | None = None
    action: ActionType | None = None
    verdict: Verdict | None = None

    # what the playbook said
    policy_rule: Identifier | None = None
    area_km2: float | None = None
    area_cap_km2: float | None = None
    lifetime_minutes: float | None = None
    lifetime_cap_minutes: float | None = None

    # the second key
    # what fusion did to the geometry before any of this: both the area the
    # feeds published and the area that was judged, with the reason they
    # differ. Carried here so the audit stream alone answers "what was cut,
    # and why" without a reader having to join it to the hazards stream.
    hazard_narrowing: GeometryNarrowing | None = None

    two_key_required: bool = False
    two_key_reason: AuditText | None = None
    approved_by: Identifier | None = None
    approval_token: AuditText | None = None

    # what was published
    advisory_id: Identifier | None = None
    advisory_version: int | None = None
    signature: AuditText | None = None
    key_fingerprint: AuditText | None = None
    key_label: AuditText | None = None
    stream_id: AuditText | None = None

    # who answered the critical-access question
    poi_checker: AuditText | None = None

    compiled_by: Identifier = "compiler"
    compiled_at: UtcDatetime = Field(default_factory=utcnow)

    @property
    def published(self) -> bool:
        return self.decision is Decision.PUBLISHED

    @classmethod
    def from_result(
        cls,
        result: CompileResult,
        *,
        decision: Decision | None = None,
        key_fingerprint: str | None = None,
        key_label: str | None = None,
        stream_id: str | None = None,
    ) -> CompileAudit:
        """Build the record for one compile result.

        `decision` defaults to PUBLISHED or REJECTED; a caller whose write to
        `advisories` failed passes PUBLISH_FAILED so the log does not claim an
        advisory that nobody can read.
        """
        context = result.context
        proposal = result.proposal
        record = cls(
            proposal_id=result.proposal_id,
            decision=decision or (Decision.PUBLISHED if result.ok else Decision.REJECTED),
            checks=[
                AuditCheck(code=check.code.value, ok=check.ok, detail=check.detail[:1000])
                for check in result.checks
            ],
            reasons=[reason[:1000] for reason in result.reasons],
            key_fingerprint=key_fingerprint,
            key_label=key_label,
            stream_id=stream_id,
        )
        if proposal is not None:
            record.hazard_class = proposal.hazard.hazard_class
            record.severity = proposal.hazard.severity
            record.action = proposal.action.type
            record.hazard_narrowing = proposal.hazard.narrowing
        if context is not None:
            record.verdict = context.verdict.verdict
            record.policy_rule = context.playbook.rule_for(context.action)
            record.area_km2 = round(context.area_km2, 6)
            record.area_cap_km2 = context.playbook.max_area_km2
            record.lifetime_minutes = round(context.lifetime_minutes, 3)
            record.lifetime_cap_minutes = context.playbook.max_lifetime_minutes
            record.poi_checker = context.poi.describes_itself()
            why = two_key_reason(context)
            record.two_key_required = why is not None
            record.two_key_reason = why
            if context.approval is not None:
                record.approved_by = context.approval.approved_by
                record.approval_token = context.approval.fingerprint
        if result.ok:
            properties = result.advisory.properties
            record.advisory_id = properties.id
            record.advisory_version = properties.version
            record.policy_rule = properties.policy_rule
            record.signature = properties.signature
        return record

    def summary(self) -> str:
        """One line for a console or a log file."""
        head = f"{self.decision.value} {self.proposal_id}"
        if self.advisory_id:
            head += f" -> {self.advisory_id}"
        if self.reasons:
            head += ": " + "; ".join(self.reasons)
        return head
