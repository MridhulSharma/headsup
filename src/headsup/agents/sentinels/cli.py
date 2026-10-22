"""`hu agents --only flood,signal`: run sentinels as workers.

One verb, like `hu sim`: the agents are a thing you start and watch. `--only`
names which of them to run, so the demo can bring up the two sentinels S1 and
S2 need without the rest of the pipeline being in the way.

Every selected sentinel gets its own task, its own consumer group and its own
row in the closing table. One `LlmClient` is shared: the spend meter and the
budget are per process, and two clients would mean two budgets.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from headsup.agents.sentinels.flood import FloodSentinel
from headsup.agents.sentinels.signals import SignalOutageSentinel
from headsup.agents.sentinels.worker import SentinelWorker, WorkerReport
from headsup.bus import Bus
from headsup.bus.acl import Role, url_for
from headsup.contracts import CandidateHazard
from headsup.llm import LlmClient, LlmError, mode_from_env

console = Console()
err = Console(stderr=True)

#: `--only` name -> what to build. The flood sentinel takes the shared client;
#: the signal sentinel reads structured feed fields and needs no model at all.
AGENTS: dict[str, str] = {
    "flood": "NWS flood polygons, corroborated by USGS gauges",
    "signal": "Austin signals in unscheduled flash, clustered",
}

#: The agents that ask a model anything. The signal sentinel reads structured
#: feed fields and never needs one.
MODEL_AGENTS = frozenset({"flood"})

_NAMES = ", ".join(AGENTS)


def _build(name: str, llm: LlmClient | None) -> FloodSentinel | SignalOutageSentinel:
    if name == "flood":
        return FloodSentinel(llm=llm)
    return SignalOutageSentinel()


def _selected(only: str) -> list[str]:
    """Parse `--only flood,signal`, or every agent when it is empty."""
    if not only.strip():
        return list(AGENTS)
    chosen = [part.strip() for part in only.split(",") if part.strip()]
    unknown = [name for name in chosen if name not in AGENTS]
    if unknown:
        raise ValueError(f"unknown agent(s) {', '.join(unknown)}; one of: {_NAMES}")
    # De-duplicated, in the table's order rather than the flag's.
    return [name for name in AGENTS if name in chosen]


def _print_candidate(sentinel: str, candidate: CandidateHazard) -> None:
    console.print(
        f"[dim]{candidate.detected_at:%H:%M:%SZ}[/dim] "
        f"[cyan]{sentinel:<24}[/cyan] "
        f"[bold]{candidate.hazard_class.value:<14}[/bold] "
        f"sev {candidate.severity} conf {candidate.confidence:.2f} "
        f"[dim]{candidate.id}[/dim] {candidate.geometry.type}"
    )


def agents(
    only: str = typer.Option("", "--only", help=f"Comma-separated agents: {_NAMES}."),
    events: int = typer.Option(0, "--events", help="Stop after N events; 0 has no cap."),
    once: bool = typer.Option(
        False, "--once", help="Drain what is on the streams and exit, instead of following."
    ),
    namespace: str = typer.Option("", "--namespace", help="Prefix every stream name."),
    llm: bool = typer.Option(
        True, "--llm/--no-llm", help="Let the flood sentinel fill schema fields with the model."
    ),
) -> None:
    """Run the event-driven agents: feeds in, candidate hazards out."""
    try:
        chosen = _selected(only)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    console.print(
        f"[bold]agents[/bold] {'+'.join(chosen)}"
        + (f" namespace={namespace}" if namespace else "")
        + (
            f" llm={mode_from_env().value}"
            if llm and any(name in MODEL_AGENTS for name in chosen)
            else " [yellow]llm off[/yellow]"
        )
        + (" --once" if once else "")
    )
    for name in chosen:
        console.print(f"[dim]  {name}: {AGENTS[name]}[/dim]")

    try:
        reports = asyncio.run(_run(chosen, namespace, events or None, once, llm))
    except LlmError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(0) from None

    _report(reports)


async def _run(
    chosen: list[str],
    namespace: str,
    events: int | None,
    once: bool,
    use_llm: bool,
) -> list[WorkerReport]:
    """One bus, one client, one task per agent, torn down on the same loop.

    The client is built only if an agent that asks a model is running: in
    `claude` mode building one opens an HTTP client and a local-model
    fallback, and a signal-only worker has no use for either.
    """
    bus = Bus(url_for(Role.AGENT), namespace=namespace, writer=None)
    wants_model = use_llm and any(name in MODEL_AGENTS for name in chosen)
    client = LlmClient.from_env(bus=bus) if wants_model else None
    workers = [
        SentinelWorker(_build(name, client), bus, on_candidate=_print_candidate) for name in chosen
    ]
    try:
        return list(
            await asyncio.gather(*(worker.run(events=events, once=once) for worker in workers))
        )
    finally:
        if client is not None:
            await client.aclose()
        await bus.close()


def _report(reports: list[WorkerReport]) -> None:
    table = Table(title="agents")
    table.add_column("agent")
    table.add_column("events", justify="right")
    table.add_column("candidates", justify="right")
    table.add_column("skipped", justify="right")
    for report in reports:
        table.add_row(
            report.sentinel,
            str(report.events),
            str(report.candidates),
            f"[red]{report.skipped}[/red]" if report.skipped else "0",
        )
    console.print(table)
    for report in reports:
        for problem in report.errors:
            err.print(f"[yellow]{report.sentinel}: {problem}[/yellow]")


def register(app: typer.Typer) -> None:
    """Add `hu agents` to the top-level app."""
    app.command("agents")(agents)
