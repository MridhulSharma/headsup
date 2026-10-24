"""Intersection lookup: a dispatcher's cross-street string to one junction.

Two graphs are used here on purpose. The committed 4x4 grid
(`tests/fixtures/world_grid.graphml`) is what the lookup is exercised on,
because it needs no city build; the string normalising is tested on its own,
because that is where the real feed's abbreviations land.
"""

from __future__ import annotations

import pytest

from fixtures.world import at, grid_graph
from headsup.world.geocode import IntersectionIndex, cross_streets, index_for, street

# --- normalising -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "base", "direction"),
    [
        ("W 6TH ST", "6th street", "west"),
        ("w 6th st", "6th street", "west"),
        ("West 6th Street", "6th street", "west"),
        ("GUADALUPE ST", "guadalupe street", ""),
        # A house number or a block range is not part of a street name.
        ("910-919 W Cesar Chavez St", "cesar chavez street", "west"),
        ("600 BLOCK OF W 7TH ST", "7th street", "west"),
        # A number inside the name is part of the name.
        ("N 1 Ave", "1 avenue", "north"),
        ("1500 N Ih 35 Sb", "ih 35", "north"),
    ],
)
def test_a_dispatch_street_name_reduces_to_what_osm_can_agree_with(
    text: str, base: str, direction: str
) -> None:
    parsed = street(text)
    assert parsed is not None
    assert (parsed.base, parsed.direction) == (base, direction)


def test_a_block_range_on_its_own_names_no_street() -> None:
    assert street("1100 BLOCK") is None
    assert street("  ") is None


def test_a_name_with_no_street_type_matches_any_suffix() -> None:
    """The feed writes `GUADALUPE`; OSM writes `Guadalupe Street`."""
    bare, spelled = street("GUADALUPE"), street("Guadalupe Street")
    assert bare is not None and spelled is not None
    assert bare.matches(spelled)
    assert spelled.matches(bare)


def test_an_ordinal_does_not_match_a_longer_ordinal() -> None:
    """`6TH` must not find `West 16th Street`, which is the one real collision.

    Downtown Austin has both, two blocks apart on the same avenue, so a
    substring match would place a scene on the wrong corner.
    """
    sixth, sixteenth = street("6TH ST"), street("West 16th Street")
    assert sixth is not None and sixteenth is not None
    assert not sixth.matches(sixteenth)


def test_a_direction_the_feed_dropped_still_matches() -> None:
    plain, directed = street("6TH ST"), street("West 6th Street")
    assert plain is not None and directed is not None
    assert plain.matches(directed)


def test_two_directions_of_the_same_name_are_different_streets() -> None:
    east, west = street("E 6TH ST"), street("W 6th Street")
    assert east is not None and west is not None
    assert not east.matches(west)


@pytest.mark.parametrize(
    "text",
    [
        "W 6TH ST / GUADALUPE ST",
        "W 6TH ST & GUADALUPE ST",
        "W 6TH ST AT GUADALUPE ST",
        "W 6TH ST AND GUADALUPE ST",
        "W 6TH ST @ GUADALUPE ST",
        # A note in parentheses is not part of either name.
        "W 6TH ST / GUADALUPE ST (NB LANES BLKD)",
    ],
)
def test_every_separator_dispatch_uses_splits_a_cross_street_pair(text: str) -> None:
    found = cross_streets(text)
    assert [item.base for item in found] == ["6th street", "guadalupe street"]


def test_one_street_is_a_stretch_of_road_rather_than_a_corner() -> None:
    assert len(cross_streets("910-919 W Cesar Chavez St")) == 1


# --- the index -------------------------------------------------------------


def test_the_grid_index_finds_the_junction_a_pair_names() -> None:
    """`N 1 AVE / W 2 ST` is one intersection of the fixture grid."""
    index = IntersectionIndex(grid_graph())
    node = index.node_for("N 1 AVE / W 2 ST")
    assert node is not None
    lon, lat = index.locate("N 1 AVE / W 2 ST")  # type: ignore[misc]
    expected_lon, expected_lat = at(1, 2)
    assert lon == pytest.approx(expected_lon, abs=1e-6)
    assert lat == pytest.approx(expected_lat, abs=1e-6)


def test_the_index_holds_only_nodes_with_two_street_names() -> None:
    """A mid-block node is not an intersection anybody names."""
    index = IntersectionIndex(grid_graph())
    assert index.junctions == grid_graph().node_count
    assert len(index.streets_at(index.node_for("N 1 AVE / W 2 ST") or 0)) >= 2


def test_abbreviations_and_case_do_not_matter_to_the_index() -> None:
    index = IntersectionIndex(grid_graph())
    assert index.node_for("n 1 avenue / w 2 street") == index.node_for("N 1 AVE / W 2 ST")


def test_a_pair_the_graph_has_no_junction_for_is_not_located() -> None:
    index = IntersectionIndex(grid_graph())
    # Two avenues that never meet, and a street the grid does not have.
    assert index.node_for("N 1 AVE / N 2 AVE") is None
    assert index.node_for("CONGRESS AVE / W 2 ST") is None
    assert index.locate("CONGRESS AVE / W 2 ST") is None


def test_a_single_street_is_not_located() -> None:
    index = IntersectionIndex(grid_graph())
    assert index.node_for("W 2 ST") is None


def test_the_index_is_built_once_per_graph() -> None:
    """The sentinel asks per record; the parse of the graph happens once."""
    assert index_for(grid_graph()) is index_for(grid_graph())
