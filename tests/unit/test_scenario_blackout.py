"""`hu scenario synth-blackout`: the same seed writes the same recording.

Most of these run against a synthetic inventory built here, so they do not
depend on what the city's signal map happened to look like the day it was
snapshotted. Two of them do read the committed snapshot, because "the cluster
sits on real intersections" is the one claim the scenario makes that only the
real file can support.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from headsup.contracts import Source
from headsup.ingest import AustinSignalsAdapter, Recording
from headsup.ingest.austin_signals import (
    SCHEDULED_FLASH_STATE,
    UNSCHEDULED_FLASH_STATES,
)
from headsup.scenario import blackout as mod
from headsup.scenario import descriptor as descriptor_mod
from headsup.scenario.inventory import Signal, SignalInventory
from headsup.sim.scenarios import AUSTIN_CLUSTER_HALF_M, AUSTIN_DOWNTOWN

SEED = 7


def grid(rows: int = 7, columns: int = 7, step_m: float = 95.0) -> list[Signal]:
    """A downtown-sized block grid centred on the S2 centre.

    95 m apart, which is a downtown Austin block, so a handful of them cluster
    at `EPS_M` exactly as the real grid does.
    """
    import math

    from headsup.world.bbox import M_PER_DEG_LAT, M_PER_DEG_LON_EQUATOR

    lon0, lat0 = AUSTIN_DOWNTOWN
    d_lat = step_m / M_PER_DEG_LAT
    d_lon = step_m / (M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0)))
    signals = []
    for row in range(rows):
        for column in range(columns):
            index = row * columns + column
            signals.append(
                Signal(
                    signal_id=str(100 + index),
                    name=f"{row}TH ST / {column}TH AVE",
                    lon=lon0 + (column - columns // 2) * d_lon,
                    lat=lat0 + (row - rows // 2) * d_lat,
                )
            )
    return signals


def far_signals(count: int = 4) -> list[Signal]:
    """Signals well beyond `NOISE_RADIUS_M`, for the noise rows."""
    lon0, lat0 = AUSTIN_DOWNTOWN
    return [
        Signal(
            signal_id=str(900 + index),
            name=f"FAR {index} / RIVER RD",
            lon=lon0 + 0.03,
            lat=lat0 - 0.02 - index * 0.001,
        )
        for index in range(count)
    ]


def inventory() -> SignalInventory:
    return SignalInventory(
        city="austin",
        dataset="test-grid",
        source_url="https://example.invalid/resource/test-grid.json",
        retrieved_at="2026-10-22",
        bbox=(-97.76, 30.258, -97.728, 30.282),
        signals=tuple(grid() + far_signals()),
    )


@pytest.fixture
def snapshot() -> SignalInventory:
    return inventory()


def dark_counts(generated: mod.Blackout) -> list[int]:
    """How much of the *cluster* each poll reports dark, poll by poll.

    The noise signals flash in every poll by design, so they are excluded
    here: the phase timeline is about the outage, not about the city's
    everyday handful of faults.
    """
    cluster = {item.signal_id for item in generated.cluster}
    return [
        sum(
            1
            for row in payload.body["records"]
            if row["signal_id"] in cluster
            and int(row["operation_state"]) in UNSCHEDULED_FLASH_STATES
        )
        for payload in generated.payloads
    ]


# --- determinism ------------------------------------------------------------


def test_the_same_seed_writes_the_same_recording(snapshot: SignalInventory) -> None:
    first = mod.build(snapshot, seed=SEED)
    second = mod.build(snapshot, seed=SEED)
    assert [item.to_wire() for item in first.payloads] == [
        item.to_wire() for item in second.payloads
    ]
    assert [item.signal_id for item in first.cluster] == [item.signal_id for item in second.cluster]


def test_a_different_seed_darkens_a_different_set(snapshot: SignalInventory) -> None:
    chosen = {
        seed: tuple(item.signal_id for item in mod.build(snapshot, seed=seed).cluster)
        for seed in (7, 8, 9)
    }
    assert len(set(chosen.values())) > 1


def test_writing_twice_replaces_the_recording_rather_than_appending(
    snapshot: SignalInventory, tmp_path: Path
) -> None:
    generated = mod.build(snapshot, seed=SEED)
    first = mod.write(generated, root=tmp_path)
    lines = len(first.read_bytes().splitlines())
    second = mod.write(generated, root=tmp_path)

    assert second == first
    assert len(second.read_bytes().splitlines()) == lines == len(generated.payloads)


# --- the timeline -----------------------------------------------------------


def test_the_outage_starts_under_the_sentinels_threshold(
    snapshot: SignalInventory,
) -> None:
    """The onset poll is the point of the scenario: it must publish nothing."""
    from headsup.agents.sentinels import MIN_SIGNALS

    counts = dark_counts(mod.build(snapshot, seed=SEED))
    onset = int(mod.ONSET_MIN * 60 // mod.POLL_SECONDS)
    assert counts[onset] == mod.ONSET_SIGNALS < MIN_SIGNALS


def test_the_cluster_fills_holds_and_clears_in_two_steps(
    snapshot: SignalInventory,
) -> None:
    generated = mod.build(snapshot, seed=SEED)
    counts = dark_counts(generated)
    poll = lambda minutes: int(minutes * 60 // mod.POLL_SECONDS)  # noqa: E731

    assert counts[0] == 0
    assert counts[poll(mod.ONSET_MIN)] == mod.ONSET_SIGNALS
    assert counts[poll(mod.FULL_MIN)] == len(generated.cluster)
    assert counts[poll(mod.PARTIAL_MIN)] == mod.PARTIAL_SIGNALS
    assert counts[poll(mod.THIN_MIN)] == mod.THIN_SIGNALS
    assert counts[-1] == 0


def test_every_poll_is_one_feed_refresh_apart(snapshot: SignalInventory) -> None:
    generated = mod.build(snapshot, seed=SEED)
    gaps = {
        (later.fetched_at - earlier.fetched_at).total_seconds()
        for earlier, later in zip(generated.payloads, generated.payloads[1:], strict=False)
    }
    assert gaps == {mod.POLL_SECONDS}
    start, end = generated.window
    assert generated.payloads[0].fetched_at == start
    assert end - start == timedelta(minutes=generated.minutes)


def test_a_window_too_short_to_hold_the_timeline_is_refused(
    snapshot: SignalInventory,
) -> None:
    with pytest.raises(ValueError, match="--minutes"):
        mod.build(snapshot, seed=SEED, minutes=20)


def test_an_inventory_too_thin_to_place_the_cluster_is_refused() -> None:
    thin = SignalInventory(
        city="austin",
        dataset="test-grid",
        source_url="",
        retrieved_at="2026-10-22",
        bbox=(-97.76, 30.258, -97.728, 30.282),
        signals=tuple(grid(rows=2, columns=2) + far_signals()),
    )
    with pytest.raises(ValueError, match="signals within"):
        mod.build(thin, seed=SEED)


# --- what is in the payloads ------------------------------------------------


def test_the_rows_are_what_the_adapter_parses_back_out(snapshot: SignalInventory) -> None:
    generated = mod.build(snapshot, seed=SEED)
    adapter = AustinSignalsAdapter()
    full = generated.payloads[int(mod.FULL_MIN * 60 // mod.POLL_SECONDS)]
    events = adapter.parse(full)

    dark = [
        event for event in events if event.payload["flashing"] and not event.payload["scheduled"]
    ]
    assert len(dark) == len(generated.cluster) + len(generated.noise)
    assert {event.source for event in events} == {Source.AUSTIN_SIGNALS}
    assert all(event.geometry.type == "Point" for event in events)
    assert generated.events() == sum(len(payload.body["records"]) for payload in generated.payloads)


def test_the_decoys_are_in_the_area_and_never_an_unscheduled_flash(
    snapshot: SignalInventory,
) -> None:
    generated = mod.build(snapshot, seed=SEED)
    cluster_ids = {item.signal_id for item in generated.cluster}
    rows = {
        row["signal_id"]: int(row["operation_state"])
        for row in generated.payloads[-1].body["records"]
    }

    assert len(generated.decoys) == mod.DECOY_SCHEDULED + mod.DECOY_COMMS
    for decoy in generated.decoys:
        assert decoy.signal_id not in cluster_ids
        assert rows[decoy.signal_id] in {SCHEDULED_FLASH_STATE, mod.COMMUNICATION_ISSUE}
        assert mod.metres_between((decoy.lon, decoy.lat), AUSTIN_DOWNTOWN) <= AUSTIN_CLUSTER_HALF_M


def test_the_noise_signals_are_too_far_away_to_join_the_cluster(
    snapshot: SignalInventory,
) -> None:
    from headsup.agents.sentinels import EPS_M

    generated = mod.build(snapshot, seed=SEED)
    for signal in generated.noise:
        distance = mod.metres_between((signal.lon, signal.lat), AUSTIN_DOWNTOWN)
        assert distance > mod.NOISE_RADIUS_M > EPS_M


def test_the_timestamps_are_the_feeds_own_naive_local_form(
    snapshot: SignalInventory,
) -> None:
    row = mod.build(snapshot, seed=SEED).payloads[0].body["records"][0]
    assert row["processed_datetime"].endswith(".000")
    assert "+" not in row["processed_datetime"]
    assert "Z" not in row["processed_datetime"]


# --- the real signal map ----------------------------------------------------


def test_the_committed_signal_map_places_the_cluster_on_real_intersections() -> None:
    real = SignalInventory.load("austin")
    generated = mod.build(real, seed=SEED)
    by_id = {item.signal_id: item for item in real}

    assert len(real) > mod.DEFAULT_SIGNALS
    for signal in generated.cluster:
        assert by_id[signal.signal_id] == signal
        assert " / " in signal.name
        assert mod.metres_between((signal.lon, signal.lat), AUSTIN_DOWNTOWN) <= (
            AUSTIN_CLUSTER_HALF_M
        )


def test_the_committed_recording_matches_the_committed_seed() -> None:
    """`data/recordings/s2_blackout` is what seed 7 produces, or it is stale."""
    real = SignalInventory.load("austin")
    generated = mod.build(real, seed=SEED)
    recorded = Recording.locate("s2_blackout").payloads()

    assert [item.to_wire() for item in recorded] == [item.to_wire() for item in generated.payloads]


# --- the descriptor ---------------------------------------------------------


def test_the_descriptor_says_the_scenario_is_scripted(
    snapshot: SignalInventory, tmp_path: Path
) -> None:
    generated = mod.build(snapshot, seed=SEED)
    recording = mod.write(generated, root=tmp_path)
    written = descriptor_mod.write(
        generated,
        recording=recording,
        events=generated.events(),
        bytes_=recording.stat().st_size,
        root=tmp_path,
    )
    document = yaml.safe_load(written.read_text(encoding="utf-8"))

    assert document["id"] == "s2_blackout"
    assert document["scripted"] is True
    assert document["hazard_class"] == "SIGNAL_OUTAGE"
    assert document["recording"]["payloads"] == len(generated.payloads)
    assert document["recording"]["events"] == generated.events()
    assert document["provenance"]["austin_signals"]["status"] == "synthetic"
    assert document["provenance"]["austin_signals"]["signal_map"]["status"] == "real"
    assert document["provenance"]["generator"]["seed"] == SEED
    assert len(document["feeds"]["austin_signals"]["cluster"]["signals"]) == len(generated.cluster)


def test_the_committed_descriptor_matches_the_committed_recording() -> None:
    document = yaml.safe_load(descriptor_mod.path_for().read_text(encoding="utf-8"))
    recorded = Recording.locate("s2_blackout")
    payloads = recorded.payloads()
    adapter = AustinSignalsAdapter()

    assert document["recording"]["payloads"] == len(payloads)
    assert document["recording"]["events"] == sum(
        len(adapter.parse(payload)) for payload in payloads
    )
    assert document["provenance"]["generator"]["seed"] == SEED
