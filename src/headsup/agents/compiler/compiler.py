"""The compiler: one proposal plus one verdict in, a signed advisory or a rejection out.

This is the only code allowed to turn a `ProposedAdvisory` into an `Advisory`,
and the `advisories` stream has exactly one writer (see `headsup.bus.acl`). It
is deliberately dull: no model call, no network, no clock of its own beyond an
injectable one, and every decision recorded as a `Check`.

    compiler = Compiler()
    result = compiler.compile(proposal, verdict)
    if result.ok:
        publish(result.advisory)          # signed, verifiable, expiring
    else:
        log(result.reasons)               # every rule that said no

`CompilerService` in `service.py` is what actually writes to the bus; this
module stays pure so the property tests can run thousands of proposals through
it without Redis.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from headsup.agents.compiler.approvals import Approval, ApprovalStore, NoApprovals
from headsup.agents.compiler.playbooks import PlaybookSet, default_playbooks
from headsup.agents.compiler.poi import BlockedAccess, NullPoiChecker, PoiChecker
from headsup.agents.compiler.rules import (
    Check,
    RuleCode,
    RuleContext,
    check_geometry,
    check_playbook,
    check_proposal_schema,
    check_verdict_matches,
    check_verdict_schema,
    evaluate,
    failed,
    passed,
    two_key_reason,
)
from headsup.agents.compiler.signing import Signer
from headsup.contracts import (
    ISSUER_PATTERN,
    Advisory,
    AdvisoryProperties,
    CriticVerdict,
    Geometry,
    ProposedAdvisory,
    UtcDatetime,
    utcnow,
)

#: `headsup/<city>`, the issuer stamped on every advisory this process signs.
ENV_ISSUER = "HU_ISSUER"

#: The city the issuer is derived from when `HU_ISSUER` is unset.
ENV_CITY = "HU_CITY"

#: S1, the flood scenario, is the demo default.
DEFAULT_CITY = "san_antonio"

#: Shown instead of a proposal id when the payload was too broken to have one.
UNKNOWN_PROPOSAL = "unknown"

_ISSUER_RE = re.compile(ISSUER_PATTERN)


class CompilerError(RuntimeError):
    """The compiler is misconfigured: a bad issuer, or no playbooks."""


def default_issuer() -> str:
    """`HU_ISSUER`, else `headsup/<HU_CITY>`, else `headsup/san_antonio`."""
    issuer = os.environ.get(ENV_ISSUER, "").strip()
    if not issuer:
        city = os.environ.get(ENV_CITY, "").strip() or DEFAULT_CITY
        issuer = f"headsup/{city}"
    if not _ISSUER_RE.fullmatch(issuer):
        raise CompilerError(
            f"{issuer!r} is not a usable issuer; expected headsup/<city> matching {ISSUER_PATTERN}"
        )
    return issuer


def advisory_id_for(proposal_id: str) -> str:
    """The advisory id one proposal always compiles to.

    Derived, not drawn at random, so a replay of the same scenario produces
    the same advisory ids and the audit log of two runs can be compared. A
    re-issue keeps the id and bumps `version`.
    """
    if proposal_id.startswith("prop_"):
        suffix = proposal_id[len("prop_") :]
    else:
        suffix = hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:12]
    return f"adv_{suffix}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Rejection:
    """A proposal that did not pass, with every rule that said no."""

    proposal_id: str
    checks: tuple[Check, ...]
    proposal: ProposedAdvisory | None = None
    context: RuleContext | None = None

    #: Never an advisory. `if result.ok:` is the only test a caller needs.
    ok: bool = field(default=False, init=False)

    @property
    def failures(self) -> tuple[Check, ...]:
        """The checks that failed, in pipeline order."""
        return tuple(check for check in self.checks if not check.ok)

    @property
    def codes(self) -> tuple[RuleCode, ...]:
        return tuple(check.code for check in self.failures)

    @property
    def reasons(self) -> tuple[str, ...]:
        """One readable line per failed rule."""
        return tuple(check.reason for check in self.failures)

    @property
    def needs_approval(self) -> bool:
        """True when a human approval token is the only thing missing.

        D3.2's `GET /approvals` pending list is exactly this set.
        """
        return self.codes == (RuleCode.TWO_KEY_APPROVED,)

    def __str__(self) -> str:
        return f"rejected {self.proposal_id}: " + "; ".join(self.reasons)


@dataclass(frozen=True, slots=True, kw_only=True)
class Compiled:
    """A proposal that passed every rule, as a signed advisory."""

    proposal_id: str
    checks: tuple[Check, ...]
    advisory: Advisory
    proposal: ProposedAdvisory | None = None
    context: RuleContext | None = None

    ok: bool = field(default=True, init=False)

    @property
    def reasons(self) -> tuple[str, ...]:
        return ()

    @property
    def advisory_id(self) -> str:
        return self.advisory.properties.id

    def __str__(self) -> str:
        properties = self.advisory.properties
        return (
            f"published {properties.id} v{properties.version} "
            f"({properties.action.type.value}, {properties.policy_rule})"
        )


#: What `Compiler.compile` returns. `result.ok` tells the two apart.
CompileResult = Compiled | Rejection


class Compiler:
    """The deterministic publishing gate.

    Everything that could make two runs disagree is injected: the playbooks,
    the signing key, the POI checker, the approval store and the clock.
    """

    def __init__(
        self,
        *,
        playbooks: PlaybookSet | None = None,
        signer: Signer | None = None,
        poi: PoiChecker | None = None,
        approvals: ApprovalStore | None = None,
        issuer: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.playbooks = playbooks if playbooks is not None else default_playbooks()
        self.signer = signer if signer is not None else Signer.from_env(allow_dev_key=True)
        self.poi: PoiChecker = poi if poi is not None else NullPoiChecker()
        self.approvals: ApprovalStore = approvals if approvals is not None else NoApprovals()
        self.issuer = issuer if issuer is not None else default_issuer()
        if not _ISSUER_RE.fullmatch(self.issuer):
            raise CompilerError(f"{self.issuer!r} is not a usable issuer")
        self._clock = clock or utcnow

    @property
    def now(self) -> datetime:
        return self._clock()

    def compile(
        self,
        proposal: ProposedAdvisory | Mapping[str, Any],
        verdict: CriticVerdict | Mapping[str, Any],
        *,
        version: int = 1,
    ) -> CompileResult:
        """Judge one proposal. Returns a signed advisory or a rejection.

        Raises nothing a caller has to catch: a malformed payload, an unknown
        hazard class or a POI checker that blows up all come back as a
        `Rejection`, because an exception on this path would mean a proposal
        quietly never answered for.
        """
        now = self.now
        checks: list[Check] = []

        # --- structural: is there something well formed to judge? -----------
        parsed, schema_check = check_proposal_schema(proposal)
        checks.append(schema_check)
        if parsed is None:
            return Rejection(proposal_id=_raw_id(proposal), checks=tuple(checks))

        parsed_verdict, verdict_check = check_verdict_schema(verdict)
        checks.append(verdict_check)
        if parsed_verdict is None:
            return Rejection(proposal_id=parsed.id, checks=tuple(checks), proposal=parsed)

        match_check = check_verdict_matches(parsed, parsed_verdict)
        checks.append(match_check)
        if not match_check.ok:
            return Rejection(proposal_id=parsed.id, checks=tuple(checks), proposal=parsed)

        geometry, valid_from, valid_until = _effective(parsed, parsed_verdict)
        playbook = self.playbooks.get(parsed.hazard.hazard_class)
        checks.append(check_geometry(geometry))
        checks.append(check_playbook(playbook, parsed))
        if playbook is None or not all(check.ok for check in checks):
            return Rejection(proposal_id=parsed.id, checks=tuple(checks), proposal=parsed)

        # --- policy: is it inside its playbook? -----------------------------
        blocked, poi_check = self._ask_poi(geometry, parsed)
        if poi_check is not None:
            checks.append(poi_check)
            return Rejection(proposal_id=parsed.id, checks=tuple(checks), proposal=parsed)

        context = RuleContext(
            proposal=parsed,
            verdict=parsed_verdict,
            playbook=playbook,
            geometry=geometry,
            valid_from=valid_from,
            valid_until=valid_until,
            now=now,
            poi=self.poi,
            approval=self._approval_for(parsed.id),
            blocked=blocked,
        )
        checks.extend(evaluate(context))
        if not all(check.ok for check in checks):
            return Rejection(
                proposal_id=parsed.id, checks=tuple(checks), proposal=parsed, context=context
            )

        # --- sign -----------------------------------------------------------
        advisory, build_check = self._build(context, version=version)
        checks.append(build_check)
        if advisory is None:
            return Rejection(
                proposal_id=parsed.id, checks=tuple(checks), proposal=parsed, context=context
            )
        return Compiled(
            proposal_id=parsed.id,
            checks=tuple(checks),
            advisory=advisory,
            proposal=parsed,
            context=context,
        )

    # --- helpers ------------------------------------------------------------

    def _approval_for(self, proposal_id: str) -> Approval | None:
        """The approval on record, or None if the store cannot answer.

        A store that raises is treated as having no approval: the two-key rule
        then rejects, which is the safe direction.
        """
        try:
            return self.approvals.approval_for(proposal_id)
        except Exception:
            return None

    def _ask_poi(
        self, geometry: Geometry, proposal: ProposedAdvisory
    ) -> tuple[tuple[BlockedAccess, ...], Check | None]:
        """Ask the POI checker, failing closed if it cannot answer."""
        try:
            return tuple(self.poi.blocked_access(geometry, proposal.action.type)), None
        except Exception as exc:
            return (), failed(
                RuleCode.CRITICAL_ACCESS_OPEN,
                f"{type(self.poi).__name__} could not answer, so access is assumed blocked: {exc}",
            )

    def _build(self, context: RuleContext, *, version: int) -> tuple[Advisory | None, Check]:
        """Assemble the advisory and sign it.

        The contract validates once more here, on the artifact itself, because
        this is the object that leaves the building.
        """
        proposal = context.proposal
        try:
            advisory = Advisory(
                geometry=context.geometry,
                properties=AdvisoryProperties(
                    id=advisory_id_for(proposal.id),
                    version=version,
                    hazard=proposal.hazard,
                    action=proposal.action,
                    valid_from=context.valid_from,
                    valid_until=context.valid_until,
                    evidence=proposal.evidence,
                    policy_rule=context.playbook.rule_for(context.action),
                    issuer=self.issuer,
                ),
            )
            signed = self.signer.sign(advisory)
        except (ValidationError, ValueError) as exc:
            return None, failed(RuleCode.ADVISORY_VALID, f"could not build the advisory: {exc}")
        return signed, passed(
            RuleCode.ADVISORY_VALID,
            f"{signed.properties.id} signed with {self.signer.fingerprint}",
        )

    def describe(self) -> str:
        """One line for a startup log: which caps, which key, which checkers."""
        return (
            f"compiler issuer={self.issuer} playbooks={len(self.playbooks)} "
            f"key={self.signer.fingerprint}({self.signer.label}) "
            f"poi={self.poi.describes_itself()}"
        )


def _effective(
    proposal: ProposedAdvisory, verdict: CriticVerdict
) -> tuple[Geometry, UtcDatetime, UtcDatetime]:
    """What would actually publish, with any critic revision applied."""
    geometry = verdict.revised_geometry or proposal.geometry
    valid_until = verdict.revised_valid_until or proposal.valid_until
    return geometry, proposal.valid_from, valid_until


def _raw_id(payload: ProposedAdvisory | Mapping[str, Any]) -> str:
    """A proposal id to file an audit record under, even for a broken payload."""
    if isinstance(payload, ProposedAdvisory):
        return payload.id
    if isinstance(payload, Mapping):
        candidate = payload.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate[:128]
    return UNKNOWN_PROPOSAL


def two_key_needed(context: RuleContext) -> str | None:
    """Re-exported for the API's pending-approvals list in D3.2."""
    return two_key_reason(context)
