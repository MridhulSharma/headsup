"""`hu world build | info | pois | edges`.

`build` is the one command that goes to the network: it downloads a city once
and leaves a cache every other command and every agent reads. The other three
are for looking at what it built, and `edges` is how a person checks that a
polygon covers the roads they think it covers before an advisory is written
against it.

    uv run hu world build --city austin
    uv run hu world info --city austin
    uv run hu world edges --city austin --bbox -97.745,30.267,-97.741,30.270
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from headsup.world import raw
from headsup.world.bbox import BOXES
from headsup.world.bbox import box as lookup_box
from headsup.world.db import DbError, table_counts, write_world
from headsup.world.graph import WorldError
from headsup.world.model import World, is_built, world_cache_dir
from headsup.world.pois import PoiKind, counts

world_app = typer.Typer(
    help="OSM road graph, H3 edge index, POIs in PostGIS.",
    no_args_is_help=True,
)

console = Console()
err = Console(stderr=True)

_CITY_KEYS = ", ".join(sorted(BOXES))

#: `--city all` builds every city in one go, which is what the step's
#: Done-when check times.
ALL = "all"


def _cache_dir(cache: str | None) -> Path:
    """`--cache` if it was given, else whatever the environment says."""
    return Path(cache) if cache else world_cache_dir()


def _cities(city: str) -> list[str]:
    if city == ALL:
        return sorted(BOXES)
    try:
        lookup_box(city)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    return [city]


def _load(city: str, cache_dir: Path) -> World:
    try:
        return World.load(city, cache_dir=cache_dir)
    except (WorldError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _poi_table(world: World) -> Table:
    table = Table(title=f"{world.city} POIs")
    table.add_column("kind")
    table.add_column("count", justify="right")
    table.add_column("critical", justify="center")
    tally = counts(world.pois)
    for kind in PoiKind:
        mark = "[yellow]yes[/yellow]" if kind in {p.kind for p in world.critical_pois} else ""
        table.add_row(kind.value, str(tally[kind]), mark)
    return table


@world_app.command("build")
def world_build(
    city: str = typer.Option("austin", "--city", help=f"One of: {_CITY_KEYS}, or {ALL}."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-download even when a graphml cache exists."
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help=(
            "Rebuild from the raw Overpass responses saved under "
            "data/cache/<city>/raw/ instead of the network. Needs one "
            "networked build of that city first."
        ),
    ),
    db: bool = typer.Option(True, "--db/--no-db", help="Also load roads and POIs into PostGIS."),
    cache: str | None = typer.Option(
        None, "--cache", help="Cache directory (default: HU_WORLD_CACHE, else data/cache)."
    ),
) -> None:
    """Download, simplify, index and cache a city's road graph and POIs."""
    from headsup.world import osm

    cache_dir = _cache_dir(cache)
    total = 0.0
    for key in _cities(city):
        how = "rebuilding offline" if offline else "building"
        console.print(f"[bold]{how} {key}[/bold] {lookup_box(key)}")
        try:
            report = osm.build(key, cache_dir=cache_dir, refresh=refresh, offline=offline)
        except WorldError as exc:
            err.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        total += report.total_s
        console.print(str(report))
        console.print(f"  [dim]{report.graph_file.as_posix()}[/dim]")
        console.print(f"  [dim]{report.pois_file.as_posix()}[/dim]")
        if db:
            try:
                written = write_world(report.world)
            except DbError as exc:
                err.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
            console.print(f"[green]{written!s}[/green]")
        console.print(_poi_table(report.world))
    how = "rebuilt offline" if offline else "built"
    console.print(f"[green]{how} in {total:.1f}s[/green]")


@world_app.command("info")
def world_info(
    city: str = typer.Option("austin", "--city", help=f"One of: {_CITY_KEYS}, or {ALL}."),
    cache: str | None = typer.Option(
        None, "--cache", help="Cache directory (default: HU_WORLD_CACHE, else data/cache)."
    ),
    db: bool = typer.Option(False, "--db", help="Also count the rows in PostGIS."),
) -> None:
    """Show what is cached for a city: graph size, index size, POIs."""
    cache_dir = _cache_dir(cache)
    for key in _cities(city):
        if not is_built(key, cache_dir):
            err.print(f"[yellow]{key} is not built - run `hu world build --city {key}`[/yellow]")
            raise typer.Exit(1)
        world = _load(key, cache_dir)
        console.print(f"[bold]{key}[/bold] {lookup_box(key)}")
        console.print(world.summary())
        console.print(world.checker().describes_itself())
        stored = raw.archive(key, cache_dir)
        colour = "green" if stored.complete else "yellow"
        console.print(f"[{colour}]{stored}[/{colour}]")
        if not stored.complete:
            console.print(
                "[yellow]  no offline rebuild until this city is built with "
                "network access once[/yellow]"
            )
        if db:
            try:
                rows = table_counts(key)
            except DbError as exc:
                err.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
            console.print(f"postgis: {rows}")


