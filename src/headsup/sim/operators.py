"""The three operator personas, and the mask state machine behind each one.

From the D2.2 brief: `op_a` accepts every advisory immediately, `op_b` accepts
after a 30-90 second delay, and `op_c` declines anything whose confidence is
under 0.7. The personas are fixed rather than configurable because the whole
point is a controlled comparison -- the ablation table reads these names.

`op_b`'s delay is drawn from a substream keyed by `(advisory_id, version)`,
not by an arrival counter. That matters: an extra advisory appearing, or two
advisories arriving in a different order, must not shift the delay any other
advisory gets, or two runs of one seed stop being comparable.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from headsup.contracts import Ack, AckStatus, Advisory
from headsup.sim.masks import EMPTY_MASK, Mask, merge
from headsup.sim.rng import SeedTree

#: op_b's acceptance delay window, in seconds, straight from the brief.
OP_B_DELAY_LO = 30
OP_B_DELAY_HI = 90

#: op_c's confidence floor, straight from the brief.
OP_C_CONFIDENCE_FLOOR = 0.7


@dataclass(frozen=True, slots=True)
class Decision:
    """How an operator answered one advisory, and when it takes effect."""

    status: AckStatus
    reason: str | None
    effective_at: datetime

    @property
    def accepted(self) -> bool:
        return self.status is not AckStatus.DECLINED


@dataclass(frozen=True, slots=True)
class Persona:
    """One operator's fixed disposition toward advice."""

    name: str
    delay_lo: int = 0
    delay_hi: int = 0
    confidence_floor: float = 0.0
    #: A ghost operator sees every advisory and acts on none of them.
    ignores: bool = False

    def decide(self, advisory: Advisory, at: datetime, seeds: SeedTree) -> Decision:
        """Answer one advisory. Pure: same inputs, same answer, always."""
        if self.ignores:
            return Decision(AckStatus.DECLINED, "ghost fleet ignores advisories", at)

        confidence = float(advisory.hazard.confidence)
        if confidence < self.confidence_floor:
            return Decision(
                AckStatus.DECLINED,
                (
                    f"hazard confidence {confidence:.2f} is below "
                    f"{self.name}'s {self.confidence_floor:.2f} threshold"
                ),
                at,
            )

        delay = 0
        if self.delay_hi > 0:
            delay = seeds.draw_int(
                self.name,
                "delay",
                advisory.properties.id,
                advisory.properties.version,
                lo=self.delay_lo,
                hi=self.delay_hi,
            )
        return Decision(AckStatus.ACCEPTED, None, at + timedelta(seconds=delay))


OP_A = Persona(name="op_a")
OP_B = Persona(name="op_b", delay_lo=OP_B_DELAY_LO, delay_hi=OP_B_DELAY_HI)
OP_C = Persona(name="op_c", confidence_floor=OP_C_CONFIDENCE_FLOOR)

PERSONA_BY_NAME: dict[str, Persona] = {OP_A.name: OP_A, OP_B.name: OP_B, OP_C.name: OP_C}


def ghost_persona(name: str) -> Persona:
    """A ghost twin of a persona: same name, no reaction to advice.

    The name is preserved so live and ghost counters line up operator by
    operator. A ghost fleet with one undifferentiated persona could not be
    compared against three live fleets.
    """
    return Persona(name=name, ignores=True)


@dataclass(slots=True)
class _Held:
    """An advisory an operator has answered, with its resolved mask."""

    advisory: Advisory
    mask: Mask
    decision: Decision
    effective: bool = False


@dataclass(slots=True)
class MaskChange:
    """What changed for one operator on one tick."""

    changed: bool = False
    #: Advisories that just took effect, in the order they were offered.
    newly_effective: tuple[Advisory, ...] = ()
    #: Advisory ids that just expired or were withdrawn.
    expired: tuple[str, ...] = ()


