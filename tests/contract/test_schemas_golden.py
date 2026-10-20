"""The committed schemas/ files are what the models produce.

If this fails, either the contract changed on purpose (run `hu schemas export`
and say so in the commit message) or a model changed by accident.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from headsup.contracts import (
    EXPORTED_MODELS,
    GEOMETRY_SCHEMA_NAME,
    SCHEMA_SUFFIX,
    check,
    export,
    iter_schemas,
    schema_filenames,
    schema_name,
)
from samples import SAMPLES

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


def test_schema_directory_is_committed() -> None:
    assert SCHEMA_DIR.is_dir(), "run `uv run hu schemas export`"


def test_one_file_per_model_plus_the_geometry_union() -> None:
    expected = {f"{schema_name(model)}{SCHEMA_SUFFIX}" for model in EXPORTED_MODELS}
    expected.add(f"{GEOMETRY_SCHEMA_NAME}{SCHEMA_SUFFIX}")
    on_disk = {path.name for path in SCHEMA_DIR.glob(f"*{SCHEMA_SUFFIX}")}
    assert on_disk == expected
    assert len(schema_filenames()) == len(expected)


def test_committed_schemas_match_the_models() -> None:
    assert check(SCHEMA_DIR) == []


def test_export_into_a_temp_dir_is_byte_identical(tmp_path: Path) -> None:
    for path in export(tmp_path):
        committed = SCHEMA_DIR / path.name
        assert path.read_bytes() == committed.read_bytes(), f"{path.name} drifted"


def test_export_is_deterministic(tmp_path: Path) -> None:
    first = {path.name: path.read_bytes() for path in export(tmp_path / "a")}
    second = {path.name: path.read_bytes() for path in export(tmp_path / "b")}
    assert first == second


def test_check_reports_a_missing_file(tmp_path: Path) -> None:
    export(tmp_path)
    (tmp_path / f"advisory{SCHEMA_SUFFIX}").unlink()
    assert any("advisory" in item and "missing" in item for item in check(tmp_path))


def test_check_reports_an_edited_file(tmp_path: Path) -> None:
    export(tmp_path)
    (tmp_path / f"hazard{SCHEMA_SUFFIX}").write_text("{}", encoding="utf-8")
    assert any("hazard" in item and "out of date" in item for item in check(tmp_path))


def test_check_reports_a_file_no_model_owns(tmp_path: Path) -> None:
    export(tmp_path)
    (tmp_path / f"ghost_fleet{SCHEMA_SUFFIX}").write_text("{}", encoding="utf-8")
    assert any("ghost_fleet" in item for item in check(tmp_path))


def test_schemas_are_json_with_a_dialect_and_an_id() -> None:
    for name, _ in iter_schemas():
        schema = json.loads((SCHEMA_DIR / f"{name}{SCHEMA_SUFFIX}").read_text(encoding="utf-8"))
        assert schema["$schema"].startswith("https://json-schema.org/")
        assert schema["$id"].endswith(f"{name}{SCHEMA_SUFFIX}")
        assert schema.get("type") or schema.get("oneOf") or schema.get("anyOf")


def test_schema_files_use_lf_endings() -> None:
    """CLAUDE.md: LF everywhere, including the files Windows regenerates."""
    for name, _ in iter_schemas():
        raw = (SCHEMA_DIR / f"{name}{SCHEMA_SUFFIX}").read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")


@pytest.mark.parametrize("model_name", sorted(SAMPLES))
def test_each_sample_satisfies_its_published_schema(model_name: str) -> None:
    """The schema and the model agree about required fields and aliases."""
    schema = json.loads(
        (SCHEMA_DIR / f"{schema_name(type(SAMPLES[model_name]))}{SCHEMA_SUFFIX}").read_text(
            encoding="utf-8"
        )
    )
    payload = SAMPLES[model_name].to_wire()
    for field in schema.get("required", []):
        assert field in payload, f"{model_name}: schema requires {field}"
    assert set(payload) <= set(schema["properties"])
