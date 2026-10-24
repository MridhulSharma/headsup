"""A5: an unverified responder cannot move a fleet, and cannot reach the gate.

A move order is the one message in this system that comes from outside it. A
fire captain asking vehicles out of a staging area is not a public feed and not
a fleet operator: it is a person with authority over a street for the next
twenty minutes, and the whole of that authority rests on the server being able
to tell them from anybody who found the port.

So the attack is simply to ask. `POST /responder/move` with a plausible order
and no token, with somebody else's token, with a token that shares a prefix
with a real one, and with a body that names an agency and a fingerprint of its
own. Every one of those is refused before a handler runs, and nothing is
recorded.

The second half of the claim is the one that matters more, and it is
structural rather than conditional: **there is no path from this route to the
compiler.** A verified order is recorded as a `MoveOrder` and listed over
`GET /responder/orders`, and that is the end of it. The full liaison - an
order weighed against what the feeds say, a counter-offer, a corridor
negotiated with the fleet - is not built, so what these tests pin is the
boundary rather than a pipeline: no advisory is written, nothing lands in
`PendingCompiles`, and `MoveOrder` is not even in `STREAM_BY_MODEL`, so the
bus has no default stream to carry one onto.

What each test here checks:

* `test_a_move_order_with_no_token_is_refused_and_recorded_nowhere` and the
  three beside it - the 401s, including the body that tries to name itself.
* `test_nothing_a_responder_sends_reaches_the_compiler` - the boundary, with
  a verified order in hand.
* `test_a_verified_order_carries_the_identity_the_token_matched` - who the
  order says it is from, and that the credential itself is never written down.
* the rest: the window, the listing, the counters, and an empty registry
  refusing everybody.

Nothing here needs Redis: `REDIS_URL` points at a closed port, as in
`test_api_app`, and no route under test touches the bus.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fixtures.compiler import box
from headsup.api.app import ApiState, create_app
from headsup.api.responders import (
    ENV_TOKENS,
    MAX_MINUTES,
    RESPONDER_HEADER,
    ResponderRegistry,
    fingerprint,
)
from headsup.bus.streams import STREAM_BY_MODEL
from headsup.contracts import MoveOrder

#: The one verified responder these tests run with, and the agency they are
#: from. `HU_RESPONDER_TOKENS` is `id:agency:token`.
RESPONDER = "resp_safd_12"
AGENCY = "san_antonio_fire"
TOKEN = "a-real-responder-token-9f2c"
TOKENS = f"{RESPONDER}:{AGENCY}:{TOKEN}"

#: A second responder, so `check` has more than one entry to walk.
OTHER = "resp_apd_4:austin_police:a-different-token"

#: Wrong tokens worth trying separately. The prefix one is the interesting
#: case: a comparison that stopped at the first differing character would
#: leak how much of the token was right.
WRONG = (
    "",
    "   ",
    "a-real-responder-token-9f2b",
    "a-real-responder-token-9f2",
    "a-real-responder-token-9f2cc",
    TOKEN.upper(),
    fingerprint(TOKEN),  # the digest is not the credential
)


def order_body(**overrides: Any) -> dict[str, Any]:
    """A plausible staging-area order: a small polygon and two vehicles."""
    body: dict[str, Any] = {
        "geometry": box(0.05).model_dump(mode="json"),
        "vehicle_ids": ["veh_op_a_017", "veh_op_b_002"],
        "reason": "Swift-water rescue staging at the crossing.",
        "minutes": 30.0,
    }
    body.update(overrides)
    return body


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, ApiState]]:
    """The app with one verified responder and a Redis that is not there."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    monkeypatch.setenv("HU_LLM_MODE", "mock")
    monkeypatch.setenv(ENV_TOKENS, f"{TOKENS},{OTHER}")
    state = ApiState(namespace="test_a5_responder", credential="test-credential")
    with TestClient(create_app(state)) as instance:
        yield instance, state


