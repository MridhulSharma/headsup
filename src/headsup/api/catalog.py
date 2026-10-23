"""The scenario catalogue the viewer's Test Scene launcher reads.

`scenarios/*.yaml` is the only description of a scenario there is: what it is,
whether its signals were observed or generated, and where to point the camera.
This module reads those files and nothing else, so adding a scenario is adding
a file and the launcher picks it up on the next request.

Only the launcher's fields are read here. The rest of a descriptor -- the
provenance block, the feed-by-feed counts, the expected sentinel output -- is
for the people and the tests that check a recording is what it claims to be,
and the API has no business summarising it.

`scripted` travels through unchanged because the God's Eye View badges a run
with it. A scenario that forgot to say either way is read as scripted: the
badge appearing when it should not is a cosmetic bug, and the badge missing
when it should be there is a run that looks more real than it is.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from headsup.ingest.recorder import PROJECT_ROOT

log = logging.getLogger(__name__)

#: Where the descriptors live.
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"

#: Camera the viewer starts a scene from when the descriptor names none: the
#: whole scene from above, which is wrong for every city and obviously so.
FALLBACK_VIEW = {"lon": -97.7431, "lat": 30.2686, "height": 4000.0, "heading": 0.0, "pitch": -45.0}


class StartView(BaseModel):
    """Where the viewer puts the camera when a scene opens.

    Cesium's own terms: a position in degrees and metres, and the two angles
    that matter for a city scene. `roll` is not here -- a tilted horizon is
    never what a launcher wants.
    """

    model_config = ConfigDict(extra="forbid")

    lon: float = Field(ge=-180.0, le=180.0)
    lat: float = Field(ge=-90.0, le=90.0)
    #: Metres above the ellipsoid.
    height: float = Field(gt=0.0, le=20_000_000.0)
    #: Degrees clockwise from north.
    heading: float = Field(default=0.0, ge=0.0, lt=360.0)
    #: Degrees from the horizon, negative looking down.
    pitch: float = Field(default=-45.0, ge=-90.0, le=90.0)


class ScenarioCard(BaseModel):
    """One row in the Test Scene launcher."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    #: The one line that says what the run is asking. Prose, shown as given.
    what_if: str
    city: str
    #: True when any signal in the recording was generated rather than observed.
    scripted: bool
    start_view: StartView
    hazard_class: str | None = None
    #: The speed factor the descriptor was built around, as the launcher's
    #: default. A start request may override it.
    default_speed: float = 1.0


class CatalogError(RuntimeError):
    """A descriptor is missing or unreadable."""


def _card_from(document: dict[str, Any], path: Path) -> ScenarioCard:
    """One descriptor as a launcher row, with the file named in any error."""
    scenario_id = str(document.get("id") or path.stem)
    view = document.get("start_view") or FALLBACK_VIEW
    if document.get("start_view") is None:
        log.warning("%s has no start_view; the launcher will open on a default camera", path.name)
    replay = document.get("replay") or {}
    return ScenarioCard(
        id=scenario_id,
        title=str(document.get("name") or scenario_id),
        what_if=str(document.get("what_if") or "").strip() or f"Replay {scenario_id}.",
        city=str(document.get("city") or ""),
        scripted=bool(document.get("scripted", True)),
        start_view=StartView.model_validate(view),
        hazard_class=document.get("hazard_class"),
        default_speed=float(replay.get("default_speed", 1.0)),
    )


class ScenarioCatalog:
    """Every `scenarios/*.yaml`, read on demand.

    Read per request rather than cached: a descriptor is a few kilobytes, and
    editing one while the server runs is exactly what a scene author does.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or SCENARIOS_DIR

    def files(self) -> list[Path]:
        return sorted(self.directory.glob("*.yaml"))

    def cards(self) -> list[ScenarioCard]:
        """Every readable descriptor, in id order.

        A file that will not parse is logged and skipped: one broken
        descriptor must not empty the launcher.
        """
        cards: list[ScenarioCard] = []
        for path in self.files():
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                cards.append(_card_from(document, path))
            except Exception as exc:
                log.warning("%s is not a usable scenario descriptor: %s", path.as_posix(), exc)
        return sorted(cards, key=lambda card: card.id)

    def card(self, scenario_id: str) -> ScenarioCard:
        """One descriptor by id, or raise with the ids that do exist."""
        for card in self.cards():
            if card.id == scenario_id:
                return card
        known = ", ".join(card.id for card in self.cards()) or "none"
        raise CatalogError(
            f"no scenario {scenario_id!r} in {self.directory.as_posix()}; have: {known}"
        )
