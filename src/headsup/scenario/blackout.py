"""S2: a synthetic signal blackout on the real downtown Austin signal map.

S1 is a real event replayed from recorded feeds. S2 cannot be: the city's
signal status feed serves only the present, and a downtown feeder failure big
enough to darken a dozen intersections has not happened while this prototype
has been recording. So S2 is generated - and labelled. `scripted: true` in
`scenarios/s2_blackout.yaml` is what makes the viewer badge the run, and this
module is the only thing in the repository that invents a feed payload.

What is real and what is not:

* **Real:** every signal id, intersection name and coordinate. They come from
  the city's own signal inventory, through `scenario.inventory`, so the
  cluster sits on the actual grid between Congress Avenue and Lavaca Street
  and the hull a sentinel draws is the shape of real city blocks.
* **Real:** the shape of the payload. The records are the columns of
  `5zpr-dehc`, in its order, with its state codes and its naive
  Austin-local timestamps, so `AustinSignalsAdapter.parse` is exercised
  exactly as it is on the live feed.
* **Invented:** which signals are dark, and when.

The timeline is built to exercise the sentinel's threshold rather than to
flatter it. Three signals flash first, which must publish nothing; the cluster
then fills out, holds, and clears in two steps - the second of which drops it
back under `MIN_SIGNALS`. Two distant signals flash for the whole window as
noise DBSCAN has to label as noise, and inside the cluster area sit a
scheduled flash and two communication issues, which the sentinel has to ignore
on their state alone. If any of those four were counted, the hull would change
shape and `tests/unit/test_scenario_blackout.py` would fail.

Everything is derived from `--seed`, so the same seed writes a byte-identical
recording.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

from headsup.contracts import Source
from headsup.ingest.austin_signals import (
    SCHEDULED_FLASH_STATE,
    SIGNAL_STATUS_DATASET,
    SODA_BASE,
    AustinSignalsAdapter,
)
from headsup.ingest.base import RawPayload, make_ref
from headsup.ingest.recorder import FIXTURE_DIR, Recorder, scenario_dir
from headsup.scenario.inventory import Signal, SignalInventory
from headsup.sim.scenarios import AUSTIN_CLUSTER_HALF_M, AUSTIN_DOWNTOWN
from headsup.world.bbox import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR

SCENARIO = "s2_blackout"
CITY = "austin"

#: The scenario's window. A Wednesday evening peak, 19:30 to 21:00 local: the
#: hour in which a dark downtown grid costs a fleet the most.
WINDOW_START = datetime(2026, 10, 22, 0, 30, tzinfo=UTC)
DEFAULT_MINUTES = 90.0

#: The status feed's own refresh cadence, which is the recording's poll gap.
POLL_SECONDS = 300.0

#: Minutes from the window start, by phase:
ONSET_MIN = 10.0  # the first three signals drop out: under the threshold
FULL_MIN = 15.0  # the rest of the cluster follows: one candidate
PARTIAL_MIN = 70.0  # crews restore some of it, four still dark
THIN_MIN = 80.0  # two left: back under the threshold
CLEAR_MIN = 85.0  # the grid is back

#: How many signals the outage darkens at its peak.
DEFAULT_SIGNALS = 9
#: Dark at `ONSET_MIN`: one short of what a candidate needs.
ONSET_SIGNALS = 3
#: Still dark at `PARTIAL_MIN`, and at `THIN_MIN`.
PARTIAL_SIGNALS = 4
THIN_SIGNALS = 2

#: The cluster is drawn from the signals nearest the S2 centre, so it lands on
#: the ground-truth area `sim.scenarios.austin_signal_cluster` already uses.
#: Sampling from twice as many as it needs keeps the seed meaningful without
#: letting the cluster sprawl past 400 m.
CLUSTER_POOL_FACTOR = 2

#: Flashing signals this far from the centre cannot join the cluster at
#: `sentinels.signals.EPS_M`, so they are the noise DBSCAN must discard.
NOISE_RADIUS_M = 1200.0
DEFAULT_NOISE = 2

#: The noise signals have been flashing since well before the window opened:
#: they are the city's everyday handful of faults, not part of the outage.
NOISE_SINCE = WINDOW_START - timedelta(hours=6)

#: Rows inside the cluster area that look like flashes and are not: one
#: overnight scheduled flash, two signals the centre cannot reach.
DECOY_SCHEDULED = 1
DECOY_COMMS = 2

#: Feed state codes. `2` is the unscheduled (conflict) flash this scenario is
#: about; `3` is a communication issue.
UNSCHEDULED_FLASH = 2
COMMUNICATION_ISSUE = 3

STATE_TEXT: dict[int, str] = {
    SCHEDULED_FLASH_STATE: "Scheduled flash",
    UNSCHEDULED_FLASH: "Unscheduled (conflict) flash",
    COMMUNICATION_ISSUE: "Communication issue",
}

#: The plan id the feed reports for a signal that is not running a plan.
FLASH_PLAN_ID = "254"


@dataclass(frozen=True, slots=True)
class Blackout:
    """One generated scenario: the payloads, and what they are made of."""

    payloads: tuple[RawPayload, ...]
    cluster: tuple[Signal, ...]
    noise: tuple[Signal, ...]
    decoys: tuple[Signal, ...]
    seed: int
    minutes: float
    inventory: SignalInventory

    @property
    def window(self) -> tuple[datetime, datetime]:
        return WINDOW_START, WINDOW_START + timedelta(minutes=self.minutes)

    @property
    def records(self) -> int:
        return sum(len(payload.body.get("records", [])) for payload in self.payloads)

    def events(self) -> int:
        """How many `CivicEvent`s the adapter parses out of this recording."""
        adapter = AustinSignalsAdapter()
        return sum(len(adapter.parse(payload)) for payload in self.payloads)


def metres_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Flat-earth metres between two nearby points: a sort key, not a measure."""
    cos_lat = max(math.cos(math.radians((a[1] + b[1]) / 2)), 1e-6)
    dx = (a[0] - b[0]) * M_PER_DEG_LON_EQUATOR * cos_lat
    dy = (a[1] - b[1]) * M_PER_DEG_LAT
    return math.hypot(dx, dy)


