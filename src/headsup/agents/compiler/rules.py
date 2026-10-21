"""The rules every advisory passes before it is signed, one function each.

Each rule is a pure function from a `RuleContext` to a `Check`. Nothing here
reads the environment, the clock or the bus, so the gate is reproducible: the
same proposal, verdict, playbook and approval always produce the same checks,
in the same order, with the same text.

The rules divide in two:

* **structural** - is there something well formed to judge at all (schema,
  verdict, geometry, playbook)? A failure here stops the pipeline, because the
  later rules would be measuring nothing;
* **policy** - is this advisory inside its playbook, approved if it needs to
  be, and does it leave the emergency services a way through? These all run,
  so a rejection names every reason at once instead of one at a time.

Codes are named for what must hold (`area_under_cap`), so an audit record
reads as a checklist: `area_under_cap: ok`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from headsup.agents.compiler.approvals import Approval
from headsup.agents.compiler.geo import area_km2, covers, sound
from headsup.agents.compiler.playbooks import Playbook
from headsup.agents.compiler.poi import BLOCKING_ACTIONS, BlockedAccess, PoiChecker
from headsup.contracts import (
    ActionType,
    CriticVerdict,
    Geometry,
    ProposedAdvisory,
    UtcDatetime,
    Verdict,
)


class RuleCode(StrEnum):
    """Every check the compiler records, in pipeline order."""

    # structural
    SCHEMA_VALID = "schema_valid"
    VERDICT_VALID = "verdict_valid"
    VERDICT_MATCHES_PROPOSAL = "verdict_matches_proposal"
    GEOMETRY_VALID = "geometry_valid"
    PLAYBOOK_KNOWN = "playbook_known"
    # policy
    ACTION_ALLOWED = "action_allowed"
    AREA_UNDER_CAP = "area_under_cap"
    LIFETIME_UNDER_CAP = "lifetime_under_cap"
    WINDOW_USABLE = "window_usable"
    REVISION_NARROWS = "revision_narrows"
    TWO_KEY_APPROVED = "two_key_approved"
    CRITICAL_ACCESS_OPEN = "critical_access_open"
    # the artifact itself
    ADVISORY_VALID = "advisory_valid"


#: Failing one of these means the rest cannot be measured, so the pipeline stops.
STRUCTURAL_CODES = frozenset(
    {
        RuleCode.SCHEMA_VALID,
        RuleCode.VERDICT_VALID,
        RuleCode.VERDICT_MATCHES_PROPOSAL,
        RuleCode.GEOMETRY_VALID,
        RuleCode.PLAYBOOK_KNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class Check:
    """One rule's verdict on one proposal."""

    code: RuleCode
    ok: bool
    detail: str = ""

    def __str__(self) -> str:
        state = "ok" if self.ok else "FAILED"
        return f"{self.code.value}: {state}{f' - {self.detail}' if self.detail else ''}"

    @property
    def reason(self) -> str:
        """The rejection reason this check contributes, if it failed."""
        return f"{self.code.value}: {self.detail}" if self.detail else self.code.value


def passed(code: RuleCode, detail: str = "") -> Check:
    return Check(code=code, ok=True, detail=detail)


def failed(code: RuleCode, detail: str) -> Check:
    return Check(code=code, ok=False, detail=detail)


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Everything the policy rules are allowed to look at.

    The geometry and window here are the *effective* ones: a critic REVISE has
    already been applied, so the rules measure what would actually publish.
    """

    proposal: ProposedAdvisory
    verdict: CriticVerdict
    playbook: Playbook
    geometry: Geometry
    valid_from: UtcDatetime
    valid_until: UtcDatetime
    now: datetime
    poi: PoiChecker
    approval: Approval | None = None
    blocked: tuple[BlockedAccess, ...] = field(default_factory=tuple)

    @property
    def action(self) -> ActionType:
        return self.proposal.action.type

    @property
    def severity(self) -> int:
        return self.proposal.hazard.severity

    @property
    def area_km2(self) -> float:
        return area_km2(self.geometry)

    @property
    def lifetime_minutes(self) -> float:
        return (self.valid_until - self.valid_from).total_seconds() / 60.0

    @property
    def revised(self) -> bool:
        return self.verdict.verdict is Verdict.REVISE and self.verdict.revised


# --- structural rules -------------------------------------------------------


def check_proposal_schema(
    raw: ProposedAdvisory | Mapping[str, Any],
) -> tuple[ProposedAdvisory | None, Check]:
    """Validate whatever was handed in against the ProposedAdvisory contract.

    The compiler accepts a raw mapping as well as a model so that this rule is
    real: a proposal that arrived as JSON from an agent, a file or the API is
    validated here, by the contract, before any other rule sees it.
    """
    if isinstance(raw, ProposedAdvisory):
        return raw, passed(RuleCode.SCHEMA_VALID, "ProposedAdvisory")
    if not isinstance(raw, Mapping):
        return None, failed(
            RuleCode.SCHEMA_VALID,
            f"expected a ProposedAdvisory or a mapping, got {type(raw).__name__}",
        )
    try:
        return ProposedAdvisory.model_validate(dict(raw)), passed(
            RuleCode.SCHEMA_VALID, "validated from a mapping"
        )
    except ValidationError as exc:
        return None, failed(RuleCode.SCHEMA_VALID, _format_errors(exc))


def check_verdict_schema(
    raw: CriticVerdict | Mapping[str, Any],
) -> tuple[CriticVerdict | None, Check]:
    """Validate the critic's verdict the same way."""
    if isinstance(raw, CriticVerdict):
        return raw, passed(RuleCode.VERDICT_VALID, raw.verdict.value)
    if not isinstance(raw, Mapping):
        return None, failed(
            RuleCode.VERDICT_VALID,
            f"expected a CriticVerdict or a mapping, got {type(raw).__name__}",
        )
    try:
        verdict = CriticVerdict.model_validate(dict(raw))
    except ValidationError as exc:
        return None, failed(RuleCode.VERDICT_VALID, _format_errors(exc))
    return verdict, passed(RuleCode.VERDICT_VALID, verdict.verdict.value)


