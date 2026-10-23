"""Deterministic mesoscopic fleet simulator, including ghost (baseline) mode.

The deterministic core -- `config`, `rng`, `network`, `masks`, `router`,
`vehicles`, `ground_truth`, `operators`, `counters`, `hashing`, `engine` --
has no Redis, no asyncio and no wall clock in it, so a simulation can be
built and stepped inside a unit test with nothing running. `runner` is the
only module that touches the bus, and `cli` is the only one that prints.

Module map:

* `config`       every tunable, frozen, plus `SimMode`
* `rng`          named seed substreams, so features cannot disturb each other
* `network`      flat arrays for the tick loop, the strong core, the zones
* `masks`        advisory -> graph restrictions, resolved once per version
* `router`       reverse distance labels per (mask, zone) and greedy next-hop
* `vehicles`     mutable per-tick state as slotted dataclasses
* `ground_truth` what is physically true, independent of any advisory
* `operators`    the three personas and their mask state machines
* `counters`     integer counters and the key names D3.5/D4.1 read
* `hashing`      the trajectory hash
* `engine`       the two-phase 1-second tick
* `runner`       pacing and bus I/O
* `scenarios`    the S1 and S2 hazard areas, derived from the recordings
"""

from headsup.sim.config import PERSONAS, UNPACED, SimConfig, SimMode
from headsup.sim.counters import Counters
from headsup.sim.engine import SimReport, Simulation, TickResult
from headsup.sim.ground_truth import (
    EmptyHazardField,
    GroundHazard,
    HazardField,
    HazardRegion,
    HazardSnapshot,
    ScriptedHazardField,
)
from headsup.sim.hashing import TrajectoryHash
from headsup.sim.masks import EMPTY_MASK, Mask, corridor_polygon, merge, resolve, weights_for
from headsup.sim.network import DriveNetwork, NetworkError
from headsup.sim.operators import (
    OP_A,
    OP_B,
    OP_C,
    Decision,
    Operator,
    OperatorSet,
    Persona,
    ghost_persona,
)
from headsup.sim.rng import SeedTree, split_seed, substream
from headsup.sim.router import Router
from headsup.sim.runner import SimRunner, TickTap, modes_for
from headsup.sim.vehicles import Intent, Vehicle

__all__ = [
    "EMPTY_MASK",
    "OP_A",
    "OP_B",
    "OP_C",
    "PERSONAS",
    "UNPACED",
    "Counters",
    "Decision",
    "DriveNetwork",
    "EmptyHazardField",
    "GroundHazard",
    "HazardField",
    "HazardRegion",
    "HazardSnapshot",
    "Intent",
    "Mask",
    "NetworkError",
    "Operator",
    "OperatorSet",
    "Persona",
    "Router",
    "ScriptedHazardField",
    "SeedTree",
    "SimConfig",
    "SimMode",
    "SimReport",
    "SimRunner",
    "Simulation",
    "TickResult",
    "TickTap",
    "TrajectoryHash",
    "Vehicle",
    "corridor_polygon",
    "ghost_persona",
    "merge",
    "modes_for",
    "resolve",
    "split_seed",
    "substream",
    "weights_for",
]
