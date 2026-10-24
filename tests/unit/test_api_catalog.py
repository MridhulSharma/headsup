"""The launcher's rows, read off `scenarios/*.yaml` and nothing else.

Two of these guard the demo rather than the code. The committed descriptors
have to carry a `what_if` line and a camera, because a Test Scene row with
neither is a row nobody can read or open; and a descriptor that forgets to say
whether its signals were observed or generated has to be read as generated,
because the badge appearing wrongly is cosmetic and the badge missing wrongly
makes a run look more real than it is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from headsup.api.catalog import SCENARIOS_DIR, CatalogError, ScenarioCatalog

#: Every scenario the repository ships.
SHIPPED = ("s1_flood", "s2_blackout", "s3_incident")


@pytest.fixture(scope="module")
def catalog() -> ScenarioCatalog:
    return ScenarioCatalog()


def test_the_committed_scenarios_are_all_listed(catalog: ScenarioCatalog) -> None:
    assert [card.id for card in catalog.cards()] == list(SHIPPED)


@pytest.mark.parametrize("scenario_id", SHIPPED)
def test_every_scenario_can_be_drawn_as_a_launcher_row(
    catalog: ScenarioCatalog, scenario_id: str
) -> None:
    card = catalog.card(scenario_id)
    assert card.title and card.title != scenario_id
    assert card.what_if.strip()
    assert card.city in {"austin", "san_antonio"}
    assert card.hazard_class
    assert 1.0 <= card.default_speed <= 60.0
    # A camera over the city it is about, looking down at it.
    assert card.start_view.height > 0
    assert card.start_view.pitch < 0


def test_s1_is_not_scripted_and_s2_is(catalog: ScenarioCatalog) -> None:
    """S1 replays real observations; S2's signal states were generated."""
    assert catalog.card("s1_flood").scripted is False
    assert catalog.card("s2_blackout").scripted is True


def test_s3_is_scripted_because_its_scene_was_written_here(catalog: ScenarioCatalog) -> None:
    """S3's window is real and the scene inside it is not, so the run is badged.

    The per-record `scripted` flag in the recording says which records were
    written here; the card says the run as a whole contains some, which is
    what the viewer badges. A run that looked more real than it is would be
    the one bad outcome.
    """
    assert catalog.card("s3_incident").scripted is True


def test_an_unknown_scenario_says_which_ones_exist(catalog: ScenarioCatalog) -> None:
    with pytest.raises(CatalogError, match="s1_flood"):
        catalog.card("s3_nothing")


def test_a_descriptor_with_no_scripted_flag_is_read_as_scripted(tmp_path: Path) -> None:
    (tmp_path / "s9_test.yaml").write_text(
        "id: s9_test\nname: Test\ncity: austin\n", encoding="utf-8"
    )
    card = ScenarioCatalog(tmp_path).card("s9_test")
    assert card.scripted is True
    # And it is still openable: the camera falls back rather than failing.
    assert card.start_view.height > 0
    assert card.what_if


def test_one_broken_descriptor_does_not_empty_the_launcher(tmp_path: Path) -> None:
    (tmp_path / "broken.yaml").write_text("id: broken\nstart_view: {lon: 999}\n", encoding="utf-8")
    (tmp_path / "s9_test.yaml").write_text(
        "id: s9_test\nname: Test\ncity: austin\n", encoding="utf-8"
    )
    assert [card.id for card in ScenarioCatalog(tmp_path).cards()] == ["s9_test"]


def test_the_catalog_reads_the_repository_scenarios_directory() -> None:
    assert SCENARIOS_DIR.is_dir()
    assert (SCENARIOS_DIR / "s1_flood.yaml").is_file()
