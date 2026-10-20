"""A published advisory is a GeoJSON Feature that a third party can read.

The fixture in tests/fixtures/advisory_s1_flood.json is written by hand to the
contract in docs/build-packet.md, not generated from the models, so it fails if
the models drift away from the packet.
"""

from __future__ import annotations

import json
from pathlib import Path

import orjson
import pytest

from headsup.contracts import Advisory, Polygon
from samples import ADVISORY

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "advisory_s1_flood.json"

#: Every field the packet names on an advisory.
CONTRACT_FIELDS = (
    "id",
    "version",
    "hazard",
    "action",
    "valid_from",
    "valid_until",
    "evidence",
    "policy_rule",
    "issuer",
    "signature",
)


@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def advisory(raw: dict) -> Advisory:
    return Advisory.model_validate(raw)


def test_the_sample_advisory_validates(advisory: Advisory) -> None:
    assert advisory.properties.id == "adv_s1_flood_0001"
    assert advisory.properties.issuer == "headsup/san_antonio"


def test_it_is_a_geojson_feature(advisory: Advisory, raw: dict) -> None:
    assert raw["type"] == "Feature"
    assert advisory.type == "Feature"
    assert advisory.id == advisory.properties.id
    assert isinstance(advisory.geometry, Polygon)


def test_the_properties_carry_every_contract_field(advisory: Advisory) -> None:
    properties = advisory.to_wire()["properties"]
    assert tuple(properties) == CONTRACT_FIELDS


def test_the_action_is_one_of_the_six(advisory: Advisory) -> None:
    assert advisory.action.type == "NO_ENTRY"
    assert advisory.action.applies_to.operators == ["*"]


def test_the_hazard_is_cited_and_classed(advisory: Advisory) -> None:
    assert advisory.hazard.hazard_class == "FLOOD"
    assert advisory.hazard.severity == 4
    assert {item.source for item in advisory.hazard.evidence} == {"nws", "usgs"}
    assert all(item.authoritative for item in advisory.properties.evidence)


def test_it_expires(advisory: Advisory) -> None:
    assert advisory.properties.valid_until > advisory.properties.valid_from
    assert advisory.is_active(advisory.properties.valid_from)
    assert not advisory.is_active(advisory.properties.valid_until)


def test_it_round_trips_without_losing_a_field(advisory: Advisory, raw: dict) -> None:
    again = Advisory.model_validate(json.loads(advisory.to_json()))
    assert again == advisory
    assert again.to_wire()["geometry"] == raw["geometry"]


def test_signing_bytes_cover_everything_but_the_signature(advisory: Advisory) -> None:
    payload = orjson.loads(advisory.signing_bytes())
    assert "signature" not in payload["properties"]
    assert payload["properties"]["policy_rule"] == "flood.no_entry.v1"
    assert payload["geometry"]["type"] == "Polygon"


def test_an_unsigned_advisory_is_still_a_valid_contract(raw: dict) -> None:
    """The compiler validates first and signs second, so `signature` is optional."""
    unsigned = {**raw, "properties": {**raw["properties"], "signature": None}}
    assert Advisory.model_validate(unsigned).properties.signature is None


def test_the_fixture_and_the_sample_agree_on_shape() -> None:
    fixture = Advisory.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert set(fixture.to_wire()) == set(ADVISORY.to_wire())
    assert set(fixture.to_wire()["properties"]) == set(ADVISORY.to_wire()["properties"])


def test_a_feature_with_no_properties_is_not_an_advisory() -> None:
    with pytest.raises(ValueError):
        Advisory.model_validate({"type": "Feature", "geometry": None, "properties": None})


def test_an_advisory_without_evidence_is_rejected(raw: dict) -> None:
    broken = {**raw, "properties": {**raw["properties"], "evidence": []}}
    with pytest.raises(ValueError):
        Advisory.model_validate(broken)
