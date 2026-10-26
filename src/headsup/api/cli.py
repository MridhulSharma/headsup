"""`hu serve` and `hu demo`: the API and the agents as one long-lived process.

`hu serve` is the process the viewer talks to. It is one process rather than a
server plus a separate `hu agents` for a reason the launcher forces: starting a
scenario means resetting the streams and the simulator, and whatever owns the
socket has to own those too. So the app's own lifespan brings the agents up
when a scenario starts and drops them when it stops.

`hu demo s1`, `s2` and `s3` are `hu serve` with that scenario started as soon
as the server is up, which is all the demo ever was: one command, the viewer
pointed at `ws://localhost:8765/viewer`, and the scenario running.

* **s1** is the San Antonio flood: a real NWS and USGS recording, a hazard
  narrowed to the reach of the gauges that corroborated it, and one NO_ENTRY.
* **s2** is the downtown Austin signal blackout: a generated feed on the
  city's own signal map, and three advisories for one hazard -- the junctions
  become all-way stops, with a density cap and a speed cap beside it.
* **s3** is the downtown Austin emergency scene: a real twelve-hour window of
  the city's incident board with one scripted scene inside it, free dispatch
  text read into a fixed schema, and one KEEP_CLEAR along the route the next
  engine takes to it.

It also brings up the stand-in operator in `api.second_key`, because S1's
narrowed flood hazard is a severity 4 and will not publish without a human
approval token. The stand-in issues one the way a person would, through
`POST /approvals/{proposal_id}`, and signs it `demo_operator` so no audit
record of a demo claims a person reviewed the hazard. `--no-approve` leaves
the second key unheld and the gate refusing, which is the other half of the
demo. `hu serve` never runs it.

S2 asks nothing of the second key and that is the scenario, not a shortcut.
The signal sentinel publishes its cluster at severity 3; fusion then caps it
at 2, because the Austin signal feed is not in `AUTHORITATIVE_SOURCES` and is
the only source the cluster has. Either figure is under the severity 4 that
`playbooks/signal_outage.yaml` wants a token for, and 0.2 km2 is well under
its 3 km2. The stand-in is still started and simply never has anything to
approve. S3 asks nothing of it either, for the same reason: the incident feed
is not authoritative, so a single-source scene is capped at 2 as well, and a
KEEP_CLEAR corridor of about 0.1 km2 is far under
`playbooks/emergency_scene.yaml` two-key area of 0.5 km2.

**The ghost fleet runs beside the live one.** Same seed, same roads, the
advice ignored: it is the baseline the demo's claim is measured against. Both
fleets are stepped by one tick loop in one `SimRunner`, so they cannot drift
apart, and the ghost's telemetry reaches the viewer as `ghost_telemetry`
frames beside the live `telemetry` ones. The `counters` frame carries both
sets side by side, with a `headline` of the four figures the panel shows:
hazard entries, assist queue depth, blocked corridor minutes and completed
rides. `--no-ghost` runs the advised fleet alone, which is cheaper per tick
and shows nothing to compare it to.

Nothing here reads the streams or publishes to them. It builds the app, hands
it to uvicorn, and prints the two URLs a person needs.
"""

from __future__ import annotations

import logging

import typer
import uvicorn
from rich.console import Console

from headsup.api.app import ApiState, create_app, default_host, default_port
from headsup.api.catalog import CatalogError, ScenarioCatalog
from headsup.api.runner import StartOptions
from headsup.api.second_key import DEMO_APPROVER
from headsup.llm import mode_from_env

console = Console()
err = Console(stderr=True)

#: `hu demo s1` rather than `hu demo s1_flood`: the demo names are short. Any
#: scenario id in `scenarios/` also works, so this is a convenience rather
#: than the list of what can be run.
DEMO_ALIASES: dict[str, str] = {
    "s1": "s1_flood",
    "s2": "s2_blackout",
    "s3": "s3_incident",
}

_ALIASES = ", ".join(DEMO_ALIASES)


def _resolve(name: str, catalog: ScenarioCatalog) -> str:
    """A demo name or a scenario id, as a scenario id that exists."""
    scenario_id = DEMO_ALIASES.get(name.strip().lower(), name.strip())
    return catalog.card(scenario_id).id


def _options(speed: float, vehicles: int, seed: int, llm: bool, ghost: bool) -> StartOptions:
    return StartOptions(speed=speed or None, vehicles=vehicles, seed=seed, llm=llm, ghost=ghost)


