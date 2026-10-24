"""`ablation.csv`, `ablation.md` and the three PNGs, from rows and nothing else.

The outputs are built off `RunMetrics` rows, so these tests hand them rows
directly: what is checked is the table's own rules. Two of them matter beyond
formatting.

* **A run whose chain did not verify is in no cell.** It is in the CSV with the
  break against it and named in the Markdown under its own heading, because a
  number nobody can re-check must not be averaged into one that can.
* **A metric no run produced is a dash, not a zero.** "No advisory was ever
  published" and "the advisory was published instantly" are opposite results
  and the table has to tell them apart.
"""

from __future__ import annotations

from pathlib import Path

from headsup.eval import charts as charts_mod
from headsup.eval import tables as tables_mod
from headsup.eval.metrics import RunMetrics


def row(
    *,
    variant: str = "V4",
    scenario: str = "s3_incident",
    seed: int = 42,
    chain_ok: bool = True,
    error: str = "",
    **changes: object,
) -> RunMetrics:
    """One metrics row, with the fields a table reads filled in."""
    base = RunMetrics(
        run_id=f"eval_{scenario}_{variant}_s{seed}",
        scenario=scenario,
        variant=variant,
        seed=seed,
        fleet="ghost" if variant == "V0" else "live",
        chain_ok=chain_ok,
        chain="verified" if chain_ok else "broken at line 3: hash_mismatch",
        error=error,
        entries=120,
        vehicles=300,
        sim_minutes=60.0,
        hazard_entries=4,
        time_to_advisory_s=18.0,
        assist_peak=7,
        assist_p95_wait_s=42.0,
        blocked_corridor_minutes=12.5,
        completed_rides=180,
        completed_ride_rate_per_vehicle_hour=0.6,
        rerouted=12,
        extra_minutes_per_rerouted_trip=1.4,
        false_positive_area_hours=0.02,
        llm_usd=0.3,
        llm_usd_per_sim_hour=0.3,
        p95_agent_latency_ms=2100.0,
        advisories=2,
        missed_hazards=0,
        labelled_hazards=1,
    )
    for name, value in changes.items():
        setattr(base, name, value)
    return base


def sweep_rows() -> list[RunMetrics]:
    """A small sweep: three variants, two scenarios, two seeds each."""
    rows: list[RunMetrics] = []
    for variant, entries, blocked, missed in (
        ("V0", 30, 48.0, 1),
        ("V1", 12, 22.0, 1),
        ("V4", 3, 6.0, 0),
    ):
        for scenario in ("s2_blackout", "s3_incident"):
            for seed in (42, 43):
                rows.append(
                    row(
                        variant=variant,
                        scenario=scenario,
                        seed=seed,
                        hazard_entries=entries + seed - 42,
                        blocked_corridor_minutes=blocked,
                        missed_hazards=missed,
                        queue_curve=tuple((index * 0.5, index % 4) for index in range(12)),
                    )
                )
    return rows


# --- the CSV ---------------------------------------------------------------