def _by_distance(inventory: SignalInventory) -> list[tuple[float, Signal]]:
    """Every signal in the snapshot, nearest the S2 centre first."""
    return sorted(
        ((metres_between((item.lon, item.lat), AUSTIN_DOWNTOWN), item) for item in inventory),
        key=lambda pair: (pair[0], pair[1].signal_id),
    )


def _local(moment: datetime) -> str:
    """A UTC instant as the feed writes it: Austin local, no offset, millis."""
    from headsup.ingest.austin_signals import FEED_TZ

    return moment.astimezone(FEED_TZ).replace(tzinfo=None).isoformat(timespec="milliseconds")


def choose(
    inventory: SignalInventory,
    *,
    seed: int,
    signals: int = DEFAULT_SIGNALS,
    noise: int = DEFAULT_NOISE,
) -> tuple[tuple[Signal, ...], tuple[Signal, ...], tuple[Signal, ...]]:
    """Pick the cluster, the distant noise and the in-area decoys, by seed.

    The cluster comes out ordered by distance from the centre, because the
    onset phase darkens its innermost signals first: an outage spreads from
    the failure, not from a random corner of the grid.
    """
    rng = Random(seed)
    ranked = _by_distance(inventory)
    near = [item for distance, item in ranked if distance <= AUSTIN_CLUSTER_HALF_M]
    far = [item for distance, item in ranked if distance > NOISE_RADIUS_M]

    wanted = signals + DECOY_SCHEDULED + DECOY_COMMS
    if len(near) < wanted:
        raise ValueError(
            f"the signal map has {len(near)} signals within "
            f"{AUSTIN_CLUSTER_HALF_M:g} m of the S2 centre; this scenario needs {wanted}"
        )
    if len(far) < noise:
        raise ValueError(
            f"the signal map has {len(far)} signals beyond {NOISE_RADIUS_M:g} m "
            f"of the S2 centre; this scenario needs {noise} for noise"
        )

    pool = near[: min(len(near), signals * CLUSTER_POOL_FACTOR)]
    chosen = rng.sample(pool, signals)
    order = {item.signal_id: index for index, (_d, item) in enumerate(ranked)}
    cluster = tuple(sorted(chosen, key=lambda item: order[item.signal_id]))

    rest = [item for item in near if item not in cluster]
    decoys = tuple(rng.sample(rest, DECOY_SCHEDULED + DECOY_COMMS))
    noisy = tuple(rng.sample(far, noise))
    return cluster, noisy, decoys


