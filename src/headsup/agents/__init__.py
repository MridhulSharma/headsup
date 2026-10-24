"""Event-driven agent workers: sentinels, fusion, the proposer, the compiler.

The pipeline, in the order a signal travels it:

| stage | module | in | out |
| --- | --- | --- | --- |
| sentinels | `agents.sentinels` | `events.*` | `candidates` |
| fusion | `agents.fusion` | `candidates` | `hazards` |
| proposer | `agents.proposer` | `hazards` | `proposals` |
| critic | `agents.critic` | a proposal + its evidence | a verdict |
| compiler | `agents.compiler` | proposal + verdict | `advisories` |

The critic is not a bus worker: `ProposerWorker` reviews each proposal it
publishes before handing the pair to the compiler, so one judgment call is
spent per proposal and none on a raw event. Neither is `agents.triage`, which
sits on the other side of the fleet: it reads the `assist` stream the vehicles
write to and collapses a surge of requests into one decision card per
scenario, for a person to answer over HTTP.

`agents.pipeline` holds the two bus workers for the middle stages, and
`hu agents --only fusion,proposer` is how they are started. Fusion and the
proposer each ask a `judgment` model one question and each have a complete
rules path behind it, so variant V1 of the ablation runs on nothing else.
Only `agents.compiler` may write to the `advisories` stream.
"""

from __future__ import annotations

from headsup.agents.critic import (
    CRITIC_NAME,
    CRITIC_RULES,
    MIN_FRACTION,
    Critic,
    CriticAnswer,
    Finding,
    narrow_geometry,
    review_rules,
)
from headsup.agents.fusion import (
    CAPPED_SEVERITY,
    EVIDENCE_SEVERITY,
    FUSION_RULES,
    MAX_CONFIDENCE,
    OVERLAP_PAD_M,
    STALE_FLOOR,
    Fusion,
    FusionRationale,
)
from headsup.agents.pipeline import FusionWorker, ProposerWorker, Publisher
from headsup.agents.proposer import (
    BLOCKING_SEVERITY,
    PREFERENCE,
    PROPOSER_RULES,
    ProposedAction,
    Proposer,
    playbook_block,
)
from headsup.agents.triage import (
    ACTION_BY_REASON,
    RESOLUTION,
    TRIAGE_NAME,
    TRIAGE_RULES,
    WINDOW,
    AlreadyDecided,
    Cluster,
    ClusterSummary,
    DecisionOutcome,
    ScenarioAction,
    ScenarioKey,
    TriageDecision,
    TriageDesk,
    key_of,
    rules_summary,
)

__all__ = [
    "ACTION_BY_REASON",
    "BLOCKING_SEVERITY",
    "CAPPED_SEVERITY",
    "CRITIC_NAME",
    "CRITIC_RULES",
    "EVIDENCE_SEVERITY",
    "FUSION_RULES",
    "MAX_CONFIDENCE",
    "MIN_FRACTION",
    "OVERLAP_PAD_M",
    "PREFERENCE",
    "PROPOSER_RULES",
    "RESOLUTION",
    "STALE_FLOOR",
    "TRIAGE_NAME",
    "TRIAGE_RULES",
    "WINDOW",
    "AlreadyDecided",
    "Cluster",
    "ClusterSummary",
    "Critic",
    "CriticAnswer",
    "DecisionOutcome",
    "Finding",
    "Fusion",
    "FusionRationale",
    "FusionWorker",
    "ProposedAction",
    "Proposer",
    "ProposerWorker",
    "Publisher",
    "ScenarioAction",
    "ScenarioKey",
    "TriageDecision",
    "TriageDesk",
    "key_of",
    "narrow_geometry",
    "playbook_block",
    "review_rules",
    "rules_summary",
]
