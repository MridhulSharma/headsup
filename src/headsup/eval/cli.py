"""`hu eval`: run the ablation and write the table and the charts.

    uv run hu eval --variants V0,V1,V2,V3,V4 --scenarios s1,s2,s3 \
                   --seeds rules=20,ai=3 --minutes 10 --out results/

One verb rather than a group, the way `hu sim` and `hu demo` are: a sweep is
one thing you start and watch. It runs every combination sequentially in this
process (see `eval.runs` for why there is no parallel option), prints a line
per run as its log closes, then reads every log back and writes
`ablation.csv`, `ablation.md` and the three PNGs.

**Measuring can be done on its own.** `hu eval --table-only` recomputes the
table and the charts from the logs already in `data/audit/` and runs nothing,
which is the whole promise of a log-derived metric: the figures can be
rebuilt, and checked, by anyone holding the files.

`--minutes` is wall-clock minutes **per run**. A full sweep of five variants,
three scenarios and `rules=20,ai=3` is 147 runs, so ten minutes each is about
a day of wall clock: the console prints the arithmetic before it starts, and
`--dry-run` prints it and stops.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from headsup.api.catalog import CatalogError
from headsup.api.runner import DEFAULT_VEHICLES
from headsup.audit.chain import runs as audit_runs
from headsup.eval import charts as charts_mod
from headsup.eval import tables as tables_mod
from headsup.eval.metrics import RunContext, RunMetrics, measure
from headsup.eval.runs import RunResult, plan_from, sweep
from headsup.eval.variants import ORDER, parse, parse_list, parse_seeds, spec_for
from headsup.llm import mode_from_env

console = Console()
err = Console(stderr=True)

#: Where the table and the charts go.
DEFAULT_OUT = "results"

#: Default sweep: everything, which is what the step asks for.
DEFAULT_VARIANTS = ",".join(variant.value for variant in ORDER)
DEFAULT_SCENARIOS = "s1,s2,s3"
DEFAULT_SEEDS = "rules=20,ai=3"


def _plan_table(plan: object, total: int) -> Table:
    table = Table(title="hu eval")
    table.add_column("variant")
    table.add_column("label")
    table.add_column("fleets")
    table.add_column("agents")
    table.add_column("seeds")
    table.add_column("runs", justify="right")
    scenarios = getattr(plan, "scenarios", ())
    seeds = getattr(plan, "seeds", {})
    for variant in getattr(plan, "variants", ()):
        spec = spec_for(variant)
        count = seeds.get(spec.seed_group, 0) * len(scenarios)
        table.add_row(
            variant.value,
            spec.label,
            "+".join(mode.value for mode in spec.modes),
            "orchestrator" if spec.orchestrator else ("mesh" if spec.mesh else "none"),
            f"{spec.seed_group}={seeds.get(spec.seed_group, 0)}",
            str(count),
        )
    table.caption = f"{total} runs over {', '.join(scenarios)}"
    return table


def _results_table(rows: list[dict[str, object]]) -> Table:
    table = Table(title="ablation (medians over seeds)")
    table.add_column("variant")
    table.add_column("scenario")
    table.add_column("runs", justify="right")
    for name, _field, _digits in tables_mod.COLUMNS:
        table.add_column(name, justify="right")
    for row in rows:
        cells = []
        for _name, field, digits in tables_mod.COLUMNS:
            value = row.get(field)
            cells.append("-" if value is None else f"{float(value):.{digits}f}")
        table.add_row(str(row["variant"]), str(row["scenario"]), str(row["runs"]), *cells)
    return table


def _write(rows: list[RunMetrics], out: Path, *, minutes: float) -> None:
    """The CSV, the charts, then the Markdown that names them."""
    csv_path = tables_mod.write_csv(rows, out)
    drawn = charts_mod.write_charts(rows, out)
    md_path = tables_mod.write_markdown(
        rows, out, minutes=minutes, llm_mode=mode_from_env().value, charts=drawn
    )
    console.print(_results_table(tables_mod.summary_rows(rows)))
    console.print(f"[bold]wrote[/bold] {csv_path.as_posix()}")
    console.print(f"[bold]wrote[/bold] {md_path.as_posix()}")
    for path in drawn:
        console.print(f"[bold]wrote[/bold] {path.as_posix()}")


def eval_(
    variants: str = typer.Option(
        DEFAULT_VARIANTS, "--variants", help="Comma-separated: V0,V1,V2,V3,V4."
    ),
    scenarios: str = typer.Option(
        DEFAULT_SCENARIOS, "--scenarios", help="Comma-separated scenario ids or s1,s2,s3."
    ),
    seeds: str = typer.Option(DEFAULT_SEEDS, "--seeds", help="Seeds per group: rules=20,ai=3."),
    minutes: float = typer.Option(10.0, "--minutes", help="Wall-clock minutes per run."),
    out: str = typer.Option(DEFAULT_OUT, "--out", help="Where the table and charts go."),
    speed: float = typer.Option(
        0.0, "--speed", help="Replay and fleet speed; 0 uses each scenario's own."
    ),
    vehicles: int = typer.Option(DEFAULT_VEHICLES, "--vehicles", help="Vehicles per run."),
    llm: bool = typer.Option(True, "--llm/--no-llm", help="Let the model-using variants ask."),
    base_seed: int = typer.Option(42, "--base-seed", help="First seed; the rest count up."),
    audit_dir: str = typer.Option("", "--audit-dir", help="Where the run logs go."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan and stop."),
    table_only: bool = typer.Option(
        False,
        "--table-only",
        help="Run nothing: rebuild the table and charts from the logs on disk.",
    ),
    log_level: str = typer.Option("warning", "--log-level", help="Python log level."),
) -> None:
    """Run the ablation: every variant x scenario x seed, each with its own log."""
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.WARNING))
    if table_only:
        _table(out=out, audit_dir=audit_dir, minutes=minutes, vehicles=vehicles)
        return
    try:
        plan = plan_from(
            variants=parse_list(variants),
            scenarios=[name for name in scenarios.split(",") if name.strip()],
            seeds=parse_seeds(seeds),
            minutes=minutes,
            speed=speed,
            vehicles=vehicles,
            llm=llm,
            base_seed=base_seed,
        )
    except (ValueError, CatalogError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    total = plan.total()
    console.print(_plan_table(plan, total))
    console.print(
        f"[bold]{total} runs[/bold] of up to {minutes:g} wall minutes each "
        f"(about {total * minutes / 60:.1f} h), llm={mode_from_env().value}. "
        "Nothing else must run `uv run` while this does."
    )
    if dry_run:
        return

    done: list[RunResult] = []

    def progress(result: RunResult) -> None:
        done.append(result)
        state = "[red]failed[/red]" if result.error else "ok"
        console.print(
            f"[{len(done)}/{total}] {result.plan.label()} {state} "
            f"{result.wall_s:.0f}s {result.entries} entries"
            + (f" - {result.error}" if result.error else "")
        )

    results = asyncio.run(sweep(plan, audit_dir=audit_dir or None, on_run=progress))
    rows = [measure(result.context) for result in results]
    _write(rows, Path(out), minutes=minutes)


def _table(*, out: str, audit_dir: str, minutes: float, vehicles: int) -> None:
    """Rebuild the table and the charts from the logs already on disk.

    Every `eval_<scenario>_<variant>_s<seed>` log in the audit directory is
    measured again. Nothing is run, nothing is overwritten but the outputs,
    and the figures that come out are the same ones the sweep wrote -- which
    is the point of computing them off the logs in the first place.
    """
    found = [name for name in audit_runs(audit_dir or None) if name.startswith("eval_")]
    if not found:
        err.print(
            f"[red]no eval logs in {audit_dir or 'data/audit'}[/red]: run `uv run hu eval` first"
        )
        raise typer.Exit(2)
    rows: list[RunMetrics] = []
    for run_id in found:
        context = _context_from(run_id, audit_dir=audit_dir or None, vehicles=vehicles)
        if context is None:
            err.print(f"[yellow]skipping {run_id}: not an eval run id[/yellow]")
            continue
        rows.append(measure(context))
    console.print(f"measured {len(rows)} run(s) from {audit_dir or 'data/audit'}")
    _write(rows, Path(out), minutes=minutes)


def _context_from(run_id: str, *, audit_dir: str | None, vehicles: int) -> RunContext | None:
    """Pull the scenario, variant and seed back out of a run id.

    The id is the only thing a log carries about which cell of the table it
    belongs to, which is exactly why it is built out of those three fields.
    """
    if not run_id.startswith("eval_"):
        return None
    body = run_id[len("eval_") :]
    head, _, seed_part = body.rpartition("_s")
    scenario, _, variant_part = head.rpartition("_")
    if not scenario or not variant_part or not seed_part.isdigit():
        return None
    try:
        variant = parse(variant_part)
    except ValueError:
        return None
    return RunContext(
        run_id=run_id,
        scenario=scenario,
        variant=variant,
        seed=int(seed_part),
        vehicles=vehicles,
        directory=Path(audit_dir) if audit_dir else None,
    )


def register(app: typer.Typer) -> None:
    """Add `hu eval` to the top-level app.

    One verb, registered the way `hu sim` and `hu demo` are: the step's own
    command line is `hu eval --variants ... --out results/`, so the options
    belong on the verb itself and not on a subcommand of a group.
    """
    app.command("eval")(eval_)
