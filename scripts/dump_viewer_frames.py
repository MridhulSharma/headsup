"""Write the viewer's frame fixture from the API's own frame builders.

The viewer validates every socket frame in
`gev/src/layers/headsup/records.js`, and for the life of this build those
validators read a shape no producer ever sent -- so every pipeline frame was
dropped and the agent rail stayed dark. Nothing caught it because the two
sides had no shared example to disagree over.

This is that example. It is generated from `headsup.api.frames`, which is
the only producer, so the fixture cannot describe a contract the server does
not actually speak. `tests/unit/test_viewer_frame_fixture.py` regenerates it
and fails when it drifts; `records.contract.test.mjs` feeds it to the real
validators. A change to a frame's shape therefore breaks the Python suite
and the JavaScript suite together, in the same commit that makes it.

Run: uv run python scripts/dump_viewer_frames.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from headsup.api.frames import (  # noqa: E402
    RailIndex,
    ack_frame,
    advisory_frame,
    assist_frame,
    candidate_frame,
    counters_frame,
    hazard_frame,
    proposal_frame,
    verdict_frame,
)
from samples import (  # noqa: E402
    ACK,
    ADVISORY,
    ASSIST_REQUEST,
    CANDIDATE,
    HAZARD,
    PROPOSAL,
    VERDICT,
)

FIXTURE = ROOT / "gev" / "src" / "layers" / "headsup" / "__fixtures__" / "api-frames.json"


def build() -> dict[str, object]:
    """Every frame kind the viewer socket sends, in pipeline order."""
    index = RailIndex()
    # Built in chain order so the rail links are populated the way a live run
    # populates them: a proposal's candidate ids come from its hazard.
    candidate = candidate_frame(CANDIDATE, index)
    hazard = hazard_frame(HAZARD, index)
    proposal = proposal_frame(PROPOSAL, index)
    verdict = verdict_frame(VERDICT, index)
    advisory = advisory_frame(ADVISORY, index, signature_valid=True)
    ack = ack_frame(ACK, index)
    assist = assist_frame(ASSIST_REQUEST)
    counters = counters_frame(
        {
            "live": {"hazard_entries": 3, "assist_open": 7, "rides_completed": 41},
            "ghost": {"hazard_entries": 12, "assist_open": 23, "rides_completed": 38},
        },
        {"live": {"corr-1": 4.5}, "ghost": {"corr-1": 19.25}},
    )
    return {
        "candidate": candidate,
        "hazard": hazard,
        "proposal": proposal,
        "verdict": verdict,
        "advisory": advisory,
        "ack": ack,
        "assist": assist,
        "counters": counters,
    }


def main() -> None:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    FIXTURE.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
