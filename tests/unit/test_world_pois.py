"""POIs: the OSM shapes they arrive in, and the access edges they carry.

`from_features` is fed frames shaped like OSMnx's, built by hand - the point
is to pin down what happens to a hospital mapped as a building outline, a
place with no name, and the same station mapped twice.
"""

from __future__ import annotations

import json

import pytest
from shapely.geometry import Point
from shapely.geometry import Polygon as ShapelyPolygon

from fixtures.world import FIRE_STATION, HOSPITAL, SCHOOL, at, grid_graph, grid_world
from headsup.world import OSM_TAGS, PoiKind, WorldPoi, box, load_pois, resolve_access, save_pois
from headsup.world.pois import (
    DUPLICATE_RADIUS_M,
    classify,
    counts,
    dedupe,
    from_features,
    merged_tags,
    poi_id,
    poi_rows,
)


class FakeFrame:
    """The two things `from_features` uses of an OSMnx features frame."""

    def __init__(self, rows: list[tuple[tuple[str, int], dict]]) -> None:
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    def sort_index(self) -> FakeFrame:
        return FakeFrame(sorted(self._rows, key=lambda item: item[0]))

    def iterrows(self):
        return iter(self._rows)


def row(name: str | None, geometry) -> dict:
    return {"name": name, "geometry": geometry}


# --- ids and tags ----------------------------------------------------------


def test_poi_ids_are_derived_from_the_osm_element_not_counted() -> None:
    """A rebuild has to give the same hospital the same id."""
    assert poi_id(PoiKind.HOSPITAL, "node", 12345) == "poi_hospital_n12345"
    assert poi_id(PoiKind.FIRE_STATION, "way", 7) == "poi_fire_station_w7"
    assert poi_id(PoiKind.EMS_STATION, "relation", 9) == "poi_ems_station_r9"
    assert poi_id(PoiKind.SCHOOL, "something-new", 9) == "poi_school_x9"


def test_every_poi_kind_has_osm_tags_to_download_it_with() -> None:
    assert set(OSM_TAGS) == set(PoiKind)
    assert OSM_TAGS[PoiKind.FIRE_STATION] == {"amenity": "fire_station"}


# --- one query for every kind ----------------------------------------------


def test_merged_tags_asks_for_every_kind_in_one_query() -> None:
    """Four queries were four trips through the Overpass queue."""
    merged = merged_tags()
    assert merged["amenity"] == [
        "ambulance_station",
        "fire_station",
        "hospital",
        "kindergarten",
        "school",
    ]
    assert merged["healthcare"] == ["hospital"]
    assert merged["emergency"] == ["ambulance_station"]


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"amenity": "hospital"}, PoiKind.HOSPITAL),
        ({"healthcare": "hospital"}, PoiKind.HOSPITAL),
        ({"amenity": "fire_station"}, PoiKind.FIRE_STATION),
        ({"emergency": "ambulance_station"}, PoiKind.EMS_STATION),
        ({"amenity": "ambulance_station"}, PoiKind.EMS_STATION),
        ({"amenity": "school"}, PoiKind.SCHOOL),
        ({"amenity": "kindergarten"}, PoiKind.SCHOOL),
        ({"amenity": "cafe"}, None),
        ({"amenity": None}, None),
        ({}, None),
    ],
)
def test_a_feature_is_classified_by_its_own_tags(tags, expected) -> None:
    assert classify(tags) == expected


def test_a_building_that_is_both_is_the_more_critical_kind() -> None:
    """A hospital with a school on the campus is a hospital."""
    assert classify({"amenity": "school", "healthcare": "hospital"}) is PoiKind.HOSPITAL


def test_a_mixed_frame_is_split_into_kinds() -> None:
    frame = FakeFrame(
        [
            (
                ("node", 1),
                {"name": "Grid General", "amenity": "hospital", "geometry": Point(*at(1, 1))},
            ),
            (
                ("node", 2),
                {"name": "Station 1", "amenity": "fire_station", "geometry": Point(*at(2, 1))},
            ),
            (
                ("node", 3),
                {"name": "Grid Elementary", "amenity": "school", "geometry": Point(*at(0, 2))},
            ),
            (
                ("node", 4),
                {"name": "Taco Place", "amenity": "restaurant", "geometry": Point(*at(1, 2))},
            ),
        ]
    )
    pois = from_features(None, frame)
    assert [poi.kind for poi in pois] == [
        PoiKind.HOSPITAL,
        PoiKind.FIRE_STATION,
        PoiKind.SCHOOL,
    ]
    assert [poi.id for poi in pois] == [
        "poi_hospital_n1",
        "poi_fire_station_n2",
        "poi_school_n3",
    ]


