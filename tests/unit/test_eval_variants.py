"""The variants are flags, and the flags reach the thing they claim to configure.

The ablation's whole claim is that V0 to V4 are one pipeline configured five
ways. So these tests check the two halves of that: the table of specs says what
each variant is, and `ScenarioRunner` brings up what the spec asked for -- no
agents at all for V0, the single orchestrator for V2, the mesh without a critic
for V3, and the mesh with one for V1 and V4.

`_agents` is exercised against a stand-in `_Run` rather than a started
scenario: what is being tested is the branch, and a real run needs Redis, a
city world and a replay. The e2e test is where a variant is run for real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from headsup.agents.compiler.playbooks import default_playbooks
from headsup.agents.compiler.poi import NullPoiChecker
from headsup.agents.orchestrator import Orchestrator, OrchestratorWorker
from headsup.agents.pipeline import FusionWorker, ProposerWorker
from headsup.agents.sentinels.flood import FloodSentinel
from headsup.agents.sentinels.worker import SentinelWorker
from headsup.api import runner as runner_mod
from headsup.api.runner import ScenarioRunner, StartOptions, modes_for_run
from headsup.eval.variants import (
    AI_GROUP,
    DEFAULT_SPEC,
    ORDER,
    RULES_GROUP,
    SPECS,
    Variant,
    parse,
    parse_list,
    parse_seeds,
    seeds_for,
    spec_for,
)
from headsup.sim.config import SimMode

# --- the table of specs ----------------------------------------------------


def test_the_demo_runs_the_full_system() -> None:
    """The runner's default is V4, so `hu demo` is not a configuration of the eval."""
    assert DEFAULT_SPEC.variant is Variant.V4
    assert DEFAULT_SPEC.llm and DEFAULT_SPEC.mesh and DEFAULT_SPEC.critic
    assert not DEFAULT_SPEC.orchestrator


@pytest.mark.parametrize("variant", list(ORDER))
def test_every_variant_has_a_spec_and_a_seed_group(variant: Variant) -> None:
    spec = spec_for(variant)
    assert spec.variant is variant
    assert spec.label
    assert spec.seed_group in (RULES_GROUP, AI_GROUP)
    assert spec.modes


def test_the_baseline_reads_no_feeds_and_runs_one_fleet() -> None:
    spec = SPECS[Variant.V0]
    assert spec.modes == (SimMode.GHOST,)
    assert not spec.agents
    assert not spec.llm


def test_the_rules_only_variant_is_the_mesh_with_no_model() -> None:
    spec = SPECS[Variant.V1]
    assert spec.mesh and spec.critic
    assert not spec.llm
    assert spec.seed_group == RULES_GROUP


def test_the_model_variants_draw_from_the_small_seed_budget() -> None:
    for variant in (Variant.V2, Variant.V3, Variant.V4):
        assert spec_for(variant).seed_group == AI_GROUP


def test_v3_differs_from_v4_by_exactly_one_flag() -> None:
    three, four = SPECS[Variant.V3], SPECS[Variant.V4]
    assert three.critic is False and four.critic is True
    for field in ("modes", "llm", "mesh", "orchestrator"):
        assert getattr(three, field) == getattr(four, field)


# --- parsing the command line ----------------------------------------------


def test_variants_parse_in_the_order_given_without_repeats() -> None:
    assert parse_list("V4,v0,V4") == (Variant.V4, Variant.V0)
    assert parse("v2") is Variant.V2


def test_an_unknown_variant_says_which_ones_exist() -> None:
    with pytest.raises(ValueError, match="V0, V1, V2, V3, V4"):
        parse("V9")
    with pytest.raises(ValueError, match="no variants given"):
        parse_list(" , ")


def test_seeds_need_both_groups_and_count_up_from_the_base() -> None:
    counts = parse_seeds("rules=20,ai=3")
    assert counts == {RULES_GROUP: 20, AI_GROUP: 3}
    assert seeds_for(SPECS[Variant.V4], counts, base=42) == (42, 43, 44)
    assert len(seeds_for(SPECS[Variant.V1], counts, base=7)) == 20


