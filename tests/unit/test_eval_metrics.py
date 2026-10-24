"""The ablation's metrics, computed off synthetic audit logs and nothing else.

No Redis, no world build, no simulator. `fixtures.eval` seals two real chains
whose payloads are the shapes the auditor really writes -- a V0-shaped log (the
ghost fleet alone, nothing read, nothing advised) and a V4-shaped one (the
whole pipeline, both fleets) -- and every assertion here is a figure the table
prints.

The two claims that matter most:

* **every metric is computed.** `test_every_metric_is_computed_for_a_full_run`
  walks the row and fails on a `None` the brief asks for a number for, so a
  metric that silently stopped being computed cannot pass.
* **V0 drives into more hazards than V4.** Which is the whole point of the
  comparison, and here it is asserted off two logs rather than off a claim.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from fixtures.audit import tamper
from fixtures.eval import (
    CALL_USD,
    STRIDE,
    WAITING_FRAMES,
    LogShape,
    v0_log,
    v4_log,
    write_log,
)
from headsup.contracts import HazardClass
from headsup.eval.labels import Decoy, ScenarioLabels
from headsup.eval.metrics import RunContext, measure, median, percentile
from headsup.eval.variants import Variant
from headsup.sim.ground_truth import HazardRegion
from samples import POLYGON, T0

SCENARIO = "s1_flood"
VEHICLES = 6


def context(run_id: str, variant: Variant, directory: Path) -> RunContext:
    return RunContext(
        run_id=run_id,
        scenario=SCENARIO,
        variant=variant,
        seed=42,
        vehicles=VEHICLES,
        directory=directory,
    )


def covering_labels(start=T0) -> ScenarioLabels:
    """Labels whose one region is exactly where the sample advisory is.

    So a run that published that advisory is scored as a hit, which is the
    other side of the false-positive test: the same code has to answer both.
    """
    return ScenarioLabels(
        scenario_id=SCENARIO,
        start=start,
        regions=(
            HazardRegion(
                id="label-crossing",
                hazard_class=HazardClass.FLOOD,
                geometry=POLYGON,
                valid_from=start,
                valid_until=start + timedelta(hours=2),
                severity=4,
            ),
        ),
    )


def elsewhere_labels(start=T0) -> ScenarioLabels:
    """Labels whose region is nowhere near the advisory, so it is a miss."""
    moved = POLYGON.model_copy(
        update={"coordinates": [[[lon + 1.0, lat + 1.0] for lon, lat in POLYGON.coordinates[0]]]}
    )
    return ScenarioLabels(
        scenario_id=SCENARIO,
        start=start,
        regions=(
            HazardRegion(
                id="label-far-away",
                hazard_class=HazardClass.FLOOD,
                geometry=moved,
                valid_from=start,
                valid_until=start + timedelta(hours=2),
                severity=4,
            ),
        ),
    )


# --- the headline comparison ------------------------------------------------


def test_the_baseline_drives_into_more_hazards_than_the_full_system(tmp_path: Path) -> None:
    v0_log(tmp_path)
    v4_log(tmp_path)
    baseline = measure(context("eval_s1_flood_V0_s42", Variant.V0, tmp_path))
    full = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path))
    assert baseline.hazard_entries > full.hazard_entries
    # And the comparison is between two fleets that are what they claim to be.
    assert baseline.fleet == "ghost"
    assert full.fleet == "live"


def test_every_metric_is_computed_for_a_full_run(tmp_path: Path) -> None:
    v4_log(tmp_path)
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path))
    for field in (
        "hazard_entries",
        "time_to_advisory_s",
        "assist_peak",
        "assist_p95_wait_s",
        "blocked_corridor_minutes",
        "completed_ride_rate_per_vehicle_hour",
        "extra_minutes_per_rerouted_trip",
        "false_positive_area_hours",
        "llm_usd_per_sim_hour",
        "p95_agent_latency_ms",
    ):
        assert getattr(row, field) is not None, f"{field} was not computed"


def test_the_baseline_has_no_figure_for_what_it_never_did(tmp_path: Path) -> None:
    """A V0 run published nothing and called no model, and says so with None.

    Zero would read as "instantly, for free", which is the opposite of what a
    run with no pipeline in it means.
    """
    v0_log(tmp_path)
    row = measure(context("eval_s1_flood_V0_s42", Variant.V0, tmp_path))
    assert row.time_to_advisory_s is None
    assert row.p95_agent_latency_ms is None
    assert row.advisories == 0
    assert row.llm_usd == 0.0
    assert row.llm_usd_per_sim_hour == 0.0


# --- each metric against the shape it was built from -----------------------


def test_counters_are_read_off_the_last_frame_not_summed(tmp_path: Path) -> None:
    shape = LogShape(run_id="eval_s1_flood_V4_s42", hazard_entries=7, frames=5)
    write_log(tmp_path, shape)
    row = measure(context(shape.run_id, Variant.V4, tmp_path))
    assert row.hazard_entries == 7


def test_blocked_corridor_minutes_come_from_the_seconds_underneath(tmp_path: Path) -> None:
    """A corridor held for 90 vehicle-seconds is 1.5 minutes, not 1."""
    shape = LogShape(run_id="eval_s1_flood_V4_s42", blocked_seconds=90)
    write_log(tmp_path, shape)
    row = measure(context(shape.run_id, Variant.V4, tmp_path))
    assert row.blocked_corridor_minutes == pytest.approx(1.5)


def test_time_to_advisory_is_measured_from_the_observation_it_cites(tmp_path: Path) -> None:
    v4_log(tmp_path)
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path))
    # The fixture puts the event first and the advisory five lines later, one
    # second apart, and the advisory cites that event.
    assert row.time_to_advisory_s == pytest.approx(5.0)


def test_the_assist_wait_is_as_long_as_the_frames_show_the_vehicle_waiting(
    tmp_path: Path,
) -> None:
    v4_log(tmp_path)
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path))
    assert row.assist_requests == 2
    assert row.assist_peak == 1
    assert row.assist_p95_wait_s == pytest.approx((WAITING_FRAMES - 1) * STRIDE)


def test_llm_dollars_per_simulated_hour_come_from_the_audit_stream(tmp_path: Path) -> None:
    shape = LogShape(run_id="eval_s1_flood_V4_s42", llm_calls=4)
    write_log(tmp_path, shape)
    row = measure(context(shape.run_id, Variant.V4, tmp_path))
    assert row.llm_calls == 4
    assert row.llm_usd == pytest.approx(4 * CALL_USD)
    assert row.llm_usd_per_sim_hour == pytest.approx(row.llm_usd / (row.sim_minutes / 60), rel=1e-3)


def test_extra_minutes_per_reroute_is_measured_against_the_runs_own_ghost(
    tmp_path: Path,
) -> None:
    """The advised fleet completes fewer, longer trips; the baseline is the control."""
    v4_log(tmp_path)
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path))
    assert row.mean_trip_minutes is not None
    assert row.ghost_mean_trip_minutes is not None
    assert row.mean_trip_minutes > row.ghost_mean_trip_minutes
    # Recomputed from the row's own rounded means, so the tolerance is the
    # rounding and nothing else.
    expected = (
        (row.mean_trip_minutes - row.ghost_mean_trip_minutes) * row.completed_rides / row.rerouted
    )
    assert row.extra_minutes_per_rerouted_trip == pytest.approx(expected, rel=1e-2)


def test_p95_agent_latency_is_the_hand_offs_between_the_stages(tmp_path: Path) -> None:
    v4_log(tmp_path)
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path))
    # Each stage of the fixture is one second after the last, so every
    # hand-off is 1000 ms except the compile, which spans two.
    assert row.p95_agent_latency_ms is not None
    assert row.p95_agent_latency_ms >= 1000.0


# --- what was true ---------------------------------------------------------


def test_an_advisory_over_a_labelled_hazard_is_not_a_false_positive(tmp_path: Path) -> None:
    v4_log(tmp_path)
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path), labels=covering_labels())
    assert row.false_positive_area_hours == 0.0
    assert row.missed_hazards == 0
    assert row.labelled_hazards == 1


def test_an_advisory_over_nothing_is_charged_in_area_hours(tmp_path: Path) -> None:
    v4_log(tmp_path)
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path), labels=elsewhere_labels())
    assert row.false_positive_area_hours > 0.0
    # And the hazard nobody was told about is counted as missed.
    assert row.missed_hazards == 1


def test_a_hazard_a_run_said_nothing_about_is_missed(tmp_path: Path) -> None:
    v0_log(tmp_path)
    row = measure(context("eval_s1_flood_V0_s42", Variant.V0, tmp_path), labels=covering_labels())
    assert row.advisories == 0
    assert row.missed_hazards == 1


def test_an_advisory_citing_an_unlabelled_record_is_neither(tmp_path: Path) -> None:
    """S3's own `scoring` block: a real incident carries no label, so an
    advisory over one is counted apart rather than charged as a mistake."""
    v4_log(tmp_path)
    labels = ScenarioLabels(
        scenario_id="s3_incident",
        start=T0,
        regions=elsewhere_labels().regions,
        decoys=(Decoy(report_id="scripted-7th-stalled-vehicle"),),
        unlabelled="real_records",
    )
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path), labels=labels)
    assert row.advisories_unlabelled == 1
    assert row.false_positive_area_hours == 0.0


# --- a log that cannot be trusted, and one that is not there ----------------


def test_a_tampered_log_is_a_row_that_says_so_not_an_exception(tmp_path: Path) -> None:
    path = v4_log(tmp_path)
    # The careless edit: one line's contents changed and its own digest left
    # alone, which `verify` catches at that line.
    tamper(path, 3, {"stream": "telemetry"})
    row = measure(context("eval_s1_flood_V4_s42", Variant.V4, tmp_path))
    assert not row.chain_ok
    assert "broken" in row.chain


def test_a_missing_log_is_a_row_that_says_so(tmp_path: Path) -> None:
    row = measure(context("eval_s1_flood_V4_s99", Variant.V4, tmp_path))
    assert not row.ok
    assert "no audit log" in row.error


def test_an_empty_log_computes_zeroes_and_nones_rather_than_raising(tmp_path: Path) -> None:
    shape = LogShape(
        run_id="eval_s1_flood_V1_s42",
        pipeline=False,
        advisories=0,
        llm_calls=0,
        assists=0,
        frames=0,
    )
    write_log(tmp_path, shape)
    row = measure(context(shape.run_id, Variant.V1, tmp_path))
    assert row.hazard_entries == 0
    assert row.assist_peak == 0
    assert row.time_to_advisory_s is None
    assert row.sim_minutes == 0.0


# --- the two statistics helpers --------------------------------------------


def test_percentile_takes_the_nearest_rank_and_invents_nothing() -> None:
    assert percentile([], 0.95) == 0.0
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert percentile(list(range(1, 21)), 0.95) == 19.0


def test_median_is_none_for_nothing_and_the_middle_otherwise() -> None:
    assert median([]) is None
    assert median([3.0]) == 3.0
    assert median([1.0, 3.0]) == 2.0
    assert median([1.0, 2.0, 30.0]) == 2.0
