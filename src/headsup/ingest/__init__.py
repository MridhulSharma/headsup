"""Feed adapters, raw recorder and replayer. Feed text is data, never instructions.

Live and replay share one parse path: an adapter's `fetch` produces
`RawPayload`s, the recorder writes them verbatim, and `parse` turns a payload
into `CivicEvent`s whether it arrived over HTTP a second ago or came off disk.
"""

from __future__ import annotations

from headsup.ingest.austin_signals import (
    ENV_APP_TOKEN,
    FLASHING_STATES,
    SCHEDULED_FLASH_STATE,
    SIGNAL_INVENTORY_DATASET,
    SIGNAL_STATUS_DATASET,
    SODA_BASE,
    STATE_KINDS,
    UNSCHEDULED_FLASH_STATES,
    AustinSignalsAdapter,
    app_token,
)
from headsup.ingest.base import (
    DEFAULT_USER_AGENT,
    Adapter,
    Clock,
    FakeClock,
    IngestError,
    RawPayload,
    SystemClock,
    derive_id,
    make_ref,
    parse_timestamp,
    slugify,
    user_agent,
)
from headsup.ingest.cities import AUSTIN, CITIES, GAUGE_HEIGHT, SAN_ANTONIO, City, city
from headsup.ingest.live import (
    NWS_INTERVAL,
    SIGNALS_INTERVAL,
    USGS_INTERVAL,
    FeedRun,
    LiveIngest,
    city_adapters,
)
from headsup.ingest.nws import ALERTS_URL, NwsAdapter
from headsup.ingest.recorder import (
    FIXTURE_DIR,
    RAW_DIR,
    RECORDINGS_ROOT,
    Recorder,
    Recording,
    scenario_dir,
)
from headsup.ingest.registry import ADAPTERS, adapter_class, parser_for, parsers
from headsup.ingest.replayer import (
    MAX_SPEED,
    MIN_SPEED,
    Emission,
    Replayer,
    ReplayReport,
    check_speed,
)
from headsup.ingest.usgs import ACTION_STAGE_FT, OGC_BASE, UsgsAdapter

__all__ = [
    "ACTION_STAGE_FT",
    "ADAPTERS",
    "ALERTS_URL",
    "AUSTIN",
    "CITIES",
    "DEFAULT_USER_AGENT",
    "ENV_APP_TOKEN",
    "FIXTURE_DIR",
    "FLASHING_STATES",
    "GAUGE_HEIGHT",
    "MAX_SPEED",
    "MIN_SPEED",
    "NWS_INTERVAL",
    "OGC_BASE",
    "RAW_DIR",
    "RECORDINGS_ROOT",
    "SAN_ANTONIO",
    "SCHEDULED_FLASH_STATE",
    "SIGNALS_INTERVAL",
    "SIGNAL_INVENTORY_DATASET",
    "SIGNAL_STATUS_DATASET",
    "SODA_BASE",
    "STATE_KINDS",
    "UNSCHEDULED_FLASH_STATES",
    "USGS_INTERVAL",
    "Adapter",
    "AustinSignalsAdapter",
    "City",
    "Clock",
    "Emission",
    "FakeClock",
    "FeedRun",
    "IngestError",
    "LiveIngest",
    "NwsAdapter",
    "RawPayload",
    "Recorder",
    "Recording",
    "ReplayReport",
    "Replayer",
    "SystemClock",
    "UsgsAdapter",
    "adapter_class",
    "app_token",
    "check_speed",
    "city",
    "city_adapters",
    "derive_id",
    "make_ref",
    "parse_timestamp",
    "parser_for",
    "parsers",
    "scenario_dir",
    "slugify",
    "user_agent",
]
