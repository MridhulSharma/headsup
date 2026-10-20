"""`hu ingest live | record | replay`.

Kept out of `headsup.cli` so the top-level CLI stays a table of contents and
the ingest commands sit next to the code they drive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from headsup.bus import Bus
from headsup.contracts import CivicEvent
from headsup.ingest.base import IngestError
from headsup.ingest.cities import CITIES
from headsup.ingest.cities import city as lookup_city
from headsup.ingest.live import LiveIngest, city_adapters
from headsup.ingest.recorder import RAW_DIR, Recorder, Recording
from headsup.ingest.replayer import MAX_SPEED, MIN_SPEED, Emission, Replayer, check_speed

ingest_app = typer.Typer(
    help="Public feeds in, CivicEvents out: live polling, recording and replay.",
    no_args_is_help=True,
)

console = Console()
err = Console(stderr=True)

_CITY_KEYS = ", ".join(sorted(CITIES))


def _print_event(event: CivicEvent) -> None:
    console.print(
        f"[dim]{event.observed_at:%H:%M:%SZ}[/dim] "
        f"[cyan]{event.source.value:<6}[/cyan] {event.kind:<20} "
        f"[dim]{event.id}[/dim] {event.geometry.type}"
    )


def _print_emission(emission: Emission) -> None:
    console.print(
        f"[dim]+{emission.replay_offset:7.1f}s[/dim] "
        f"[dim]{emission.event.observed_at:%H:%M:%SZ}[/dim] "
        f"[cyan]{emission.source.value:<6}[/cyan] {emission.event.kind:<20} "
        f"[dim]{emission.event.id}[/dim] {emission.event.geometry.type}"
    )


def _bus_or_none(publish: bool) -> Bus | None:
    return Bus.from_env() if publish else None


async def _shutdown(bus: Bus | None) -> None:
    if bus is not None:
        await bus.close()


@ingest_app.command("live")
def ingest_live(
    city: str = typer.Option("austin", "--city", help=f"One of: {_CITY_KEYS}."),
    minutes: float = typer.Option(0.0, "--minutes", help="Stop after N minutes; 0 runs on."),
    publish: bool = typer.Option(True, "--publish/--no-publish", help="Write to the bus."),
) -> None:
    """Poll the live feeds for a city and put the events on the bus."""
    try:
        target = lookup_city(city)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    console.print(
        f"[bold]live ingest[/bold] {target.name} "
        f"(NWS area {target.nws_area}, {len(target.gauge_sites)} gauges)"
        + ("" if publish else " [yellow]- not publishing[/yellow]")
    )
    asyncio.run(_run_live(target.key, minutes, publish, scenario=None))


@ingest_app.command("record")
def ingest_record(
    scenario: str = typer.Option(..., "--scenario", help="Recording name under data/recordings."),
    minutes: float = typer.Option(10.0, "--minutes", help="How long to record."),
    city: str = typer.Option("san_antonio", "--city", help=f"One of: {_CITY_KEYS}."),
    publish: bool = typer.Option(False, "--publish/--no-publish", help="Also write to the bus."),
) -> None:
    """Poll the live feeds and write every raw payload to disk."""
    try:
        target = lookup_city(city)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    if minutes <= 0:
        err.print("[red]--minutes must be greater than 0[/red]")
        raise typer.Exit(2)

    console.print(
        f"[bold]recording[/bold] {scenario} from {target.name} for {minutes:g} min "
        f"-> data/recordings/{scenario}/{RAW_DIR}/"
    )
    asyncio.run(_run_live(target.key, minutes, publish, scenario=scenario))


async def _run_live(city_key: str, minutes: float, publish: bool, scenario: str | None) -> None:
    target = lookup_city(city_key)
    bus = _bus_or_none(publish)
    recorder = Recorder(scenario) if scenario else None
    feeds = city_adapters(target)
    ingest = LiveIngest(
        feeds,
        bus=bus,
        recorder=recorder,
        on_event=_print_event,
        on_error=lambda source, exc: err.print(f"[yellow]{source.value}: {exc}[/yellow]"),
    )
    try:
        await ingest.run(duration_s=minutes * 60 if minutes > 0 else None)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("[yellow]interrupted[/yellow]")
    finally:
        await ingest.aclose()
        if recorder is not None:
            recorder.close()
        await _shutdown(bus)

    table = Table(title="feeds")
    table.add_column("feed")
    table.add_column("polls", justify="right")
    table.add_column("payloads", justify="right")
    table.add_column("events", justify="right")
    table.add_column("errors", justify="right")
    for run in ingest.runs.values():
        table.add_row(
            run.source.value,
            str(run.polls),
            str(run.payloads),
            str(run.events),
            f"[red]{len(run.errors)}[/red]" if run.errors else "0",
        )
    console.print(table)
    if recorder is not None:
        console.print(
            f"[green]wrote {recorder.total} payloads to {recorder.directory.as_posix()}[/green]"
        )


@ingest_app.command("replay")
def ingest_replay(
    scenario: str = typer.Option(..., "--scenario", help="Recording name under data/recordings."),
    speed: float = typer.Option(
        1.0, "--speed", help=f"Playback factor, {MIN_SPEED:g}x to {MAX_SPEED:g}x."
    ),
    publish: bool = typer.Option(True, "--publish/--no-publish", help="Write to the bus."),
    limit: int = typer.Option(0, "--limit", help="Stop after N events; 0 plays it all."),
    source_dir: str = typer.Option("", "--dir", help="Read this directory instead."),
) -> None:
    """Replay a recording onto the bus with its original spacing."""
    try:
        check_speed(speed)
        recording = (
            Recording.at(Path(source_dir), scenario=scenario)
            if source_dir
            else Recording.locate(scenario)
        )
    except (ValueError, IngestError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    console.print(
        f"[bold]replay[/bold] {scenario} at {speed:g}x from "
        f"{recording.directory.as_posix()}"
        + ("" if publish else " [yellow]- not publishing[/yellow]")
    )
    asyncio.run(_run_replay(recording, speed, publish, limit or None))


async def _run_replay(recording: Recording, speed: float, publish: bool, limit: int | None) -> None:
    bus = _bus_or_none(publish)
    replayer = Replayer(recording, bus=bus, speed=speed)
    count = 0
    last: Emission | None = None
    try:
        async for emission in replayer.run(limit=limit):
            _print_emission(emission)
            last = emission
            count += 1
    except Exception as exc:  # a dead feed or a dead Redis, reported not traced
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        await _shutdown(bus)

    feed_span = last.feed_offset if last else 0.0
    replay_span = last.replay_offset if last else 0.0
    console.print(
        f"[green]{count} events from {len(replayer.payloads())} payloads; "
        f"{feed_span / 60:.1f} min of feed time in {replay_span:.1f} s at {speed:g}x[/green]"
    )