def test_the_csv_has_one_row_per_run_and_names_the_log(tmp_path: Path) -> None:
    rows = sweep_rows()
    path = tables_mod.write_csv(rows, tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(rows) + 1
    header = lines[0].split(",")
    assert header[0] == "run_id"
    for field in ("chain_ok", "hazard_entries", "false_positive_area_hours", "missed_hazards"):
        assert field in header
    assert "eval_s3_incident_V4_s43" in path.read_text(encoding="utf-8")


def test_the_csv_carries_no_chart_series(tmp_path: Path) -> None:
    """The queue curve is for the chart; a CSV cell holding a hundred pairs is noise."""
    path = tables_mod.write_csv(sweep_rows(), tmp_path)
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert "queue_curve" not in header


# --- the Markdown ----------------------------------------------------------


def test_the_table_prints_a_median_per_variant_with_its_run_count(tmp_path: Path) -> None:
    path = tables_mod.write_markdown(sweep_rows(), tmp_path, minutes=10, llm_mode="mock")
    text = path.read_text(encoding="utf-8")
    assert "# Ablation: what Heads Up changes" in text
    assert "s3_incident" in text and "s2_blackout" in text
    for variant in ("V0", "V1", "V4"):
        assert f"| {variant} | 2 |" in text
    assert "mock" in text


def test_the_table_says_the_figures_came_from_the_logs(tmp_path: Path) -> None:
    text = tables_mod.markdown(sweep_rows())
    assert "audit logs" in text
    assert "hu audit verify" in text


def test_the_headline_compares_the_baseline_the_rules_and_the_full_system(
    tmp_path: Path,
) -> None:
    text = tables_mod.markdown(sweep_rows())
    headline = text.split("## The headline")[1]
    assert "blocked corridor" in headline and "missed hazards" in headline
    for variant in ("V0", "V1", "V4"):
        assert f"| {variant} |" in headline
    # V2 and V3 are in the table above but not in the headline comparison.
    assert "V3" not in headline


def test_a_metric_no_run_produced_is_a_dash_not_a_zero() -> None:
    rows = [row(time_to_advisory_s=None, p95_agent_latency_ms=None)]
    assert tables_mod.cell(rows, "time_to_advisory_s").text() == "-"
    assert tables_mod.cell(rows, "p95_agent_latency_ms").text(0) == "-"


def test_a_run_whose_chain_broke_is_in_no_cell_and_is_named(tmp_path: Path) -> None:
    rows = [
        row(variant="V4", seed=42),
        row(variant="V4", seed=43, chain_ok=False, hazard_entries=999),
    ]
    text = tables_mod.markdown(rows)
    assert "| V4 | 1 |" in text
    assert "Runs no figure above rests on" in text
    assert "eval_s3_incident_V4_s43" in text
    assert tables_mod.cell(tables_mod.usable(rows), "hazard_entries").value == 4


def test_a_run_that_never_started_is_named_with_its_reason(tmp_path: Path) -> None:
    rows = [row(variant="V1", error="ScenarioError: austin is not built")]
    text = tables_mod.markdown(rows)
    assert "austin is not built" in text
    assert tables_mod.usable(rows) == []


# --- the charts ------------------------------------------------------------


def test_all_three_charts_are_written_and_are_not_empty(tmp_path: Path) -> None:
    drawn = charts_mod.write_charts(sweep_rows(), tmp_path)
    names = {path.name for path in drawn}
    assert names == {
        charts_mod.HAZARD_CHART,
        charts_mod.QUEUE_CHART,
        charts_mod.HEADLINE_CHART,
    }
    for path in drawn:
        assert path.is_file()
        assert path.stat().st_size > 5_000, f"{path.name} looks empty"
        assert path.read_bytes()[:4] == b"\x89PNG"


def test_a_chart_with_nothing_to_draw_is_skipped_rather_than_written_empty(
    tmp_path: Path,
) -> None:
    """An axis with no marks on it in a results directory reads as a zero."""
    rows = [row(variant="V4", scenario="s1_flood", queue_curve=())]
    drawn = charts_mod.write_charts(rows, tmp_path)
    names = {path.name for path in drawn}
    assert charts_mod.HAZARD_CHART in names
    assert charts_mod.QUEUE_CHART not in names
    assert charts_mod.HEADLINE_CHART not in names


def test_the_queue_chart_picks_the_median_seed_not_the_luckiest(tmp_path: Path) -> None:
    rows = [
        row(
            variant="V0",
            scenario="s2_blackout",
            seed=seed,
            assist_peak=peak,
            queue_curve=((0.0, peak),),
        )
        for seed, peak in ((42, 1), (43, 9), (44, 5))
    ]
    rows += [
        row(variant="V4", scenario="s2_blackout", seed=42, assist_peak=2, queue_curve=((0.0, 2),))
    ]
    path = charts_mod.queue_depth_chart(rows, tmp_path)
    assert path is not None and path.is_file()


def test_the_summary_rows_carry_every_table_column() -> None:
    summary = tables_mod.summary_rows(sweep_rows())
    assert len(summary) == 6  # three variants x two scenarios
    for entry in summary:
        for _name, field, _digits in tables_mod.COLUMNS:
            assert field in entry
        assert entry["runs"] == 2
