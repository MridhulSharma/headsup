"""`hu sim`: run the fleet simulator.

One verb rather than a group, which is the shape `docs/build-packet.md` names:
`hu sim --city san_antonio --fleets 3 --vehicles 900 --seed 42 --speed 10`.
Registered the same way the compiler's verbs are, via `register(app)`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from headsup.bus import Bus
from headsup.bus.acl import Role, url_for
from headsup.contracts import HazardClass
from headsup.ingest.replayer import check_speed
from headsup.sim.config import UNPACED, SimConfig, SimMode
from headsup.sim.ground_truth import (
    EmptyHazardField,
    HazardRegion,
    ScriptedHazardField,
    load_regions,
)
from headsup.sim.network import DriveNetwork, NetworkError
from headsup.sim.runner import SimRunner, modes_for
from headsup.sim.scenarios import SCENARIO_CITY, scenario_regions
from headsup.world.bbox import BOXES
from headsup.world.graph import WorldError
from headsup.world.model import is_built, load_world

console = Console()
err = Console(stderr=True)

_CITIES = ", ".join(sorted(BOXES))
_SCENARIOS = ", ".join(sorted(SCENARIO_CITY))


def sim(
    city: str = typer.Option("san_antonio", "--city", help=f"One of: {_CITIES}."),
    fleets: int = typer.Option(3, "--fleets", help="Operator fleets, 1 to 3."),
    vehicles: int = typer.Option(900, "--vehicles", help="Total vehicles across all fleets."),
    seed: int = typer.Option(42, "--seed", help="Run seed; the same seed replays exactly."),
    speed: float = typer.Option(
        10.0, "--speed", help="Sim seconds per wall second, 1 to 60. 0 runs unpaced."
    ),
    minutes: float = typer.Option(0.0, "--minutes", help="Simulated minutes; 0 runs until Ctrl+C."),
    ticks: int = typer.Option(0, "--ticks", help="Exact tick count; overrides --minutes."),
    ghost: bool = typer.Option(False, "--ghost", help="Run only the ghost fleet (baseline V0)."),
    mode: str = typer.Option("", "--mode", help="live, ghost or both. Overrides --ghost."),
    scenario: str = typer.Option("", "--scenario", help=f"Ground truth from: {_SCENARIOS}."),
    hazard: list[str] = typer.Option(
        None, "--hazard", help="GeoJSON file of a ground-truth hazard area. Repeatable."
    ),
    zones: int = typer.Option(48, "--zones", help="Destination zones."),
    any_vertex_destinations: bool = typer.Option(
        False,
        "--any-vertex-destinations",
        help="Trips may end at any vertex. Much slower; for evaluation only.",
    ),
    telemetry_hz: float = typer.Option(
        2.0, "--telemetry-hz", help="Telemetry rate, in wall-clock Hz."
    ),
    publish: bool = typer.Option(True, "--publish/--no-publish", help="Write to the bus."),
    namespace: str = typer.Option("", "--namespace", help="Prefix every stream name."),
) -> None:
    """Run the deterministic mesoscopic fleet simulator."""
    try:
        if speed != UNPACED:
            check_speed(speed)
        cfg = SimConfig(
            city=city,
            seed=seed,
            vehicles=vehicles,
            fleets=fleets,
            speed=speed,
            zones=zones,
            telemetry_hz=telemetry_hz,
            any_vertex_destinations=any_vertex_destinations,
        )
        cfg.validate()
        if city not in BOXES:
            raise ValueError(f"unknown city {city!r}; one of: {_CITIES}")
        if not is_built(city):
            raise ValueError(f"{city} is not built - run `hu world build --city {city}`")
        if scenario and scenario not in SCENARIO_CITY:
            raise ValueError(f"unknown scenario {scenario!r}; one of: {_SCENARIOS}")
        if scenario and SCENARIO_CITY[scenario] != city:
            raise ValueError(f"scenario {scenario} is set in {SCENARIO_CITY[scenario]}, not {city}")
        run_modes = modes_for(ghost, mode == "both") if not mode else _modes_from(mode)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    total_ticks = ticks or (round(minutes * 60) if minutes > 0 else None)

    try:
        world = load_world(city)
    except WorldError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    start = datetime.now(UTC).replace(microsecond=0)
    regions: list[HazardRegion] = []
    if scenario:
        regions.extend(
            scenario_regions(
                scenario, valid_from=start, valid_until=start.replace(year=start.year + 1)
            )
        )
    for path in hazard or []:
        regions.extend(_regions_from_file(path, start))

    console.print(
        f"[bold]sim[/bold] {city} seed={seed} vehicles={vehicles} fleets={fleets} "
        f"modes={'+'.join(m.value for m in run_modes)} "
        f"speed={'unpaced' if speed == UNPACED else f'{speed:g}x'} "
        f"zones={zones}{' any-vertex' if any_vertex_destinations else ''}"
    )
    if speed != UNPACED and cfg.telemetry_stride * cfg.telemetry_hz != speed:
        console.print(
            f"[dim]telemetry every {cfg.telemetry_stride} ticks "
            f"(~{speed / cfg.telemetry_stride:.1f} Hz wall)[/dim]"
        )
    if regions:
        console.print(f"[dim]ground truth: {len(regions)} hazard area(s)[/dim]")
    else:
        console.print("[yellow]no ground truth: nothing for a vehicle to stall on[/yellow]")

    bus = None
    if publish:
        bus = Bus(url_for(Role.AGENT), namespace=namespace, writer=None)

    try:
        net = DriveNetwork.build(world, cfg, anchors=[region.geometry for region in regions])
    except NetworkError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    field = ScriptedHazardField.from_regions(regions, net) if regions else EmptyHazardField()
    runner = SimRunner(
        world,
        cfg,
        modes=run_modes,
        bus=bus,
        hazards=field,
        start=start,
        net=net,
    )

    async def go():
        """Run and tear down inside one event loop.

        The bus holds a connection created on this loop, so closing it from a
        second `asyncio.run` raises `RuntimeError: Event loop is closed` after
        an otherwise perfectly good run.
        """
        try:
            return await runner.run(ticks=total_ticks)
        finally:
            if bus is not None:
                await bus.close()

    try:
        report = asyncio.run(go())
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(0) from None
    except Exception as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    _report(report, cfg)


def _modes_from(name: str) -> tuple[SimMode, ...]:
    if name == "both":
        return (SimMode.LIVE, SimMode.GHOST)
    if name in {"live", "ghost"}:
        return (SimMode(name),)
    raise ValueError(f"--mode must be live, ghost or both, got {name!r}")


def _regions_from_file(path: str, start: datetime) -> list[HazardRegion]:
    """A hazard area from a GeoJSON file, or a scenario YAML's own block."""
    target = Path(path)
    if target.suffix.lower() in {".yaml", ".yml"}:
        return list(load_regions(target))
    document = json.loads(target.read_text(encoding="utf-8"))
    geometry = document.get("geometry", document)
    properties = document.get("properties", {}) or {}
    return [
        HazardRegion(
            id=str(properties.get("id") or target.stem),
            hazard_class=HazardClass(properties.get("class", HazardClass.FLOOD.value)),
            geometry=geometry,
            valid_from=start,
            valid_until=start.replace(year=start.year + 1),
            severity=int(properties.get("severity", 4)),
        )
    ]


