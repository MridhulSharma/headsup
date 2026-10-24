"""The Heads Up server: advisories, hazards, scenario control, two sockets.

One process, one port, one Redis connection per role. `hu serve` runs this app
and the agents together so the viewer can start a scenario on demand, which is
the whole reason the API and the agents are not two services: the launcher has
to be able to reset the streams and the simulator, and something has to own
both.

The routes, and who they are for:

| route | for |
| --- | --- |
| `GET /scenarios`, `POST /scenarios/{id}/start`, `/stop` | the Test Scene launcher |
| `GET /advisories`, `GET /hazards` | anyone; this is the neutral layer's public face |
| `POST /advisories` | a compiler process, credential only |
| `POST /approvals/{proposal_id}`, `GET /approvals` | whoever holds the second key |
| `POST /responder/move`, `GET /responder/orders` | a verified responder |
| `WS /viewer` | the God's Eye View layer |
| `WS /operators/{op}` | one fleet |

**`POST /advisories` is not a way in.** It exists so a compiler running in
another process can publish through this server, and it is bolted down three
ways: the credential in `X-Headsup-Credential`, a signature check on the
advisory itself, and the compiler-only Redis writer underneath. An advisory
that does not verify is refused rather than published and marked, because
publishing it would put this server's name on it.

**`POST /responder/move` verifies before it records, and records nothing
else.** A move order is an emergency responder asking vehicles out of an area,
so the route checks the token in `X-Headsup-Responder` against
`api.responders` and refuses the call outright if it does not match: nothing is
recorded and nothing is handed on. A call that does check out is recorded as a
`MoveOrder` and goes no further - it is not a proposal, it is not an advisory,
and there is no path from this route to the compiler. See
`headsup.api.responders` for why the identity comes off the token rather than
out of the body (attack A5).

**Feed and model text is data here too.** Advisory rationales, hazard notes and
critic reasons travel through the sockets and the JSON responses as values. No
handler reads them, matches on them, or passes them anywhere that interprets.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Any

import orjson
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from headsup import __version__
from headsup.agents.compiler.approvals import (
    DEFAULT_TTL,
    Approval,
    InMemoryApprovals,
    PendingCompiles,
)
from headsup.agents.compiler.geo import area_km2
from headsup.agents.compiler.signing import verify
from headsup.agents.triage import AlreadyDecided, DecisionOutcome, TriageDesk
from headsup.api.catalog import CatalogError, ScenarioCard, ScenarioCatalog
from headsup.api.frames import RailIndex
from headsup.api.hub import Subscriber, ViewerHub
from headsup.api.operators import (
    KNOWN_OPERATORS,
    AckRejected,
    OperatorRegistry,
    OperatorSession,
)
from headsup.api.responders import (
    ACCEPTED_NOTE,
    REFUSED_NOTE,
    RESPONDER_HEADER,
    MoveOrderRequest,
    Responder,
    ResponderRegistry,
)
from headsup.api.runner import AlreadyRunning, ScenarioError, ScenarioRunner, StartOptions
from headsup.api.second_key import DEMO_APPROVER, hold_the_second_key
from headsup.api.store import QueryError, parse_bbox, read_advisories, read_hazards
from headsup.api.tail import StreamTailer
from headsup.bus import STREAM_ACKS, STREAM_ADVISORIES, Bus, BusError
from headsup.bus.acl import PROTECTED_WRITER, Role, url_for
from headsup.contracts import Ack, Advisory, utcnow

log = logging.getLogger(__name__)

#: The port `viz/mock_stream.py` served and the viewer layer already looks at.
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"

ENV_HOST = "HU_API_HOST"
ENV_PORT = "HU_API_PORT"
#: The credential `POST /advisories` requires. Unset means one is generated
#: per process: closed by default, never open by default.
ENV_CREDENTIAL = "HU_API_CREDENTIAL"

CREDENTIAL_HEADER = "X-Headsup-Credential"

#: Frames one operator socket may fall behind by. An operator that stops
#: reading is dropping advisories, which the counters say.
OPERATOR_QUEUE = 128


def default_host() -> str:
    return os.environ.get(ENV_HOST, "").strip() or DEFAULT_HOST


def default_port() -> int:
    raw = os.environ.get(ENV_PORT, "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PORT


@dataclass(slots=True)
class _OperatorLink:
    """One connected operator: its session, and the frames waiting for it."""

    session: OperatorSession
    queue: asyncio.Queue[dict[str, Any]]
    dropped: int = 0

    def offer(self, frame: dict[str, Any]) -> None:
        while True:
            try:
                self.queue.put_nowait(frame)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()
                    self.dropped += 1


@dataclass(slots=True)
class ApiState:
    """Everything the server holds for as long as it is up.

    One bus as the `agent` role for reading and for the agents, and a second
    as `compiler` for the one route that may write to `advisories`. Two
    connections rather than one because the single-writer rule is enforced by
    which user the connection authenticated as, not by who asks.
    """

    namespace: str = ""
    credential: str = ""
    catalog: ScenarioCatalog = field(default_factory=ScenarioCatalog)
    hub: ViewerHub = field(default_factory=ViewerHub)
    index: RailIndex = field(default_factory=RailIndex)
    registry: OperatorRegistry = field(default_factory=OperatorRegistry)
    approvals: InMemoryApprovals = field(default_factory=InMemoryApprovals)
    #: The responders whose move orders this server accepts, from
    #: `HU_RESPONDER_TOKENS`. Empty means every call to `POST /responder/move`
    #: is refused, which is the closed-by-default stance the credential takes.
    responders: ResponderRegistry = field(default_factory=ResponderRegistry.from_env)
    #: The assist queue, clustered into decision cards. Fed by the tailer and
    #: answered over `/assist/clusters`.
    triage: TriageDesk = field(default_factory=TriageDesk)
    pending: PendingCompiles = field(default_factory=PendingCompiles)
    bus: Bus | None = None
    writer_bus: Bus | None = None
    runner: ScenarioRunner | None = None
    tailer: StreamTailer | None = None
    #: A scenario to start once the server is up, for `hu demo`. Started as a
    #: task, not inline: building a city world takes seconds, and the viewer
    #: should be able to connect while it happens.
    autostart: tuple[str, StartOptions] | None = None
    #: `http://host:port` for the stand-in operator `hu demo` runs, which
    #: approves what the gate is waiting on through `POST /approvals/...`.
    #: Unset on `hu serve`: there, the second key belongs to a person.
    second_key_url: str = ""
    _tail_task: asyncio.Task[None] | None = None
    _autostart_task: asyncio.Task[None] | None = None
    _second_key_task: asyncio.Task[None] | None = None
    _operators: dict[int, _OperatorLink] = field(default_factory=dict)
    _next_link: int = 0

    # --- lifecycle ----------------------------------------------------------

    async def startup(self) -> None:
        """Open the buses, start the tailer, and go idle waiting for a scenario."""
        if not self.credential:
            configured = os.environ.get(ENV_CREDENTIAL, "").strip()
            self.credential = configured or secrets.token_urlsafe(24)
            if not configured:
                # Closed by default, so the credential has to be said out loud
                # once for a compiler in another process to be able to use it.
                # Pin it with HU_API_CREDENTIAL to keep it out of the log.
                log.info(
                    "generated a compiler credential for POST /advisories: %s", self.credential
                )
        self.bus = Bus(url_for(Role.AGENT), namespace=self.namespace, writer=None)
        self.writer_bus = Bus(
            url_for(Role.COMPILER), namespace=self.namespace, writer=PROTECTED_WRITER
        )
        self.runner = ScenarioRunner(
            self.bus,
            self.hub,
            self.index,
            self.registry,
            approvals=self.approvals,
            pending=self.pending,
            triage=self.triage,
            namespace=self.namespace,
        )
        self.tailer = StreamTailer(
            self.bus,
            self.hub,
            self.index,
            on_advisory=self.fan_advisory,
            on_assist=self.triage.offer,
        )
        self._tail_task = asyncio.create_task(self.tailer.run(), name="viewer-tailer")
        self.hub.reset(self.runner.status())
        if self.autostart is not None:
            self._autostart_task = asyncio.create_task(self._autostart(), name="autostart")
        if self.second_key_url:
            log.warning(
                "a stand-in operator is holding the second key at %s: every two-key "
                "advisory this run publishes was approved by %s, not by a person",
                self.second_key_url,
                DEMO_APPROVER,
            )
            self._second_key_task = asyncio.create_task(
                hold_the_second_key(self.second_key_url), name="second-key"
            )

    async def _autostart(self) -> None:
        """Start the scenario `hu demo` asked for, and report if it cannot."""
        if self.autostart is None:  # pragma: no cover - guarded by the caller
            return
        scenario_id, options = self.autostart
        try:
            card = self.catalog.card(scenario_id)
            await self.require_runner().start(card, options)
            log.info("scenario %s running", scenario_id)
        except Exception as exc:
            log.error("could not start %s: %s", scenario_id, exc)

    async def shutdown(self) -> None:
        """Stop the scenario, the tailer and the sockets, then close the buses."""
        if self.runner is not None:
            with contextlib.suppress(Exception):
                await self.runner.stop()
        for task in (self._second_key_task, self._autostart_task, self._tail_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self.hub.close_all()
        for bus in (self.bus, self.writer_bus):
            if bus is not None:
                with contextlib.suppress(Exception):
                    await bus.close()

    # --- the parts the routes need -----------------------------------------

    def require_bus(self) -> Bus:
        if self.bus is None:  # pragma: no cover - startup always runs first
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the server is not started")
        return self.bus

    def require_runner(self) -> ScenarioRunner:
        if self.runner is None:  # pragma: no cover - startup always runs first
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the server is not started")
        return self.runner

    # --- operator sockets ---------------------------------------------------

    def attach_operator(self, session: OperatorSession) -> tuple[int, _OperatorLink]:
        link = _OperatorLink(session, asyncio.Queue(maxsize=OPERATOR_QUEUE))
        self._next_link += 1
        token = self._next_link
        self._operators[token] = link
        return token, link

    def detach_operator(self, token: int) -> None:
        self._operators.pop(token, None)

    def fan_advisory(self, advisory: Advisory) -> None:
        """Offer one advisory to every connected operator it is for."""
        for link in tuple(self._operators.values()):
            frame = link.session.offer(advisory)
            if frame is not None:
                link.offer(frame)


class StartResponse(BaseModel):
    """What `POST /scenarios/{id}/start` answers with: the new state."""

    model_config = ConfigDict(extra="allow")


class ApprovalRequest(BaseModel):
    """The second key, as the holder issues it."""

    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")
    minutes: float = Field(default=DEFAULT_TTL.total_seconds() / 60, gt=0.0, le=1440.0)
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class DecideRequest(BaseModel):
    """One operator's answer to one decision card."""

    model_config = ConfigDict(extra="forbid")

    outcome: DecisionOutcome = DecisionOutcome.PROCEED
    decided_by: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")
    note: str | None = Field(default=None, min_length=1, max_length=2000)


