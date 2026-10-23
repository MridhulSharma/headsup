"""The operator socket: signed advisories out, acknowledgements in.

An operator connects to `/operators/{op}` and gets the advisories that apply
to its fleet inside its service area. It answers with `Ack` messages, which
this server writes to the `acks` stream.

**The signature step, both ways.** Nothing reaches an operator that this
server has not verified: `offer` runs `verify` over the advisory's canonical
bytes and refuses to push one that does not check out, because an operator
asked to close a road on an unverifiable advisory has no way of knowing who
asked. Coming back, an `Ack` is accepted only for an advisory this session
actually pushed, at a version it pushed -- an operator cannot acknowledge an
advisory it was never sent, and a forged id cannot be used to write a
fabricated acceptance into the audit trail.

**Service areas.** An operator serves a city, and by default its area is that
city's bbox: the same box the road graph and the simulator are built on. The
filter is deliberately a real one rather than a formality -- an advisory about
San Antonio must not arrive on an Austin fleet's socket -- but it is not a
subdivision of the city between the three fleets, because the simulator spreads
every fleet across the whole network and an area narrower than that would
describe a fleet that does not exist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from shapely.geometry import base as shapely_base

from headsup.agents.compiler.geo import to_shapely
from headsup.agents.compiler.signing import verify
from headsup.contracts import Ack, Advisory
from headsup.sim.config import PERSONAS
from headsup.world.bbox import BOXES

log = logging.getLogger(__name__)

#: The operator ids a server will open a socket for: the simulator's three
#: personas. An unknown id is a 404 rather than an empty socket.
KNOWN_OPERATORS: tuple[str, ...] = tuple(PERSONAS)


class AckRejected(ValueError):
    """An operator sent something this server will not write to `acks`."""


@dataclass(frozen=True, slots=True)
class ServiceArea:
    """Where one operator's fleet runs."""

    operator: str
    city: str
    shape: shapely_base.BaseGeometry

    def covers(self, geometry: Any) -> bool:
        """True if an advisory's footprint reaches this fleet at all."""
        try:
            return self.shape.intersects(to_shapely(geometry))
        except Exception:  # an unparseable geometry is nobody's service area
            return False


class OperatorRegistry:
    """Which operators exist, and the area each one serves.

    The city is rebound when a scenario starts, because the fleet in the
    simulator is the fleet of whichever city is running.
    """

    def __init__(self, city: str = "", operators: tuple[str, ...] = KNOWN_OPERATORS) -> None:
        self.operators = operators
        self._city = city
        self._areas: dict[str, ServiceArea] = {}
        self.set_city(city)

    @property
    def city(self) -> str:
        return self._city

    def set_city(self, city: str) -> None:
        """Point every operator at a city's bbox, or at nothing if unknown."""
        self._city = city
        box = BOXES.get(city)
        self._areas = (
            {name: ServiceArea(name, city, box.to_shapely()) for name in self.operators}
            if box is not None
            else {}
        )

    def known(self, operator: str) -> bool:
        return operator in self.operators

    def area(self, operator: str) -> ServiceArea | None:
        return self._areas.get(operator)


def applies_to(advisory: Advisory, operator: str) -> bool:
    """True if the advisory's action binds this operator.

    `["*"]` is every fleet, which is what the proposer asks for today; a
    playbook that narrows `applies_to.operators` is honoured here.
    """
    selectors = advisory.action.applies_to.operators
    return "*" in selectors or operator in selectors


class OperatorSession:
    """One connected operator: what it has been sent, and what it may ack.

    Transport-free on purpose. Whoever owns the WebSocket calls `offer` with
    each advisory and `accept` with each message that arrives, so the protocol
    can be tested without a socket.
    """

    def __init__(self, operator: str, area: ServiceArea | None) -> None:
        self.operator = operator
        self.area = area
        #: advisory id -> the versions this session pushed, verified.
        self._pushed: dict[str, set[int]] = {}
        #: Advisories withheld because they did not verify.
        self.unverified = 0
        self.pushed = 0
        self.acks = 0

    # --- outbound -----------------------------------------------------------

    def offer(self, advisory: Advisory) -> dict[str, Any] | None:
        """The frame to send this operator, or None if this one is not for it.

        None has three causes and they are not the same thing: the action does
        not bind this operator, the footprint is outside its service area, or
        the signature did not verify. Only the last one is counted, because
        only the last one is something being withheld.
        """
        if not applies_to(advisory, self.operator):
            return None
        if self.area is not None and not self.area.covers(advisory.geometry):
            return None
        if not verify(advisory):
            self.unverified += 1
            log.warning(
                "withheld advisory %s from %s: signature does not verify",
                advisory.properties.id,
                self.operator,
            )
            return None

        properties = advisory.properties
        self._pushed.setdefault(properties.id, set()).add(properties.version)
        self.pushed += 1
        return {
            "type": "advisory",
            "operator": self.operator,
            "id": properties.id,
            "version": properties.version,
            "action": advisory.action.type.value,
            "valid_from": properties.valid_from.isoformat(),
            "valid_until": properties.valid_until.isoformat(),
            "feature": advisory.to_wire(),
        }

    # --- inbound ------------------------------------------------------------

    def accept(self, payload: Any) -> Ack:
        """One inbound message as an `Ack`, or raise `AckRejected`.

        The operator field is overwritten with this socket's own operator
        rather than trusted: a connection to `/operators/op_a` may not ack as
        `op_b`, whatever it puts in the body.
        """
        if not isinstance(payload, dict):
            raise AckRejected("expected a JSON object holding one Ack")
        body = {key: value for key, value in payload.items() if key != "type"}
        body["operator"] = self.operator
        try:
            ack = Ack.model_validate(body)
        except ValidationError as exc:
            raise AckRejected(f"not a usable Ack: {exc.error_count()} field error(s)") from exc

        versions = self._pushed.get(ack.advisory_id)
        if versions is None:
            raise AckRejected(
                f"{self.operator} was never sent advisory {ack.advisory_id}, so it cannot ack it"
            )
        if ack.advisory_version not in versions:
            raise AckRejected(
                f"{self.operator} was sent advisory {ack.advisory_id} at version(s) "
                f"{sorted(versions)}, not {ack.advisory_version}"
            )
        self.acks += 1
        return ack
