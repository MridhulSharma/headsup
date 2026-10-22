"""Per-feed sentinels that turn CivicEvents into CandidateHazards.

A sentinel reads one or two feeds and says what it thinks it is looking at. It
never decides anything: fusion (D2.5) weighs several sentinels' readings into
a `Hazard`, and only the compiler publishes. Two of them exist today:

| sentinel | reads | emits |
| --- | --- | --- |
| `FloodSentinel` | `events.nws`, `events.usgs` | FLOOD, per polygon, gauges attached |
| `SignalOutageSentinel` | `events.austin_signals` | SIGNAL_OUTAGE, per signal cluster |

`SentinelWorker` is how either one runs on the bus, and `hu agents --only
flood,signal` is how both are started.

Feed text reaches a model in exactly one place - the flood sentinel's
`volume` call that fills `FloodFields` - and it reaches it as a `data_block`
inside a user turn. There is no instruction slot in this package for a feed
to write into, and `tests/unit/test_sentinel_flood.py` holds that.
"""

from __future__ import annotations

from headsup.agents.sentinels.base import (
    GEOD,
    Sentinel,
    distance_m,
    noisy_or,
    pad_m,
    stable_id,
)
from headsup.agents.sentinels.flood import (
    FIELD_RULES,
    FLOOD_PRODUCTS,
    GAUGE_RADIUS_M,
    PURPOSE,
    FloodFields,
    FloodSentinel,
)
from headsup.agents.sentinels.signals import (
    EPS_M,
    MIN_SIGNALS,
    PAD_M,
    SignalOutageSentinel,
    cluster_labels,
)
from headsup.agents.sentinels.worker import SentinelWorker, WorkerReport

__all__ = [
    "EPS_M",
    "FIELD_RULES",
    "FLOOD_PRODUCTS",
    "GAUGE_RADIUS_M",
    "GEOD",
    "MIN_SIGNALS",
    "PAD_M",
    "PURPOSE",
    "FloodFields",
    "FloodSentinel",
    "Sentinel",
    "SentinelWorker",
    "SignalOutageSentinel",
    "WorkerReport",
    "cluster_labels",
    "distance_m",
    "noisy_or",
    "pad_m",
    "stable_id",
]