def check_verdict_matches(proposal: ProposedAdvisory, verdict: CriticVerdict) -> Check:
    """The verdict in front of us must be about this proposal.

    Without this, a stale APPROVE could carry a different proposal past the
    critic entirely.
    """
    if verdict.proposal_id != proposal.id:
        return failed(
            RuleCode.VERDICT_MATCHES_PROPOSAL,
            f"verdict is for {verdict.proposal_id!r}, proposal is {proposal.id!r}",
        )
    return passed(RuleCode.VERDICT_MATCHES_PROPOSAL, proposal.id)


def check_geometry(geometry: Geometry) -> Check:
    """Re-validate the geometry about to be signed.

    The contracts validate on construction; this is the compiler not trusting
    that the object in front of it was built that way, and the only place a
    critic's revised geometry is checked at all.
    """
    problem = sound(geometry)
    if problem:
        return failed(RuleCode.GEOMETRY_VALID, problem)
    return passed(RuleCode.GEOMETRY_VALID, geometry.type)


def check_playbook(playbook: Playbook | None, proposal: ProposedAdvisory) -> Check:
    """There must be a playbook for this hazard class.

    No playbook means no caps, and no caps means nothing publishes.
    """
    hazard_class = proposal.hazard.hazard_class
    if playbook is None:
        return failed(
            RuleCode.PLAYBOOK_KNOWN,
            f"no playbook for {hazard_class.value}; nothing may be published for this class",
        )
    return passed(RuleCode.PLAYBOOK_KNOWN, playbook.policy_rule)


# --- policy rules -----------------------------------------------------------


def check_action_allowed(context: RuleContext) -> Check:
    """The action must be one this hazard class is allowed to ask for."""
    action = context.action
    if not context.playbook.allows(action):
        return failed(
            RuleCode.ACTION_ALLOWED,
            f"{action.value} is not allowed for "
            f"{context.playbook.hazard_class.value}; allowed: "
            f"{', '.join(context.playbook.action_names)}",
        )
    return passed(RuleCode.ACTION_ALLOWED, action.value)


def check_area(context: RuleContext) -> Check:
    """The footprint must be under the class's area cap."""
    area = context.area_km2
    cap = context.playbook.max_area_km2
    if area > cap:
        return failed(
            RuleCode.AREA_UNDER_CAP,
            f"{area:.4f} km2 is over the {cap:g} km2 cap for {context.playbook.hazard_class.value}",
        )
    return passed(RuleCode.AREA_UNDER_CAP, f"{area:.4f} km2 of {cap:g} km2")


def check_lifetime(context: RuleContext) -> Check:
    """The validity window must be no longer than the class's lifetime cap."""
    lifetime = context.lifetime_minutes
    cap = context.playbook.max_lifetime_minutes
    if lifetime > cap:
        return failed(
            RuleCode.LIFETIME_UNDER_CAP,
            f"{lifetime:.1f} min is over the {cap:g} min cap for "
            f"{context.playbook.hazard_class.value}",
        )
    return passed(RuleCode.LIFETIME_UNDER_CAP, f"{lifetime:.1f} min of {cap:g} min")


def check_window(context: RuleContext) -> Check:
    """The window must be ordered and must not already be over.

    An advisory that expired before it was signed is noise on the bus and a
    fleet that acts on it is acting on nothing.
    """
    if context.valid_until <= context.valid_from:
        return failed(
            RuleCode.WINDOW_USABLE,
            f"valid_until {context.valid_until.isoformat()} is not after "
            f"valid_from {context.valid_from.isoformat()}",
        )
    if context.valid_until <= context.now:
        return failed(
            RuleCode.WINDOW_USABLE,
            f"valid_until {context.valid_until.isoformat()} has already passed "
            f"at {context.now.isoformat()}",
        )
    return passed(
        RuleCode.WINDOW_USABLE,
        f"{context.valid_from.isoformat()} .. {context.valid_until.isoformat()}",
    )


