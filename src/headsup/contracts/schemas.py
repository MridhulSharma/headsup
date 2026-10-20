"""JSON Schema export for every contract, one file per model.

`hu schemas export` writes `schemas/<model>.schema.json`; the golden-file test
in tests/contract re-exports into a temp directory and compares byte for byte,
so a contract can only change when the commit says it changed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from headsup.contracts.advisories import (
    Action,
    Advisory,
    AdvisoryProperties,
    AppliesTo,
    CriticVerdict,
    ProposedAdvisory,
)
from headsup.contracts.base import HeadsUpModel
from headsup.contracts.events import CandidateHazard, CivicEvent, Evidence, Hazard
from headsup.contracts.fleet import (
    Ack,
    AssistRequest,
    KeepClear,
    MoveOrder,
    Telemetry,
    VehicleState,
)
from headsup.contracts.geometry import Geometry

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_BASE = "https://headsup.local/schemas"
SCHEMA_SUFFIX = ".schema.json"

#: Models that travel the bus as a message of their own.
MESSAGE_MODELS: tuple[type[HeadsUpModel], ...] = (
    CivicEvent,
    CandidateHazard,
    Hazard,
    ProposedAdvisory,
    CriticVerdict,
    Advisory,
    Ack,
    Telemetry,
    AssistRequest,
    MoveOrder,
    KeepClear,
)

#: Models that only ever appear nested inside a message, exported so a
#: third-party reader can resolve them on their own.
COMPONENT_MODELS: tuple[type[HeadsUpModel], ...] = (
    Evidence,
    AppliesTo,
    Action,
    AdvisoryProperties,
    VehicleState,
)

EXPORTED_MODELS: tuple[type[HeadsUpModel], ...] = MESSAGE_MODELS + COMPONENT_MODELS

#: Name of the extra schema for the GeoJSON geometry union.
GEOMETRY_SCHEMA_NAME = "geometry"

#: Message type name -> model, used by the bus to parse what it reads.
MODEL_BY_NAME: dict[str, type[HeadsUpModel]] = {model.__name__: model for model in EXPORTED_MODELS}


def schema_name(model: type[HeadsUpModel]) -> str:
    """`CivicEvent` -> `civic_event`."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", model.__name__).lower()


def _wrap(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$id": f"{SCHEMA_ID_BASE}/{name}{SCHEMA_SUFFIX}",
        **schema,
    }


def model_schema(model: type[HeadsUpModel]) -> dict[str, Any]:
    """JSON Schema for one model, describing its wire form."""
    schema = model.model_json_schema(by_alias=True, mode="serialization")
    return _wrap(schema_name(model), schema)


def geometry_schema() -> dict[str, Any]:
    """JSON Schema for the GeoJSON geometry union every contract shares."""
    schema = TypeAdapter(Geometry).json_schema(by_alias=True, mode="serialization")
    schema.setdefault("title", "Geometry")
    return _wrap(GEOMETRY_SCHEMA_NAME, schema)


def iter_schemas() -> Iterator[tuple[str, dict[str, Any]]]:
    """Every (file stem, schema) pair `hu schemas export` writes."""
    for model in EXPORTED_MODELS:
        yield schema_name(model), model_schema(model)
    yield GEOMETRY_SCHEMA_NAME, geometry_schema()


def schema_filenames() -> list[str]:
    """The file names an export produces, in export order."""
    return [f"{name}{SCHEMA_SUFFIX}" for name, _ in iter_schemas()]


def _render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def export(out_dir: Path | str) -> list[Path]:
    """Write one schema file per model into `out_dir`, returning the paths."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in iter_schemas():
        path = target / f"{name}{SCHEMA_SUFFIX}"
        path.write_text(_render(schema), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def check(out_dir: Path | str) -> list[str]:
    """Names of the schema files in `out_dir` that are missing or out of date."""
    target = Path(out_dir)
    drift: list[str] = []
    for name, schema in iter_schemas():
        path = target / f"{name}{SCHEMA_SUFFIX}"
        if not path.is_file():
            drift.append(f"{path.name}: missing")
        elif path.read_text(encoding="utf-8") != _render(schema):
            drift.append(f"{path.name}: out of date")
    expected = set(schema_filenames())
    for path in sorted(target.glob(f"*{SCHEMA_SUFFIX}")):
        if path.name not in expected:
            drift.append(f"{path.name}: no model exports this file any more")
    return drift