@pytest.mark.parametrize("spec", ["rules=20", "ai=3", "rules=0,ai=3", "rules=x,ai=3", "r=1,ai=2"])
def test_a_seed_spec_that_cannot_be_read_is_refused(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_seeds(spec)


# --- the fleets one run steps ----------------------------------------------


def test_the_baseline_steps_the_ghost_fleet_alone() -> None:
    assert modes_for_run(StartOptions(), SPECS[Variant.V0]) == (SimMode.GHOST,)


def test_every_other_variant_runs_both_fleets_on_one_clock() -> None:
    for variant in (Variant.V1, Variant.V2, Variant.V3, Variant.V4):
        assert modes_for_run(StartOptions(), SPECS[variant]) == (SimMode.LIVE, SimMode.GHOST)


def test_no_ghost_drops_the_baseline_from_an_advised_run() -> None:
    assert modes_for_run(StartOptions(ghost=False), SPECS[Variant.V4]) == (SimMode.LIVE,)


def test_no_ghost_cannot_empty_the_baseline_variant() -> None:
    """V0 is the ghost fleet; a flag that removed it would leave no fleet at all."""
    assert modes_for_run(StartOptions(ghost=False), SPECS[Variant.V0]) == (SimMode.GHOST,)


# --- which workers a variant brings up -------------------------------------


class _Service:
    """Just enough compiler service for `_agents`: a gate with a playbook set."""

    class _Compiler:
        poi = NullPoiChecker()
        playbooks = default_playbooks()

    compiler = _Compiler()


class _Scene:
    def corridor(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def describes_itself(self) -> str:
        return "stub scene router"


def _run(spec_mesh: bool) -> Any:
    """A `_Run` with only the fields `_agents` reads."""
    return runner_mod._Run(
        card=None,  # type: ignore[arg-type]
        options=StartOptions(),
        cfg=None,  # type: ignore[arg-type]
        speed=1.0,
        started_at=datetime(2026, 10, 24, tzinfo=UTC),
        sim=None,  # type: ignore[arg-type]
        replayer=None,  # type: ignore[arg-type]
        service=_Service(),  # type: ignore[arg-type]
        compiler_bus=None,  # type: ignore[arg-type]
        client=None,
        sentinel=FloodSentinel() if spec_mesh else None,
        scene=_Scene(),  # type: ignore[arg-type]
        world=_World(),
    )


class _World:
    """A city with nothing but the attribute the orchestrator's geocoder needs."""

    graph = object()


def _runner(variant: Variant) -> ScenarioRunner:
    return ScenarioRunner(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        spec=SPECS[variant],
    )


def test_the_baseline_brings_up_no_agents_at_all() -> None:
    assert _runner(Variant.V0)._agents(_run(spec_mesh=False)) == []


@pytest.mark.parametrize("variant", [Variant.V1, Variant.V3, Variant.V4])
def test_the_mesh_variants_bring_up_the_three_workers(variant: Variant) -> None:
    workers = _runner(variant)._agents(_run(spec_mesh=True))
    assert [type(worker) for worker in workers] == [
        SentinelWorker,
        FusionWorker,
        ProposerWorker,
    ]


def test_only_v3_runs_the_mesh_without_a_critic() -> None:
    without = _runner(Variant.V3)._agents(_run(spec_mesh=True))[-1]
    with_one = _runner(Variant.V4)._agents(_run(spec_mesh=True))[-1]
    assert without.critic is None and without.review is False
    assert with_one.critic is not None and with_one.review is True


def test_the_single_orchestrator_replaces_the_mesh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_mod, "index_for", lambda _graph: None)
    workers = _runner(Variant.V2)._agents(_run(spec_mesh=False))
    assert [type(worker) for worker in workers] == [OrchestratorWorker]
    assert isinstance(workers[0].orchestrator, Orchestrator)
    # It is handed the gate's own playbooks and POI checker, so it is judged
    # against the same caps the compiler will measure it against.
    assert workers[0].orchestrator.playbooks is _Service._Compiler.playbooks
