"""The three personas, exactly as the brief specifies them."""

from __future__ import annotations

from datetime import timedelta

from fixtures.sim import advisory_for, grid_net, tiny_config
from headsup.contracts import AckStatus
from headsup.sim.masks import resolve
from headsup.sim.operators import (
    OP_A,
    OP_B,
    OP_B_DELAY_HI,
    OP_B_DELAY_LO,
    OP_C,
    OP_C_CONFIDENCE_FLOOR,
    Operator,
    ghost_persona,
)
from headsup.sim.rng import SeedTree
from samples import T0

CFG = tiny_config()


def _operator(persona, seed: int = 42) -> Operator:
    return Operator(persona, SeedTree(seed))


def _mask(advisory):
    return resolve(advisory, grid_net(), CFG)


def test_op_a_accepts_on_the_tick_it_is_offered() -> None:
    advisory = advisory_for()
    op = _operator(OP_A)
    decision = op.offer(advisory, _mask(advisory), T0)
    assert decision.status is AckStatus.ACCEPTED
    assert decision.effective_at == T0
    assert op.tick(T0).newly_effective == (advisory,)
    assert not op.mask.empty


def test_op_b_accepts_after_a_delay_inside_the_specified_window() -> None:
    advisory = advisory_for()
    op = _operator(OP_B)
    decision = op.offer(advisory, _mask(advisory), T0)
    delay = (decision.effective_at - T0).total_seconds()
    assert OP_B_DELAY_LO <= delay <= OP_B_DELAY_HI
    assert decision.status is AckStatus.ACCEPTED

    assert op.tick(T0).newly_effective == ()
    assert op.mask.empty
    assert op.tick(T0 + timedelta(seconds=delay)).newly_effective == (advisory,)
    assert not op.mask.empty


def test_op_bs_delay_is_stable_for_the_same_advisory_and_seed() -> None:
    advisory = advisory_for()
    first = _operator(OP_B).offer(advisory, _mask(advisory), T0).effective_at
    second = _operator(OP_B).offer(advisory, _mask(advisory), T0).effective_at
    assert first == second


def test_op_bs_delay_does_not_shift_when_another_advisory_arrives_first() -> None:
    """Keyed by `(advisory id, version)`, not by arrival order.

    If this fails, adding one advisory to a scenario silently changes how
    every later one is handled, and two runs stop being comparable.
    """
    target = advisory_for(advisory_id="adv-target")
    noise = advisory_for(advisory_id="adv-noise", confidence=0.95)

    alone = _operator(OP_B)
    solo = alone.offer(target, _mask(target), T0).effective_at

    crowded = _operator(OP_B)
    crowded.offer(noise, _mask(noise), T0)
    assert crowded.offer(target, _mask(target), T0).effective_at == solo


def test_op_bs_delay_differs_between_advisories() -> None:
    """A single shared delay would be a suspiciously tidy fleet."""
    op = _operator(OP_B)
    delays = set()
    for index in range(8):
        advisory = advisory_for(advisory_id=f"adv-{index}")
        decision = op.offer(advisory, _mask(advisory), T0)
        delays.add((decision.effective_at - T0).total_seconds())
    assert len(delays) > 1


def test_op_c_declines_just_below_the_confidence_floor() -> None:
    advisory = advisory_for(confidence=0.69)
    op = _operator(OP_C)
    decision = op.offer(advisory, _mask(advisory), T0)
    assert decision.status is AckStatus.DECLINED
    assert op.mask.empty


def test_op_c_accepts_exactly_at_the_confidence_floor() -> None:
    """The threshold is "under 0.7", so 0.70 itself is acceptable."""
    advisory = advisory_for(confidence=OP_C_CONFIDENCE_FLOOR)
    decision = _operator(OP_C).offer(advisory, _mask(advisory), T0)
    assert decision.status is AckStatus.ACCEPTED


def test_a_declined_ack_carries_a_reason_because_the_contract_demands_one() -> None:
    """`Ack` raises for any status other than ACCEPTED without a reason."""
    advisory = advisory_for(confidence=0.5)
    op = _operator(OP_C)
    op.offer(advisory, _mask(advisory), T0)
    acks = op.drain_acks()
    assert len(acks) == 1
    assert acks[0].status is AckStatus.DECLINED
    assert acks[0].reason
    assert "0.50" in acks[0].reason
    assert acks[0].advisory_version == advisory.properties.version


def test_an_accepted_advisory_produces_no_ack_from_the_operator_itself() -> None:
    """The engine emits it, once it knows the reroute count."""
    advisory = advisory_for()
    op = _operator(OP_A)
    op.offer(advisory, _mask(advisory), T0)
    assert op.drain_acks() == []


def test_the_same_version_offered_twice_is_answered_once() -> None:
    """A redelivered stream entry must not double-count an ack."""
    advisory = advisory_for(confidence=0.5)
    op = _operator(OP_C)
    first = op.offer(advisory, _mask(advisory), T0)
    second = op.offer(advisory, _mask(advisory), T0 + timedelta(seconds=5))
    assert first == second
    assert len(op.drain_acks()) == 1


def test_a_higher_version_is_a_fresh_decision() -> None:
    low = advisory_for(advisory_id="adv-x", version=1, confidence=0.5)
    high = advisory_for(advisory_id="adv-x", version=2, confidence=0.95)
    op = _operator(OP_C)
    assert op.offer(low, _mask(low), T0).status is AckStatus.DECLINED
    assert op.offer(high, _mask(high), T0).status is AckStatus.ACCEPTED


def test_an_expired_advisory_drops_out_of_the_mask() -> None:
    advisory = advisory_for(valid_from=T0, valid_until=T0 + timedelta(seconds=60))
    op = _operator(OP_A)
    op.offer(advisory, _mask(advisory), T0)
    op.tick(T0)
    assert not op.mask.empty
    change = op.tick(T0 + timedelta(seconds=61))
    assert change.expired == (advisory.properties.id,)
    assert op.mask.empty


def test_a_ghost_operator_keeps_its_name_and_ignores_everything() -> None:
    """Names are kept so live and ghost counters compare operator by operator."""
    persona = ghost_persona("op_a")
    assert persona.name == "op_a"
    advisory = advisory_for()
    op = Operator(persona, SeedTree(42))
    assert op.offer(advisory, _mask(advisory), T0).status is AckStatus.DECLINED
    op.tick(T0)
    assert op.mask.empty
    assert op.ignores_advisories
