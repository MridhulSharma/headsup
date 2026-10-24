"""`hu agents --only flood,signal,fusion,proposer`: run the pipeline as workers.

One verb, like `hu sim`: the agents are a thing you start and watch. `--only`
names which of them to run, so the demo can bring up just the stages a scenario
needs and a bug can be cornered one stage at a time.

Every selected agent gets its own consumer group and its own row in the closing
table. One `LlmClient` is shared: the spend meter and the budget are per
process, and two clients would mean two budgets.

**Follow mode runs them together; `--once` runs them in order.** Following, the
four workers are concurrent tasks and the pipeline fills as the feeds arrive.
With `--once` each worker drains its stream and exits, so running them
concurrently would let the proposer find `hazards` empty and quit before fusion
had written to it. In that mode they are run in pipeline order instead, which
makes `hu agents --once` a usable end-to-end drain of whatever a replay left on
the streams.

**`--compile` is opt-in.** The proposer hands each proposal and the
critic's verdict to `CompilerService`, which refuses to start without
`HU_CITY` and a world built for it - it is the only writer of `advisories` and
will not publish closures it cannot check for hospital access. Without the flag
the pipeline stops at `proposals`, which is what lets the agents run on a box
with no world build and no signing key.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from headsup.agents.compiler.compiler import CompilerError
from headsup.agents.compiler.service import CompilerService
from headsup.agents.critic import Critic
from headsup.agents.fusion import Fusion
from headsup.agents.pipeline import FusionWorker, ProposerWorker
from headsup.agents.proposer import Proposer
from headsup.agents.sentinels.flood import FloodSentinel
from headsup.agents.sentinels.signals import SignalOutageSentinel
from headsup.agents.sentinels.worker import SentinelWorker, WorkerReport
from headsup.bus import Bus
from headsup.bus.acl import Role, url_for
from headsup.contracts import CandidateHazard, Hazard, ProposedAdvisory
from headsup.llm import LlmClient, LlmError, mode_from_env

console = Console()
err = Console(stderr=True)

#: `--only` name -> what it does, in pipeline order. The order is the table's
#: order and, under `--once`, the order they are run in.
AGENTS: dict[str, str] = {
    "flood": "NWS flood polygons, corroborated by USGS gauges",
    "signal": "Austin signals in unscheduled flash, clustered",
    "fusion": "candidates grouped and weighed into hazards",
    "proposer": "one playbook-allowed action per hazard",
}

#: The agents that ask a model anything. The signal sentinel reads structured
#: feed fields and never needs one.
MODEL_AGENTS = frozenset({"flood", "fusion", "proposer"})

#: The agents downstream of a sentinel, which is what `--compile` is about.
PIPELINE_AGENTS = frozenset({"fusion", "proposer"})

_NAMES = ", ".join(AGENTS)

#: Any of the four workers; they share `run` and a `WorkerReport`.
Worker = SentinelWorker | FusionWorker | ProposerWorker


def _selected(only: str) -> list[str]:
    """Parse `--only flood,fusion`, or every agent when it is empty."""
    if not only.strip():
        return list(AGENTS)
    chosen = [part.strip() for part in only.split(",") if part.strip()]
    unknown = [name for name in chosen if name not in AGENTS]
    if unknown:
        raise ValueError(f"unknown agent(s) {', '.join(unknown)}; one of: {_NAMES}")
    # De-duplicated, in pipeline order rather than the flag's.
    return [name for name in AGENTS if name in chosen]


def _print_candidate(sentinel: str, candidate: CandidateHazard) -> None:
    console.print(
        f"[dim]{candidate.detected_at:%H:%M:%SZ}[/dim] "
        f"[cyan]{sentinel:<24}[/cyan] "
        f"[bold]{candidate.hazard_class.value:<14}[/bold] "
        f"sev {candidate.severity} conf {candidate.confidence:.2f} "
        f"[dim]{candidate.id}[/dim] {candidate.geometry.type}"
    )


def _print_hazard(agent: str, hazard: Hazard) -> None:
    console.print(
        f"[dim]{hazard.updated_at:%H:%M:%SZ}[/dim] "
        f"[magenta]{agent:<24}[/magenta] "
        f"[bold]{hazard.hazard_class.value:<14}[/bold] "
        f"sev {hazard.severity} conf {hazard.confidence:.2f} "
        f"[dim]{hazard.id}[/dim] {len(hazard.evidence)} citations"
    )


def _print_proposal(agent: str, proposal: ProposedAdvisory) -> None:
    console.print(
        f"[dim]{proposal.created_at:%H:%M:%SZ}[/dim] "
        f"[green]{agent:<24}[/green] "
        f"[bold]{proposal.action.type.value:<14}[/bold] "
        f"{proposal.policy_rule} "
        f"[dim]{proposal.id}[/dim] until {proposal.valid_until:%H:%M:%SZ}"
    )


def _build(name: str, bus: Bus, llm: LlmClient | None, service: CompilerService | None) -> Worker:
    """One worker, by `--only` name.

    The proposer borrows the compiler service's POI checker when there is one,
    so both ends of the pipeline answer the critical-access question off the
    same road graph: the proposer stops asking for a closure the gate was
    always going to refuse. Without `--compile` it gets the null checker and
    proposes closures the compiler would have to check for itself.
    """
    if name == "flood":
        return SentinelWorker(FloodSentinel(llm=llm), bus, on_candidate=_print_candidate)
    if name == "signal":
        return SentinelWorker(SignalOutageSentinel(), bus, on_candidate=_print_candidate)
    if name == "fusion":
        return FusionWorker(Fusion(llm=llm), bus, on_hazard=_print_hazard)
    poi = service.compiler.poi if service is not None else None
    return ProposerWorker(
        Proposer(llm=llm, poi=poi),
        bus,
        critic=Critic(llm=llm),
        compiler=service,
        on_proposal=_print_proposal,
    )


def agents(
    only: str = typer.Option("", "--only", help=f"Comma-separated agents: {_NAMES}."),
    events: int = typer.Option(0, "--events", help="Stop after N messages each; 0 has no cap."),
    once: bool = typer.Option(
        False, "--once", help="Drain the streams in pipeline order and exit, instead of following."
    ),
    namespace: str = typer.Option("", "--namespace", help="Prefix every stream name."),
    llm: bool = typer.Option(
        True, "--llm/--no-llm", help="Let the agents that ask a model ask it."
    ),
    compile_: bool = typer.Option(
        False,
        "--compile/--no-compile",
        help="Review each proposal and hand the pair to the compiler. Needs HU_CITY and a world.",
    ),
) -> None:
    """Run the event-driven agents: feeds in, candidates, hazards, proposals out."""
    try:
        chosen = _selected(only)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    wants_compile = compile_ and "proposer" in chosen
    console.print(
        f"[bold]agents[/bold] {'+'.join(chosen)}"
        + (f" namespace={namespace}" if namespace else "")
        + (
            f" llm={mode_from_env().value}"
            if llm and any(name in MODEL_AGENTS for name in chosen)
            else " [yellow]llm off[/yellow]"
        )
        + (" --compile" if wants_compile else "")
        + (" --once" if once else "")
    )
    for name in chosen:
        console.print(f"[dim]  {name}: {AGENTS[name]}[/dim]")

    try:
        reports = asyncio.run(_run(chosen, namespace, events or None, once, llm, wants_compile))
    except (CompilerError, LlmError) as exc:
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
    use_compiler: bool,
) -> list[WorkerReport]:
    """One bus, one client, one worker per agent, torn down on the same loop.

    The client is built only if an agent that asks a model is running: in
    `claude` mode building one opens an HTTP client and a local-model fallback,
    and a signal-only worker has no use for either.
    """
    bus = Bus(url_for(Role.AGENT), namespace=namespace, writer=None)
    wants_model = use_llm and any(name in MODEL_AGENTS for name in chosen)
    client = LlmClient.from_env(bus=bus) if wants_model else None
    service = CompilerService.from_env(namespace=namespace) if use_compiler else None
    workers = [_build(name, bus, client, service) for name in chosen]
    try:
        if once:
            # Pipeline order, one stage at a time: a draining proposer must not
            # find `hazards` empty because fusion has not run yet.
            return [await worker.run(events=events, once=True) for worker in workers]
        return list(await asyncio.gather(*(worker.run(events=events) for worker in workers)))
    finally:
        if service is not None:
            await service.close()
        if client is not None:
            await client.aclose()
        await bus.close()


def _report(reports: list[WorkerReport]) -> None:
    """One row per worker: what it read, what it wrote, what it skipped."""
    table = Table(title="agents")
    table.add_column("agent")
    table.add_column("read", justify="right")
    table.add_column("written", justify="right")
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
