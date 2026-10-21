"""Ed25519 signing: what the signature covers, and every way verify says no."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fixtures.compiler import approve, clock, proposal
from headsup.agents.compiler import (
    DEV_SEED,
    ENV_SIGNING_KEY,
    ENV_VERIFY_KEY,
    Compiler,
    Signer,
    SigningError,
    fingerprint,
    verify,
)
from samples import ADVISORY

DEV_KEY_B64 = Signer.dev().verify_key_b64


@pytest.fixture
def signed():
    """A freshly signed advisory from the fixed-clock compiler."""
    result = Compiler(clock=clock(), signer=Signer.dev()).compile(proposal(), approve())
    assert result.ok, result.reasons
    return result.advisory


# --- keys -------------------------------------------------------------------


def test_the_development_seed_is_a_usable_ed25519_seed() -> None:
    assert len(DEV_SEED) == 32
    assert Signer.dev().is_dev_key


def test_a_generated_key_is_not_the_development_key() -> None:
    assert not Signer.generate().is_dev_key


def test_a_seed_round_trips_through_base64() -> None:
    original = Signer.generate()
    restored = Signer.from_seed_b64(original.seed_b64)
    assert restored.verify_key_b64 == original.verify_key_b64
    assert restored.fingerprint == original.fingerprint


def test_a_fingerprint_names_the_algorithm_and_is_stable() -> None:
    signer = Signer.dev()
    assert signer.fingerprint.startswith("ed25519:")
    assert signer.fingerprint == fingerprint(signer.verify_key)


def test_a_malformed_seed_is_refused() -> None:
    with pytest.raises(SigningError, match="not valid base64"):
        Signer.from_seed_b64("not base64 at all!")


def test_a_short_seed_is_refused() -> None:
    with pytest.raises(SigningError, match="expected 32"):
        Signer.from_seed_b64("c2hvcnQ=")


def test_from_env_refuses_to_guess(monkeypatch) -> None:
    """A process that signs for real must be given a key, not fall back to one."""
    monkeypatch.delenv(ENV_SIGNING_KEY, raising=False)
    with pytest.raises(SigningError, match=ENV_SIGNING_KEY):
        Signer.from_env()


def test_from_env_may_fall_back_to_the_development_key(monkeypatch) -> None:
    monkeypatch.delenv(ENV_SIGNING_KEY, raising=False)
    assert Signer.from_env(allow_dev_key=True).is_dev_key


def test_from_env_prefers_the_environment(monkeypatch) -> None:
    mine = Signer.generate()
    monkeypatch.setenv(ENV_SIGNING_KEY, mine.seed_b64)
    assert Signer.from_env().verify_key_b64 == mine.verify_key_b64


# --- signing ----------------------------------------------------------------


def test_a_signed_advisory_verifies(signed) -> None:
    assert verify(signed, verify_key=DEV_KEY_B64)


def test_signing_does_not_mutate_its_input() -> None:
    """The caller keeps an unsigned advisory; the signer hands back a copy."""
    unsigned = ADVISORY.model_copy(deep=True)
    unsigned.properties.signature = None
    signed = Signer.dev().sign(unsigned)
    assert unsigned.properties.signature is None
    assert signed.properties.signature is not None


def test_an_advisory_survives_a_json_round_trip(signed) -> None:
    """The viewer and any third party read the wire form, so that must verify."""
    from headsup.contracts import Advisory

    assert verify(Advisory.model_validate_json(signed.to_json()), verify_key=DEV_KEY_B64)


def test_resigning_the_same_advisory_is_byte_identical(signed) -> None:
    again = Signer.dev().sign(signed)
    assert again.properties.signature == signed.properties.signature


# --- what the signature covers ---------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda a: setattr(a.properties, "version", 2), id="version"),
        pytest.param(lambda a: setattr(a.properties, "issuer", "headsup/austin"), id="issuer"),
        pytest.param(
            lambda a: setattr(a.properties, "policy_rule", "flood.speed_cap.v1"),
            id="policy_rule",
        ),
        pytest.param(lambda a: setattr(a.properties.hazard, "severity", 5), id="severity"),
        pytest.param(
            lambda a: setattr(
                a.properties, "valid_until", a.properties.valid_until + timedelta(hours=3)
            ),
            id="valid_until",
        ),
        pytest.param(
            lambda a: setattr(
                a.properties, "valid_from", a.properties.valid_from - timedelta(hours=3)
            ),
            id="valid_from",
        ),
        pytest.param(
            lambda a: setattr(a.properties.action, "params", {"speed_mph": 45}), id="action_params"
        ),
    ],
)
def test_changing_a_signed_field_breaks_the_signature(signed, change) -> None:
    tampered = signed.model_copy(deep=True)
    change(tampered)
    assert not verify(tampered, verify_key=DEV_KEY_B64)


def test_widening_a_published_polygon_breaks_the_signature(signed) -> None:
    from headsup.contracts import Polygon

    tampered = signed.model_copy(deep=True)
    tampered.geometry = Polygon(
        coordinates=[[[-99.0, 29.0], [-98.0, 29.0], [-98.0, 30.0], [-99.0, 30.0], [-99.0, 29.0]]]
    )
    assert not verify(tampered, verify_key=DEV_KEY_B64)


def test_dropping_the_evidence_breaks_the_signature(signed) -> None:
    tampered = signed.model_copy(deep=True)
    tampered.properties.evidence = tampered.properties.evidence[:1]
    assert not verify(tampered, verify_key=DEV_KEY_B64)


# --- every way verify says no ----------------------------------------------


def test_an_unsigned_advisory_does_not_verify(signed) -> None:
    unsigned = signed.model_copy(deep=True)
    unsigned.properties.signature = None
    assert not verify(unsigned, verify_key=DEV_KEY_B64)


def test_the_hand_written_sample_signature_does_not_verify() -> None:
    """samples.ADVISORY carries a made-up signature, and must not pass."""
    assert not verify(ADVISORY, verify_key=DEV_KEY_B64)


def test_a_signature_that_is_not_base64_does_not_verify(signed) -> None:
    tampered = signed.model_copy(deep=True)
    tampered.properties.signature = "this is not base64!"
    assert not verify(tampered, verify_key=DEV_KEY_B64)


def test_a_signature_of_the_wrong_length_does_not_verify(signed) -> None:
    tampered = signed.model_copy(deep=True)
    tampered.properties.signature = "c2hvcnQ="
    assert not verify(tampered, verify_key=DEV_KEY_B64)


def test_another_key_does_not_verify(signed) -> None:
    assert not verify(signed, verify_key=Signer.generate().verify_key_b64)


def test_an_unreadable_key_does_not_verify(signed) -> None:
    assert not verify(signed, verify_key="not-a-key")


# --- key resolution ---------------------------------------------------------


def test_verify_falls_back_to_the_development_key(monkeypatch, signed) -> None:
    """`hu verify` must check what `hu compile` printed on a fresh clone."""
    monkeypatch.delenv(ENV_SIGNING_KEY, raising=False)
    monkeypatch.delenv(ENV_VERIFY_KEY, raising=False)
    assert verify(signed)


def test_verify_prefers_the_public_key_in_the_environment(monkeypatch, signed) -> None:
    monkeypatch.delenv(ENV_SIGNING_KEY, raising=False)
    monkeypatch.setenv(ENV_VERIFY_KEY, Signer.generate().verify_key_b64)
    assert not verify(signed)


def test_verify_derives_the_public_key_from_the_signing_key(monkeypatch) -> None:
    mine = Signer.generate()
    monkeypatch.delenv(ENV_VERIFY_KEY, raising=False)
    monkeypatch.setenv(ENV_SIGNING_KEY, mine.seed_b64)
    result = Compiler(clock=clock(), signer=mine).compile(proposal(), approve())
    assert verify(result.advisory)
