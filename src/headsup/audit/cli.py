"""`hu audit record | verify | report | list`.

    uv run hu audit record run_demo --once   # seal whatever is on the streams
    uv run hu audit verify run_demo          # recompute the chain
    uv run hu audit report run_demo          # write the Markdown after-action

`verify` exits non-zero when the chain is broken, so it is usable as a check in
a script: a run whose log does not verify is a run whose numbers should not be
quoted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from headsup.agents.auditor import Auditor, AuditorReport
from headsup.audit.chain import (
    CITE_LENGTH,
    AuditLog,
    AuditRecord,
    ChainError,
    ChainReport,
    log_path,
    run_id_from_env,
    runs,
    verify,
)
from headsup.audit.report import AfterAction, write_report
from headsup.bus.acl import Role, url_for
from headsup.bus.client import Bus
from headsup.llm import LlmClient, LlmError, mode_from_env

audit_app = typer.Typer(
    help="The hash-chained audit log: record it, verify it, report on it.",
    no_args_is_help=True,
)

console = Console()
err = Console(stderr=True)

RUN_ARG = typer.Argument(
    "",
    help="Run id. Defaults to HU_RUN_ID, or a new id from the clock.",
    metavar="RUN_ID",
)


def _print_record(record: AuditRecord) -> None:
    console.print(
        f"[dim]{record.seq:>6}[/dim] "
        f"[cyan]{record.short}[/cyan] "
        f"[bold]{record.stream:<18}[/bold] "
        f"{record.type or '-':<18} [dim]{record.entry_id}[/dim]"
    )


@audit_app.command("record")
def audit_record(
    run_id: str = RUN_ARG,
    entries: int = typer.Option(0, "--entries", help="Stop after N entries; 0 has no cap."),
    once: bool = typer.Option(
        False, "--once", help="Seal what is on the streams now and exit, instead of following."
    ),
    namespace: str = typer.Option("", "--namespace", help="Prefix every stream name."),
    quiet: bool = typer.Option(False, "--quiet", help="Do not print a line per entry."),
) -> None:
    """Consume every stream and append each entry to the run's chain."""
    chosen = run_id or run_id_from_env()
    try:
        report = asyncio.run(_record(chosen, namespace, entries or None, once, quiet))
    except ChainError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(0) from None
    _print_auditor(report)


async def _record(
    run_id: str, namespace: str, entries: int | None, once: bool, quiet: bool
) -> AuditorReport:
    audit_log = AuditLog(run_id)
    bus = Bus(url_for(Role.AGENT), namespace=namespace, writer=None)
    console.print(
        f"[bold]audit record[/bold] {run_id} -> {audit_log.path.as_posix()}"
        + (f" namespace={namespace}" if namespace else "")
        + (" --once" if once else "")
    )
    try:
        async with Auditor(audit_log, bus, on_record=None if quiet else _print_record) as auditor:
            return await auditor.run(events=entries, once=once)
    finally:
        await bus.close()


def _print_auditor(report: AuditorReport) -> None:
    table = Table(title=f"audit {report.run_id}")
    table.add_column("stream")
    table.add_column("entries", justify="right")
    for name, count in report.by_stream.most_common():
        table.add_row(name, str(count))
    table.add_row("[bold]total[/bold]", f"[bold]{report.entries}[/bold]")
    console.print(table)
    console.print(f"head [cyan]{report.head or '-'}[/cyan]  {report.path}")
    if report.skipped:
        err.print(f"[red]{report.skipped} entries could not be recorded[/red]")
    for problem in report.errors:
        err.print(f"[yellow]{problem}[/yellow]")


@audit_app.command("verify")
def audit_verify(run_id: str = RUN_ARG) -> None:
    """Recompute the chain and report the first broken line, if there is one."""
    chosen = run_id or run_id_from_env()
    try:
        report = verify(chosen)
    except ChainError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _print_chain(report)
    if not report.ok:
        raise typer.Exit(1)


def _print_chain(report: ChainReport) -> None:
    console.print(f"[bold]{report.path.as_posix()}[/bold]")
    if report.ok:
        console.print(
            f"[green]chain intact[/green]: {report.verified} lines verified from the "
            f"genesis hash\nhead [cyan]{report.head}[/cyan]"
        )
        return
    broken = report.broken
    assert broken is not None  # not ok means there is one
    err.print(
        f"[red]chain broken[/red] at line [bold]{broken.line}[/bold] "
        f"({broken.reason.value})\n  {broken.detail}"
    )
    if broken.stream:
        err.print(f"  entry: {broken.stream} {broken.entry_id}")
    err.print(
        f"  {report.verified} line(s) before it verified; "
        f"last good head {report.head[:CITE_LENGTH]}"
    )


@audit_app.command("report")
def audit_report(
    run_id: str = RUN_ARG,
    llm: bool = typer.Option(
        True, "--llm/--no-llm", help="Let the judgment model write the prose."
    ),
) -> None:
    """Write `data/audit/<run_id>.md`: the after-action report, cited by hash."""
    chosen = run_id or run_id_from_env()
    console.print(
        f"[bold]audit report[/bold] {chosen}"
        + (f" llm={mode_from_env().value}" if llm else " [yellow]llm off[/yellow]")
    )
    try:
        path, chain, action = asyncio.run(_report(chosen, llm))
    except (ChainError, LlmError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    _print_written(path, chain, action)
    if not chain.ok:
        raise typer.Exit(1)


async def _report(run_id: str, use_llm: bool) -> tuple[Path, ChainReport, AfterAction]:
    client = LlmClient.from_env() if use_llm else None
    try:
        return await write_report(run_id, llm=client)
    finally:
        if client is not None:
            await client.aclose()


def _print_written(path: Path, chain: ChainReport, action: AfterAction) -> None:
    console.print(f"  wrote {path.as_posix()}")
    console.print(f"  prose from [bold]{action.source}[/bold], {len(action.findings)} finding(s)")
    if chain.ok:
        console.print(f"[green]chain intact[/green]: {chain.verified} lines verified")
    else:
        err.print(f"[red]chain broken[/red]: {chain.broken.summary() if chain.broken else ''}")


@audit_app.command("list")
def audit_list() -> None:
    """Every run that has a log, oldest first."""
    found = runs()
    if not found:
        console.print(f"[yellow]no audit logs in {log_path('x').parent.as_posix()}[/yellow]")
        return
    for name in found:
        console.print(f"  {name}")
