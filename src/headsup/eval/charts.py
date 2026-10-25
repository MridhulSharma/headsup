"""The three PNGs: hazard entries by variant, S2's queue over time, the headline.

    paths = write_charts(rows, Path("results"))

Rendered with matplotlib on the `Agg` backend, so a sweep draws them with no
display and no interactive window -- a run on a laptop, in CI or over SSH
produces the same files.

**Every chart is drawn from the metrics rows**, which were themselves read off
the audit logs. The queue chart needs more than a median, so it reads the
`queue_curve` each row carries: `(simulated minute, queue depth)` sampled off
that run's own telemetry frames.

**No dual-axis chart.** The headline compares two measures of different scale
-- blocked corridor vehicle-minutes and a count of missed hazards -- and
putting them on two y-scales in one frame is the one chart mistake that makes
any two series look however the axes were chosen. They are two stacked panels
over the same variants instead, which says the same thing and cannot mislead.

**Identity is never colour alone.** Every series is in the legend or directly
labelled, and the values are printed on the marks, so the figure survives
greyscale printing and colour-vision deficiency.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

from headsup.eval.metrics import RunMetrics, median
from headsup.eval.tables import (
    HEADLINE_SCENARIO,
    HEADLINE_VARIANTS,
    QUEUE_SCENARIO,
    cell,
    grouped,
    usable,
)
from headsup.eval.variants import ORDER, Variant

log = logging.getLogger(__name__)

HAZARD_CHART = "hazard_entries_by_variant.png"
QUEUE_CHART = "s2_queue_depth.png"
HEADLINE_CHART = "headline_s3.png"

#: Categorical slots 1-3 of the reference palette, in fixed order. Assigned to
#: entities and never cycled: scenario one is blue in every chart here.
SERIES: tuple[str, ...] = ("#2a78d6", "#eb6834", "#1baf7a")

#: Chart chrome. The surface is explicit so a PNG dropped on a dark page is
#: still legible, and the grid and axis are recessive by design.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

DPI = 160
FIGSIZE = (9.0, 5.0)


def write_charts(rows: Sequence[RunMetrics], directory: Path | str) -> list[Path]:
    """Draw all three charts, returning the ones that had data to draw.

    A chart with nothing to draw is skipped and logged rather than written
    empty: an axis with no marks on it in a results directory reads as a
    measurement of zero.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    drawn: list[Path] = []
    for draw in (hazard_entries_chart, queue_depth_chart, headline_chart):
        path = draw(rows, target)
        if path is not None:
            drawn.append(path)
    return drawn


def _style(ax: plt.Axes, *, title: str, subtitle: str = "", ylabel: str = "") -> None:
    """One chart's chrome: recessive axes, a hairline grid, text in ink."""
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=INK, fontsize=13, loc="left", pad=18 if subtitle else 10)
    if subtitle:
        ax.text(
            0.0,
            1.02,
            subtitle,
            transform=ax.transAxes,
            color=INK_SECONDARY,
            fontsize=9.5,
            va="bottom",
        )
    if ylabel:
        ax.set_ylabel(ylabel, color=INK_SECONDARY, fontsize=10)
    ax.tick_params(colors=MUTED, labelsize=9.5)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)


def _variants(rows: Sequence[RunMetrics]) -> list[Variant]:
    """The variants present in these rows, in table order."""
    seen = {row.variant for row in rows}
    return [variant for variant in ORDER if variant.value in seen]


def hazard_entries_chart(rows: Sequence[RunMetrics], directory: Path) -> Path | None:
    """Hazard entries by variant, one bar group per scenario.

    The measurement the whole prototype is about: how many times a vehicle
    drove into a hazard nothing had told it about. Medians over the seeds of
    each cell, which is what the table prints too.
    """
    good = usable(rows)
    buckets = grouped(good)
    variants = _variants(good)
    scenarios = sorted({row.scenario for row in good})
    if not variants or not scenarios:
        log.info("charts: no measurable run for the hazard-entries chart")
        return None

    figure, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI, facecolor=SURFACE)
    width = min(0.26, 0.8 / len(scenarios))
    positions = range(len(variants))
    for index, scenario in enumerate(scenarios):
        offset = (index - (len(scenarios) - 1) / 2) * width
        values = [
            cell(buckets.get((variant.value, scenario), []), "hazard_entries").value or 0.0
            for variant in variants
        ]
        bars = ax.bar(
            [position + offset for position in positions],
            values,
            width=width * 0.92,  # a 2px surface gap between adjacent bars
            label=scenario,
            color=SERIES[index % len(SERIES)],
            edgecolor=SURFACE,
            linewidth=1.5,
        )
        ax.bar_label(bars, fmt="%.0f", color=INK_SECONDARY, fontsize=8.5, padding=2)

    ax.set_xticks(list(positions))
    ax.set_xticklabels([variant.value for variant in variants])
    _style(
        ax,
        title="Hazard entries by variant",
        subtitle="Times a vehicle drove into a hazard nothing had warned it about. "
        "Median over seeds; lower is better.",
        ylabel="hazard entries",
    )
    legend = ax.legend(frameon=False, fontsize=9.5, loc="upper right")
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)
    path = directory / HAZARD_CHART
    figure.tight_layout()
    figure.savefig(path, facecolor=SURFACE)
    plt.close(figure)
    return path