class Operator:
    """One operator's accepted advisories and the mask they add up to.

    Holds no vehicles: the engine owns those. This class only answers "what is
    this operator driving under right now", which is what keeps the persona
    logic testable without a simulation.
    """

    __slots__ = ("_held", "_mask", "_order", "_pending_acks", "_persona", "_seeds")

    def __init__(self, persona: Persona, seeds: SeedTree) -> None:
        self._persona = persona
        self._seeds = seeds
        self._held: dict[str, _Held] = {}
        #: Offer order, so iteration never depends on dict insertion semantics
        #: being stable across versions.
        self._order: list[str] = []
        self._mask: Mask = EMPTY_MASK
        self._pending_acks: list[Ack] = []

    @property
    def name(self) -> str:
        return self._persona.name

    @property
    def persona(self) -> Persona:
        return self._persona

    @property
    def mask(self) -> Mask:
        """The union of every advisory currently in force for this operator."""
        return self._mask

    @property
    def ignores_advisories(self) -> bool:
        return self._persona.ignores

    def offer(self, advisory: Advisory, mask: Mask, at: datetime) -> Decision:
        """Put an advisory to this operator. Returns its answer.

        A re-issue of the same advisory id at a higher version is a fresh
        decision; the same version again is ignored, so a redelivered stream
        entry cannot double-count an ack.
        """
        key = advisory.properties.id
        existing = self._held.get(key)
        if existing is not None:
            if existing.advisory.properties.version >= advisory.properties.version:
                return existing.decision
            self._drop(key)

        decision = self._persona.decide(advisory, at, self._seeds)
        self._held[key] = _Held(advisory=advisory, mask=mask, decision=decision)
        self._order.append(key)

        if not decision.accepted:
            # A decline is final and immediate, and the Ack contract requires
            # a reason for anything other than ACCEPTED.
            self._pending_acks.append(
                Ack(
                    advisory_id=key,
                    advisory_version=advisory.properties.version,
                    operator=self.name,
                    status=decision.status,
                    reason=decision.reason,
                    acknowledged_at=at,
                )
            )
        return decision

    def tick(self, at: datetime) -> MaskChange:
        """Promote due acceptances, drop expired advisories, rebuild the mask."""
        change = MaskChange()
        newly: list[Advisory] = []
        expired: list[str] = []

        for key in list(self._order):
            held = self._held.get(key)
            if held is None:
                continue
            if not held.advisory.is_active(at=at):
                self._drop(key)
                expired.append(key)
                continue
            if held.decision.accepted and not held.effective and at >= held.decision.effective_at:
                held.effective = True
                newly.append(held.advisory)

        if newly or expired:
            change.changed = True
            change.newly_effective = tuple(newly)
            change.expired = tuple(expired)
            self._rebuild()
        return change

    def active_masks(self) -> tuple[Mask, ...]:
        """Masks currently in force, in offer order."""
        return tuple(
            self._held[key].mask
            for key in self._order
            if key in self._held and self._held[key].effective
        )

    def accepted_ids(self) -> tuple[str, ...]:
        return tuple(key for key in self._order if key in self._held and self._held[key].effective)

    def drain_acks(self) -> list[Ack]:
        """Take the acks produced since the last drain."""
        acks, self._pending_acks = self._pending_acks, []
        return acks

    def queue_ack(self, ack: Ack) -> None:
        """Hand back an ack the engine built once it knew the reroute count."""
        self._pending_acks.append(ack)

    def _drop(self, key: str) -> None:
        self._held.pop(key, None)
        with contextlib.suppress(ValueError):
            self._order.remove(key)

    def _rebuild(self) -> None:
        if self._persona.ignores:
            self._mask = EMPTY_MASK
            return
        self._mask = merge(self.active_masks())


@dataclass(slots=True)
class OperatorSet:
    """The operators of one fleet set, in persona order."""

    operators: tuple[Operator, ...]
    by_name: dict[str, Operator] = field(default_factory=dict)

    @classmethod
    def build(cls, personas: tuple[Persona, ...], seeds: SeedTree) -> OperatorSet:
        operators = tuple(Operator(persona, seeds) for persona in personas)
        return cls(operators=operators, by_name={op.name: op for op in operators})

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(op.name for op in self.operators)

    def live_mask_keys(self) -> frozenset[str]:
        return frozenset(op.mask.key for op in self.operators)