def test_a_pinned_kind_overrides_the_tags() -> None:
    """The per-kind path the tests use stays exact."""
    frame = FakeFrame(
        [(("node", 1), {"name": "Odd", "amenity": "school", "geometry": Point(*at(1, 1))})]
    )
    (poi,) = from_features(PoiKind.HOSPITAL, frame)
    assert poi.kind is PoiKind.HOSPITAL


# --- from_features ---------------------------------------------------------


def test_a_point_poi_keeps_its_coordinates() -> None:
    lon, lat = at(1.5, 1.5)
    frame = FakeFrame([(("node", 1), row("Grid General", Point(lon, lat)))])
    (poi,) = from_features(PoiKind.HOSPITAL, frame)
    assert (poi.lon, poi.lat) == (lon, lat)
    assert poi.osm_id == "node/1"
    assert poi.critical


def test_a_hospital_mapped_as_a_building_becomes_a_point_inside_it() -> None:
    outline = ShapelyPolygon([at(1.1, 1.1), at(1.9, 1.1), at(1.9, 1.9), at(1.1, 1.9), at(1.1, 1.1)])
    frame = FakeFrame([(("way", 2), row("Grid General", outline))])
    (poi,) = from_features(PoiKind.HOSPITAL, frame)
    assert Point(poi.lon, poi.lat).within(outline)


def test_an_unnamed_poi_is_kept_with_an_empty_name() -> None:
    """A fire station nobody named still has to be protected."""
    frame = FakeFrame([(("node", 3), row(None, Point(*at(1, 1))))])
    (poi,) = from_features(PoiKind.FIRE_STATION, frame)
    assert poi.name == ""
    assert poi.critical


def test_a_pandas_nan_name_does_not_become_the_string_nan() -> None:
    frame = FakeFrame([(("node", 4), row("nan", Point(*at(1, 1))))])
    (poi,) = from_features(PoiKind.SCHOOL, frame)
    assert poi.name == ""


def test_an_empty_or_missing_geometry_is_skipped() -> None:
    frame = FakeFrame(
        [
            (("node", 5), row("No geometry", None)),
            (("node", 6), row("Empty", ShapelyPolygon())),
            (("node", 7), row("Real", Point(*at(1, 1)))),
        ]
    )
    pois = from_features(PoiKind.SCHOOL, frame)
    assert [poi.osm_id for poi in pois] == ["node/7"]


def test_a_poi_outside_the_bbox_is_dropped() -> None:
    austin = box("austin")
    frame = FakeFrame(
        [
            (("node", 8), row("Inside", Point(-97.7431, 30.2672))),
            (("node", 9), row("Elsewhere", Point(-98.5, 29.5))),
        ]
    )
    pois = from_features(PoiKind.HOSPITAL, frame, within=austin)
    assert [poi.name for poi in pois] == ["Inside"]


def test_an_empty_frame_is_no_pois() -> None:
    assert from_features(PoiKind.HOSPITAL, FakeFrame([])) == ()
    assert from_features(PoiKind.HOSPITAL, None) == ()


def test_features_come_back_in_osm_element_order() -> None:
    frame = FakeFrame(
        [
            (("way", 20), row("Second", Point(*at(1, 1)))),
            (("node", 10), row("First", Point(*at(2, 2)))),
        ]
    )
    assert [poi.name for poi in from_features(PoiKind.SCHOOL, frame)] == ["First", "Second"]


# --- dedupe ----------------------------------------------------------------


def _poi(kind: PoiKind, name: str, east: float, north: float, osm: str = "node/1") -> WorldPoi:
    lon, lat = at(east, north)
    return WorldPoi(id=f"poi_{name}_{east}", kind=kind, name=name, osm_id=osm, lon=lon, lat=lat)


def test_the_same_place_mapped_twice_is_one_poi() -> None:
    both = [
        _poi(PoiKind.HOSPITAL, "Grid General", 1.5, 1.5, "node/1"),
        _poi(PoiKind.HOSPITAL, "Grid General", 1.6, 1.5, "way/2"),
    ]
    kept = dedupe(both)
    assert len(kept) == 1
    assert kept[0].osm_id == "node/1"


def test_two_unnamed_stations_stay_two_stations() -> None:
    """Merging on proximity alone would delete a building the rule protects."""
    both = [
        _poi(PoiKind.FIRE_STATION, "", 1.5, 1.5),
        _poi(PoiKind.FIRE_STATION, "", 1.51, 1.5),
    ]
    assert len(dedupe(both)) == 2


