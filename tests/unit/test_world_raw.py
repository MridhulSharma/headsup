"""Raw Overpass responses: recorded, replayed, and refused when missing.

No test here goes near Overpass. The seam the recorder and the replayer stand
on is `osmnx._overpass._overpass_request`, so a stub is put there first and
the recorder wraps *that* - which is exactly what happens in a real build when
OSMnx answers from its own HTTP cache.
"""

from __future__ import annotations

import json

import pytest

from headsup.world import raw
from headsup.world.bbox import BBox, box
from headsup.world.graph import WorldError

AUSTIN = box("austin")

#: What a stubbed Overpass returns: the shape OSMnx expects, not the content.
NETWORK_RESPONSE = {"elements": [{"type": "node", "id": 1, "lat": 30.27, "lon": -97.74}]}
FEATURES_RESPONSE = {"elements": [{"type": "node", "id": 2, "tags": {"amenity": "hospital"}}]}


@pytest.fixture
def seam(monkeypatch):
    """A stubbed Overpass seam, and the queries it was asked for."""
    import osmnx._overpass as op

    asked: list[str] = []

    def stub(data):
        asked.append(str(data.get("data", "")))
        return NETWORK_RESPONSE if "highway" in asked[-1] else FEATURES_RESPONSE

    monkeypatch.setattr(op, "_overpass_request", stub)
    return asked


def request(query: str) -> dict:
    """Ask the seam the way OSMnx asks it."""
    import osmnx._overpass as op

    return op._overpass_request({"data": query})


# --- where it lives --------------------------------------------------------


def test_raw_responses_live_under_the_citys_own_directory(tmp_path) -> None:
    assert raw.raw_dir("austin", tmp_path) == tmp_path / "austin" / "raw"


def test_the_cache_directory_comes_from_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HU_WORLD_CACHE", str(tmp_path))
    assert raw.raw_dir("austin") == tmp_path / "austin" / "raw"


def test_a_city_with_nothing_recorded_has_no_raw(tmp_path) -> None:
    assert not raw.has_raw("austin", tmp_path)
    assert not raw.archive("austin", tmp_path).complete
    assert "no raw Overpass responses" in str(raw.archive("austin", tmp_path))


# --- recording -------------------------------------------------------------


def test_recording_writes_one_file_per_response(tmp_path, seam) -> None:
    directory = tmp_path / "raw"
    with raw.record(directory, raw.PHASE_NETWORK) as written:
        request("[out:json];way[highway];out;")
        request("[out:json];way[highway];out meta;")
    assert len(written) == 2
    assert [path.name for path in written] == ["network_001.json", "network_002.json"]
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["phase"] == raw.PHASE_NETWORK
    assert payload["response"] == NETWORK_RESPONSE
    assert payload["query"].startswith("[out:json]")
    assert payload["recorded_at"]


def test_recording_passes_the_response_through_unchanged(tmp_path, seam) -> None:
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        assert request("[out:json];way[highway];out;") == NETWORK_RESPONSE


def test_the_seam_is_put_back_afterwards(tmp_path, seam) -> None:
    import osmnx._overpass as op

    before = op._overpass_request
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        assert op._overpass_request is not before
    assert op._overpass_request is before


def test_the_seam_is_put_back_even_when_the_build_fails(tmp_path, seam) -> None:
    import osmnx._overpass as op

    before = op._overpass_request
    with pytest.raises(RuntimeError), raw.record(tmp_path, raw.PHASE_NETWORK):
        raise RuntimeError("simplification blew up")
    assert op._overpass_request is before


def test_a_rebuild_replaces_a_citys_raw_set(tmp_path, seam) -> None:
    """Two recordings must not interleave into one unusable set."""
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("one[highway]")
        request("two[highway]")
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("three[highway]")
    assert len(raw.response_files(tmp_path, raw.PHASE_NETWORK)) == 1
    assert raw.load_responses(tmp_path, raw.PHASE_NETWORK)[0]["query"] == "three[highway]"


def test_the_two_phases_are_recorded_side_by_side(tmp_path, seam) -> None:
    """Re-recording one phase leaves the other one alone."""
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("[out:json];way[highway];out;")
    with raw.record(tmp_path, raw.PHASE_FEATURES):
        request("[out:json];nwr[amenity=hospital];out;")
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("[out:json];way[highway];out meta;")
    assert len(raw.response_files(tmp_path, raw.PHASE_NETWORK)) == 1
    assert len(raw.response_files(tmp_path, raw.PHASE_FEATURES)) == 1
    assert raw.load_responses(tmp_path, raw.PHASE_FEATURES)[0]["response"] == FEATURES_RESPONSE


def test_a_city_needs_both_phases_to_rebuild_offline(tmp_path, seam) -> None:
    """Roads with no POI response would rebuild a world with nothing guarded."""
    directory = raw.raw_dir("austin", tmp_path)
    with raw.record(directory, raw.PHASE_NETWORK):
        request("[out:json];way[highway];out;")
    assert not raw.has_raw("austin", tmp_path)
    with raw.record(directory, raw.PHASE_FEATURES):
        request("[out:json];nwr[amenity=hospital];out;")
    assert raw.has_raw("austin", tmp_path)
    assert raw.archive("austin", tmp_path).complete


# --- replaying -------------------------------------------------------------


def test_replaying_serves_the_recorded_response(tmp_path, seam) -> None:
    query = "[out:json];way[highway];out;"
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request(query)
    with raw.replay(tmp_path, raw.PHASE_NETWORK) as served:
        assert request(query) == NETWORK_RESPONSE
    assert served == [query]


