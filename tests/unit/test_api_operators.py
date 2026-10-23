"""The operator socket's protocol: what is pushed, and what may be acked.

Two rules carry the weight here. Nothing unverifiable is pushed, because an
operator asked to close a road on an unverifiable advisory cannot tell who
asked. And nothing may be acked that was not pushed, because an ack is written
to the `acks` stream and becomes part of what the run says happened.
"""

from __future__ import annotations

import pytest

from headsup.agents.compiler.signing import Signer
from headsup.api.operators import (
    KNOWN_OPERATORS,
    AckRejected,
    OperatorRegistry,
    OperatorSession,
    applies_to,
)
from headsup.contracts import AckStatus, AppliesTo, Polygon
from samples import ADVISORY

#: Inside the built San Antonio bbox: the Salado Creek reach S1 is about. The
#: sample advisory's own crossing sits south of that box, and an advisory
#: outside a fleet's service area is one this socket is right not to push.
INSIDE_SERVICE_AREA = Polygon(
    coordinates=[
        [
            [-98.5450, 29.5850],
            [-98.5350, 29.5850],
            [-98.5350, 29.5950],
            [-98.5450, 29.5950],
            [-98.5450, 29.5850],
        ]
    ]
)

#: The sample advisory carries a placeholder signature. Signed with the
#: published development key, it is one a verifier accepts.
UNSIGNED = ADVISORY.model_copy(deep=True, update={"geometry": INSIDE_SERVICE_AREA})
SIGNED = Signer.dev().sign(UNSIGNED)


def _session(operator: str = "op_a", city: str = "san_antonio") -> OperatorSession:
    registry = OperatorRegistry(city)
    return OperatorSession(operator, registry.area(operator))


def _ack(advisory_id: str = "adv_flood_1", version: int = 1, **fields: object) -> dict[str, object]:
    return {
        "type": "ack",
        "advisory_id": advisory_id,
        "advisory_version": version,
        "status": AckStatus.ACCEPTED.value,
        **fields,
    }


# --- what gets pushed ------------------------------------------------------


def test_the_three_personas_are_the_operators_a_socket_opens_for() -> None:
    assert KNOWN_OPERATORS == ("op_a", "op_b", "op_c")
    registry = OperatorRegistry("san_antonio")
    assert registry.known("op_a")
    assert not registry.known("op_z")


def test_an_advisory_in_the_service_area_is_pushed() -> None:
    session = _session()
    frame = session.offer(SIGNED)
    assert frame is not None
    assert frame["id"] == SIGNED.properties.id
    assert frame["operator"] == "op_a"
    assert frame["feature"] == SIGNED.to_wire()
    assert session.pushed == 1


def test_an_advisory_in_another_city_is_not_pushed() -> None:
    """An Austin fleet is not asked about a San Antonio crossing."""
    session = _session(city="austin")
    assert session.offer(SIGNED) is None
    assert session.pushed == 0
    assert session.unverified == 0


def test_an_advisory_that_does_not_verify_is_withheld_and_counted() -> None:
    session = _session()
    assert session.offer(UNSIGNED) is None
    assert session.unverified == 1
    assert session.pushed == 0


def test_an_action_that_names_other_operators_is_not_pushed() -> None:
    narrowed = SIGNED.model_copy(deep=True)
    narrowed.properties.action.applies_to = AppliesTo(operators=["op_b"], vehicle_classes=["*"])
    assert applies_to(narrowed, "op_b")
    assert not applies_to(narrowed, "op_a")
    assert _session("op_a").offer(Signer.dev().sign(narrowed)) is None


def test_an_unknown_city_leaves_every_area_open() -> None:
    """A server with no scenario running has no city, and filters on nothing."""
    registry = OperatorRegistry("")
    assert registry.area("op_a") is None
    assert OperatorSession("op_a", None).offer(SIGNED) is not None


def test_a_service_area_ignores_a_geometry_it_cannot_read() -> None:
    area = OperatorRegistry("san_antonio").area("op_a")
    assert area is not None
    assert area.covers(INSIDE_SERVICE_AREA)
    assert not area.covers({"type": "Polygon", "coordinates": "not coordinates"})


# --- what may be acked -----------------------------------------------------


def test_an_ack_for_a_pushed_advisory_is_accepted() -> None:
    session = _session()
    session.offer(SIGNED)
    ack = session.accept(_ack())
    assert ack.advisory_id == SIGNED.properties.id
    assert ack.operator == "op_a"
    assert session.acks == 1


def test_an_ack_cannot_claim_another_operator() -> None:
    """A socket to /operators/op_a may not ack as op_b, whatever it sends."""
    session = _session()
    session.offer(SIGNED)
    ack = session.accept(_ack(operator="op_b"))
    assert ack.operator == "op_a"


def test_an_ack_for_an_advisory_that_was_never_sent_is_refused() -> None:
    session = _session()
    with pytest.raises(AckRejected, match="never sent"):
        session.accept(_ack("adv_never_sent"))


def test_an_ack_for_a_version_that_was_never_sent_is_refused() -> None:
    session = _session()
    session.offer(SIGNED)
    with pytest.raises(AckRejected, match="version"):
        session.accept(_ack(version=7))


def test_an_ack_that_was_withheld_cannot_be_acked() -> None:
    """Withholding an advisory withholds the ability to answer it."""
    session = _session()
    session.offer(UNSIGNED)
    with pytest.raises(AckRejected, match="never sent"):
        session.accept(_ack())


def test_a_declined_ack_still_needs_a_reason() -> None:
    """The contract's own rule, enforced where the message arrives."""
    session = _session()
    session.offer(SIGNED)
    with pytest.raises(AckRejected, match="not a usable Ack"):
        session.accept(_ack(status=AckStatus.DECLINED.value))
    ack = session.accept(_ack(status=AckStatus.DECLINED.value, reason="fleet is off shift"))
    assert ack.status is AckStatus.DECLINED


def test_anything_that_is_not_an_object_is_refused() -> None:
    session = _session()
    with pytest.raises(AckRejected, match="JSON object"):
        session.accept(["not", "an", "ack"])
