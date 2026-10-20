"""`hu` command line entry point."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from headsup import __version__
from headsup import infra as infra_mod

app = typer.Typer(
    name="hu",
    help="Heads Up: civic signals compiled into signed, expiring fleet advisories.",
    no_args_is_help=True,
    add_completion=False,
)
infra_app = typer.Typer(help="Redis and PostGIS via docker compose.", no_args_is_help=True)
app.add_typer(infra_app, name="infra")

console = Console()
err = Console(stderr=True)


@app.command()
def version() -> None:
    """Print the Heads Up version."""
    console.print(f"headsup {__version__}")


def _status_table(states: list[infra_mod.ServiceState]) -> Table:
    table = Table(title="headsup infra")
    table.add_column("service")
    table.add_column("state")
    table.add_column("health")
    for svc in states:
        colour = "green" if svc.healthy else "red"
        table.add_row(svc.name, svc.state, f"[{colour}]{svc.health}[/{colour}]")
    return table


def _report(states: list[infra_mod.ServiceState]) -> None:
    """Print the status table and exit non-zero unless every service is healthy."""
    console.print(_status_table(states))
    unhealthy = [svc.name for svc in states if not svc.healthy]
    if unhealthy:
        err.print(f"[red]not healthy:[/red] {', '.join(unhealthy)} - run `hu infra up`")
        raise typer.Exit(1)
    console.print("[green]all services healthy[/green]")


@infra_app.command("up")
def infra_up(
    timeout: int = typer.Option(120, help="Seconds to wait for both healthchecks."),
) -> None:
    """Start the services and wait until both healthchecks pass."""
    try:
        infra_mod.compose("up", "--detach", "--wait", "--wait-timeout", str(timeout))
        _report(infra_mod.status())
    except infra_mod.InfraError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@infra_app.command("down")
def infra_down(
    volumes: bool = typer.Option(False, "--volumes", help="Also delete the named volumes."),
) -> None:
    """Stop and remove the services."""
    args = ["down", "--remove-orphans"]
    if volumes:
        args.append("--volumes")
    try:
        infra_mod.compose(*args)
    except infra_mod.InfraError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print("[green]infra down[/green]")


@infra_app.command("status")
def infra_status() -> None:
    """Show whether redis and postgis are running and healthy."""
    try:
        _report(infra_mod.status())
    except infra_mod.InfraError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
