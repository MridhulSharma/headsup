"""Event-driven agent workers: sentinels, fusion, the proposer, the compiler.

The pipeline, in the order a signal travels it:

| stage | module | in | out |
| --- | --- | --- | --- |
| sentinels | `agents.sentinels` | `events.*` | `candidates` |
| fusion | `agents.fusion` | `candidates` | `hazards` |
| proposer | `agents.proposer` | `hazards` | `proposals` |
| critic | `agents.critic` | a proposal | a verdict (a stub until D3.2) |
| compiler | `agents.compiler` | proposal + verdict | `advisories` |

`agents.pipeline` holds the two bus workers for the middle stages, and
`hu agents --only fusion,proposer` is how they are started. Fusion and the
proposer each ask a `judgment` model one question and each have a complete
rules path behind it, so variant V1 of the ablation runs on nothing else.
Only `agents.compiler` may write to the `advisories` stream.
"""

from __future__ import annotations

from headsup.agents.critic import STUB_NAME, STUB_REASON, stub_verdict
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

__all__ = [
    "BLOCKING_SEVERITY",
    "CAPPED_SEVERITY",
    "EVIDENCE_SEVERITY",
    "FUSION_RULES",
    "MAX_CONFIDENCE",
    "OVERLAP_PAD_M",
    "PREFERENCE",
    "PROPOSER_RULES",
    "STALE_FLOOR",
    "STUB_NAME",
    "STUB_REASON",
    "Fusion",
    "FusionRationale",
    "FusionWorker",
    "ProposedAction",
    "Proposer",
    "ProposerWorker",
    "Publisher",
    "playbook_block",
    "stub_verdict",
]