def check_revision(context: RuleContext) -> Check:
    """A critic REVISE may narrow an advisory, never grow it.

    REVISE exists so the critic can cut an over-broad polygon or a too-long
    window down. Letting it widen either one would turn the critic into a
    second proposer with no review of its own.
    """
    if not context.revised:
        return passed(RuleCode.REVISION_NARROWS, "no revision")
    problems: list[str] = []
    revised_geometry = context.verdict.revised_geometry
    if revised_geometry is not None and not covers(context.proposal.geometry, revised_geometry):
        problems.append("revised geometry reaches outside the proposed geometry")
    revised_until = context.verdict.revised_valid_until
    if revised_until is not None and revised_until > context.proposal.valid_until:
        problems.append(
            f"revised valid_until {revised_until.isoformat()} is later than the proposed "
            f"{context.proposal.valid_until.isoformat()}"
        )
    if problems:
        return failed(RuleCode.REVISION_NARROWS, "; ".join(problems))
    return passed(RuleCode.REVISION_NARROWS, "revision narrows the proposal")


def two_key_reason(context: RuleContext) -> str | None:
    """Why this advisory needs a human approval token, or None if it does not."""
    playbook = context.playbook
    if context.verdict.verdict is Verdict.ESCALATE:
        return "the critic escalated"
    if context.severity >= playbook.two_key_severity:
        return f"severity {context.severity} is at or above {playbook.two_key_severity}"
    if context.area_km2 > playbook.two_key_area_km2:
        return (
            f"{context.area_km2:.4f} km2 is over the "
            f"{playbook.two_key_area_km2:g} km2 two-key limit"
        )
    return None


def check_two_key(context: RuleContext) -> Check:
    """Severe or wide advisories need a second key: a human approval token."""
    why = two_key_reason(context)
    if why is None:
        return passed(RuleCode.TWO_KEY_APPROVED, "no second key needed")
    approval = context.approval
    if approval is None:
        return failed(RuleCode.TWO_KEY_APPROVED, f"{why}, so it needs a human approval token")
    if approval.proposal_id != context.proposal.id:
        return failed(
            RuleCode.TWO_KEY_APPROVED,
            f"approval is for {approval.proposal_id!r}, proposal is {context.proposal.id!r}",
        )
    if not approval.usable_at(context.now):
        return failed(
            RuleCode.TWO_KEY_APPROVED,
            f"approval from {approval.approved_by} is not usable at "
            f"{context.now.isoformat()} (approved {approval.approved_at.isoformat()}, "
            f"expires {approval.expires_at.isoformat() if approval.expires_at else 'never'})",
        )
    return passed(
        RuleCode.TWO_KEY_APPROVED,
        f"{why}; approved by {approval.approved_by} with token {approval.fingerprint}",
    )


def check_critical_access(context: RuleContext) -> Check:
    """No advisory may close every way into a hospital, fire or EMS station."""
    if context.action not in BLOCKING_ACTIONS:
        return passed(
            RuleCode.CRITICAL_ACCESS_OPEN,
            f"{context.action.value} does not block access",
        )
    if context.blocked:
        return failed(
            RuleCode.CRITICAL_ACCESS_OPEN,
            "; ".join(str(item) for item in context.blocked),
        )
    return passed(RuleCode.CRITICAL_ACCESS_OPEN, context.poi.describes_itself())


#: The policy rules, in the order the audit record lists them.
POLICY_RULES = (
    check_action_allowed,
    check_area,
    check_lifetime,
    check_window,
    check_revision,
    check_two_key,
    check_critical_access,
)


def evaluate(context: RuleContext) -> tuple[Check, ...]:
    """Run every policy rule and return all the checks, failures included."""
    return tuple(rule(context) for rule in POLICY_RULES)


def blocked_access(
    context_poi: PoiChecker, geometry: Geometry, action: ActionType
) -> tuple[BlockedAccess, ...]:
    """Ask the POI checker which critical POIs this action would cut off."""
    found: Sequence[BlockedAccess] = context_poi.blocked_access(geometry, action)
    return tuple(found)


def _format_errors(exc: ValidationError, limit: int = 4) -> str:
    """A validation error as one line, short enough for an audit record."""
    parts = []
    for error in exc.errors()[:limit]:
        where = ".".join(str(item) for item in error["loc"]) or "<root>"
        parts.append(f"{where}: {error['msg']}")
    more = exc.error_count() - min(exc.error_count(), limit)
    if more:
        parts.append(f"and {more} more")
    return "; ".join(parts)
