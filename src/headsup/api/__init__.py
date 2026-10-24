"""FastAPI app: REST plus operator and viewer WebSockets.

    from headsup.api import ApiState, create_app
    app = create_app(ApiState())

| module | what it owns |
| --- | --- |
| `app` | the routes, the two sockets, and the server's state |
| `runner` | one scenario at a time: replay, agents, both fleets, one clock |
| `catalog` | `scenarios/*.yaml` as the Test Scene launcher's rows |
| `tail` | one reader of the pipeline streams, fanned out to both sockets |
| `frames` | bus entries as viewer frames, with the ids that link them up |
| `hub` | fan-out to every viewer, and the snapshot a late joiner gets |
| `operators` | service areas, and the signed push / verified ack protocol |
| `responders` | which move orders are from a verified responder (attack A5) |
| `store` | `GET /advisories` and `GET /hazards`, read back off the streams |
| `cli` | `hu serve` and `hu demo` |
"""

from __future__ import annotations

from headsup.api.app import (
    CREDENTIAL_HEADER,
    DEFAULT_HOST,
    DEFAULT_PORT,
    ApiState,
    create_app,
    default_host,
    default_port,
)
from headsup.api.catalog import (
    SCENARIOS_DIR,
    CatalogError,
    ScenarioCard,
    ScenarioCatalog,
    StartView,
)
from headsup.api.frames import (
    FRAME_ADVISORY,
    FRAME_COUNTERS,
    FRAME_GHOST_TELEMETRY,
    FRAME_SCENARIO_STATE,
    FRAME_TELEMETRY,
    RAIL_STAGES,
    RailIndex,
)
from headsup.api.hub import Subscriber, ViewerHub
from headsup.api.operators import (
    KNOWN_OPERATORS,
    AckRejected,
    OperatorRegistry,
    OperatorSession,
    ServiceArea,
)
from headsup.api.responders import (
    ENV_TOKENS,
    RESPONDER_HEADER,
    MoveOrderRequest,
    Responder,
    ResponderRegistry,
    fingerprint,
)
from headsup.api.runner import (
    AlreadyRunning,
    ScenarioError,
    ScenarioRunner,
    ScenarioState,
    StartOptions,
)
from headsup.api.store import QueryError, parse_bbox, read_advisories, read_hazards
from headsup.api.tail import StreamTailer

__all__ = [
    "CREDENTIAL_HEADER",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ENV_TOKENS",
    "FRAME_ADVISORY",
    "FRAME_COUNTERS",
    "FRAME_GHOST_TELEMETRY",
    "FRAME_SCENARIO_STATE",
    "FRAME_TELEMETRY",
    "KNOWN_OPERATORS",
    "RAIL_STAGES",
    "RESPONDER_HEADER",
    "SCENARIOS_DIR",
    "AckRejected",
    "AlreadyRunning",
    "ApiState",
    "CatalogError",
    "MoveOrderRequest",
    "OperatorRegistry",
    "OperatorSession",
    "QueryError",
    "RailIndex",
    "Responder",
    "ResponderRegistry",
    "ScenarioCard",
    "ScenarioCatalog",
    "ScenarioError",
    "ScenarioRunner",
    "ScenarioState",
    "ServiceArea",
    "StartOptions",
    "StartView",
    "StreamTailer",
    "Subscriber",
    "ViewerHub",
    "create_app",
    "default_host",
    "default_port",
    "fingerprint",
    "parse_bbox",
    "read_advisories",
    "read_hazards",
]
