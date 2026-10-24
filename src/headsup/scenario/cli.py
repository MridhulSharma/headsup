"""`hu scenario signals | synth-blackout | fetch-incidents`: the built scenarios.

The first two belong to S2, in the order you use them. `signals --refresh`
fetches the city's signal map once and commits a snapshot of it;
`synth-blackout` reads that snapshot and writes the S2 recording and its
descriptor. Only the first touches the network, so a generated scenario is
reproducible offline.

`fetch-incidents` belongs to S3 and is one command rather than two, because
its recording is not generated from a snapshot: it *is* a window of the real
incident board, with the scripted scene injected into it. It reaches the
network every time, and the archive it reads answers the same way twice.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import typer
from rich.console import Console
from rich.table import Table

from headsup.ingest.base import IngestError
from headsup.scenario import blackout as blackout_mod
from headsup.scenario import descriptor as descriptor_mod
from headsup.scenario import incident_descriptor as incident_descriptor_mod
from headsup.scenario import incidents as incidents_mod
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


@scenario_app.command("fetch-incidents")
def fetch_incidents(
    window: str = typer.Option(
        "",
        "--window",
        help="Window start, YYYY-MM-DD or an ISO instant. Default: the committed window.",
    ),
    minutes: float = typer.Option(
        incidents_mod.WINDOW_MINUTES, "--minutes", help="Length of the window in feed minutes."
    ),
    descriptor: bool = typer.Option(
        True, "--descriptor/--no-descriptor", help="Also write scenarios/s3_incident.yaml."
    ),
) -> None:
    """Write the S3 recording: a real incident window with the scene injected."""
    try:
        start = _window_start(window)
        real = asyncio.run(incidents_mod.fetch(start=start, minutes=minutes))
        built = incidents_mod.build(real, start=start, minutes=minutes)
        recording = incidents_mod.write(built)
        events = built.events()
        written = (
            incident_descriptor_mod.write(
                built,
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

    start_at, end_at = built.window
    console.print(
        f"[bold]{incidents_mod.SCENARIO}[/bold] "
        f"{start_at:%Y-%m-%dT%H:%MZ} .. {end_at:%Y-%m-%dT%H:%MZ} ({built.minutes:g} min)"
    )

    table = Table(title="recorded")
    table.add_column("what")
    table.add_column("count", justify="right")
    table.add_column("detail")
    table.add_row(
        "polls", str(len(built.payloads)), f"every {incidents_mod.POLL_SECONDS:g} s, reconstructed"
    )
    table.add_row("real records", str(len(built.real_rows)), "the city's own, verbatim")
    table.add_row(
        "scripted records",
        str(len(built.scripted_rows)),
        f"the scene at {incidents_mod.SCENE_ADDRESS} and two decoys",
    )
    table.add_row("records", str(built.records), "rows across every poll")
    table.add_row("events", str(events), "CivicEvents the adapter parses")
    size_kb = recording.stat().st_size / 1024
    table.add_row("fixture", f"{size_kb:.0f} KB", recording.name)
    console.print(table)
    console.print(f"[green]{recording.as_posix()}[/green]")
    if written is not None:
        console.print(f"[green]{written.as_posix()}[/green]")


def _window_start(value: str) -> datetime:
    """The window start a run was asked for, or the committed default.

    A bare date is read as midnight UTC on that day plus the committed
    window's own time of day, so `--window 2026-10-21` asks for the same
    twelve hours of a different Wednesday rather than a different twelve
    hours.
    """
    text = value.strip()
    if not text:
        return incidents_mod.WINDOW_START
    default = incidents_mod.WINDOW_START
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            return default.replace(year=day.year, month=day.month, day=day.day)
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a date or an ISO instant") from exc
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


def register(app: typer.Typer) -> None:
    """Add the `hu scenario` group to the top-level app."""
    app.add_typer(scenario_app, name="scenario")
