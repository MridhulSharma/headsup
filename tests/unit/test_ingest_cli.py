"""`hu ingest` is wired up and replays the committed S1 fixture without Redis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from fixtures.ingest import USGS_BODY
from headsup.cli import app
from headsup.contracts import Source
from headsup.ingest import RawPayload, Recorder

runner = CliRunner()


def test_ingest_help_lists_live_record_and_replay() -> None:
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("live", "record", "replay"):
        assert command in result.output


def test_replay_prints_the_event_stream_and_finishes() -> None:
    # Three events is inside the first recorded poll, so this runs on a real
    # clock without waiting out a gap. The timing itself is covered against a
    # fake clock in tests/unit/test_ingest_replay.py.
    result = runner.invoke(
        app,
        [
            "ingest",
            "replay",
            "--scenario",
            "s1_flood",
            "--speed",
            "60",
            "--no-publish",
            "--limit",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "replay s1_flood at 60x" in result.output
    assert "gauge_height" in result.output
    assert "3 events" in result.output


def test_replay_reads_an_explicit_directory(tmp_path: Path) -> None:
    with Recorder("tiny", root=tmp_path) as recorder:
        for seq, offset in enumerate((0, 60)):
            recorder.write(
                RawPayload(
                    source=Source.USGS,
                    ref=f"usgs/x/{seq}",
                    url="https://example.invalid/items",
                    fetched_at=datetime(2026, 4, 20, 18, tzinfo=UTC) + timedelta(seconds=offset),
                    body=USGS_BODY,
                )
            )

    result = runner.invoke(
        app,
        [
            "ingest",
            "replay",
            "--scenario",
            "tiny",
            "--speed",
            "60",
            "--no-publish",
            "--dir",
            str(tmp_path / "tiny" / "raw"),
        ],
    )
    assert result.exit_code == 0, result.output
    # Two readings per payload, and a one-minute gap played in one second.
    assert "4 events from 2 payloads" in result.output


def test_replay_refuses_a_speed_over_sixty() -> None:
    result = runner.invoke(
        app, ["ingest", "replay", "--scenario", "s1_flood", "--speed", "120", "--no-publish"]
    )
    assert result.exit_code == 2
    assert "outside" in result.output


def test_replay_names_the_scenario_it_cannot_find() -> None:
    result = runner.invoke(app, ["ingest", "replay", "--scenario", "no_such_thing", "--no-publish"])
    assert result.exit_code == 2
    assert "no recording for scenario" in result.output


def test_live_rejects_an_unknown_city() -> None:
    result = runner.invoke(app, ["ingest", "live", "--city", "atlantis", "--no-publish"])
    assert result.exit_code == 2
    assert "unknown city" in result.output


def test_record_needs_a_positive_duration() -> None:
    result = runner.invoke(app, ["ingest", "record", "--scenario", "s_tmp", "--minutes", "0"])
    assert result.exit_code == 2
    assert "--minutes" in result.output
