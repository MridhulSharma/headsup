"""Seed splitting, and the things the deterministic core must never do.

The grep tests at the bottom are blunt but they catch the class of mistake
that is hardest to find by reading: one `datetime.now()` or one bare
`random.random()` somewhere in the tick path makes every determinism
assertion in this suite meaningless without making any of them fail.
"""

from __future__ import annotations

import re
from pathlib import Path

from headsup.sim.rng import SeedTree, split_seed, substream

SIM_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "headsup" / "sim"


def _sources(exclude: tuple[str, ...] = ()) -> list[Path]:
    return [path for path in sorted(SIM_PACKAGE.glob("*.py")) if path.name not in exclude]


def test_a_label_path_gives_the_same_seed_every_time() -> None:
    assert split_seed(42, "trip", 7, "zone") == split_seed(42, "trip", 7, "zone")


def test_different_labels_give_different_seeds() -> None:
    assert split_seed(42, "trip", 7) != split_seed(42, "trip", 8)
    assert split_seed(42, "trip", 7) != split_seed(42, "spawn", 7)


def test_label_parts_cannot_collide_by_concatenation() -> None:
    """("a", "bc") and ("ab", "c") are different paths, not the same string."""
    assert split_seed(42, "a", "bc") != split_seed(42, "ab", "c")


def test_a_different_run_seed_gives_a_different_substream() -> None:
    assert split_seed(1, "trip") != split_seed(2, "trip")


def test_a_substream_is_reproducible() -> None:
    first = [substream(42, "zones").random() for _ in range(5)]
    second = [substream(42, "zones").random() for _ in range(5)]
    assert first == second


def test_adding_a_new_domain_does_not_disturb_the_existing_ones() -> None:
    """The whole point of splitting by name rather than by draw order.

    A feature added later consults a new label; every established domain must
    produce the sequence it produced before.
    """
    tree = SeedTree(42)
    before = [tree.stream("spawn").random() for _ in range(5)]

    fresh = SeedTree(42)
    fresh.stream("a-feature-added-later").random()
    fresh.stream("and-another").random()
    after = [fresh.stream("spawn").random() for _ in range(5)]
    assert before == after


def test_a_one_shot_draw_ignores_how_often_the_domain_was_consulted() -> None:
    """`draw_int` is keyed by its label path, not by the generator's position.

    This is what makes op_b's acceptance delay stable when another advisory
    arrives first.
    """
    tree = SeedTree(42)
    direct = tree.draw_int("op_b", "delay", "adv-2", 1, lo=30, hi=90)

    noisy = SeedTree(42)
    for other in range(5):
        noisy.draw_int("op_b", "delay", f"adv-other-{other}", 1, lo=30, hi=90)
    assert noisy.draw_int("op_b", "delay", "adv-2", 1, lo=30, hi=90) == direct


def test_draw_choice_refuses_an_empty_option_tuple() -> None:
    import pytest

    with pytest.raises(ValueError, match="empty option tuple"):
        SeedTree(42).draw_choice("x", options=())


def test_the_core_never_uses_the_random_module_globals() -> None:
    """Module-level `random` is shared, mutable process state."""
    offenders = []
    pattern = re.compile(r"(?<![\w.])random\.(random|randint|choice|shuffle|sample|uniform)\(")
    for path in _sources(exclude=("rng.py",)):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "use SeedTree, not the random module:\n" + "\n".join(offenders)


def test_the_core_never_reads_the_wall_clock() -> None:
    """Sim time is derived from the start instant and the tick count.

    `runner.py` is exempt: pacing against real time is its whole job.
    """
    offenders = []
    pattern = re.compile(r"datetime\.now\(|utcnow\(|time\.time\(|time\.monotonic\(")
    for path in _sources(exclude=("runner.py", "cli.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "the core must take time as input:\n" + "\n".join(offenders)


def test_the_core_never_calls_is_active_without_an_instant() -> None:
    """`Advisory.is_active()` defaults to `utcnow()`, which would be a wall-clock read."""
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "is_active()" in line:
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "pass at=sim_time:\n" + "\n".join(offenders)
