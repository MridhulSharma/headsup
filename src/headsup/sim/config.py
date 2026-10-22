"""Every tunable of a simulation run, in one frozen place.

A run is reproducible from `SimConfig` plus the city world and the hazard
field, and nothing else: no environment, no wall clock, no Redis. That is what
lets the determinism test build two simulations in one process and compare
trajectory hashes, and what lets `hu eval` record a variant as a config.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

#: The three operator personas of D2.2. Fixed: the brief names them, the
#: counters are keyed by them, and the ablation table compares them.
PERSONAS: tuple[str, ...] = ("op_a", "op_b", "op_c")

#: `--speed 0` means "as fast as the laptop will go", for the throughput check.
UNPACED = 0.0


class SimMode(StrEnum):
    """Whether a fleet set listens to advisories or ignores them.

    `GHOST` is the V0 baseline: the same seed, the same physics, the same
    hazards, and no advice. It is a mode rather than a separate code path so
    that D3.5 can run both on one clock without a second implementation.
    """

    LIVE = "live"
    GHOST = "ghost"


@dataclass(frozen=True, slots=True)
class SimConfig:
    """One run's parameters. Validated once, by `validate`, before anything runs."""

    city: str
    seed: int
    vehicles: int = 900
    fleets: int = 3
    speed: float = 10.0

    #: Destination zones. Labels cost O(zones) per mask, so this is the single
    #: number that bounds all routing work. See `router.Router`.
    zones: int = 48
    #: When true, trips may end at any core vertex and the router falls back to
    #: per-vehicle shortest paths. Off by default: it is ~13x more routing work
    #: (measured 432 ms vs 23 ms per mask) and exists for evaluation only.
    any_vertex_destinations: bool = False

    #: Telemetry rate in **wall-clock** Hz. A 1-second tick cannot emit 2 Hz of
    #: sim time, and the viewer socket wants 2-5 Hz of real time, so the stride
    #: is derived from `speed`. See `telemetry_stride`.
    telemetry_hz: float = 2.0
    tick_s: int = 1

    #: Reverse-Dijkstra labels refreshed per tick. Amortises a mask change
    #: instead of paying for every zone at once.
    label_budget: int = 8

    stall_to_assist_s: int = 15
    recovery_s: int = 20
    all_way_stop_delay_s: int = 4
    dwell_s: int = 30
    #: Hops before a vehicle is declared lost and asks for help.
    hop_budget: int = 400
    keep_clear_default_width_m: float = 12.0

    #: Congestion multiplier range, drawn once per edge from the seeded
    #: `("congestion", "base")` substream. Static on purpose: a density-coupled
    #: factor would make the live and ghost fleets diverge for reasons other
    #: than the advice, which is the one thing the baseline must isolate.
    congestion_low: float = 0.6
    congestion_high: float = 1.0

    def validate(self) -> None:
        """Raise `ValueError` on anything the engine cannot honour.

        The CLI turns these into exit code 2, so the message is what the user
        reads; say what is wrong and what the bound is.
        """
        if not self.city:
            raise ValueError("a city is required")
        if self.vehicles < 1:
            raise ValueError(f"--vehicles must be at least 1, got {self.vehicles}")
        if not 1 <= self.fleets <= len(PERSONAS):
            raise ValueError(
                f"--fleets must be 1 to {len(PERSONAS)} "
                f"(one per persona: {', '.join(PERSONAS)}), got {self.fleets}"
            )
        if self.vehicles % self.fleets:
            raise ValueError(
                f"--vehicles {self.vehicles} does not divide evenly into "
                f"{self.fleets} fleets; use a multiple of {self.fleets}"
            )
        if self.zones < 2:
            raise ValueError(f"--zones must be at least 2, got {self.zones}")
        if self.zones > self.vehicles:
            raise ValueError(
                f"--zones {self.zones} exceeds --vehicles {self.vehicles}; "
                "there would be more destinations than vehicles"
            )
        if self.telemetry_hz <= 0:
            raise ValueError(f"--telemetry-hz must be positive, got {self.telemetry_hz}")
        if self.label_budget < 1:
            raise ValueError(f"label_budget must be at least 1, got {self.label_budget}")
        if self.tick_s != 1:
            raise ValueError("D2.2 fixes the tick at 1 second; the trajectory hash assumes it")
        if not 0.0 < self.congestion_low <= self.congestion_high <= 1.0:
            raise ValueError(
                "congestion must satisfy 0 < low <= high <= 1, got "
                f"low={self.congestion_low} high={self.congestion_high}"
            )
        if self.speed != UNPACED and not 1.0 <= self.speed <= 60.0:
            raise ValueError(f"--speed must be 0 (unpaced) or 1 to 60, got {self.speed}")

    @property
    def per_fleet(self) -> int:
        """Vehicles in each fleet. Equal by construction so operators compare."""
        return self.vehicles // self.fleets

    @property
    def personas(self) -> tuple[str, ...]:
        return PERSONAS[: self.fleets]

    @property
    def telemetry_stride(self) -> int:
        """Ticks between telemetry frames, so frames land at `telemetry_hz` wall.

        At `--speed 10` and 2 Hz that is every 5th tick. Unpaced runs publish
        on the same stride as 1x, because there is no wall-clock rate to track.
        """
        speed = 1.0 if self.speed == UNPACED else self.speed
        return max(1, round(speed / self.telemetry_hz))

    def replace(self, **changes: object) -> SimConfig:
        """A copy with fields overridden. For tests and for variant sweeps."""
        return replace(self, **changes)  # type: ignore[arg-type]