def _serve(
    *,
    host: str,
    port: int,
    scenario: str | None,
    options: StartOptions,
    namespace: str,
    log_level: str,
    second_key: bool = False,
) -> None:
    """Run the server, with one scenario started once it is up."""
    state = ApiState(namespace=namespace)
    if scenario is not None:
        state.autostart = (scenario, options)
    if second_key:
        state.second_key_url = f"http://{host}:{port}"
    app = create_app(state)
    console.print(
        f"[bold]hu serve[/bold] http://{host}:{port}  "
        f"viewer ws://{host}:{port}/viewer  llm={mode_from_env().value}"
        + (f"  namespace={namespace}" if namespace else "")
        + (f"  scenario={scenario}" if scenario else "")
        + (f"  fleets={'live+ghost' if options.ghost else 'live'}" if scenario else "")
    )
    if second_key:
        console.print(
            f"[yellow]second key held by {DEMO_APPROVER}[/yellow]: two-key advisories are "
            "approved through POST /approvals/{proposal_id} by a stand-in, not by a person. "
            "Pass --no-approve to watch the gate refuse them instead."
        )
    try:
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("[yellow]interrupted[/yellow]")


def serve(
    host: str = typer.Option("", "--host", help="Interface to bind. Default 127.0.0.1."),
    port: int = typer.Option(0, "--port", help="Port to bind. Default 8765."),
    scenario: str = typer.Option("", "--scenario", help="Start this scenario once up."),
    speed: float = typer.Option(
        0.0, "--speed", help="Replay and fleet speed; 0 uses the scenario's."
    ),
    vehicles: int = typer.Option(300, "--vehicles", help="Vehicles across all fleets."),
    seed: int = typer.Option(42, "--seed", help="Run seed; the same seed replays exactly."),
    namespace: str = typer.Option("", "--namespace", help="Prefix every stream name."),
    llm: bool = typer.Option(True, "--llm/--no-llm", help="Let the agents ask a model."),
    ghost: bool = typer.Option(
        True,
        "--ghost/--no-ghost",
        help="Run the unadvised baseline fleet beside the advised one, on the same seed.",
    ),
    approve: bool = typer.Option(
        False,
        "--approve/--no-approve",
        help=(
            "Have a stand-in operator approve two-key advisories over the approvals "
            "API. Off by default: a severity-4 advisory is meant to wait for a person, "
            "and on this command the person is whoever is driving the viewer."
        ),
    ),
    log_level: str = typer.Option("warning", "--log-level", help="uvicorn log level."),
) -> None:
    """Run the API and the agents on one port, for the viewer to drive."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # The stand-in operator polls the approvals endpoint, and httpx logs every
    # request at INFO: left on, that one client buries the pipeline's own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    chosen: str | None = None
    try:
        if scenario:
            chosen = _resolve(scenario, ScenarioCatalog())
        options = _options(speed, vehicles, seed, llm, ghost)
    except (CatalogError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _serve(
        host=host or default_host(),
        port=port or default_port(),
        scenario=chosen,
        options=options,
        namespace=namespace,
        log_level=log_level,
        second_key=approve,
    )


def demo(
    scenario: str = typer.Argument("s1", help=f"One of: {_ALIASES}, or a scenario id."),
    host: str = typer.Option("", "--host", help="Interface to bind. Default 127.0.0.1."),
    port: int = typer.Option(0, "--port", help="Port to bind. Default 8765."),
    speed: float = typer.Option(
        0.0, "--speed", help="Replay and fleet speed; 0 uses the scenario's."
    ),
    vehicles: int = typer.Option(300, "--vehicles", help="Vehicles across all fleets."),
    seed: int = typer.Option(42, "--seed", help="Run seed; the same seed replays exactly."),
    llm: bool = typer.Option(True, "--llm/--no-llm", help="Let the agents ask a model."),
    ghost: bool = typer.Option(
        True,
        "--ghost/--no-ghost",
        help="Run the unadvised baseline fleet beside the advised one, on the same seed.",
    ),
    approve: bool = typer.Option(
        True,
        "--approve/--no-approve",
        help="Have a stand-in operator approve two-key advisories over the approvals API.",
    ),
    log_level: str = typer.Option("warning", "--log-level", help="uvicorn log level."),
) -> None:
    """Run `hu serve` with one scenario started: the whole demo, one command."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # The stand-in operator polls the approvals endpoint, and httpx logs every
    # request at INFO: left on, that one client buries the pipeline's own lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        chosen = _resolve(scenario, ScenarioCatalog())
        options = _options(speed, vehicles, seed, llm, ghost)
    except (CatalogError, ValueError) as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _serve(
        host=host or default_host(),
        port=port or default_port(),
        scenario=chosen,
        options=options,
        namespace="",
        log_level=log_level,
        second_key=approve,
    )


def register(app: typer.Typer) -> None:
    """Add `hu serve` and `hu demo` to the top-level app."""
    app.command("serve")(serve)
    app.command("demo")(demo)