def _report(report: object, cfg: SimConfig) -> None:
    """Per-operator counters, the achieved speed factor, and the hash."""
    counters_by_mode = report.counters_by_mode  # type: ignore[attr-defined]
    for mode, counters in counters_by_mode.items():
        table = Table(title=f"{mode} fleet")
        table.add_column("operator")
        table.add_column("hazard entries", justify="right")
        table.add_column("assists", justify="right")
        table.add_column("rides", justify="right")
        table.add_column("blocked corridor min", justify="right")
        for op in cfg.personas:
            minutes = report.blocked_minutes_by_mode[mode].get(op, 0.0)  # type: ignore[attr-defined]
            table.add_row(
                op,
                str(counters.get(f"hazard_entries_{op}", 0)),
                str(counters.get(f"assist_opened_{op}", 0)),
                str(counters.get(f"rides_completed_{op}", 0)),
                f"{minutes:.1f}",
            )
        console.print(table)

    ticks = report.ticks  # type: ignore[attr-defined]
    wall = report.wall_span_s  # type: ignore[attr-defined]
    speedup = report.speedup  # type: ignore[attr-defined]
    console.print(f"{ticks} ticks in {wall:.1f} s wall = [bold]{speedup:.1f}x[/bold] real time")
    for mode, digest in report.hashes_by_mode.items():  # type: ignore[attr-defined]
        console.print(f"[dim]{mode} trajectory hash {digest}[/dim]")


def register(app: typer.Typer) -> None:
    """Add `hu sim` to the top-level app."""
    app.command("sim")(sim)
