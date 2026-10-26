"""The viewer's frame fixture is the server's own output, and stays that way.

`gev/src/layers/headsup/__fixtures__/api-frames.json` is what the JavaScript
suite feeds to the viewer's frame validators. It is generated from the frame
builders in this package rather than hand-written, so the two sides cannot
quietly disagree about what a frame looks like.

This test is the alarm on that arrangement. It rebuilds the fixture and
compares, so a change to any frame's shape fails HERE -- in the suite the
person changing `api/frames.py` is already running -- instead of surfacing
weeks later as a viewer panel that renders nothing.

That is not hypothetical. The viewer read every pipeline frame out of a
nested envelope, under field names this module does not use, for the life of
the build: six validators returned `None` for every frame, the agent rail
and the status strip stayed dark, and no test on either side ever noticed,
because no test ever put a real frame through a real validator.

Regenerate with: uv run python scripts/dump_viewer_frames.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.dump_viewer_frames import FIXTURE, build


def test_the_viewer_fixture_matches_the_frames_this_server_builds() -> None:
    """The committed fixture is byte-for-byte what the builders produce."""
    if not FIXTURE.exists():  # pragma: no cover - only before the first run
        pytest.fail(
            f"{FIXTURE} is missing. Regenerate it with:\n"
            "    uv run python scripts/dump_viewer_frames.py"
        )
    committed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert committed == build(), (
        "The viewer's frame fixture no longer matches this server's frames.\n"
        "A frame's shape changed, so the viewer's validators in\n"
        "gev/src/layers/headsup/records.js may now be reading fields that are\n"
        "no longer sent -- which drops the frame silently and blanks a panel.\n"
        "Update the validators, then regenerate the fixture with:\n"
        "    uv run python scripts/dump_viewer_frames.py"
    )


def test_the_fixture_covers_every_frame_kind_the_socket_sends() -> None:
    """A new frame kind arrives with viewer coverage, or this fails."""
    from headsup.api import frames as frame_module

    # Every FRAME_* constant except the two the viewer reads off other paths:
    # `scenario_state` drives the readout, not the rail, and `telemetry` and
    # `ghost_telemetry` are the fleet ticks, covered by their own tests.
    covered_elsewhere = {
        frame_module.FRAME_SCENARIO_STATE,
        frame_module.FRAME_TELEMETRY,
        frame_module.FRAME_GHOST_TELEMETRY,
    }
    declared = {
        value
        for name, value in vars(frame_module).items()
        if name.startswith("FRAME_") and isinstance(value, str)
    }
    missing = declared - covered_elsewhere - set(build())
    assert not missing, (
        f"These frame kinds reach the viewer with no fixture, so nothing checks "
        f"that the viewer can read them: {sorted(missing)}. Add each to "
        f"scripts/dump_viewer_frames.py and regenerate."
    )