def test_a_same_named_place_far_away_is_a_different_place() -> None:
    far = DUPLICATE_RADIUS_M / 100.0 + 1.0  # blocks are 100 m
    both = [
        _poi(PoiKind.SCHOOL, "Grid Elementary", 0, 0),
        _poi(PoiKind.SCHOOL, "Grid Elementary", far, 0),
    ]
    assert len(dedupe(both)) == 2


def test_a_hospital_and_a_school_sharing_a_name_are_not_duplicates() -> None:
    both = [
        _poi(PoiKind.HOSPITAL, "Shared Campus", 1.5, 1.5),
        _poi(PoiKind.SCHOOL, "Shared Campus", 1.5, 1.5),
    ]
    assert len(dedupe(both)) == 2


# --- access edges ----------------------------------------------------------


def test_resolve_access_fills_in_the_ways_into_each_block() -> None:
    graph = grid_graph()
    bare = _poi(PoiKind.HOSPITAL, "Grid General", 1.5, 1.5)
    assert bare.access_edge_ids == ()
    (resolved,) = resolve_access([bare], graph)
    assert len(resolved.access_edge_ids) == 16
    assert resolved.access_edge_ids == tuple(sorted(set(resolved.access_edge_ids)))


def test_access_edges_point_at_real_edges() -> None:
    world = grid_world()
    graph = world.graph
    for poi in world.pois:
        for edge_id in poi.access_edge_ids:
            assert graph.edge(edge_id).id == edge_id


def test_the_school_is_not_counted_as_critical() -> None:
    world = grid_world()
    assert {poi.id for poi in world.critical_pois} == {HOSPITAL, FIRE_STATION}
    assert not world.poi(SCHOOL).critical


def test_counts_covers_every_kind_even_at_zero() -> None:
    tally = counts(grid_world().pois)
    assert set(tally) == set(PoiKind)
    assert tally[PoiKind.HOSPITAL] == 1
    assert tally[PoiKind.EMS_STATION] == 0


# --- persistence -----------------------------------------------------------


def test_a_poi_survives_a_round_trip_through_json(tmp_path) -> None:
    world = grid_world()
    target = save_pois(tmp_path / "pois.json", world.city, world.pois)
    city, back = load_pois(target)
    assert city == world.city
    assert back == world.pois


def test_a_poi_record_round_trips_through_a_dict() -> None:
    poi = grid_world().poi(HOSPITAL)
    assert WorldPoi.from_dict(poi.to_dict()) == poi


def test_a_broken_poi_record_is_refused_with_the_record_in_the_message() -> None:
    with pytest.raises(ValueError, match="not a usable POI record"):
        WorldPoi.from_dict({"id": "poi_x", "kind": "HOSPITAL"})
    with pytest.raises(ValueError, match="not a usable POI record"):
        WorldPoi.from_dict({"id": "poi_x", "kind": "CASTLE", "lon": 0, "lat": 0})


def test_a_poi_file_that_is_not_json_says_so(tmp_path) -> None:
    broken = tmp_path / "pois.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="is not JSON"):
        load_pois(broken)


def test_a_poi_file_that_is_not_an_object_is_refused(tmp_path) -> None:
    target = tmp_path / "pois.json"
    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        load_pois(target)


def test_a_missing_poi_file_says_which_one(tmp_path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        load_pois(tmp_path / "nope.json")


# --- rows for PostGIS ------------------------------------------------------


def test_poi_rows_carry_wkt_access_edges_and_the_critical_flag() -> None:
    world = grid_world()
    rows = {row["poi_id"]: row for row in poi_rows(world.city, world.pois)}
    assert rows[HOSPITAL]["wkt"].startswith("POINT (")
    assert rows[HOSPITAL]["critical"] is True
    assert rows[SCHOOL]["critical"] is False
    assert len(rows[HOSPITAL]["access_edge_ids"]) == 16
    assert rows[HOSPITAL]["kind"] == "HOSPITAL"


# --- the compiler's own POI shape -----------------------------------------


def test_a_world_poi_converts_to_the_compiler_poi_with_access_points() -> None:
    """So a geometry-only `StubPoiChecker` can be fed real data."""
    world = grid_world()
    poi = world.poi(HOSPITAL)
    converted = poi.to_compiler_poi(world.graph)
    assert converted.id == poi.id
    assert converted.critical
    assert len(converted.access_points) == len(poi.access_edge_ids)
    assert all(len(point) == 2 for point in converted.access_points)


def test_converting_without_a_graph_gives_no_access_points() -> None:
    converted = grid_world().poi(HOSPITAL).to_compiler_poi()
    assert converted.access_points == ()


def test_a_poi_describes_itself_for_a_cli_table() -> None:
    text = str(grid_world().poi(HOSPITAL))
    assert "HOSPITAL" in text and "Grid General" in text and "16 access edges" in text
