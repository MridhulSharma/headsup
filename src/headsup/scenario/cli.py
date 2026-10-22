"""`hu scenario synth-blackout | signals`: the generated S2 scenario.

Two verbs, in the order you use them. `signals --refresh` fetches the city's
signal map once and commits a snapshot of it; `synth-blackout` reads that
snapshot and writes the S2 recording and its descriptor. Only the first one
touches the network, so a generated scenario is reproducible offline.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from headsup.ingest.base import IngestError
from headsup.scenario import blackout as blackout_mod
from headsup.scenario import descriptor as descriptor_mod
from headsup.scenario import inventory as inventory_mod
from headsup.scenario.inventory import SignalInventory

scenario_app = typer.Typer(
    help="Generated scenarios: the S2 signal blackout on the real signal map.",
    no_args_is_help=True,
)

console = Console()
err = Console(stderr=True)


@scenario_app.command("signals")
def signals(
    city: str = typer.Option(blackout_mod.CITY, "--city", help="City whose signal map to show."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Fetch the map from the city's Socrata portal and rewrite it."
    ),
) -> None:
    """Show, or re-fetch, the committed snapshot of a city's signal map."""
    try:
        snapshot = asyncio.run(inventory_mod.fetch(city)) if refresh else SignalInventory.load(city)
        if refresh:
            path = snapshot.write()
            console.print(f"[green]wrote {len(snapshot)} signals to {path.as_posix()}[/green]")
    except (IngestError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    table = Table(title=f"{snapshot.city} signal map")
    table.add_column("field")
    table.add_column("value")
    table.add_row("dataset", snapshot.dataset)
    table.add_row("signals", str(len(snapshot)))
    table.add_row("bbox", ", ".join(f"{value:g}" for value in snapshot.bbox))
    table.add_row("retrieved", snapshot.retrieved_at)
    table.add_row("source", snapshot.source_url)
    console.print(table)


@scenario_app.command("synth-blackout")
def synth_blackout(
    seed: int = typer.Option(7, "--seed", help="Everything generated is derived from this."),
    signals_dark: int = typer.Option(
        blackout_mod.DEFAULT_SIGNALS, "--signals", help="Signals dark at the peak of the outage."
    ),
    noise: int = typer.Option(
        blackout_mod.DEFAULT_NOISE, "--noise", help="Distant flashing signals, as DBSCAN noise."
    ),
    minutes: float = typer.Option(
        blackout_mod.DEFAULT_MINUTES, "--minutes", help="Length of the scenario window."
    ),
    descriptor: bool = typer.Option(
        True, "--descriptor/--no-descriptor", help="Also write scenarios/s2_blackout.yaml."
    ),
) -> None:
    """Write the S2 recording: a cluster of flashing signals on the real map."""
    try:
        snapshot = SignalInventory.load(blackout_mod.CITY)
        generated = blackout_mod.build(
            snapshot, seed=seed, signals=signals_dark, noise=noise, minutes=minutes
        )
        recording = blackout_mod.write(generated)
        events = generated.events()
        written = (
            descriptor_mod.write(
                generated,
                recording=recording,
                events=events,
                bytes_=recording.stat().st_size,
            )
            if descriptor
            else None
        )
    except (IngestError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    start, end = generated.window
    console.print(
        f"[bold]{blackout_mod.SCENARIO}[/bold] seed={seed} "
        f"{start:%Y-%m-%dT%H:%MZ} .. {end:%H:%MZ} ({generated.minutes:g} min)"
    )

    table = Table(title="generated")
    table.add_column("what")
    table.add_column("count", justify="right")
    table.add_column("detail")
    table.add_row("polls", str(len(generated.payloads)), f"every {blackout_mod.POLL_SECONDS:g} s")
    table.add_row("records", str(generated.records), "rows across every poll")
    table.add_row("events", str(events), "CivicEvents the adapter parses")
    table.add_row(
        "cluster",
        str(len(generated.cluster)),
        ", ".join(item.name for item in generated.cluster[:3]) + ", ...",
    )
    table.add_row(
        "noise",
        str(len(generated.noise)),
        f"beyond {blackout_mod.NOISE_RADIUS_M:g} m of the centre",
    )
    table.add_row(
        "decoys",
        str(len(generated.decoys)),
        "scheduled flash and communication issues, in area",
    )
    console.print(table)
    console.print(f"[green]{recording.as_posix()}[/green]")
    if written is not None:
        console.print(f"[green]{written.as_posix()}[/green]")


def register(app: typer.Typer) -> None:
    """Add the `hu scenario` group to the top-level app."""
    app.add_typer(scenario_app, name="scenario")