def test_replaying_matches_on_the_query_text(tmp_path, seam) -> None:
    """OSMnx splits a big bbox into several requests; order is not enough."""
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("first[highway]")
        request("second[highway]")
    saved = raw.load_responses(tmp_path, raw.PHASE_NETWORK)
    # Make the two responses tell each other apart.
    for index, record in enumerate(saved):
        path = raw.response_files(tmp_path, raw.PHASE_NETWORK)[index]
        record["response"] = {"elements": [{"id": index}]}
        path.write_text(json.dumps(record), encoding="utf-8")

    with raw.replay(tmp_path, raw.PHASE_NETWORK):
        assert request("second[highway]") == {"elements": [{"id": 1}]}
        assert request("first[highway]") == {"elements": [{"id": 0}]}


def test_replaying_falls_back_to_order_for_an_unrecognised_query(tmp_path, seam) -> None:
    """A query OSMnx phrases differently still gets the response in order."""
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("recorded[highway]")
    with raw.replay(tmp_path, raw.PHASE_NETWORK):
        assert request("phrased differently") == NETWORK_RESPONSE


def test_replaying_an_empty_directory_says_to_build_online_first(tmp_path) -> None:
    with (
        pytest.raises(WorldError, match="run `hu world build`"),
        raw.replay(tmp_path, raw.PHASE_NETWORK),
    ):
        pass


def test_asking_for_more_than_was_recorded_is_an_error_not_an_empty_city(tmp_path, seam) -> None:
    """Silently returning nothing would rebuild a city missing its roads."""
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("only[highway]")
    with pytest.raises(WorldError, match="not in"), raw.replay(tmp_path, raw.PHASE_NETWORK):
        request("a[highway]")
        request("b[highway]")


def test_replaying_puts_the_seam_back(tmp_path, seam) -> None:
    import osmnx._overpass as op

    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("one[highway]")
    before = op._overpass_request
    with raw.replay(tmp_path, raw.PHASE_NETWORK):
        pass
    assert op._overpass_request is before


def test_a_corrupt_recording_is_refused(tmp_path) -> None:
    (tmp_path / "network_001.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(WorldError, match="cannot read"):
        raw.load_responses(tmp_path, raw.PHASE_NETWORK)


def test_a_file_that_is_not_a_recording_is_refused(tmp_path) -> None:
    (tmp_path / "network_001.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")
    with pytest.raises(WorldError, match="not a recorded Overpass response"):
        raw.load_responses(tmp_path, raw.PHASE_NETWORK)


# --- the manifest ----------------------------------------------------------


def test_the_manifest_records_what_the_responses_are_of(tmp_path, seam) -> None:
    with raw.record(tmp_path, raw.PHASE_NETWORK):
        request("[out:json];way[highway];out;")
    raw.write_manifest(
        tmp_path,
        city="austin",
        bbox=AUSTIN,
        network_type="drive",
        tags={"amenity": ["hospital"]},
    )
    stored = raw.read_manifest(tmp_path)
    assert stored["city"] == "austin"
    assert tuple(stored["bbox"]) == AUSTIN.as_tuple
    assert stored["network_type"] == "drive"
    assert stored["osmnx"]
    assert stored["responses"][raw.PHASE_NETWORK] == 1


def test_no_manifest_is_an_empty_dict_not_an_error(tmp_path) -> None:
    assert raw.read_manifest(tmp_path) == {}
    assert raw.check_manifest(tmp_path, AUSTIN) is None


def test_a_manifest_for_another_bbox_is_reported(tmp_path) -> None:
    raw.write_manifest(
        tmp_path,
        city="austin",
        bbox=BBox(west=-98.0, south=29.0, east=-97.9, north=29.1),
        network_type="drive",
        tags={},
    )
    note = raw.check_manifest(tmp_path, AUSTIN)
    assert note is not None
    assert "recorded for bbox" in note


def test_a_manifest_for_this_bbox_has_nothing_to_report(tmp_path) -> None:
    raw.write_manifest(tmp_path, city="austin", bbox=AUSTIN, network_type="drive", tags={})
    assert raw.check_manifest(tmp_path, AUSTIN) is None


def test_a_corrupt_manifest_does_not_stop_a_rebuild(tmp_path) -> None:
    (tmp_path / raw.MANIFEST).write_text("{not json", encoding="utf-8")
    assert raw.read_manifest(tmp_path) == {}


# --- the offline build -----------------------------------------------------


def test_an_offline_build_without_raw_responses_says_what_to_run(tmp_path) -> None:
    from headsup.world import osm

    with pytest.raises(WorldError, match="run `hu world build`"):
        osm.build("austin", cache_dir=tmp_path, offline=True)


def test_an_offline_build_does_not_wait_on_a_rate_limit(tmp_path) -> None:
    """Nothing is being asked of a server, so there is nothing to pause for."""
    import osmnx as ox

    from headsup.world import osm

    osm.configure(tmp_path, offline=True)
    assert ox.settings.overpass_rate_limit is False
    osm.configure(tmp_path, offline=False)
    assert ox.settings.overpass_rate_limit is True


def test_the_seam_the_whole_scheme_rests_on_is_still_there() -> None:
    """If a future OSMnx moves it, fail here rather than mid-build."""
    module = raw._seam()
    assert callable(getattr(module, raw._SEAM_NAME))
