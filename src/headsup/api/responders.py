"""Who may ask a fleet out of an area, and how the server checks it.

Attack A5. `MoveOrder` already says what the contract requires - a
`token_fingerprint`, "an order without one never reaches the compiler" - and
this is the check behind that sentence. The full responder liaison (a verified
agency's order fused with what the feeds say, counter-offers, a corridor
negotiated with the fleet) is not built; what is built is the part that
decides whether a request is from a responder at all, because that is the part
an unverified caller attacks.

**The order says who it is from; the token decides.** A request body carries a
geometry, the vehicles it is about and a reason. It does *not* carry
`responder_id`, `agency` or `token_fingerprint`: those three are read off the
token the registry matched, so a caller cannot name itself. A body that tried
would be refused by `MoveOrderRequest`'s own `extra="forbid"`.

**Tokens are compared as digests, in constant time.** The registry holds
`sha256(token)` and never the token, `check` walks every entry so a wrong
token costs the same as a wrong-length one, and the digest it matched is what
goes into the order as `token_fingerprint`. A reader of the audit log can then
tell two responders apart without the log carrying either one's credential.

**A verified order is a record, not an advisory.** It is kept here and listed
over `GET /responder/orders`, and nothing in this module touches the compiler,
the `advisories` stream or `PendingCompiles`. Only
`agents.compiler.CompilerService` publishes an advisory, and it publishes one
from a proposal and a verdict - there is no path from this route to it, which
is the half of A5 that matters more than the 401.

Configured out of band, like the compiler credential:

    HU_RESPONDER_TOKENS=resp_safd_12:san_antonio_fire:s3cr3t,resp_apd_4:austin_police:...

Unset means no responder is verified and every call is refused, which is the
same closed-by-default stance `POST /advisories` takes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from headsup.contracts import Geometry, Identifier, MoveOrder, utcnow

log = logging.getLogger(__name__)

#: The header a responder's token travels in.
RESPONDER_HEADER = "X-Headsup-Responder"

#: `id:agency:token` triples, comma separated. Unset means nobody is verified.
ENV_TOKENS = "HU_RESPONDER_TOKENS"

#: How long an order asks for when the caller does not say.
DEFAULT_MINUTES = 30.0

#: Longest window a responder may ask for in one order. A scene that outlasts
#: it is a scene that gets a second order, which is a second decision.
MAX_MINUTES = 240.0

#: Orders kept in memory. The audit stream is the record; this is the window
#: `GET /responder/orders` reads.
DEFAULT_KEEP = 200


def fingerprint(token: str) -> str:
    """`sha256(token)` as 64 hex characters: what is stored and recorded."""
    return hashlib.sha256(str(token or "").encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True, slots=True)
class Responder:
    """One verified responder: who they are, and the digest of their token."""

    responder_id: str
    agency: str
    token_sha256: str

    @classmethod
    def from_token(cls, responder_id: str, agency: str, token: str) -> Responder:
        return cls(responder_id=responder_id, agency=agency, token_sha256=fingerprint(token))


class MoveOrderRequest(BaseModel):
    """What a responder sends: an area, the vehicles, and why.

    Deliberately missing `responder_id`, `agency` and `token_fingerprint`.
    Those are the server's answer to "who is this", and `extra="forbid"`
    means a caller that tries to supply them is refused rather than believed.
    """

    model_config = ConfigDict(extra="forbid")

    geometry: Geometry
    vehicle_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    reason: str | None = Field(default=None, min_length=1, max_length=2000)
    minutes: float = Field(default=DEFAULT_MINUTES, gt=0.0, le=MAX_MINUTES)


@dataclass(slots=True)
class ResponderRegistry:
    """The verified responders, and the orders they have issued this run."""

    responders: tuple[Responder, ...] = ()
    keep: int = DEFAULT_KEEP
    #: Orders accepted, newest last. Not advisories: see the module docstring.
    orders: list[MoveOrder] = field(default_factory=list)
    #: Calls refused for want of a valid token.
    refused: int = 0

    @classmethod
    def from_env(cls, raw: str | None = None) -> ResponderRegistry:
        """Parse `HU_RESPONDER_TOKENS`. An unreadable entry is skipped, loudly."""
        text = os.environ.get(ENV_TOKENS, "") if raw is None else raw
        found: list[Responder] = []
        for part in str(text).split(","):
            entry = part.strip()
            if not entry:
                continue
            fields = entry.split(":")
            if len(fields) != 3 or not all(item.strip() for item in fields):
                log.error("%s: %r is not id:agency:token", ENV_TOKENS, entry[:16])
                continue
            responder_id, agency, token = (item.strip() for item in fields)
            found.append(Responder.from_token(responder_id, agency, token))
        if not found:
            log.info("%s names no responders; POST /responder/move refuses every call", ENV_TOKENS)
        return cls(responders=tuple(found))

    def check(self, token: str | None) -> Responder | None:
        """The responder this token belongs to, or None.

        Every entry is compared whatever the first one says, so the time this
        takes does not depend on which token was sent or on how wrong it was.
        """
        digest = fingerprint(token or "")
        found: Responder | None = None
        for responder in self.responders:
            if secrets.compare_digest(digest, responder.token_sha256) and token:
                found = responder
        return found

    def issue(self, responder: Responder, request: MoveOrderRequest) -> MoveOrder:
        """Record one verified order. Nothing downstream is called.

        The identity on the order is the registry's, not the caller's, and the
        fingerprint is the digest `check` matched - so the order carries a
        pointer back to a credential this server actually verified.
        """
        now = utcnow()
        order = MoveOrder(
            responder_id=responder.responder_id,
            agency=responder.agency,
            geometry=request.geometry,
            vehicle_ids=list(request.vehicle_ids),
            reason=request.reason,
            token_fingerprint=responder.token_sha256,
            issued_at=now,
            valid_until=now + timedelta(minutes=request.minutes),
        )
        self.orders.append(order)
        del self.orders[: max(0, len(self.orders) - self.keep)]
        return order

    def waiting(self) -> list[MoveOrder]:
        """Every order recorded this run, oldest first."""
        return list(self.orders)


#: What the route answers with on success, so nobody reads a 201 as a closure.
ACCEPTED_NOTE = (
    "recorded as a verified responder order; it is not an advisory and nothing "
    "here publishes one - only the compiler does, from a proposal and a verdict"
)

#: What the route answers with when the token does not check out.
REFUSED_NOTE = (
    f"a move order needs a verified responder token in {RESPONDER_HEADER}; "
    "nothing was recorded and nothing reaches the compiler"
)
