"""Deterministic publishing gate: the only writer of the advisories stream.

One proposal plus one critic verdict go in; a signed, expiring `Advisory` or a
`Rejection` naming every rule that said no comes out. Nothing here calls a
model or the network, so the same inputs always produce the same decision.

    from headsup.agents.compiler import Compiler, verify

    result = Compiler().compile(proposal, verdict)
    if result.ok:
        assert verify(result.advisory)

The rules, in order, and where each one lives:

| rule | module |
| --- | --- |
| schema valid, verdict valid and about this proposal | `rules` |
| geometry valid | `rules`, measured in `geo` |
| a playbook exists for the hazard class | `playbooks` |
| the action is allowed for the class | `playbooks` |
| area under the class cap, lifetime under the class cap | `playbooks`, `geo` |
| the window is ordered and not already over | `rules` |
| a critic REVISE narrows, never widens | `rules` |
| severity 4+ or over the two-key area needs a human token | `approvals` |
| hospital, fire and EMS access is never fully closed | `poi` |
| the advisory validates and signs | `signing` |

`service.CompilerService` writes the result to the bus: the advisory to
`advisories`, and a `CompileAudit` to `audit` either way.
"""

from __future__ import annotations

from headsup.agents.compiler.approvals import (
    DEFAULT_TTL,
    Approval,
    ApprovalStore,
    InMemoryApprovals,
    NoApprovals,
    new_token,
)
from headsup.agents.compiler.audit import AuditCheck, CompileAudit, Decision
from headsup.agents.compiler.compiler import (
    Compiled,
    Compiler,
    CompilerError,
    CompileResult,
    Rejection,
    advisory_id_for,
    default_issuer,
)
from headsup.agents.compiler.geo import area_km2, covers, sound
from headsup.agents.compiler.playbooks import (
    DEFAULT_PLAYBOOK_DIR,
    Playbook,
    PlaybookError,
    PlaybookSet,
    default_playbooks,
)
from headsup.agents.compiler.poi import (
    BLOCKING_ACTIONS,
    CRITICAL_KINDS,
    BlockedAccess,
    NullPoiChecker,
    Poi,
    PoiChecker,
    PoiKind,
    StubPoiChecker,
)
from headsup.agents.compiler.rules import (
    POLICY_RULES,
    Check,
    RuleCode,
    RuleContext,
    evaluate,
    two_key_reason,
)
from headsup.agents.compiler.service import CompilerService, PublishOutcome
from headsup.agents.compiler.signing import (
    DEV_SEED,
    ENV_SIGNING_KEY,
    ENV_VERIFY_KEY,
    Signer,
    SigningError,
    fingerprint,
    verify,
)

__all__ = [
    "BLOCKING_ACTIONS",
    "CRITICAL_KINDS",
    "DEFAULT_PLAYBOOK_DIR",
    "DEFAULT_TTL",
    "DEV_SEED",
    "ENV_SIGNING_KEY",
    "ENV_VERIFY_KEY",
    "POLICY_RULES",
    "Approval",
    "ApprovalStore",
    "AuditCheck",
    "BlockedAccess",
    "Check",
    "CompileAudit",
    "CompileResult",
    "Compiled",
    "Compiler",
    "CompilerError",
    "CompilerService",
    "Decision",
    "InMemoryApprovals",
    "NoApprovals",
    "NullPoiChecker",
    "Playbook",
    "PlaybookError",
    "PlaybookSet",
    "Poi",
    "PoiChecker",
    "PoiKind",
    "PublishOutcome",
    "Rejection",
    "RuleCode",
    "RuleContext",
    "Signer",
    "SigningError",
    "StubPoiChecker",
    "advisory_id_for",
    "area_km2",
    "covers",
    "default_issuer",
    "default_playbooks",
    "evaluate",
    "fingerprint",
    "new_token",
    "sound",
    "two_key_reason",
    "verify",
]
