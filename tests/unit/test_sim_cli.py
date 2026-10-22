"""`hu sim` argument handling.

Exit code 2 for bad input and 1 for a runtime failure, following
`src/headsup/ingest/cli.py`. The messages matter: they are what someone reads
when a run will not start.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from headsup.cli import app
from headsup.world.model import is_built

runner = CliRunner()


def flat(output: str) -> str:
    """Rich wraps to the console width, so match on unwrapped text.

    Same helper as `test_compiler_cli.py`; `result.output` is used rather than
    `result.stdout` because errors go to the stderr console.
    """
    return " ".join(output.split())


def test_sim_is_a_top_level_verb() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "sim" in flat(result.output)


def test_help_lists_the_options_the_build_packet_names() -> None:
    result = runner.invoke(app, ["sim", "--help"])
    assert result.exit_code == 0, result.output
    for option in ("--city", "--fleets", "--vehicles", "--seed", "--speed", "--ghost"):
        assert option in flat(result.output)


def test_the_any_vertex_flag_exists_and_is_off_by_default() -> None:
    """Kept for evaluation, off by default: it is ~13x the routing work."""
    import inspect

    from headsup.sim.cli import sim as sim_command
    from headsup.sim.config import SimConfig

    parameter = inspect.signature(sim_command).parameters["any_vertex_destinations"]
    assert parameter.default.default is False
    assert SimConfig(city="austin", seed=1).any_vertex_destinations is False


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--speed", "99"], "speed"),
        (["--city", "nowhere"], "nowhere"),
        (["--fleets", "4"], "--fleets must be 1 to 3"),
        (["--vehicles", "7", "--fleets", "3"], "does not divide evenly"),
        (["--zones", "1"], "--zones must be at least 2"),
        (["--telemetry-hz", "0"], "--telemetry-hz must be positive"),
        (["--mode", "sideways"], "--mode must be live, ghost or both"),
    ],
)
def test_bad_arguments_exit_two_with_an_explanation(args: list[str], fragment: str) -> None:
    result = runner.invoke(app, ["sim", *args])
    assert result.exit_code == 2, result.output
    assert fragment in flat(result.output)


def test_a_scenario_set_in_another_city_is_refused() -> None:
    result = runner.invoke(app, ["sim", "--city", "san_antonio", "--scenario", "s2_blackout"])
    assert result.exit_code == 2, result.output
    assert "austin" in flat(result.output)


def test_an_unknown_scenario_is_refused_and_lists_the_known_ones() -> None:
    result = runner.invoke(app, ["sim", "--scenario", "s9_eclipse"])
    assert result.exit_code == 2, result.output
    assert "s1_flood" in flat(result.output)


def test_fleets_must_not_exceed_the_three_personas() -> None:
    """There are exactly three personas, and the counters are keyed by them."""
    result = runner.invoke(app, ["sim", "--fleets", "4"])
    assert result.exit_code == 2, result.output
    for persona in ("op_a", "op_b", "op_c"):
        assert persona in flat(result.output)


@pytest.mark.skipif(
    not is_built("san_antonio"),
    reason="needs a built city: run `hu world build --city san_antonio`",
)
def test_a_short_run_completes_without_a_bus_and_prints_its_hash() -> None:
    """`--no-publish` is the path the determinism and perf checks use."""
    result = runner.invoke(
        app,
        [
            "sim",
            "--city",
            "san_antonio",
            "--vehicles",
            "30",
            "--fleets",
            "3",
            "--zones",
            "6",
            "--seed",
            "42",
            "--speed",
            "0",
            "--ticks",
            "20",
            "--no-publish",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "trajectory hash" in flat(result.output)
    assert "real time" in flat(result.output)


@pytest.mark.skipif(
    not is_built("san_antonio"),
    reason="needs a built city: run `hu world build --city san_antonio`",
)
def test_the_same_seed_prints_the_same_hash_through_the_cli() -> None:
    def run() -> str:
        result = runner.invoke(
            app,
            [
                "sim",
                "--city",
                "san_antonio",
                "--vehicles",
                "30",
                "--zones",
                "6",
                "--seed",
                "7",
                "--speed",
                "0",
                "--ticks",
                "20",
                "--no-publish",
            ],
        )
        assert result.exit_code == 0, result.output
        line = next(part for part in result.output.splitlines() if "trajectory hash" in part)
        return line.strip()

    assert run() == run()


@pytest.mark.skipif(
    not is_built("san_antonio"),
    reason="needs a built city: run `hu world build --city san_antonio`",
)
def test_a_run_with_no_ground_truth_says_so() -> None:
    """Without a hazard there is nothing for a vehicle to stall on.

    Worth saying out loud: an all-zeros report would otherwise look like
    success rather than an empty experiment.
    """
    result = runner.invoke(
        app,
        [
            "sim",
            "--city",
            "san_antonio",
            "--vehicles",
            "3",
            "--fleets",
            "3",
            "--zones",
            "3",
            "--speed",
            "0",
            "--ticks",
            "5",
            "--no-publish",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no ground truth" in flat(result.output)
