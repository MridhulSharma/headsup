"""The `hu` CLI is wired up and does not need Docker to answer `version`."""

from __future__ import annotations

from typer.testing import CliRunner

from headsup import __version__
from headsup.cli import app

runner = CliRunner()


def test_version_prints_the_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_infra_help_lists_up_down_status() -> None:
    result = runner.invoke(app, ["infra", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("up", "down", "status"):
        assert command in result.output