def post(http: TestClient, *, token: str | None = None, **overrides: Any) -> Any:
    headers = {} if token is None else {RESPONDER_HEADER: token}
    return http.post("/responder/move", json=order_body(**overrides), headers=headers)


# --- the refusals ----------------------------------------------------------


def test_a_move_order_with_no_token_is_refused_and_recorded_nowhere(
    client: tuple[TestClient, ApiState],
) -> None:
    """The headline: no token, no order, and the refusal says what is missing."""
    http, state = client
    answer = post(http)

    assert answer.status_code == 401
    detail = answer.json()["detail"]
    assert RESPONDER_HEADER in detail
    assert "nothing reaches the compiler" in detail
    assert state.responders.waiting() == []
    assert state.responders.refused == 1


@pytest.mark.parametrize("token", WRONG, ids=lambda value: repr(value)[:24])
def test_a_move_order_with_the_wrong_token_is_refused(
    client: tuple[TestClient, ApiState], token: str
) -> None:
    """Every near miss, including the digest of the real token itself."""
    http, state = client
    assert post(http, token=token).status_code == 401
    assert state.responders.waiting() == []


def test_an_order_that_names_itself_is_refused_even_with_a_valid_token(
    client: tuple[TestClient, ApiState],
) -> None:
    """The body cannot say who it is from: those fields do not exist on it.

    A caller that supplies `responder_id`, `agency` or `token_fingerprint` is
    refused by the request model rather than quietly overridden, so there is
    no reading of this route in which a body's claim about its own identity
    was accepted and then ignored.
    """
    http, state = client
    for named in ("responder_id", "agency", "token_fingerprint"):
        answer = post(http, token=TOKEN, **{named: "anything"})
        assert answer.status_code == 422, named
    assert state.responders.waiting() == []
    # Refused by the schema, after the token checked out: not a 401.
    assert state.responders.refused == 0


def test_a_window_longer_than_the_cap_is_refused(
    client: tuple[TestClient, ApiState],
) -> None:
    """An order is for an operational period, not for the rest of the week."""
    http, _ = client
    assert post(http, token=TOKEN, minutes=MAX_MINUTES + 1).status_code == 422
    assert post(http, token=TOKEN, minutes=0).status_code == 422
    assert post(http, token=TOKEN, minutes=MAX_MINUTES).status_code == 201


