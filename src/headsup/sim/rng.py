"""Seeded randomness, split by name so features cannot disturb each other.

The problem this solves: if every draw comes from one sequential generator,
adding a feature that draws once shifts every later draw, and a run that was
comparable to last week's no longer is. So every decision domain gets its own
substream, derived from the seed and a label rather than from draw order.

Adding a feature later adds a *label*, never a shift. Keying op_b's acceptance
delay on `(advisory_id, version)` rather than on an arrival counter is the same
idea applied within a domain: an extra advisory, or advisories arriving in a
different order, moves nothing else.
"""

from __future__ import annotations

import random
from hashlib import blake2b

#: Separates labels so ("a", "bc") and ("ab", "c") cannot collide.
_SEP = b"\x1f"


def _label_bytes(labels: tuple[object, ...]) -> bytes:
    return _SEP.join(str(part).encode("utf-8") for part in labels)


def split_seed(seed: int, *labels: object) -> int:
    """A 64-bit seed derived from a run seed and a label path.

    `blake2b` keyed by the seed, not seeded by `hash()`: Python's `hash` is
    randomised per process for strings, which would make a run reproducible
    only within one interpreter.
    """
    key = seed.to_bytes(8, "little", signed=False) if seed >= 0 else abs(seed).to_bytes(8, "little")
    digest = blake2b(_label_bytes(labels), key=key, digest_size=8).digest()
    return int.from_bytes(digest, "little")


def substream(seed: int, *labels: object) -> random.Random:
    """A fresh `random.Random` for one decision domain."""
    return random.Random(split_seed(seed, *labels))


class SeedTree:
    """Named substreams for one run, memoised so a domain keeps its sequence.

    Two access patterns, deliberately distinct:

    * `stream(*labels)` returns a stateful generator, for a domain that draws
      repeatedly in a fixed order (initial placement walking vehicles in index
      order, for instance).
    * `draw_*(*labels, ...)` is a one-shot from a fresh generator keyed by the
      full label path, for a decision that must not depend on how many times
      the domain has been consulted before. Trip destinations and op_b's delay
      use this, which is what makes them stable under reordering.
    """

    __slots__ = ("_seed", "_streams")

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._streams: dict[tuple[object, ...], random.Random] = {}

    @property
    def seed(self) -> int:
        return self._seed

    def stream(self, *labels: object) -> random.Random:
        """The stateful generator for a domain, created on first use."""
        existing = self._streams.get(labels)
        if existing is None:
            existing = substream(self._seed, *labels)
            self._streams[labels] = existing
        return existing

    def draw_int(self, *labels: object, lo: int, hi: int) -> int:
        """A one-shot integer in `[lo, hi]`, keyed only by the label path."""
        return substream(self._seed, *labels).randint(lo, hi)

    def draw_float(self, *labels: object, lo: float = 0.0, hi: float = 1.0) -> float:
        """A one-shot float in `[lo, hi)`, keyed only by the label path."""
        return substream(self._seed, *labels).uniform(lo, hi)

    def draw_choice(self, *labels: object, options: tuple[int, ...]) -> int:
        """A one-shot pick from an ordered tuple, keyed only by the label path.

        `options` must be a tuple, not a set: a set's iteration order is not
        guaranteed across builds, which would silently break reproducibility.
        """
        if not options:
            raise ValueError("cannot choose from an empty option tuple")
        index = substream(self._seed, *labels).randrange(len(options))
        return options[index]
