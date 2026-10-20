"""The advisory chain: proposal, critic verdict, published advisory.

Only `src/headsup/agents/compiler` may turn a `ProposedAdvisory` into an
`Advisory` and sign it, and only the compiler may publish to the `advisories`
stream. An `Advisory` is a GeoJSON Feature so the viewer and any third party
can read it without a Heads Up library.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

import orjson
from pydantic import Field, StringConstraints, model_validator

from headsup.contracts.base import (
    FreeText,
    HeadsUpModel,
    Identifier,
    Selector,
    UtcDatetime,
    new_id,
    utcnow,
)
from headsup.contracts.enums import ActionType, Verdict
from headsup.contracts.events import Evidence, Hazard
from headsup.contracts.geometry import Geometry

#: `headsup/<city>`: the neutral layer names itself and the city it speaks for.
ISSUER_PATTERN = r"^headsup/[a-z0-9_\-]{2,40}$"
Issuer = Annotated[str, StringConstraints(pattern=ISSUER_PATTERN)]

#: Action parameter values stay scalar so the operator masks can apply them
#: without interpreting anything.
ParamValue = bool | int | float | str
ParamName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]

#: A non-empty selector list; `["*"]` (everyone) unless the playbook narrows it.
Selectors = Annotated[list[Selector], Field(min_length=1, default_factory=lambda: ["*"])]

#: Parameters each action type cannot work without.
REQUIRED_PARAMS: dict[ActionType, tuple[str, ...]] = {
    ActionType.SPEED_CAP: ("speed_mph",),
    ActionType.DENSITY_CAP: ("max_vehicles",),
}

#: Parameters that must be a positive number wherever they appear.
POSITIVE_PARAMS = frozenset({"speed_mph", "max_vehicles", "width_m", "delay_s", "radius_m"})


class AppliesTo(HeadsUpModel):
    """Who an action binds, and optionally which graph elements it masks."""

    operators: Selectors
    vehicle_classes: Selectors
    edges: list[Identifier] = Field(default_factory=list)
    nodes: list[Identifier] = Field(default_factory=list)


class Action(HeadsUpModel):
    """One thing a fleet is asked to do inside the advisory geometry."""

    type: ActionType
    applies_to: AppliesTo = Field(default_factory=AppliesTo)
    params: dict[ParamName, ParamValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _params_match_the_action(self) -> Action:
        missing = [name for name in REQUIRED_PARAMS.get(self.type, ()) if name not in self.params]
        if missing:
            raise ValueError(f"{self.type} needs params {', '.join(missing)}")
        for name, value in self.params.items():
            if name not in POSITIVE_PARAMS:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"param {name} must be a positive number, got {value!r}")
            if value <= 0:
                raise ValueError(f"param {name} must be greater than 0, got {value}")
        return self


class ProposedAdvisory(HeadsUpModel):
    """What the proposer sends to the critic and the compiler. Never published."""

    id: Identifier = Field(default_factory=lambda: new_id("prop"))
    hazard: Hazard
    action: Action
    geometry: Geometry
    valid_from: UtcDatetime
    valid_until: UtcDatetime
    evidence: list[Evidence] = Field(min_length=1)
    policy_rule: Identifier
    proposed_by: Identifier
    rationale: FreeText | None = None
    created_at: UtcDatetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _window_is_ordered(self) -> ProposedAdvisory:
        _check_window(self.valid_from, self.valid_until)
        return self


class CriticVerdict(HeadsUpModel):
    """The critic's independent read of one proposal, with its reasons."""

    proposal_id: Identifier
    verdict: Verdict
    reasons: Annotated[list[FreeText], Field(min_length=1)]
    revised_geometry: Geometry | None = None
    revised_valid_until: UtcDatetime | None = None
    reviewed_by: Identifier = "critic"
    created_at: UtcDatetime = Field(default_factory=utcnow)

    @property
    def revised(self) -> bool:
        return self.revised_geometry is not None or self.revised_valid_until is not None

    @model_validator(mode="after")
    def _revisions_match_the_verdict(self) -> CriticVerdict:
        if self.verdict is Verdict.REVISE and not self.revised:
            raise ValueError("REVISE needs a revised_geometry or a revised_valid_until")
        if self.verdict is not Verdict.REVISE and self.revised:
            raise ValueError(f"{self.verdict} must not carry revisions")
        return self


class AdvisoryProperties(HeadsUpModel):
    """The advisory contract, carried in the Feature's `properties`."""

    id: Identifier
    version: int = Field(default=1, ge=1)
    hazard: Hazard
    action: Action
    valid_from: UtcDatetime
    valid_until: UtcDatetime
    evidence: list[Evidence] = Field(min_length=1)
    policy_rule: Identifier
    issuer: Issuer
    signature: str | None = None

    @model_validator(mode="after")
    def _window_is_ordered(self) -> AdvisoryProperties:
        _check_window(self.valid_from, self.valid_until)
        return self


class Advisory(HeadsUpModel):
    """A published advisory: a GeoJSON Feature, signed and expiring."""

    type: Literal["Feature"] = "Feature"
    id: Identifier | None = None
    geometry: Geometry
    properties: AdvisoryProperties

    @model_validator(mode="before")
    @classmethod
    def _mirror_feature_id(cls, data: Any) -> Any:
        """GeoJSON readers look at `id`; Heads Up readers look at `properties.id`."""
        if isinstance(data, dict) and not data.get("id"):
            properties = data.get("properties")
            if isinstance(properties, dict):
                data = {**data, "id": properties.get("id")}
            elif isinstance(properties, AdvisoryProperties):
                data = {**data, "id": properties.id}
        return data

    @model_validator(mode="after")
    def _ids_agree(self) -> Advisory:
        if self.id != self.properties.id:
            raise ValueError(
                f"feature id {self.id!r} does not match properties.id {self.properties.id!r}"
            )
        return self

    @property
    def hazard(self) -> Hazard:
        return self.properties.hazard

    @property
    def action(self) -> Action:
        return self.properties.action

    def signing_bytes(self) -> bytes:
        """Canonical JSON of the feature without the signature: what Ed25519 covers."""
        payload = self.to_wire()
        payload["properties"].pop("signature", None)
        return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)

    def is_active(self, at: datetime | None = None) -> bool:
        """True while `at` (default now) is inside the validity window."""
        moment = at or utcnow()
        return self.properties.valid_from <= moment < self.properties.valid_until


def _check_window(valid_from: datetime, valid_until: datetime) -> None:
    if valid_until <= valid_from:
        raise ValueError(
            f"valid_until {valid_until.isoformat()} is not after "
            f"valid_from {valid_from.isoformat()}"
        )
