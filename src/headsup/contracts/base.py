"""Shared Pydantic configuration and field types for every Heads Up contract.

Every message on the bus is a `HeadsUpModel`: extra fields are refused, the
wire form uses aliases and ISO-8601 UTC timestamps, and `canonical_json`
gives the byte-stable form the compiler signs with Ed25519.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

import orjson
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field
from pydantic import StringConstraints as _Str


def _to_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to UTC so round-trips are byte-identical."""
    return value.astimezone(UTC)


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Short opaque identifier, e.g. `new_id("adv")` -> `adv_3f1c8a09b4d2`."""
    return f"{prefix}_{uuid4().hex[:12]}"


#: Timestamps are always timezone-aware and stored in UTC. Naive input is a
#: validation error: an advisory's validity window must never be ambiguous.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]

#: Machine identifiers (ids, policy rules, agent names). Deliberately narrow:
#: free text from a feed can never end up in a field that routes or names.
Identifier = Annotated[str, _Str(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")]

#: A feed-specific type ("flood_warning", "signal_outage"), slugified on ingest.
Kind = Annotated[str, _Str(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.\-]*$")]

#: Pointer back to the recorded raw payload (URL, file path, recording offset).
RawRef = Annotated[str, _Str(min_length=1, max_length=512)]

#: An operator id (`op_a`) or `*` for every fleet.
Selector = Annotated[str, _Str(pattern=r"^(\*|[a-z0-9_]{1,32})$")]

#: Hazard severity, 1 (nuisance) to 5 (life safety).
Severity = Annotated[int, Field(ge=1, le=5)]

#: Fused confidence that the hazard is real, 0 to 1.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

Longitude = Annotated[float, Field(ge=-180.0, le=180.0)]
Latitude = Annotated[float, Field(ge=-90.0, le=90.0)]

#: Free text that came from a feed or a model. Data, never an instruction.
FreeText = Annotated[str, _Str(min_length=1, max_length=2000)]


class HeadsUpModel(BaseModel):
    """Base class for every contract carried on the bus."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
        validate_default=True,
        use_enum_values=False,
    )

    def to_wire(self) -> dict[str, Any]:
        """JSON-ready dict in wire form: aliases, enum values, ISO timestamps."""
        return self.model_dump(mode="json", by_alias=True)

    def to_json(self) -> str:
        """Wire form as a JSON string."""
        return self.model_dump_json(by_alias=True)

    def canonical_json(self) -> bytes:
        """Byte-stable JSON (sorted keys) for hashing and signatures."""
        return orjson.dumps(self.to_wire(), option=orjson.OPT_SORT_KEYS)
