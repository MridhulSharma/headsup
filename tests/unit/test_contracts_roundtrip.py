"""Every contract model survives a trip through JSON unchanged."""

from __future__ import annotations

import orjson
import pytest

from headsup.contracts import EXPORTED_MODELS, GEOMETRY_TYPES, MODEL_BY_NAME, HeadsUpModel
from samples import GEOMETRY_SAMPLES, SAMPLES


def test_every_exported_model_has_a_sample() -> None:
    """A new contract without a sample would never be round-trip tested."""
    assert {model.__name__ for model in EXPORTED_MODELS} == set(SAMPLES)


def test_every_geometry_type_has_a_sample() -> None:
    assert {geometry.__name__ for geometry in GEOMETRY_TYPES} == set(GEOMETRY_SAMPLES)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_model_round_trips_through_json(name: str) -> None:
    sample = SAMPLES[name]
    model = type(sample)
    restored = model.model_validate_json(sample.to_json())
    assert restored == sample


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_model_round_trips_through_the_wire_dict(name: str) -> None:
    sample = SAMPLES[name]
    model = type(sample)
    restored = model.model_validate(sample.to_wire())
    assert restored == sample
    assert restored.to_wire() == sample.to_wire()


@pytest.mark.parametrize("name", sorted(GEOMETRY_SAMPLES))
def test_geometry_round_trips(name: str) -> None:
    sample = GEOMETRY_SAMPLES[name]
    model = type(sample)
    assert model.model_validate_json(sample.to_json()) == sample


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_canonical_json_is_byte_stable_and_sorted(name: str) -> None:
    """The compiler signs these bytes, so they must not depend on field order."""
    sample = SAMPLES[name]
    first = sample.canonical_json()
    assert first == sample.canonical_json()
    assert first == orjson.dumps(orjson.loads(first), option=orjson.OPT_SORT_KEYS)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_model_refuses_unknown_fields(name: str) -> None:
    """extra="forbid" everywhere: a typo in a producer fails loudly, not silently."""
    payload = {**SAMPLES[name].to_wire(), "surprise": 1}
    with pytest.raises(ValueError, match="surprise"):
        type(SAMPLES[name]).model_validate(payload)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_model_by_name_resolves_the_sample(name: str) -> None:
    """What the bus does with an entry's `type` field."""
    model = MODEL_BY_NAME[name]
    assert issubclass(model, HeadsUpModel)
    assert model.model_validate(SAMPLES[name].to_wire()) == SAMPLES[name]