def _json(payload: Any, *, code: int = status.HTTP_200_OK) -> JSONResponse:
    """A response serialised by orjson, so geometry and timestamps match the bus."""
    return JSONResponse(content=orjson.loads(orjson.dumps(payload)), status_code=code)


def create_app(state: ApiState | None = None) -> FastAPI:
    """The app, with its state attached. One per process."""
    api = state or ApiState()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await api.startup()
        try:
            yield
        finally:
            await api.shutdown()

    app = FastAPI(
        title="Heads Up",
        version=__version__,
        summary="Public city signals compiled into signed, expiring fleet advisories.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:4173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.api = api

    def credential(
        value: Annotated[str | None, Header(alias=CREDENTIAL_HEADER)] = None,
    ) -> None:
        """The compiler credential, checked in constant time."""
        if not value or not secrets.compare_digest(value, api.credential):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "POST /advisories is internal; send the compiler credential in "
                f"{CREDENTIAL_HEADER}",
            )

    # --- health -------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Whether the server can see Redis, and what it is doing."""
        bus = api.require_bus()
        try:
            alive = await bus.ping()
        except Exception as exc:  # a dead Redis is a 503, not a traceback
            return _json({"ok": False, "redis": str(exc)}, code=status.HTTP_503_SERVICE_UNAVAILABLE)
        runner = api.require_runner()
        return _json(
            {
                "ok": bool(alive),
                "version": __version__,
                "scenario": runner.scenario_id,
                "state": runner.state.value,
                "viewers": api.hub.viewers,
                "frames_published": api.hub.published,
            }
        )

    # --- advisories and hazards --------------------------------------------

    @app.get("/advisories")
    async def get_advisories(
        bbox: Annotated[str | None, Query(description="west,south,east,north")] = None,
        active: Annotated[bool, Query(description="Only advisories in force now.")] = True,
    ) -> JSONResponse:
        """Published advisories as a GeoJSON FeatureCollection."""
        bus = api.require_bus()
        try:
            window = parse_bbox(bbox)
            advisories = await read_advisories(bus, bbox=window, active=active)
        except QueryError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return _json(
            {
                "type": "FeatureCollection",
                "features": [advisory.to_wire() for advisory in advisories],
            }
        )

    @app.get("/hazards")
    async def get_hazards(
        bbox: Annotated[str | None, Query(description="west,south,east,north")] = None,
        active: Annotated[bool, Query(description="Only hazards that have not expired.")] = True,
    ) -> JSONResponse:
        """What Heads Up currently believes, with every citation attached."""
        bus = api.require_bus()
        try:
            window = parse_bbox(bbox)
            hazards = await read_hazards(bus, bbox=window, active=active)
        except QueryError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return _json({"hazards": [hazard.to_wire() for hazard in hazards]})

    @app.post(
        "/advisories",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(credential)],
    )
    async def post_advisory(payload: Annotated[dict[str, Any], Body()]) -> JSONResponse:
        """Publish one signed advisory. Internal: a compiler process only.

        The credential is a route dependency rather than a parameter of this
        function: as a parameter annotated `None` with a default, FastAPI reads
        it as an optional body field and never calls it, which leaves the route
        open. Declared here it runs before the body is even looked at.
        """
        if api.writer_bus is None:  # pragma: no cover - startup always runs first
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the server is not started")
        try:
            advisory = Advisory.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"not a usable Advisory: {exc.error_count()} field error(s)",
            ) from exc
        if not verify(advisory):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"advisory {advisory.properties.id} carries no signature this server can verify",
            )
        try:
            entry_id = await api.writer_bus.publish(advisory, stream=STREAM_ADVISORIES)
        except BusError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        return _json(
            {
                "id": advisory.properties.id,
                "version": advisory.properties.version,
                "stream_id": entry_id,
            },
            code=status.HTTP_201_CREATED,
        )

    # --- the second key -----------------------------------------------------

    @app.get("/approvals")
    async def get_approvals() -> JSONResponse:
        """The approvals this server holds that are still usable.

        Fingerprints, not tokens. A token is handed to the caller that created
        it and never read back out: anyone who could list them could spend
        them, and the second key would be a GET away from being no key.
        """
        return _json(
            {
                "approvals": [
                    {
                        "proposal_id": item.proposal_id,
                        "approved_by": item.approved_by,
                        "approved_at": item.approved_at.isoformat(),
                        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                        "fingerprint": item.fingerprint,
                        "note": item.note,
                    }
                    for item in api.approvals.pending()
                ],
                # What the gate is actually waiting on: the proposals it
                # refused with `two_key_approved` as their only failed rule.
                # This is the list whoever holds the second key works from.
                "awaiting_approval": [
                    {
                        "proposal_id": item.proposal_id,
                        "hazard_id": item.proposal.hazard.id,
                        "hazard_class": item.proposal.hazard.hazard_class.value,
                        "severity": item.proposal.hazard.severity,
                        "action": item.proposal.action.type.value,
                        "area_km2": round(area_km2(item.proposal.geometry), 4),
                        "recorded_at": item.recorded_at.isoformat(),
                    }
                    for item in api.pending.waiting()
                ],
            }
        )

    @app.post("/approvals/{proposal_id}", status_code=status.HTTP_201_CREATED)
    async def post_approval(proposal_id: str, request: ApprovalRequest) -> JSONResponse:
        """Record one person's decision to let one proposal publish.

        The token is returned once, to the caller that created it. The compiler
        reads the store, not this response, and the audit record carries only
        the token's fingerprint.
        """
        now = utcnow()
        try:
            approval = Approval(
                proposal_id=proposal_id,
                token=secrets.token_urlsafe(24),
                approved_by=request.approved_by,
                approved_at=now,
                expires_at=now + timedelta(minutes=request.minutes),
                note=request.note,
            )
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"not a usable approval: {exc.error_count()}"
            ) from exc
        api.approvals.add(approval)

        # A compile happens the instant a proposal is made, so by the time a
        # person has issued a token the gate has already refused the proposal
        # once. Hand it back: every rule runs again, including this approval's
        # own validity, and a second audit record says what came of it. The
        # token is not a bypass and this is not one either - a proposal that
        # fails any other rule is refused again, with its reasons.
        published: str | None = None
        runner = api.runner
        if runner is not None:
            outcome = await runner.recompile(proposal_id)
            if outcome is not None and outcome.ok:
                published = outcome.result.advisory_id
        body = dict(approval.to_wire())
        # `fingerprint` is a property rather than a field, so `to_wire` does
        # not carry it. It is what every audit record of this approval is
        # stamped with, so the caller that holds the token gets it here and
        # can match the two up without reading the token back out of anything.
        body["fingerprint"] = approval.fingerprint
        body["advisory_id"] = published
        return _json(body, code=status.HTTP_201_CREATED)

    # --- the assist queue ---------------------------------------------------

    @app.get("/assist/clusters")
    async def get_assist_clusters(
        open_only: Annotated[bool, Query()] = False,
    ) -> JSONResponse:
        """The decision cards the assist queue has collapsed into.

        The summaries are written here rather than as the requests arrive, so
        a surge of 300 requests into eight scenarios costs eight volume calls
        on the first read and none on the next - and a card nobody ever looked
        at costs nothing at all.
        """
        await api.triage.summarise()
        return _json(api.triage.board(open_only=open_only))

    @app.post("/assist/clusters/{cluster_id}/decide", status_code=status.HTTP_201_CREATED)
    async def decide_assist_cluster(cluster_id: str, request: DecideRequest) -> JSONResponse:
        """Answer one scenario, for every vehicle whose request matches it.

        The response says what the decision reached: the vehicles on the card
        when it was taken, and - for `PER_VEHICLE` - that it reached none of
        them and the requests are back to one card each. A card can be
        answered once; a second answer is a 409 rather than an overwrite,
        because the first one has already been applied.
        """
        try:
            decision = api.triage.decide(
                cluster_id,
                outcome=request.outcome,
                decided_by=request.decided_by,
                note=request.note,
            )
        except KeyError as exc:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"no decision card {cluster_id}"
            ) from exc
        except AlreadyDecided as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        cluster = api.triage.cluster(cluster_id)
        return _json(
            {
                "decision": decision.model_dump(mode="json"),
                "applied_to": decision.vehicle_ids if decision.action else [],
                "card": cluster.card() if cluster is not None else None,
            },
            code=status.HTTP_201_CREATED,
        )

    # --- verified responders ------------------------------------------------

    def require_responder(token: str | None) -> Responder:
        """The responder this token belongs to, or a 401.

        The only way to get a `Responder`, and the first statement of the one
        handler that needs one: an order is never built from a call whose
        token has not been matched, and a refused call is counted and
        returned before the body is looked at again.
        """
        found = api.responders.check(token)
        if found is None:
            api.responders.refused += 1
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, REFUSED_NOTE)
        return found

    @app.post("/responder/move", status_code=status.HTTP_201_CREATED)
    async def post_move_order(
        request: MoveOrderRequest,
        token: Annotated[str | None, Header(alias=RESPONDER_HEADER)] = None,
    ) -> JSONResponse:
        """One verified responder asking vehicles out of an area.

        The identity on the order is the one the token matched, and never
        anything the body said - `MoveOrderRequest` has no field for it. The
        order is recorded and that is all: nothing here calls the compiler,
        writes to `advisories` or adds to `PendingCompiles`.
        """
        order = api.responders.issue(require_responder(token), request)
        return _json(
            {"order": order.to_wire(), "note": ACCEPTED_NOTE},
            code=status.HTTP_201_CREATED,
        )

    @app.get("/responder/orders")
    async def get_move_orders() -> JSONResponse:
        """Every verified order this run, and how many calls were refused."""
        orders = api.responders.waiting()
        return _json(
            {
                "orders": [order.to_wire() for order in orders],
                "counters": {
                    "orders": len(orders),
                    "refused": api.responders.refused,
                    "responders": len(api.responders.responders),
                },
            }
        )

    # --- scenario control ---------------------------------------------------

    @app.get("/scenarios")
    async def get_scenarios() -> JSONResponse:
        """The Test Scene launcher's rows, read from `scenarios/*.yaml`."""
        runner = api.require_runner()
        return _json(
            {
                "scenarios": [card.model_dump(mode="json") for card in api.catalog.cards()],
                "running": runner.scenario_id,
                "state": runner.state.value,
            }
        )

    @app.post("/scenarios/stop")
    async def stop_scenario() -> JSONResponse:
        """Stop the running scenario and reset everything to idle."""
        runner = api.require_runner()
        return _json(await runner.stop())

    @app.post("/scenarios/{scenario_id}/start")
    async def start_scenario(
        scenario_id: str,
        options: Annotated[StartOptions | None, Body()] = None,
    ) -> JSONResponse:
        """Reset, then run one scenario: replay, agents, both fleets, one clock."""
        runner = api.require_runner()
        try:
            card: ScenarioCard = api.catalog.card(scenario_id)
        except CatalogError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        try:
            return _json(await runner.start(card, options or StartOptions()))
        except AlreadyRunning as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except (ScenarioError, ValueError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # --- the viewer socket --------------------------------------------------

    @app.websocket("/viewer")
    async def viewer_socket(websocket: WebSocket) -> None:
        """Fan-out to the God's Eye View: state, telemetry, the rail, counters."""
        await websocket.accept()
        subscriber: Subscriber = api.hub.subscribe()
        log.info("viewer connected (%d now)", api.hub.viewers)
        try:
            async for frame in subscriber.frames():
                await websocket.send_text(orjson.dumps(frame).decode())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            api.hub.unsubscribe(subscriber)
            log.info("viewer disconnected (%d left)", api.hub.viewers)

    # --- the operator sockets ----------------------------------------------

    @app.websocket("/operators/{operator}")
    async def operator_socket(websocket: WebSocket, operator: str) -> None:
        """One fleet: verified advisories out, its acks onto the `acks` stream."""
        if not api.registry.known(operator):
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=f"unknown operator {operator!r}; known: {', '.join(KNOWN_OPERATORS)}",
            )
            return
        await websocket.accept()
        session = OperatorSession(operator, api.registry.area(operator))
        token, link = api.attach_operator(session)
        log.info("operator %s connected", operator)
        try:
            await _backfill(api, session, link)
            await _pump(api, websocket, session, link)
        except (WebSocketDisconnect, RuntimeError):
            # RuntimeError is starlette's answer to a send on a socket the
            # operator has already closed: a disconnect by another name.
            pass
        finally:
            api.detach_operator(token)
            log.info(
                "operator %s disconnected: %d pushed, %d acked, %d unverified",
                operator,
                session.pushed,
                session.acks,
                session.unverified,
            )

    return app


