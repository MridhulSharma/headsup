"""Generated scenarios: the S2 signal blackout, and the real map under it.

S1 is a recording of a real event. S2 is generated, because a downtown feeder
failure has not happened while this prototype was recording - so it is
generated on real geography and labelled `scripted: true`.

    from headsup.scenario import SignalInventory, build, write

    generated = build(SignalInventory.load("austin"), seed=7)
    write(generated)

| module | what it owns |
| --- | --- |
| `inventory` | the committed snapshot of Austin's signal map, and its fetch |
| `blackout` | the S2 timeline, and the recording it writes |
| `descriptor` | `scenarios/s2_blackout.yaml`, including its provenance |
"""

from __future__ import annotations

from headsup.scenario.blackout import (
    CITY,
    DEFAULT_MINUTES,
    DEFAULT_SIGNALS,
    NOISE_RADIUS_M,
    POLL_SECONDS,
    SCENARIO,
    WINDOW_START,
    Blackout,
    build,
    choose,
    write,
)
from headsup.scenario.inventory import (
    SIGNALS_DIR,
    Signal,
    SignalInventory,
    fetch,
)

__all__ = [
    "CITY",
    "DEFAULT_MINUTES",
    "DEFAULT_SIGNALS",
    "NOISE_RADIUS_M",
    "POLL_SECONDS",
    "SCENARIO",
    "SIGNALS_DIR",
    "WINDOW_START",
    "Blackout",
    "Signal",
    "SignalInventory",
    "build",
    "choose",
    "fetch",
    "write",
]
