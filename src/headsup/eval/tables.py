"""`results/ablation.csv` and `results/ablation.md`: the rows, and the table.

Two files with two different jobs. The CSV is one row per run -- every metric,
the run id, whether its chain verified -- so a reader can go from a number in
the table to the log it came from and re-verify it with `hu audit verify`. The
Markdown is the table a person reads: medians per variant per scenario, with
the run count behind each cell, and the three comparisons the step exists to
make.

**Medians, not means.** A sweep of twenty seeds of a rules-only variant will
have a run where every vehicle happened to start away from the hazard, and a
mean of hazard entries over twenty seeds is a number one lucky seed can move.
The count behind each cell is printed beside it, so a median over three seeds
is never mistaken for a median over twenty.

**A run whose chain did not verify is counted nowhere.** It is in the CSV,
with `chain_ok=false` and the break, and it is named in the Markdown under its
own heading. Averaging it into a cell would be averaging in a number nobody
can check, which is the one thing a log-derived table must not do.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from headsup.contracts import utcnow
from headsup.eval.metrics import RunMetrics, median
from headsup.eval.variants import ORDER, Variant, spec_for

CSV_NAME = "ablation.csv"
MARKDOWN_NAME = "ablation.md"

#: The metrics the table shows, in the order the brief names them: the column
#: header, the field, and how many digits it is worth printing.
COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("hazard entries", "hazard_entries", 1),
    ("time to advisory (s)", "time_to_advisory_s", 1),
    ("assist peak", "assist_peak", 1),
    ("assist p95 wait (s)", "assist_p95_wait_s", 1),
    ("blocked corridor (veh-min)", "blocked_corridor_minutes", 1),
    ("rides/veh/h", "completed_ride_rate_per_vehicle_hour", 3),
    ("extra min per reroute", "extra_minutes_per_rerouted_trip", 2),
    ("false-positive area-hours", "false_positive_area_hours", 3),
    ("LLM $/sim hour", "llm_usd_per_sim_hour", 4),
    ("p95 agent latency (ms)", "p95_agent_latency_ms", 0),
)

#: What the headline chart and the headline table row compare.
HEADLINE_VARIANTS: tuple[Variant, ...] = (Variant.V0, Variant.V1, Variant.V4)
HEADLINE_SCENARIO = "s3_incident"
QUEUE_SCENARIO = "s2_blackout"


@dataclass(frozen=True, slots=True)
class Cell:
    """One variant-scenario cell: the median of a metric, and what it is over."""

    value: float | None
    runs: int

    def text(self, digits: int = 1) -> str:
        if self.value is None:
            return "-"
        if digits == 0:
            return f"{self.value:.0f}"
        return f"{self.value:.{digits}f}"


def usable(rows: Iterable[RunMetrics]) -> list[RunMetrics]:
    """The rows a table may average: no error, and a chain that verified."""
    return [row for row in rows if row.ok and row.chain_ok]


def cell(rows: Sequence[RunMetrics], field: str) -> Cell:
    """The median of one metric over these runs, ignoring the ones with none.

    A metric that no run in the cell produced -- time to advisory in a variant
    that published nothing -- is `None` rather than zero. Zero would read as
    "instantly", which is the opposite of what happened.
    """
    values = [
        float(getattr(row, field))
        for row in rows
        if getattr(row, field, None) is not None and not isinstance(getattr(row, field), str)
    ]
    return Cell(value=median(values), runs=len(rows))


def grouped(rows: Iterable[RunMetrics]) -> dict[tuple[str, str], list[RunMetrics]]:
    """Rows by `(variant, scenario)`."""
    out: dict[tuple[str, str], list[RunMetrics]] = {}
    for row in rows:
        out.setdefault((row.variant, row.scenario), []).append(row)
    return out


def write_csv(rows: Sequence[RunMetrics], directory: Path | str) -> Path:
    """One row per run, every column, with the run id first."""
    target = Path(directory) / CSV_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = list(RunMetrics("", "", "", 0).row())
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row.row())
    return target


def write_markdown(
    rows: Sequence[RunMetrics],
    directory: Path | str,
    *,
    minutes: float = 0.0,
    llm_mode: str = "",
    charts: Sequence[Path] = (),
) -> Path:
    """The table a person reads, written to `results/ablation.md`."""
    target = Path(directory) / MARKDOWN_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        markdown(rows, minutes=minutes, llm_mode=llm_mode, charts=charts),
        encoding="utf-8",
        newline="\n",
    )
    return target


def markdown(
    rows: Sequence[RunMetrics],
    *,
    minutes: float = 0.0,
    llm_mode: str = "",
    charts: Sequence[Path] = (),
) -> str:
    """The whole report: what was run, the table per scenario, the headline."""
    good = usable(rows)
    broken = [row for row in rows if row not in good]
    scenarios = sorted({row.scenario for row in good} | {row.scenario for row in broken})
    variants = [variant for variant in ORDER if any(row.variant == variant.value for row in rows)]
    buckets = grouped(good)

    lines: list[str] = [
        "# Ablation: what Heads Up changes",
        "",
        f"*{len(rows)} runs, {len(good)} measurable, written {utcnow().isoformat()}.*",
        "",
        "Every figure below was computed by reading the runs' hash-chained audit logs "
        "and nothing else. Each row of `ablation.csv` names the run id its numbers came "
        "from; `uv run hu audit verify <run_id>` re-checks that log line by line.",
        "",
        "## What was run",
        "",
        f"- Wall-clock minutes per run: {minutes:g}" if minutes else "- Wall-clock minutes: unset",
        f"- Model mode: {llm_mode or 'unrecorded'}",
        "- Variants:",
    ]
    for variant in variants:
        spec = spec_for(variant)
        lines.append(
            f"  - **{variant.value}** - {spec.label}: "
            f"fleets {'+'.join(mode.value for mode in spec.modes)}, "
            f"{'model' if spec.llm else 'rules only'}, "
            f"{'orchestrator' if spec.orchestrator else ('mesh' if spec.mesh else 'no agents')}"
            f"{', critic' if spec.critic else ''}, seeds from `{spec.seed_group}`"
        )

    for scenario in scenarios:
        lines += ["", f"## {scenario}", ""]
        header = "| variant | runs | " + " | ".join(name for name, _f, _d in COLUMNS) + " |"
        rule = "| --- | --- | " + " | ".join("---" for _c in COLUMNS) + " |"
        lines += [header, rule]
        for variant in variants:
            bucket = buckets.get((variant.value, scenario), [])
            if not bucket:
                continue
            cells = [cell(bucket, field).text(digits) for _name, field, digits in COLUMNS]
            lines.append(f"| {variant.value} | {len(bucket)} | " + " | ".join(cells) + " |")

    lines += ["", "## The headline", ""]
    lines += _headline(buckets)

    if charts:
        lines += ["", "## Charts", ""]
        lines += [f"- `{path.name}`" for path in charts]

    if broken:
        lines += [
            "",
            "## Runs no figure above rests on",
            "",
            "A run whose chain did not verify, or that did not start, is in the CSV and "
            "nowhere else: a number nobody can re-check does not belong in a median.",
            "",
        ]
        lines += [
            f"- `{row.run_id}`: {row.error or row.chain or 'unknown'}"
            for row in sorted(broken, key=lambda item: item.run_id)
        ]

    return "\n".join(lines) + "\n"


def _headline(buckets: Mapping[tuple[str, str], list[RunMetrics]]) -> list[str]:
    """The S3 comparison the whole step is for, as a short table."""
    rows = [
        (variant, buckets.get((variant.value, HEADLINE_SCENARIO), []))
        for variant in HEADLINE_VARIANTS
    ]
    if not any(bucket for _variant, bucket in rows):
        return ["_Not measured: no run of " + HEADLINE_SCENARIO + " verified._"]
    lines = [
        f"On `{HEADLINE_SCENARIO}`, the scenario a rules-only reading of the feed is "
        "expected to struggle with:",
        "",
        "| variant | runs | blocked corridor (veh-min) | missed hazards | advisories |",
        "| --- | --- | --- | --- | --- |",
    ]
    for variant, bucket in rows:
        if not bucket:
            continue
        blocked = cell(bucket, "blocked_corridor_minutes")
        missed = cell(bucket, "missed_hazards")
        advisories = cell(bucket, "advisories")
        lines.append(
            f"| {variant.value} | {len(bucket)} | {blocked.text(1)} | "
            f"{missed.text(1)} | {advisories.text(1)} |"
        )
    return lines


def summary_rows(rows: Sequence[RunMetrics]) -> list[dict[str, Any]]:
    """The medians as plain dicts, for the console table and the charts."""
    buckets = grouped(usable(rows))
    out: list[dict[str, Any]] = []
    for (variant, scenario), bucket in sorted(buckets.items()):
        entry: dict[str, Any] = {"variant": variant, "scenario": scenario, "runs": len(bucket)}
        for _name, field, _digits in COLUMNS:
            entry[field] = cell(bucket, field).value
        entry["missed_hazards"] = cell(bucket, "missed_hazards").value
        entry["advisories"] = cell(bucket, "advisories").value
        out.append(entry)
    return out