def test_an_empty_registry_refuses_everybody(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closed by default: with no responders configured, nobody is verified.

    The same stance `POST /advisories` takes. A server that accepted orders
    because nobody had configured who may give them would be the one failure
    mode worth refusing to ship.
    """
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    monkeypatch.delenv(ENV_TOKENS, raising=False)
    state = ApiState(namespace="test_a5_empty", credential="test-credential")
    with TestClient(create_app(state)) as http:
        assert state.responders.responders == ()
        assert post(http, token=TOKEN).status_code == 401
        assert post(http).status_code == 401
        assert state.responders.refused == 2
        assert http.get("/responder/orders").json()["counters"] == {
            "orders": 0,
            "refused": 2,
            "responders": 0,
        }


# --- the boundary ----------------------------------------------------------


def test_nothing_a_responder_sends_reaches_the_compiler(
    client: tuple[TestClient, ApiState],
) -> None:
    """The half of A5 that is not about the 401.

    A verified order is accepted and recorded, and every place an advisory
    could have come from is untouched: nothing is waiting for a second key,
    no approval was issued, and the response says in words that this is not
    an advisory.
    """
    http, state = client
    answer = post(http, token=TOKEN)

    assert answer.status_code == 201
    body = answer.json()
    assert "not an advisory" in body["note"]
    assert "only the compiler" in body["note"]

    assert len(state.responders.waiting()) == 1
    assert len(state.pending) == 0
    assert len(state.approvals) == 0
    # The order is held in the registry and nowhere else: not in the rail the
    # viewer reads, not in the triage desk, not on its way to anything.
    (order,) = state.responders.waiting()
    assert state.index.proposal_of_advisory(order.id) is None
    assert state.index.hazard_of_proposal(order.id) is None
    assert state.triage.requests == 0


def test_the_bus_has_no_stream_that_carries_a_move_order() -> None:
    """Structural, and the reason the boundary is not a matter of discipline.

    `Bus.publish` routes a message by its class name. `MoveOrder` is not in
    the table, so a line of code that tried to put one on the bus without
    naming a stream would not compile a message onto `advisories` - it would
    not find a stream at all.
    """
    assert MoveOrder.__name__ not in STREAM_BY_MODEL


# --- what a verified order says --------------------------------------------


def test_a_verified_order_carries_the_identity_the_token_matched(
    client: tuple[TestClient, ApiState],
) -> None:
    """Who the order is from is the registry's answer, not the caller's.

    And the credential is never written down: what travels with the order is
    the SHA-256 the registry matched, which is what `MoveOrder` documents
    `token_fingerprint` as and what an audit record can safely carry.
    """
    http, state = client
    body = post(http, token=TOKEN).json()["order"]

    assert body["responder_id"] == RESPONDER
    assert body["agency"] == AGENCY
    assert body["token_fingerprint"] == fingerprint(TOKEN)
    assert TOKEN not in str(body)

    (order,) = state.responders.waiting()
    assert order.responder_id == RESPONDER
    assert order.vehicle_ids == ["veh_op_a_017", "veh_op_b_002"]
    assert order.valid_until - order.issued_at == timedelta(minutes=30)
    assert order.signature is None  # nothing here signs anything


def test_the_second_responder_is_told_apart_from_the_first(
    client: tuple[TestClient, ApiState],
) -> None:
    """Two tokens, two identities, and neither one's order claims the other."""
    http, state = client
    first = post(http, token=TOKEN).json()["order"]
    second = post(http, token="a-different-token").json()["order"]

    assert first["responder_id"] == RESPONDER
    assert second["responder_id"] == "resp_apd_4"
    assert second["agency"] == "austin_police"
    assert first["token_fingerprint"] != second["token_fingerprint"]
    assert len(state.responders.waiting()) == 2


def test_the_listing_reads_back_what_was_recorded(
    client: tuple[TestClient, ApiState],
) -> None:
    """`GET /responder/orders`: the orders, and how many calls were refused."""
    http, _ = client
    post(http)  # refused
    post(http, token=TOKEN)
    post(http, token=TOKEN)

    body = http.get("/responder/orders").json()
    assert body["counters"] == {"orders": 2, "refused": 1, "responders": 2}
    assert [row["responder_id"] for row in body["orders"]] == [RESPONDER, RESPONDER]
    assert all(row["token_fingerprint"] == fingerprint(TOKEN) for row in body["orders"])


# --- the registry on its own ------------------------------------------------


def test_the_registry_stores_a_digest_and_never_the_token() -> None:
    """What is in memory if the process is dumped: digests."""
    registry = ResponderRegistry.from_env(TOKENS)
    (responder,) = registry.responders

    assert responder.token_sha256 == fingerprint(TOKEN)
    assert TOKEN not in repr(registry)
    assert registry.check(TOKEN) == responder
    assert registry.check(TOKEN[:-1]) is None
    assert registry.check(None) is None


def test_a_malformed_entry_is_skipped_rather_than_half_trusted() -> None:
    """A typo in the environment must not produce a responder nobody meant.

    Dropped, not guessed at: an entry with a missing field has no identity to
    attach to an order, and one empty field would be a responder with no
    agency on a record somebody has to read later.
    """
    registry = ResponderRegistry.from_env(f"nope,{TOKENS},also:bad,a::c, : : ")

    assert [item.responder_id for item in registry.responders] == [RESPONDER]
    assert registry.check(TOKEN) is not None
