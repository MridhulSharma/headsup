"""Closed vocabularies shared by the feeds, the agents and the fleets."""

from __future__ import annotations

from enum import StrEnum


class Source(StrEnum):
    """A public feed Heads Up ingests. Values double as `events.<source>` names."""

    NWS = "nws"
    USGS = "usgs"
    WZDX = "wzdx"
    AUSTIN_INCIDENTS = "austin_incidents"
    AUSTIN_SIGNALS = "austin_signals"
    BEXAR = "bexar"
    CITY_DISPATCH = "city_dispatch"
    SYNTHETIC = "synthetic"


#: Sources that can carry a severity 3+ hazard on their own (D2.5 evidence
#: threshold); anything else needs two independent sources.
AUTHORITATIVE_SOURCES = frozenset({Source.NWS, Source.USGS, Source.CITY_DISPATCH})


class HazardClass(StrEnum):
    """What kind of hazard a fused observation describes."""

    FLOOD = "FLOOD"
    WORK_ZONE = "WORK_ZONE"
    SIGNAL_OUTAGE = "SIGNAL_OUTAGE"
    EMERGENCY_SCENE = "EMERGENCY_SCENE"
    SCHOOL_ZONE = "SCHOOL_ZONE"
    SEVERE_WEATHER = "SEVERE_WEATHER"


class ActionType(StrEnum):
    """What an advisory asks a fleet to do. The playbook limits this per class."""

    NO_ENTRY = "NO_ENTRY"
    SPEED_CAP = "SPEED_CAP"
    ALL_WAY_STOP = "ALL_WAY_STOP"
    DENSITY_CAP = "DENSITY_CAP"
    KEEP_CLEAR = "KEEP_CLEAR"
    PAUSE_SERVICE = "PAUSE_SERVICE"


class Verdict(StrEnum):
    """The critic's answer on one proposed advisory."""

    APPROVE = "APPROVE"
    REVISE = "REVISE"
    ESCALATE = "ESCALATE"


class AckStatus(StrEnum):
    """How an operator answered an advisory."""

    ACCEPTED = "ACCEPTED"
    ACCEPTED_PARTIAL = "ACCEPTED_PARTIAL"
    DECLINED = "DECLINED"


class VehicleStatus(StrEnum):
    """Simulated vehicle state, as the viewer colours it."""

    MOVING = "MOVING"
    STOPPED = "STOPPED"
    STALLED = "STALLED"
    AWAITING_ASSIST = "AWAITING_ASSIST"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


class AssistReason(StrEnum):
    """Why a vehicle asked for remote assistance."""

    FLOODED_EDGE = "FLOODED_EDGE"
    DARK_SIGNAL = "DARK_SIGNAL"
    BLOCKED_ROUTE = "BLOCKED_ROUTE"
    EMERGENCY_SCENE = "EMERGENCY_SCENE"
    OTHER = "OTHER"
