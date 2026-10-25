"""The five variants, as config flags on one code path.

A variant is not a branch of the system: it is a `VariantSpec` handed to the
same `ScenarioRunner` the demo runs on. That is the whole claim the ablation
makes -- V4 and V1 differ by whether an `LlmClient` was built, V4 and V3 by
whether a critic reviews, V4 and V2 by which workers are brought up, and V0 by
there being no agents at all. Nothing in `src/headsup` has a second
implementation of a stage for the benefit of the eval.

    V0  ghost fleet only, no agents          the baseline: no Heads Up
    V1  the mesh, no model anywhere          rules paths end to end
    V2  one orchestrator with every tool     one judgment call per event
    V3  the mesh without the critic          no independent second read
    V4  the mesh                             full Heads Up

**Seed groups.** The rules-only variants are deterministic and cheap, so they
are run over many seeds; the ones that ask a model are run over few. That is
what `--seeds rules=20,ai=3` keys, and `seed_group` is where a variant says
which budget it draws from.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from headsup.sim.config import SimMode

#: The seed-budget groups `--seeds rules=20,ai=3` names.
RULES_GROUP = "rules"
AI_GROUP = "ai"


class Variant(StrEnum):
    """The ablation's five configurations, by the names the table uses."""

    V0 = "V0"
    V1 = "V1"
    V2 = "V2"
    V3 = "V3"
    V4 = "V4"


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """What one variant asks of the runner.

    Every field is read by `api.runner.ScenarioRunner`; none of them is read
    anywhere else, so adding a variant is adding a row to `SPECS` rather than
    a code path.
    """

    variant: Variant
    label: str
    #: Fleet sets to step. V0 runs the unadvised baseline alone; everything
    #: else runs it beside the advised fleet, on one clock, so a single run
    #: carries its own control.
    modes: tuple[SimMode, ...]
    #: Build an `LlmClient` at all. False is the rules path everywhere, which
    #: is what every agent in this repository already documents as variant V1.
    llm: bool
    #: Bring up the sentinel, fusion and the proposer.
    mesh: bool
    #: Review each proposal before the gate sees it.
    critic: bool
    #: Bring up the single orchestrator instead of the mesh.
    orchestrator: bool
    #: Which `--seeds` budget this variant draws from.
    seed_group: str

    @property
    def agents(self) -> bool:
        """True if this variant reads the feeds at all."""
        return self.mesh or self.orchestrator


SPECS: dict[Variant, VariantSpec] = {
    Variant.V0: VariantSpec(
        variant=Variant.V0,
        label="baseline, no Signum",
        modes=(SimMode.GHOST,),
        llm=False,
        mesh=False,
        critic=False,
        orchestrator=False,
        seed_group=RULES_GROUP,
    ),
    Variant.V1: VariantSpec(
        variant=Variant.V1,
        label="rules only, no model",
        modes=(SimMode.LIVE, SimMode.GHOST),
        llm=False,
        mesh=True,
        critic=True,
        orchestrator=False,
        seed_group=RULES_GROUP,
    ),
    Variant.V2: VariantSpec(
        variant=Variant.V2,
        label="single orchestrator",
        modes=(SimMode.LIVE, SimMode.GHOST),
        llm=True,
        mesh=False,
        critic=False,
        orchestrator=True,
        seed_group=AI_GROUP,
    ),
    Variant.V3: VariantSpec(
        variant=Variant.V3,
        label="mesh without critic",
        modes=(SimMode.LIVE, SimMode.GHOST),
        llm=True,
        mesh=True,
        critic=False,
        orchestrator=False,
        seed_group=AI_GROUP,
    ),
    Variant.V4: VariantSpec(
        variant=Variant.V4,
        label="full Signum",
        modes=(SimMode.LIVE, SimMode.GHOST),
        llm=True,
        mesh=True,
        critic=True,
        orchestrator=False,
        seed_group=AI_GROUP,
    ),
}

#: The variant the demo and the API run. Named so `ScenarioRunner`'s default
#: argument says which configuration "no variant chosen" means.
DEFAULT_SPEC = SPECS[Variant.V4]

#: Every variant, in table order.
ORDER: tuple[Variant, ...] = tuple(Variant)


def spec_for(variant: Variant | str) -> VariantSpec:
    """One variant's spec, by enum or by the name the CLI takes."""
    return SPECS[parse(variant)]


def parse(variant: Variant | str) -> Variant:
    """`"v2"`, `"V2"` or `Variant.V2`, as a `Variant`."""
    if isinstance(variant, Variant):
        return variant
    try:
        return Variant(str(variant).strip().upper())
    except ValueError as exc:
        known = ", ".join(item.value for item in ORDER)
        raise ValueError(f"{variant!r} is not a variant; one of: {known}") from exc


def parse_list(names: str) -> tuple[Variant, ...]:
    """`"V0,V1,V4"` as variants, in the order given, without repeats."""
    chosen: list[Variant] = []
    for raw in str(names).split(","):
        if not raw.strip():
            continue
        variant = parse(raw)
        if variant not in chosen:
            chosen.append(variant)
    if not chosen:
        raise ValueError("no variants given; e.g. --variants V0,V1,V2,V3,V4")
    return tuple(chosen)


def parse_seeds(spec: str) -> dict[str, int]:
    """`"rules=20,ai=3"` as a seed count per group.

    Both groups are required: a sweep that silently ran one seed of V4 because
    a key was misspelled would be a measurement nobody could read back.
    """
    counts: dict[str, int] = {}
    for part in str(spec).split(","):
        if not part.strip():
            continue
        name, _, value = part.partition("=")
        key = name.strip().lower()
        if key not in (RULES_GROUP, AI_GROUP):
            raise ValueError(f"unknown seed group {name.strip()!r}; use rules= and ai=")
        try:
            count = int(value)
        except ValueError as exc:
            raise ValueError(f"--seeds {part.strip()!r} is not a count") from exc
        if count < 1:
            raise ValueError(f"--seeds {key}={count} must be at least 1")
        counts[key] = count
    missing = [name for name in (RULES_GROUP, AI_GROUP) if name not in counts]
    if missing:
        raise ValueError(f"--seeds names no count for: {', '.join(missing)}")
    return counts


def seeds_for(spec: VariantSpec, counts: dict[str, int], *, base: int) -> tuple[int, ...]:
    """The seeds one variant is run on: `base`, `base+1`, ...

    Consecutive from one base rather than drawn at random, so the V0 run a V4
    run is compared against shares its seed, which is what makes the pair a
    controlled comparison rather than two samples.
    """
    return tuple(base + offset for offset in range(counts[spec.seed_group]))
