"""Builders for the simulator tests.

Importable module rather than a conftest, matching the rest of this repo
(`pyproject.toml` puts `tests` on the path, so `from fixtures.sim import ...`
works everywhere).

Everything here runs on the committed 4x4 fixture grid: 16 nodes, 48 edges,
one strongly connected component, 100 m edges at 7.5 s free-flow, so a vehicle
crosses an edge in 7.5 ticks and every origin-destination pair is routable. No
network, no Docker, no city download.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from fixtures.world import block_box, grid_world
from headsup.contracts import (
    Action,
    ActionType,
    Advisory,
    AdvisoryProperties,
    AppliesTo,
    Evidence,
    Hazard,
    HazardClass,
    Source,
)
from headsup.sim.config import SimConfig
from headsup.sim.ground_truth import EmptyHazardField, HazardRegion, ScriptedHazardField
from headsup.sim.network import DriveNetwork
from samples import T0

#: Three hours, comfortably longer than any test run.
WINDOW = timedelta(hours=3)
UNTIL = T0 + WINDOW

#: A north-south band across the middle of the grid. Wide enough to be a real
#: barrier rather than one street, so a detour is a genuine choice.
BARRIER = block_box(0.9, 0.0, 1.1, 3.0)

#: A band well away from the barrier, for corridor tests that must not be
#: confused by vehicles stalling on a flood. Centred on an avenue rather than
#: between two, so it contains intersections and exercises the node rules as
#: well as the edge ones.
CORRIDOR_LINE = block_box(1.9, 0.0, 2.1, 3.0)


def tiny_config(**overrides: object) -> SimConfig:
    """A small, fast, valid config on the fixture grid."""
    base = {
        "city": "fixture",
        "seed": 42,
        "vehicles": 12,
        "fleets": 3,
        "zones": 6,
        "dwell_s": 5,
        "stall_to_assist_s": 5,
        "speed": 10.0,
    }
    base.update(overrides)
    cfg = SimConfig(**base)  # type: ignore[arg-type]
    cfg.validate()
    return cfg


@lru_cache(maxsize=8)
def grid_net(zones: int = 6, seed: int = 42, any_vertex: bool = False) -> DriveNetwork:
    """A `DriveNetwork` over the fixture grid. Cached: building it is pure."""
    cfg = tiny_config(zones=zones, seed=seed, any_vertex_destinations=any_vertex)
    return DriveNetwork.build(grid_world(), cfg)


def evidence(count: int = 1) -> list[Evidence]:
    """Enough evidence to satisfy the advisory contract's `min_length=1`."""
    return [
        Evidence(
            event_id=f"ev-{index}",
            source=Source.NWS if index % 2 == 0 else Source.USGS,
            kind="flood_warning",
            observed_at=T0,
            raw_ref="data/recordings/s1_flood/fixture/nws.jsonl",
        )
        for index in range(count)
    ]


def hazard(
    *,
    geometry: object = None,
    confidence: float = 0.9,
    severity: int = 4,
    hazard_class: HazardClass = HazardClass.FLOOD,
    hazard_id: str = "haz-test",
) -> Hazard:
    geom = geometry if geometry is not None else BARRIER.__geo_interface__
    return Hazard(
        id=hazard_id,
        **{"class": hazard_class},
        severity=severity,
        confidence=confidence,
        geometry=geom,
        evidence=evidence(),
        valid_until=UNTIL,
        first_seen=T0,
        updated_at=T0,
    )


def advisory_for(
    action_type: ActionType = ActionType.NO_ENTRY,
    *,
    geometry: object = None,
    confidence: float = 0.9,
    params: dict[str, object] | None = None,
    edges: tuple[str, ...] = (),
    nodes: tuple[str, ...] = (),
    advisory_id: str = "adv-test",
    version: int = 1,
    valid_from: object = None,
    valid_until: object = None,
    hazard_class: HazardClass = HazardClass.FLOOD,
) -> Advisory:
    """A signed-shaped (but unsigned) advisory over the fixture grid.

    Unsigned on purpose: the engine never checks signatures -- that is the
    runner's job, against a real key -- so a core test should not have to
    carry one.
    """
    geom = geometry if geometry is not None else BARRIER.__geo_interface__
    ev = evidence(2)
    return Advisory(
        geometry=geom,
        properties=AdvisoryProperties(
            id=advisory_id,
            version=version,
            hazard=hazard(geometry=geom, confidence=confidence, hazard_class=hazard_class),
            action=Action(
                type=action_type,
                applies_to=AppliesTo(edges=list(edges), nodes=list(nodes)),
                params=dict(params or {}),
            ),
            valid_from=valid_from or T0,
            valid_until=valid_until or UNTIL,
            evidence=ev,
            policy_rule=f"test.{action_type.value.lower()}.v1",
            issuer="headsup/fixture",
        ),
    )


def scripted_hazards(
    net: DriveNetwork,
    *,
    geometry: object = None,
    hazard_class: HazardClass = HazardClass.FLOOD,
    severity: int = 4,
    valid_from: object = None,
    valid_until: object = None,
    hazard_id: str = "gt-test",
) -> ScriptedHazardField:
    """Ground truth over one area of the fixture grid."""
    geom = geometry if geometry is not None else BARRIER.__geo_interface__
    return ScriptedHazardField.from_regions(
        [
            HazardRegion(
                id=hazard_id,
                hazard_class=hazard_class,
                geometry=geom,
                valid_from=valid_from or T0,
                valid_until=valid_until or UNTIL,
                severity=severity,
            )
        ],
        net,
    )


def no_hazards() -> EmptyHazardField:
    return EmptyHazardField()


def drain(sim: object, ticks: int) -> tuple[list[object], list[object]]:
    """Step a simulation, collecting every ack and assist it produced."""
    acks: list[object] = []
    assists: list[object] = []
    for _ in range(ticks):
        result = sim.step()  # type: ignore[attr-defined]
        acks.extend(result.acks)
        assists.extend(result.assists)
    return acks, assists


def barrier_edges() -> frozenset[int]:
    """Edge ids the barrier covers on the fixture grid."""
    return frozenset(grid_world().graph.edge_ids_in_polygon(BARRIER))


def corridor_edges() -> frozenset[int]:
    return frozenset(grid_world().graph.edge_ids_in_polygon(CORRIDOR_LINE))
