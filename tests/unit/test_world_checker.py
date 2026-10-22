"""The real critical-access rule, on the fixture grid.

The D1.3 stub answered from POI coordinates; this answers from the road graph,
and the test that matters is the last one: the same compiler, the same
proposal, now refused because every street into the hospital's block is under
the polygon.
"""

from __future__ import annotations

import pytest

from fixtures.compiler import approve, clock, proposal
from fixtures.world import (
    FIRE_STATION,
    HOSPITAL,
    SCHOOL,
    advisory_box,
    grid_graph,
    grid_world,
)
from headsup.agents.compiler import (
    BLOCKING_ACTIONS,
    Compiler,
    NullPoiChecker,
    PoiChecker,
    PoiKind,
    RuleCode,
    verify,
)
from headsup.contracts import ActionType
from headsup.world import World, WorldPoiChecker
from headsup.world.checker import default_poi_checker

#: Over the hospital's whole block: every street out of its four corners has
#: an end inside, so every way in is closed.
SEALED = advisory_box(0.98, 0.98, 2.02, 2.02)

#: The northern half of the same block: the two northern corners are inside,
#: the two southern ones are not, so half the ways in stay open.
HALF_OPEN = advisory_box(0.98, 1.5, 2.02, 2.02)

#: The school's block, two blocks north-west, sealed the same way.
SCHOOL_BLOCK = advisory_box(-0.02, 1.98, 1.02, 3.02)


def checker() -> WorldPoiChecker:
    return grid_world().checker()


def test_the_world_checker_satisfies_the_compilers_protocol() -> None:
    assert isinstance(checker(), PoiChecker)


def test_closing_every_street_into_the_block_blocks_the_hospital() -> None:
    blocked = checker().blocked_access(SEALED, ActionType.NO_ENTRY)
    by_id = {item.poi_id: item for item in blocked}
    assert HOSPITAL in by_id
    hospital = by_id[HOSPITAL]
    assert hospital.access_total == hospital.access_blocked == 16
    assert hospital.kind is PoiKind.HOSPITAL
    assert "Grid General" in str(hospital)


def test_the_fire_station_on_the_same_block_is_blocked_too() -> None:
    blocked = {item.poi_id for item in checker().blocked_access(SEALED, ActionType.NO_ENTRY)}
    assert blocked == {HOSPITAL, FIRE_STATION}


def test_leaving_half_the_streets_open_blocks_nothing() -> None:
    assert checker().blocked_access(HALF_OPEN, ActionType.NO_ENTRY) == ()


@pytest.mark.parametrize(
    "action",
    [action for action in ActionType if action not in BLOCKING_ACTIONS],
)
def test_an_action_that_does_not_stop_a_vehicle_blocks_nothing(action) -> None:
    """A speed cap slows traffic to a hospital; it does not cut it off."""
    assert checker().blocked_access(SEALED, action) == ()


def test_pause_service_is_treated_as_a_closure() -> None:
    assert checker().blocked_access(SEALED, ActionType.PAUSE_SERVICE)


def test_sealing_a_school_is_allowed() -> None:
    """Every way into the school is closed, and that is not this rule's business."""
    world = grid_world()
    school_access = world.poi(SCHOOL).access_edge_ids
    closed = set(world.graph.edge_ids_in_polygon(SCHOOL_BLOCK))
    assert set(school_access) <= closed
    assert world.checker().blocked_access(SCHOOL_BLOCK, ActionType.NO_ENTRY) == ()


def test_a_polygon_over_no_roads_at_all_blocks_nothing() -> None:
    assert checker().blocked_access(advisory_box(10, 10, 11, 11), ActionType.NO_ENTRY) == ()


def test_a_world_with_no_critical_pois_blocks_nothing() -> None:
    world = grid_world()
    only_school = World(
        city=world.city,
        graph=world.graph,
        pois=(world.poi(SCHOOL),),
        bbox=world.bbox,
    )
    assert WorldPoiChecker(only_school).blocked_access(SEALED, ActionType.NO_ENTRY) == ()


def test_a_poi_the_graph_does_not_reach_is_reported_not_silently_cleared() -> None:
    """An unreachable POI cannot be judged, and the audit line has to say so."""
    world = grid_world()
    stranded = world.poi(HOSPITAL).with_access(())
    checker_ = WorldPoiChecker(
        World(city=world.city, graph=world.graph, pois=(stranded,), bbox=world.bbox)
    )
    assert checker_.blocked_access(SEALED, ActionType.NO_ENTRY) == ()
    assert "1 not on the graph" in checker_.describes_itself()


def test_the_audit_line_names_the_graph_that_answered() -> None:
    line = checker().describes_itself()
    assert "world-poi-checker" in line
    assert "2 critical POIs" in line
    assert f"{grid_graph().edge_count} edges" in line
    assert "h3 r10" in line


# --- the default checker ---------------------------------------------------


