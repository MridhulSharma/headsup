"""The trajectory hash: one value that says two runs took the same path.

Folded every tick, over integer state only, in vehicle-index order. Because
the cadence is counted in ticks rather than in telemetry frames, a run at
`--speed 1` and the same run at `--speed 10` produce the same digest -- the
pacing is wall-clock dressing on identical simulation steps, and the hash is
how we prove it.

Checkpoints exist so a failure is diagnosable: "the hashes differ" sends you
looking everywhere, "they diverged in minute 4" sends you to one tick.
"""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import blake2b
from struct import Struct

from headsup.sim.vehicles import STATUS_ORD, Vehicle

#: ix, edge, offset_mm, status ordinal. Little-endian and fixed width, so the
#: bytes do not depend on the platform or on how Python renders a float.
_RECORD = Struct("<IiIB")
_TICK = Struct("<I")

#: Ticks between checkpoint digests. Sixty, so a checkpoint is one sim-minute.
CHECKPOINT_TICKS = 60


class TrajectoryHash:
    """A rolling digest of every vehicle position at every tick."""

    __slots__ = ("_checkpoints", "_rolling", "_ticks")

    def __init__(self) -> None:
        self._rolling = blake2b(digest_size=16)
        self._checkpoints: list[tuple[int, str]] = []
        self._ticks = 0

    def fold(self, tick: int, vehicles: Sequence[Vehicle]) -> None:
        """Absorb one tick. `vehicles` must be in ascending `ix` order."""
        self._rolling.update(_TICK.pack(tick))
        pack = _RECORD.pack
        self._rolling.update(
            b"".join(pack(v.ix, v.edge, v.offset_mm, STATUS_ORD[v.status]) for v in vehicles)
        )
        self._ticks += 1
        if tick and tick % CHECKPOINT_TICKS == 0:
            self._checkpoints.append((tick, self._rolling.hexdigest()))

    def digest(self) -> str:
        """The hash of everything folded so far."""
        return self._rolling.hexdigest()

    @property
    def ticks(self) -> int:
        return self._ticks

    def checkpoints(self) -> tuple[tuple[int, str], ...]:
        """`(tick, digest)` every sim-minute, for localising a divergence."""
        return tuple(self._checkpoints)


def first_divergence(
    left: Sequence[tuple[int, str]], right: Sequence[tuple[int, str]]
) -> int | None:
    """The first checkpoint tick where two runs disagree, or None if they match.

    Used by the determinism test to report *where* rather than just *that*.
    """
    for (tick, a), (other_tick, b) in zip(left, right, strict=False):
        if tick != other_tick:
            return min(tick, other_tick)
        if a != b:
            return tick
    return None
