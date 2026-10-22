"""`hu world` without a network: info, pois and edges over a fake cache.

The fixture grid is copied into a tmp cache under Austin's name, which is what
`hu world build` would have left there. `build` itself is not tested here - it
downloads a city, and a unit run does not.
"""

from __future__ import annotations

import shutil

from typer.testing import CliRunner

from fixtures.world import GRAPH_FILE, at, grid_world
from headsup.cli import app
from headsup.world import graph_path, pois_path, save_pois

runner = CliRunner()


def flat(output: str) -> str:
    """Rich wraps to the console width, so match on unwrapped text."""
    return " ".join(output.split())


def cache(tmp_path, city: str = "austin"):
    """A cache directory holding the fixture grid under a real city's name."""
    shutil.copy(GRAPH_FILE, graph_path(city, tmp_path))
    save_pois(pois_path(city, tmp_path), city, grid_world().pois)
    return str(tmp_path)


def test_world_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["world", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("build", "info", "pois", "edges"):
        assert command in result.output


def test_build_help_names_both_cities() -> None:
    result = runner.invoke(app, ["world", "build", "--help"])
    assert result.exit_code == 0, result.output
    assert "austin" in result.output
    assert "san_antonio" in result.output


def test_an_unknown_city_is_refused_before_anything_downloads() -> None:
    result = runner.invoke(app, ["world", "build", "--city", "dallas", "--no-db"])
    assert result.exit_code == 2, result.output
    assert "unknown city" in result.output or "no bbox for city" in result.output


def test_build_help_explains_offline(tmp_path) -> None:
    result = runner.invoke(app, ["world", "build", "--help"])
    assert result.exit_code == 0, result.output
    assert "--offline" in result.output


def test_an_offline_build_with_no_raw_responses_says_what_to_run(tmp_path) -> None:
    """The only build worth a time budget, and it needs one online build first."""
    result = runner.invoke(
        app,
        ["world", "build", "--city", "austin", "--offline", "--no-db", "--cache", str(tmp_path)],
    )
    assert result.exit_code == 1, result.output
    assert "hu world build" in result.output


def test_info_reports_whether_a_city_can_be_rebuilt_offline(tmp_path) -> None:
    where = cache(tmp_path)
    result = runner.invoke(app, ["world", "info", "--city", "austin", "--cache", where])
    assert result.exit_code == 0, result.output
    assert "no raw Overpass responses" in flat(result.output)
    assert "no offline rebuild until" in flat(result.output)


def test_info_counts_the_raw_responses_a_city_has(tmp_path) -> None:
    from headsup.world import raw

    where = cache(tmp_path)
    directory = raw.raw_dir("austin", tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    for phase in raw.PHASES:
        (directory / f"{phase}_001.json").write_text(
            '{"phase": "x", "query": "q", "response": {"elements": []}}', encoding="utf-8"
        )
    result = runner.invoke(app, ["world", "info", "--city", "austin", "--cache", where])
    assert result.exit_code == 0, result.output
    assert "2 raw Overpass responses" in flat(result.output)
    assert "no offline rebuild until" not in flat(result.output)


def test_info_prints_the_graph_and_the_checker(tmp_path) -> None:
    result = runner.invoke(app, ["world", "info", "--city", "austin", "--cache", cache(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "48 edges" in result.output
    assert "world-poi-checker" in result.output
    assert "3 POIs" in result.output


def test_info_on_an_unbuilt_city_says_how_to_build_it(tmp_path) -> None:
    result = runner.invoke(
        app, ["world", "info", "--city", "san_antonio", "--cache", str(tmp_path)]
    )
    assert result.exit_code == 1, result.output
    assert "hu world build" in result.output


def test_pois_lists_the_access_edge_count(tmp_path) -> None:
    result = runner.invoke(app, ["world", "pois", "--city", "austin", "--cache", cache(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Grid General" in result.output
    assert "HOSPITAL" in result.output


def test_pois_can_be_filtered_to_one_kind(tmp_path) -> None:
    where = cache(tmp_path)
    result = runner.invoke(
        app, ["world", "pois", "--city", "austin", "--kind", "school", "--cache", where]
    )
    assert result.exit_code == 0, result.output
    assert "Grid Elementary" in result.output
    assert "Grid General" not in result.output


def test_pois_can_be_filtered_to_the_critical_kinds(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["world", "pois", "--city", "austin", "--critical", "--cache", cache(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "2 POIs" in result.output


def test_an_unknown_poi_kind_lists_the_known_ones(tmp_path) -> None:
    result = runner.invoke(
        app, ["world", "pois", "--city", "austin", "--kind", "castle", "--cache", cache(tmp_path)]
    )
    assert result.exit_code == 2, result.output
    assert "HOSPITAL" in result.output


def test_edges_counts_the_h3_pass_and_the_exact_answer(tmp_path) -> None:
    west, south = at(1.2, 0.98)
    east, north = at(1.8, 1.02)
    result = runner.invoke(
        app,
        [
            "world",
            "edges",
            "--city",
            "austin",
            "--bbox",
            f"{west},{south},{east},{north}",
            "--cache",
            cache(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "h3 candidates" in result.output
    assert "2 edges" in result.output
    assert "W 1 St" in result.output


def test_edges_reads_a_geojson_geometry_from_a_file(tmp_path) -> None:
    import json

    from fixtures.world import advisory_box

    where = cache(tmp_path)
    target = tmp_path / "geometry.json"
    target.write_text(json.dumps(advisory_box(0.98, 0.98, 2.02, 2.02).to_wire()), encoding="utf-8")
    result = runner.invoke(
        app, ["world", "edges", "--city", "austin", "--file", str(target), "--cache", where]
    )
    assert result.exit_code == 0, result.output
    assert "24 edges" in result.output


def test_edges_reads_the_geometry_out_of_an_advisory(tmp_path) -> None:
    """So a person can ask what a signed advisory actually covers."""
    import json

    from fixtures.world import advisory_box

    where = cache(tmp_path)
    target = tmp_path / "advisory.json"
    target.write_text(
        json.dumps(
            {
                "type": "Feature",
                "geometry": advisory_box(0.98, 0.98, 2.02, 2.02).to_wire(),
                "properties": {},
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app, ["world", "edges", "--city", "austin", "--file", str(target), "--cache", where]
    )
    assert result.exit_code == 0, result.output
    assert "24 edges" in result.output


def test_edges_needs_a_shape_to_query_with(tmp_path) -> None:
    result = runner.invoke(app, ["world", "edges", "--city", "austin", "--cache", cache(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "--bbox" in result.output


def test_edges_refuses_both_a_bbox_and_a_file(tmp_path) -> None:
    where = cache(tmp_path)
    result = runner.invoke(
        app,
        [
            "world",
            "edges",
            "--city",
            "austin",
            "--bbox",
            "-97.74,30.266,-97.73,30.267",
            "--file",
            "x.json",
            "--cache",
            where,
        ],
    )
    assert result.exit_code == 2, result.output
    assert "not both" in result.output


def test_a_malformed_bbox_is_refused(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["world", "edges", "--city", "austin", "--bbox", "1,2,3", "--cache", cache(tmp_path)],
    )
    assert result.exit_code == 2, result.output
    assert "west,south,east,north" in result.output


def test_a_bbox_that_is_not_numbers_is_refused(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["world", "edges", "--city", "austin", "--bbox", "a,b,c,d", "--cache", cache(tmp_path)],
    )
    assert result.exit_code == 2, result.output


def test_a_missing_geometry_file_is_refused(tmp_path) -> None:
    result = runner.invoke(
        app,
        [
            "world",
            "edges",
            "--city",
            "austin",
            "--file",
            str(tmp_path / "nope.json"),
            "--cache",
            cache(tmp_path),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "cannot read" in result.output