def queue_depth_chart(rows: Sequence[RunMetrics], directory: Path) -> Path | None:
    """S2's assist queue over time, the baseline against the full system.

    The surge S2 exists to show: when a block of downtown goes dark, every
    vehicle that reaches it stops and asks for a human. The curve is sampled
    off each run's own telemetry frames, so the two lines are two runs of the
    same seed and the same roads.
    """
    good = [row for row in usable(rows) if row.scenario == QUEUE_SCENARIO and row.queue_curve]
    picked: dict[str, RunMetrics] = {}
    for variant in (Variant.V0, Variant.V4):
        candidates = [row for row in good if row.variant == variant.value]
        if not candidates:
            continue
        # The run whose peak is the median of its cell, so neither line is the
        # luckiest or the unluckiest seed of the sweep.
        middle = median([float(row.assist_peak) for row in candidates])
        picked[variant.value] = min(
            candidates, key=lambda row: abs(float(row.assist_peak) - (middle or 0.0))
        )
    if not picked:
        log.info("charts: no %s run carried a queue curve", QUEUE_SCENARIO)
        return None

    figure, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI, facecolor=SURFACE)
    for index, (name, row) in enumerate(sorted(picked.items())):
        minutes = [point[0] for point in row.queue_curve]
        depth = [point[1] for point in row.queue_curve]
        colour = SERIES[index % len(SERIES)]
        label = f"{name} (seed {row.seed}, peak {row.assist_peak})"
        ax.plot(minutes, depth, linewidth=2.0, color=colour, label=label)
        if minutes:
            # Direct label at the end of the line, so identity does not rest
            # on the legend alone.
            ax.annotate(
                name,
                (minutes[-1], depth[-1]),
                textcoords="offset points",
                xytext=(6, 0),
                color=colour,
                fontsize=9.5,
                va="center",
            )
    _style(
        ax,
        title=f"Assist queue depth over time, {QUEUE_SCENARIO}",
        subtitle="Vehicles waiting for a human at once, sampled off each run's telemetry. "
        "V0 is the unadvised baseline; V4 is the full system.",
        ylabel="vehicles in the assist queue",
    )
    ax.set_xlabel("simulated minutes into the run", color=INK_SECONDARY, fontsize=10)
    legend = ax.legend(frameon=False, fontsize=9.5, loc="upper left")
    for text in legend.get_texts():
        text.set_color(INK_SECONDARY)
    path = directory / QUEUE_CHART
    figure.tight_layout()
    figure.savefig(path, facecolor=SURFACE)
    plt.close(figure)
    return path


def headline_chart(rows: Sequence[RunMetrics], directory: Path) -> Path | None:
    """The headline: S3 blocked-corridor minutes and missed hazards, V0, V1, V4.

    Two panels over the same three variants rather than two y-axes in one
    frame. The measures have nothing to do with each other's scale -- one is
    vehicle-minutes, the other a count of two or three hazards -- and a dual
    axis would let the choice of scales decide what the chart appeared to say.
    """
    good = [row for row in usable(rows) if row.scenario == HEADLINE_SCENARIO]
    buckets = grouped(good)
    variants = [
        variant for variant in HEADLINE_VARIANTS if buckets.get((variant.value, HEADLINE_SCENARIO))
    ]
    if not variants:
        log.info("charts: no measurable %s run for the headline chart", HEADLINE_SCENARIO)
        return None

    figure, (top, bottom) = plt.subplots(
        2, 1, figsize=(9.0, 7.0), dpi=DPI, facecolor=SURFACE, sharex=True
    )
    panels = (
        (top, "blocked_corridor_minutes", "blocked corridor (vehicle-minutes)", SERIES[0], "%.1f"),
        (bottom, "missed_hazards", "hazards nothing was published about", SERIES[1], "%.0f"),
    )
    for ax, field, ylabel, colour, fmt in panels:
        values = [
            cell(buckets.get((variant.value, HEADLINE_SCENARIO), []), field).value or 0.0
            for variant in variants
        ]
        bars = ax.bar(
            [variant.value for variant in variants],
            values,
            width=0.5,
            color=colour,
            edgecolor=SURFACE,
            linewidth=1.5,
        )
        ax.bar_label(bars, fmt=fmt, color=INK_SECONDARY, fontsize=9, padding=2)
        _style(ax, title="", ylabel=ylabel)

    top.set_title(
        f"{HEADLINE_SCENARIO}: what the baseline and the rules-only reading cost",
        color=INK,
        fontsize=13,
        loc="left",
        pad=18,
    )
    top.text(
        0.0,
        1.02,
        "Median over seeds. V0 has no Signum at all; V1 reads the feed with keywords "
        "and no model; V4 is the full system.",
        transform=top.transAxes,
        color=INK_SECONDARY,
        fontsize=9.5,
        va="bottom",
    )
    path = directory / HEADLINE_CHART
    figure.tight_layout()
    figure.savefig(path, facecolor=SURFACE)
    plt.close(figure)
    return path
