"""The HTTP surface, with no Redis and no scenario running.

Everything here is about what the server refuses and what it says while idle:
the launcher's list, the credential on the one internal route, the second key,
and the two sockets' front doors. A run that actually publishes something is
`tests/e2e/test_s1.py`; this is the part that must hold on a laptop with
nothing started.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from headsup.api.app import CREDENTIAL_HEADER, ApiState, create_app
from headsup.api.frames import FRAME_SCENARIO_STATE
from samples import ADVISORY

CREDENTIAL = "test-credential"


@pytest.fixture
def state() -> ApiState:
    return ApiState(namespace="test_api_app", credential=CREDENTIAL)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, state: ApiState) -> Iterator[TestClient]:
    """The app with a known credential and a Redis that is deliberately absent.

    Pointed at a closed port rather than at the dev Redis: a unit test must
    not need Docker, and it must not leave consumer groups behind on the bus
    the integration tests measure.
    """
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    with TestClient(create_app(state)) as instance:
        yield instance


def _signed() -> dict[str, Any]:
    from headsup.agents.compiler.signing import Signer

    return Signer.dev().sign(ADVISORY).to_wire()


# --- the launcher ----------------------------------------------------------


def test_the_launcher_lists_every_scenario_while_idle(client: TestClient) -> None:
    body = client.get("/scenarios").json()
    assert body["state"] == "idle"
    assert body["running"] is None
    ids = [row["id"] for row in body["scenarios"]]
    assert ids == ["s1_flood", "s2_blackout", "s3_incident"]
    row = body["scenarios"][0]
    assert set(row) >= {"id", "title", "what_if", "city", "scripted", "start_view"}


def test_starting_a_scenario_that_does_not_exist_is_a_404(client: TestClient) -> None:
    response = client.post("/scenarios/s9_nothing/start", json={})
    assert response.status_code == 404
    assert "s1_flood" in response.json()["detail"]


def test_a_start_the_launcher_could_not_have_meant_is_refused(client: TestClient) -> None:
    """Speeds outside 1x to 60x are a validation error, not a clamped run."""
    assert client.post("/scenarios/s1_flood/start", json={"speed": 600}).status_code == 422


def test_stopping_when_nothing_runs_is_idle_not_an_error(client: TestClient) -> None:
    response = client.post("/scenarios/stop")
    assert response.status_code == 200
    assert response.json() == {
        "type": "scenario_state",
        "state": "idle",
        "scenario": None,
        "errors": [],
    }


# --- the internal publish route -------------------------------------------


def test_publishing_an_advisory_needs_the_compiler_credential(client: TestClient) -> None:
    response = client.post("/advisories", json=_signed())
    assert response.status_code == 401
    assert CREDENTIAL_HEADER in response.json()["detail"]


def test_a_wrong_credential_is_refused(client: TestClient) -> None:
    response = client.post(
        "/advisories", json=_signed(), headers={CREDENTIAL_HEADER: "not-the-credential"}
    )
    assert response.status_code == 401


def test_an_unsigned_advisory_is_never_published(client: TestClient) -> None:
    """The sample's placeholder signature is exactly what must not get through."""
    response = client.post(
        "/advisories", json=ADVISORY.to_wire(), headers={CREDENTIAL_HEADER: CREDENTIAL}
    )
    assert response.status_code == 400
    assert "verify" in response.json()["detail"]


def test_something_that_is_not_an_advisory_is_refused(client: TestClient) -> None:
    response = client.post(
        "/advisories", json={"type": "Feature"}, headers={CREDENTIAL_HEADER: CREDENTIAL}
    )
    assert response.status_code == 422


# --- the second key --------------------------------------------------------


def test_an_approval_is_returned_once_and_listed_by_fingerprint(client: TestClient) -> None:
    created = client.post("/approvals/prop_flood_1", json={"approved_by": "city_operator"})
    assert created.status_code == 201
    token = created.json()["token"]
    assert token

    listed = client.get("/approvals").json()["approvals"]
    assert [row["proposal_id"] for row in listed] == ["prop_flood_1"]
    assert listed[0]["approved_by"] == "city_operator"
    # Anyone who could read the token back could spend it.
    assert "token" not in listed[0]
    assert listed[0]["fingerprint"] == f"{token[:6]}..."


def test_an_approval_needs_to_say_who_approved(client: TestClient) -> None:
    assert client.post("/approvals/prop_flood_1", json={}).status_code == 422


def test_the_pending_list_is_what_the_gate_is_actually_waiting_on(
    client: TestClient, state: ApiState
) -> None:
    """What whoever holds the second key works from.

    `awaiting_approval` is the proposals the gate refused with
    `two_key_approved` as their only failed rule - a severity 4 advisory, or
    one the critic escalated. Put one in the store the way `CompilerService`
    would and read it back, so the list the viewer's approve button is built
    on is pinned to the fields it shows.
    """
    from fixtures.compiler import proposal
    from headsup.agents.critic import CRITIC_NAME
    from headsup.contracts import CriticVerdict, Verdict

    asked = proposal(severity=4, area_km2=0.05, proposal_id="prop_flood_severe")
    state.pending.remember(
        asked,
        CriticVerdict(
            proposal_id=asked.id,
            verdict=Verdict.ESCALATE,
            reasons=["the citations do not explain an advisory this broad"],
            reviewed_by=CRITIC_NAME,
        ),
    )

    waiting = client.get("/approvals").json()["awaiting_approval"]

    assert [row["proposal_id"] for row in waiting] == ["prop_flood_severe"]
    row = waiting[0]
    assert row["severity"] == 4
    assert row["hazard_class"] == "FLOOD"
    assert row["action"] == "NO_ENTRY"
    assert row["area_km2"] > 0.0


# --- the sockets' front doors ---------------------------------------------


def test_a_viewer_is_told_the_state_as_soon_as_it_connects(client: TestClient) -> None:
    with client.websocket_connect("/viewer") as socket:
        frame = socket.receive_json()
    assert frame["type"] == FRAME_SCENARIO_STATE
    assert frame["state"] == "idle"
    assert frame["scenario"] is None


def test_an_unknown_operator_is_closed_rather_than_given_a_socket(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/operators/op_z"):
        pass  # pragma: no cover - the connect itself raises


def test_a_known_operator_gets_a_socket_with_no_scenario_running(client: TestClient) -> None:
    """An operator may connect and wait. There is simply nothing to send yet."""
    with client.websocket_connect("/operators/op_a") as socket:
        socket.send_json({"advisory_id": "adv_never_sent", "status": "ACCEPTED"})
        answer = socket.receive_json()
    assert answer["type"] == "error"
    assert "never sent" in answer["error"]