@world_app.command("pois")
def world_pois(
    city: str = typer.Option("austin", "--city", help=f"One of: {_CITY_KEYS}."),
    kind: str | None = typer.Option(None, "--kind", help="Only this PoiKind."),
    critical: bool = typer.Option(False, "--critical", help="Only the critical kinds."),
    limit: int = typer.Option(20, "--limit", help="Rows to print; 0 for all."),
    cache: str | None = typer.Option(
        None, "--cache", help="Cache directory (default: HU_WORLD_CACHE, else data/cache)."
    ),
) -> None:
    """List a city's POIs and how many ways there are into each one."""
    world = _load(_cities(city)[0], _cache_dir(cache))
    selected = world.critical_pois if critical else world.pois
    if kind:
        try:
            wanted = PoiKind(kind.upper())
        except ValueError as exc:
            kinds = ", ".join(item.value for item in PoiKind)
            err.print(f"[red]unknown kind {kind!r}; one of: {kinds}[/red]")
            raise typer.Exit(2) from exc
        selected = tuple(poi for poi in selected if poi.kind is wanted)

    table = Table(title=f"{world.city}: {len(selected)} POIs")
    table.add_column("id")
    table.add_column("kind")
    table.add_column("name", overflow="fold")
    table.add_column("access edges", justify="right")
    shown = selected if limit <= 0 else selected[:limit]
    for poi in shown:
        table.add_row(
            poi.id, poi.kind.value, poi.name or "[dim]unnamed[/dim]", str(len(poi.access_edge_ids))
        )
    console.print(table)
    if len(shown) < len(selected):
        console.print(f"[dim]{len(selected) - len(shown)} more; --limit 0 for all[/dim]")


def _geometry_from(bbox: str | None, file: str | None) -> Any:
    """The query shape, from a `w,s,e,n` bbox or a GeoJSON file."""
    if bbox and file:
        raise typer.BadParameter("pass --bbox or --file, not both")
    if bbox:
        parts = bbox.split(",")
        if len(parts) != 4:
            raise typer.BadParameter(f"--bbox wants west,south,east,north, got {bbox!r}")
        try:
            west, south, east, north = (float(part) for part in parts)
        except ValueError as exc:
            raise typer.BadParameter(f"--bbox {bbox!r} is not four numbers") from exc
        from headsup.world.bbox import BBox

        return BBox(west=west, south=south, east=east, north=north).to_shapely()
    if file:
        try:
            payload = json.loads(Path(file).read_text(encoding="utf-8"))
        except OSError as exc:
            raise typer.BadParameter(f"cannot read {file}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"{file} is not JSON: {exc}") from exc
        # A bare geometry, a Feature, or an Advisory: all carry `geometry`.
        if isinstance(payload, dict) and "coordinates" not in payload:
            payload = payload.get("geometry", payload)
        return payload
    raise typer.BadParameter("pass --bbox west,south,east,north or --file geometry.json")


@world_app.command("edges")
def world_edges(
    city: str = typer.Option("austin", "--city", help=f"One of: {_CITY_KEYS}."),
    bbox: str | None = typer.Option(None, "--bbox", help="west,south,east,north."),
    file: str | None = typer.Option(None, "--file", help="GeoJSON geometry, Feature or advisory."),
    limit: int = typer.Option(10, "--limit", help="Rows to print; 0 for all."),
    cache: str | None = typer.Option(
        None, "--cache", help="Cache directory (default: HU_WORLD_CACHE, else data/cache)."
    ),
) -> None:
    """Which edges a polygon covers: the H3 pass, then the exact one."""
    world = _load(_cities(city)[0], _cache_dir(cache))
    shape = _geometry_from(bbox, file)
    from headsup.world.graph import as_shapely

    geometry = as_shapely(shape)
    candidates = world.graph.index.candidates(geometry)
    edges = world.graph.edges_in_polygon(geometry)
    console.print(
        f"[bold]{world.city}[/bold] {geometry.geom_type}: "
        f"{len(candidates)} h3 candidates -> [green]{len(edges)} edges[/green]"
    )
    table = Table(title="edges in polygon")
    table.add_column("edge", justify="right")
    table.add_column("name", overflow="fold")
    table.add_column("highway")
    table.add_column("m", justify="right")
    table.add_column("free-flow s", justify="right")
    shown = edges if limit <= 0 else edges[:limit]
    for edge in shown:
        table.add_row(
            str(edge.id),
            edge.name or "[dim]unnamed[/dim]",
            edge.highway,
            f"{edge.length_m:.0f}",
            f"{edge.free_flow_s:.1f}",
        )
    console.print(table)
    if len(shown) < len(edges):
        console.print(f"[dim]{len(edges) - len(shown)} more; --limit 0 for all[/dim]")