async def _backfill(api: ApiState, session: OperatorSession, link: _OperatorLink) -> None:
    """Send the advisories already in force before any new one arrives.

    A fleet that connects mid-run is subject to the advisories that are live
    now, not only to the next one published, so the socket opens with them.
    """
    try:
        for advisory in await read_advisories(api.require_bus(), active=True):
            frame = session.offer(advisory)
            if frame is not None:
                link.offer(frame)
    except Exception as exc:
        # A socket that could not be backfilled still gets every advisory
        # published from now on, which is better than refusing the connection.
        log.warning("operator %s: could not backfill advisories: %s", session.operator, exc)


async def _pump(
    api: ApiState, websocket: WebSocket, session: OperatorSession, link: _OperatorLink
) -> None:
    """Write advisories out and read acks in, until either side stops.

    Two tasks rather than one loop: an operator that sends nothing for an hour
    must still receive advisories, and one that is slow to read must still be
    able to ack.
    """

    async def write() -> None:
        while True:
            frame = await link.queue.get()
            await websocket.send_text(orjson.dumps(frame).decode())

    async def read() -> None:
        while True:
            message = await websocket.receive_text()
            await _take_ack(api, websocket, session, message)

    tasks = [asyncio.create_task(write()), asyncio.create_task(read())]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # A disconnect is how this socket always ends, so it returns rather than
    # raising: the handler has already logged what the session did, and an
    # exception out of an ASGI app on a normal close is noise. Anything else
    # is a bug and is raised.
    for task in done:
        exc = None if task.cancelled() else task.exception()
        if exc is not None and not isinstance(exc, WebSocketDisconnect | RuntimeError):
            raise exc


async def _take_ack(
    api: ApiState, websocket: WebSocket, session: OperatorSession, message: str
) -> None:
    """One inbound message: an `Ack` onto the stream, or an error back.

    The error goes back to the operator rather than closing the socket: a
    malformed ack is a bug in one fleet's client, not a reason to stop sending
    that fleet advisories.
    """
    try:
        payload = orjson.loads(message)
    except orjson.JSONDecodeError:
        await _refuse(websocket, "message is not JSON")
        return
    try:
        ack: Ack = session.accept(payload)
    except AckRejected as exc:
        await _refuse(websocket, str(exc))
        return
    try:
        entry_id = await api.require_bus().publish(ack, stream=STREAM_ACKS)
    except BusError as exc:
        await _refuse(websocket, f"could not write the ack: {exc}")
        return
    await websocket.send_text(
        orjson.dumps(
            {
                "type": "ack_recorded",
                "id": ack.id,
                "advisory_id": ack.advisory_id,
                "stream_id": entry_id,
            }
        ).decode()
    )


async def _refuse(websocket: WebSocket, reason: str) -> None:
    await websocket.send_text(orjson.dumps({"type": "error", "error": reason}).decode())