def build(
    inventory: SignalInventory,
    *,
    seed: int,
    signals: int = DEFAULT_SIGNALS,
    noise: int = DEFAULT_NOISE,
    minutes: float = DEFAULT_MINUTES,
) -> Blackout:
    """Generate the recording's payloads: one poll every `POLL_SECONDS`."""
    if minutes < CLEAR_MIN + POLL_SECONDS / 60:
        raise ValueError(f"--minutes must be at least {CLEAR_MIN + POLL_SECONDS / 60:g}")
    cluster, noisy, decoys = choose(inventory, seed=seed, signals=signals, noise=noise)

    polls = int(minutes * 60 // POLL_SECONDS) + 1
    scheduled, comms = decoys[:DECOY_SCHEDULED], decoys[DECOY_SCHEDULED:]

    payloads: list[RawPayload] = []
    for index in range(polls):
        at = WINDOW_START + timedelta(seconds=POLL_SECONDS * index)
        elapsed = (at - WINDOW_START).total_seconds() / 60.0
        records = [
            *(_record(signal, UNSCHEDULED_FLASH, NOISE_SINCE, at) for signal in noisy),
            *(_record(signal, SCHEDULED_FLASH_STATE, WINDOW_START, at) for signal in scheduled),
            *(_record(signal, COMMUNICATION_ISSUE, WINDOW_START, at) for signal in comms),
            *(
                _record(signal, UNSCHEDULED_FLASH, _dark_since(elapsed), at)
                for signal in _dark_now(cluster, elapsed)
            ),
        ]
        records.sort(key=lambda row: int(row["signal_id"]))
        payloads.append(
            RawPayload(
                source=Source.AUSTIN_SIGNALS,
                ref=make_ref(Source.AUSTIN_SIGNALS, at, index),
                url=f"{SODA_BASE}/{SIGNAL_STATUS_DATASET}.json",
                fetched_at=at,
                body={"dataset": SIGNAL_STATUS_DATASET, "records": records},
            )
        )

    return Blackout(
        payloads=tuple(payloads),
        cluster=cluster,
        noise=noisy,
        decoys=decoys,
        seed=seed,
        minutes=minutes,
        inventory=inventory,
    )


def _dark_now(cluster: tuple[Signal, ...], elapsed: float) -> tuple[Signal, ...]:
    """Which of the cluster is flashing `elapsed` minutes into the window."""
    if elapsed < ONSET_MIN:
        return ()
    if elapsed < FULL_MIN:
        return cluster[:ONSET_SIGNALS]
    if elapsed < PARTIAL_MIN:
        return cluster
    if elapsed < THIN_MIN:
        return cluster[:PARTIAL_SIGNALS]
    if elapsed < CLEAR_MIN:
        return cluster[:THIN_SIGNALS]
    return ()


def _dark_since(elapsed: float) -> datetime:
    """When the signals dark at `elapsed` last changed state.

    The feed reports the moment a signal entered its current state, so this
    moves at the phase boundaries and not at every poll - which is what makes
    the parsed event ids stable while an outage holds.
    """
    if elapsed < FULL_MIN:
        return WINDOW_START + timedelta(minutes=ONSET_MIN)
    return WINDOW_START + timedelta(minutes=FULL_MIN)


def _record(signal: Signal, state: int, since: datetime, at: datetime) -> dict[str, Any]:
    """One row, in the columns and the order `5zpr-dehc` serves them in."""
    primary, _, cross = signal.name.partition(" / ")
    return {
        "signal_id": signal.signal_id,
        "location_name": signal.name,
        "operation_state": str(state),
        "operation_text": STATE_TEXT[state],
        "operation_state_datetime": _local(since),
        "processed_datetime": _local(at),
        "plan_id": FLASH_PLAN_ID,
        "primary_st": primary,
        "cross_st": cross,
        "location": {"type": "Point", "coordinates": [signal.lon, signal.lat]},
    }


def write(blackout: Blackout, *, root: Path | None = None, subdir: str = FIXTURE_DIR) -> Path:
    """Write the recording, replacing any previous generation of it.

    The recorder appends, so the file is removed first: regenerating a
    scenario with the same seed has to produce the same recording, not twice
    as long a one.
    """
    directory = scenario_dir(SCENARIO, root) / subdir
    target = directory / f"{Source.AUSTIN_SIGNALS.value}.jsonl"
    target.unlink(missing_ok=True)
    with Recorder(SCENARIO, root=root, subdir=subdir) as recorder:
        recorder.write_many(blackout.payloads)
    return target
