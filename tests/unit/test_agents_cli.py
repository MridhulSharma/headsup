"""`hu agents` and `hu scenario` argument handling.

Exit code 2 for bad input, following `src/headsup/ingest/cli.py`. Neither
command is actually run here: `hu agents` wants Redis and `hu scenario
synth-blackout` writes into the repository, so what is tested is the part that
decides whether a run should start at all.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from headsup.agents.cli import AGENTS, _selected
from headsup.cli import app

runner = CliRunner()


def flat(output: str) -> str:
    """Rich wraps to the console width, so match on unwrapped text."""
    return " ".join(output.split())


# --- hu agents --------------------------------------------------------------


def test_agents_is_a_top_level_verb() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "agents" in flat(result.output)


def test_help_names_every_stage_of_the_pipeline() -> None:
    result = runner.invoke(app, ["agents", "--help"])
    assert result.exit_code == 0, result.output
    text = flat(result.output)
    for fragment in (
        "--only",
        "flood",
        "signal",
        "fusion",
        "proposer",
        "--once",
        "--namespace",
        "--compile",
    ):
        assert fragment in text


def test_only_takes_the_comma_separated_list_from_the_build_packet() -> None:
    assert _selected("flood,signal") == ["flood", "signal"]
    assert _selected(" flood , signal ") == ["flood", "signal"]
    assert _selected("signal") == ["signal"]
    assert _selected("fusion,proposer") == ["fusion", "proposer"]


def test_only_is_de_duplicated_and_ordered_by_the_pipeline() -> None:
    """Two `--only signal` arguments are one worker, not two in one process."""
    assert _selected("signal,flood,signal") == ["flood", "signal"]
    # Whatever order the flag names them in, they run feeds-first: under
    # `--once` that order is what lets a hazard reach the proposer at all.
    assert _selected("proposer,fusion,flood") == ["flood", "fusion", "proposer"]


def test_an_empty_only_runs_every_agent() -> None:
    assert _selected("") == list(AGENTS) == ["flood", "signal", "fusion", "proposer"]


def test_an_unknown_agent_names_the_ones_that_exist() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        _selected("flood,weather")


def test_an_unknown_agent_stops_the_run_before_it_touches_redis() -> None:
    result = runner.invoke(app, ["agents", "--only", "weather"])
    assert result.exit_code == 2, result.output
    assert "unknown agent" in flat(result.output)
    assert "flood, signal, fusion, proposer" in flat(result.output)


# --- hu scenario ------------------------------------------------------------


def test_scenario_help_lists_the_synth_command() -> None:
    result = runner.invoke(app, ["scenario", "--help"])
    assert result.exit_code == 0, result.output
    text = flat(result.output)
    assert "synth-blackout" in text
    assert "signals" in text


def test_synth_blackout_help_names_the_seed() -> None:
    result = runner.invoke(app, ["scenario", "synth-blackout", "--help"])
    assert result.exit_code == 0, result.output
    text = flat(result.output)
    for option in ("--seed", "--signals", "--minutes"):
        assert option in text


def test_a_window_too_short_for_the_timeline_exits_two() -> None:
    result = runner.invoke(app, ["scenario", "synth-blackout", "--minutes", "5"])
    assert result.exit_code == 2, result.output
    assert "--minutes" in flat(result.output)


def test_an_unknown_city_has_no_signal_map_to_read() -> None:
    result = runner.invoke(app, ["scenario", "signals", "--city", "nowhere"])
    assert result.exit_code == 2, result.output
    assert "no signal map" in flat(result.output)