def test_a_loaded_world_is_parsed_once_per_process(tmp_path) -> None:
    """A compiler judging a stream of proposals must not reparse the graph."""
    import shutil

    from fixtures.world import GRAPH_FILE
    from headsup.world import graph_path, pois_path, save_pois
    from headsup.world.model import clear_world_cache, load_world

    shutil.copy(GRAPH_FILE, graph_path("austin", tmp_path))
    save_pois(pois_path("austin", tmp_path), "austin", grid_world().pois)
    clear_world_cache()
    try:
        first = load_world("austin", cache_dir=tmp_path)
        assert load_world("austin", cache_dir=tmp_path) is first
        assert default_poi_checker("austin", cache_dir=tmp_path).world is first
    finally:
        clear_world_cache()


def test_an_unbuilt_city_is_not_remembered_as_unbuilt(tmp_path) -> None:
    """A city built while the process runs has to be picked up next call."""
    import shutil

    from fixtures.world import GRAPH_FILE
    from headsup.world import graph_path, pois_path, save_pois
    from headsup.world.model import clear_world_cache

    clear_world_cache()
    try:
        assert isinstance(default_poi_checker("austin", cache_dir=tmp_path), NullPoiChecker)
        shutil.copy(GRAPH_FILE, graph_path("austin", tmp_path))
        save_pois(pois_path("austin", tmp_path), "austin", grid_world().pois)
        assert isinstance(default_poi_checker("austin", cache_dir=tmp_path), WorldPoiChecker)
    finally:
        clear_world_cache()


def test_an_unbuilt_city_falls_back_to_the_null_checker(tmp_path) -> None:
    """The compiler has to start before a world is built - and say it did."""
    fallback = default_poi_checker("austin", cache_dir=tmp_path)
    assert isinstance(fallback, NullPoiChecker)
    assert "unguarded" in fallback.describes_itself()
    assert "hu world build" in fallback.describes_itself()


def test_a_built_city_gives_the_real_checker(tmp_path) -> None:
    """A cache written by `hu world build` is what the compiler picks up."""
    import shutil

    from fixtures.world import GRAPH_FILE
    from headsup.world import graph_path, is_built, pois_path, save_pois

    shutil.copy(GRAPH_FILE, graph_path("austin", tmp_path))
    save_pois(pois_path("austin", tmp_path), "austin", grid_world().pois)
    assert is_built("austin", tmp_path)
    real = default_poi_checker("austin", cache_dir=tmp_path)
    assert isinstance(real, WorldPoiChecker)
    assert real.blocked_access(SEALED, ActionType.NO_ENTRY)


def test_a_poi_file_for_another_city_is_refused(tmp_path) -> None:
    import shutil

    from fixtures.world import GRAPH_FILE, POIS_FILE
    from headsup.world import graph_path, pois_path
    from headsup.world.graph import WorldError

    shutil.copy(GRAPH_FILE, graph_path("austin", tmp_path))
    shutil.copy(POIS_FILE, pois_path("austin", tmp_path))  # says city: fixture
    with pytest.raises(WorldError, match="holds 'fixture'"):
        World.load("austin", cache_dir=tmp_path)


def test_an_unbuilt_city_says_what_is_missing(tmp_path) -> None:
    from headsup.world.graph import WorldError

    with pytest.raises(WorldError, match="hu world build"):
        World.load("san_antonio", cache_dir=tmp_path)


# --- through the compiler --------------------------------------------------


def compiler() -> Compiler:
    """The gate with the real POI checker behind it, on a fixed clock."""
    return Compiler(clock=clock(), poi=grid_world().checker())


def test_closing_all_hospital_access_is_rejected_with_real_pois() -> None:
    """D1.3's "closes all hospital access" test, now on the road graph."""
    result = compiler().compile(proposal(geometry=SEALED), approve())
    assert not result.ok
    assert RuleCode.CRITICAL_ACCESS_OPEN in tuple(
        check.code for check in result.checks if not check.ok
    )
    assert any("Grid General" in reason for reason in result.reasons)
    assert any("16 of 16 access edges closed" in reason for reason in result.reasons)


def test_leaving_a_way_in_open_is_published_and_verifies() -> None:
    result = compiler().compile(proposal(geometry=HALF_OPEN), approve())
    assert result.ok, result.reasons
    assert verify(result.advisory)


def test_a_speed_cap_over_the_sealed_block_is_not_a_closure() -> None:
    result = compiler().compile(
        proposal(geometry=SEALED, action_type=ActionType.SPEED_CAP), approve()
    )
    assert result.ok, result.reasons


def test_the_audit_record_names_the_world_checker() -> None:
    result = compiler().compile(proposal(geometry=HALF_OPEN), approve())
    assert result.ok, result.reasons
    passed = {check.code: check.detail for check in result.checks}
    assert "world-poi-checker" in passed[RuleCode.CRITICAL_ACCESS_OPEN]


def test_the_compiler_describes_which_checker_it_is_running() -> None:
    assert "world-poi-checker" in compiler().describe()
