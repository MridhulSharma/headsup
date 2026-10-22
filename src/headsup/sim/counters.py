"""Counter names and integer accumulation.

`Telemetry.counters` is `dict[str, int]`, so every value here is an integer.
That is why blocked-corridor time accumulates as **vehicle-seconds**: the tick
is exactly one second, so the count is exact, and minutes are derived by floor
division when a snapshot is taken. Accumulating minutes directly would throw
away everything under sixty seconds.

These key names are a contract. D3.5's side-by-side panel and D4.1's ablation
table read them, and `docs/build-packet.md` already calls the corridor metric
"blocked corridor minutes", so that spelling is preserved here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

SECONDS_PER_MINUTE = 60

#: Per-operator metric families. `{metric}_{op}` plus a `{metric}_total`.
HAZARD_ENTRIES = "hazard_entries"
BLOCKED_SECONDS = "blocked_corridor_vehicle_seconds"
BLOCKED_MINUTES = "blocked_corridor_vehicle_minutes"
CORRIDOR_ENTRIES = "corridor_entries"
ASSIST_OPEN = "assist_open"
ASSIST_OPENED = "assist_opened"
RIDES_COMPLETED = "rides_completed"
VEHICLES_STALLED = "vehicles_stalled"
VEHICLES_REROUTED = "vehicles_rerouted"

#: Run-level counters with no operator dimension.
ADVISORIES_SEEN = "advisories_seen"
ADVISORIES_ACCEPTED = "advisories_accepted"
ADVISORIES_DECLINED = "advisories_declined"
ADVISORIES_REJECTED = "advisories_rejected"

#: Families that are gauges, not cumulative totals: they may fall. The
#: property test that asserts monotonicity skips exactly these.
GAUGES: frozenset[str] = frozenset({ASSIST_OPEN})

#: Families derived at snapshot time rather than accumulated.
DERIVED: frozenset[str] = frozenset({BLOCKED_MINUTES})

PER_OPERATOR: tuple[str, ...] = (
    HAZARD_ENTRIES,
    BLOCKED_SECONDS,
    BLOCKED_MINUTES,
    CORRIDOR_ENTRIES,
    ASSIST_OPEN,
    ASSIST_OPENED,
    RIDES_COMPLETED,
    VEHICLES_STALLED,
    VEHICLES_REROUTED,
)

RUN_LEVEL: tuple[str, ...] = (
    ADVISORIES_SEEN,
    ADVISORIES_ACCEPTED,
    ADVISORIES_DECLINED,
    ADVISORIES_REJECTED,
)


def key(metric: str, op: str | None = None) -> str:
    """`hazard_entries_op_a`, or `hazard_entries_total` when `op` is None."""
    return f"{metric}_{op or 'total'}"


def expected_keys(operators: Iterable[str]) -> tuple[str, ...]:
    """Every key a snapshot will carry, in a stable order. For tests."""
    ops = tuple(operators)
    names: list[str] = []
    for metric in PER_OPERATOR:
        names.extend(key(metric, op) for op in ops)
        names.append(key(metric))
    names.extend(RUN_LEVEL)
    return tuple(names)


class Counters:
    """Integer counters for one fleet set, keyed by metric and operator.

    Totals are not stored; they are summed at snapshot time, so a bump can
    never leave a total disagreeing with its parts.
    """

    __slots__ = ("_operators", "_per_op", "_run")

    def __init__(self, operators: Iterable[str]) -> None:
        self._operators: tuple[str, ...] = tuple(operators)
        self._per_op: dict[str, dict[str, int]] = {
            metric: dict.fromkeys(self._operators, 0)
            for metric in PER_OPERATOR
            if metric not in DERIVED
        }
        self._run: dict[str, int] = dict.fromkeys(RUN_LEVEL, 0)

    @property
    def operators(self) -> tuple[str, ...]:
        return self._operators

    def bump(self, metric: str, op: str, by: int = 1) -> None:
        """Add to one operator's counter. `by` may be negative for a gauge."""
        try:
            row = self._per_op[metric]
        except KeyError:
            raise KeyError(f"{metric!r} is not a per-operator counter") from None
        if op not in row:
            raise KeyError(f"{op!r} is not an operator of this fleet set")
        row[op] += by

    def bump_run(self, metric: str, by: int = 1) -> None:
        """Add to a run-level counter."""
        if metric not in self._run:
            raise KeyError(f"{metric!r} is not a run-level counter")
        self._run[metric] += by

    def get(self, metric: str, op: str) -> int:
        return self._per_op[metric][op]

    def set_gauge(self, metric: str, op: str, value: int) -> None:
        """Replace a gauge outright, for a queue depth recomputed each tick."""
        if metric not in GAUGES:
            raise KeyError(f"{metric!r} is not a gauge; use bump")
        self._per_op[metric][op] = value

    def snapshot(self) -> dict[str, int]:
        """Every counter as a flat `dict[str, int]`, ready for `Telemetry`.

        Blocked minutes are derived here by floor division, so the published
        minute figure never loses the seconds underneath it.
        """
        out: dict[str, int] = {}
        for metric, row in self._per_op.items():
            total = 0
            for op in self._operators:
                value = row[op]
                out[key(metric, op)] = value
                total += value
            out[key(metric)] = total
        for op in self._operators:
            seconds = self._per_op[BLOCKED_SECONDS][op]
            out[key(BLOCKED_MINUTES, op)] = seconds // SECONDS_PER_MINUTE
        out[key(BLOCKED_MINUTES)] = (
            out[key(BLOCKED_SECONDS)] // SECONDS_PER_MINUTE  # from the total, not a sum of floors
        )
        out.update(self._run)
        return out

    def blocked_minutes(self, op: str | None = None) -> float:
        """Blocked corridor vehicle-minutes as a float, for the CLI report."""
        if op is not None:
            return self._per_op[BLOCKED_SECONDS][op] / SECONDS_PER_MINUTE
        total = sum(self._per_op[BLOCKED_SECONDS].values())
        return total / SECONDS_PER_MINUTE


def merge_snapshots(snapshots: Mapping[str, Mapping[str, int]]) -> dict[str, dict[str, int]]:
    """Group snapshots by mode name, for a report that shows live beside ghost."""
    return {mode: dict(values) for mode, values in snapshots.items()}
