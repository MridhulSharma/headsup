"""The sweep: every variant x scenario x seed, one at a time, each with its own log.

    results = await sweep(plan, out=Path("results"))

One run is one `ScenarioRunner.start`, a wait, and a `stop`, with an `Auditor`
inside the run sealing every entry into `data/audit/<run_id>.jsonl`. Nothing
here measures anything: the metrics read the logs afterwards, which is the
whole point of writing them.

**Strictly sequential, in one process.** `CLAUDE.md` is explicit about why: a
replay paces itself against the wall clock, and a second `uv run` beside it can
reinstall the package under the running process and stall it -- a stalled replay
then fires every overdue event at once and the run's timing is worthless. Two
runs sharing one Redis would be worse again: the streams are cleared at both
ends of a run, so a second run would delete the first one's events mid-replay.
So the sweep is a loop, and a 147-run sweep takes as long as 147 runs take. The
city world is loaded once (`world.model.load_world` caches), which is the part
that would otherwise dominate.

**Why the run id says everything.** `eval_s3_incident_V4_s42` is the log's
filename, the chain's `run_id` and the table's key. A sweep can therefore be
re-measured, re-verified and re-charted from `data/audit/` alone, and a rerun
overwrites nothing: a second sweep at the same seeds appends to the same chains
rather than silently replacing them, which `AuditLog` resumes and `hu audit
verify` still checks end to end.

**The second key.** S1's narrowed flood hazard is a severity 4 and will not
publish without a human approval token, so a sweep with nobody at the keyboard
would measure the gate refusing rather than the pipeline running. A stand-in
issues one, in process, the way `api.second_key` does it over HTTP for the
demo: `approved_by` is `eval_operator`, never an operator id, so no audit
record of a sweep can be read as a person having reviewed a hazard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from headsup.agents.compiler.approvals import InMemoryApprovals, PendingCompiles
from headsup.api.catalog import CatalogError, ScenarioCard, ScenarioCatalog
from headsup.api.frames import RailIndex
from headsup.api.hub import ViewerHub
from headsup.api.operators import OperatorRegistry
from headsup.api.runner import DEFAULT_VEHICLES, ScenarioError, ScenarioRunner, StartOptions
from headsup.audit.chain import check_run_id
from headsup.bus import ALL_STREAMS, Bus
from headsup.eval.metrics import RunContext, RunMetrics, measure
from headsup.eval.variants import Variant, parse_seeds, seeds_for, spec_for

log = logging.getLogger(__name__)

#: Written into every approval the sweep issues. Not an operator id, for the
#: reason `api.second_key.DEMO_APPROVER` is not one.
EVAL_APPROVER = "eval_operator"
EVAL_NOTE = "issued by headsup.eval.runs for an ablation run; no person reviewed this hazard"

#: Seconds between looks at what the gate is waiting on.
APPROVAL_POLL_S = 0.5

#: Seconds between looks at whether the fleets have finished.
WATCH_POLL_S = 0.5

#: `StartOptions.minutes` is simulated minutes and is capped at 600. A run
#: asked for more than that is clamped and says so, rather than silently
#: running a tenth of the length the table claims.
MAX_SIM_MINUTES = 600.0

#: Grace on top of the simulated budget before a run is stopped anyway. A
#: replay paces itself against the wall clock and a loaded laptop makes it
#: late; a run that never reports `fleets_complete` must still end.
WATCH_GRACE_S = 30.0

#: Seed the sweep counts from when none is given.
BASE_SEED = 42


@dataclass(frozen=True, slots=True)
class RunPlan:
    """One run of the sweep: what to start, and how long to let it go."""

    scenario: str
    variant: Variant
    seed: int
    #: Wall-clock minutes this run is given.
    minutes: float
    speed: float = 0.0
    vehicles: int = DEFAULT_VEHICLES
    llm: bool = True

    @property
    def run_id(self) -> str:
        """The chain's filename, and the table's key for this run."""
        return check_run_id(f"eval_{self.scenario}_{self.variant.value}_s{self.seed}")

    def label(self) -> str:
        return f"{self.variant.value} {self.scenario} seed={self.seed}"


@dataclass(slots=True)
class SweepPlan:
    """Every run a sweep will do, in the order it will do them."""

    variants: tuple[Variant, ...]
    scenarios: tuple[str, ...]
    seeds: dict[str, int]
    minutes: float
    speed: float = 0.0
    vehicles: int = DEFAULT_VEHICLES
    llm: bool = True
    base_seed: int = BASE_SEED

    def runs(self) -> Iterator[RunPlan]:
        """Variant outermost, then scenario, then seed.

        Variant outermost so a sweep that is interrupted has whole variants
        done rather than a slice of each, and scenario next so one city's
        world is loaded, used and then left alone.
        """
        for variant in self.variants:
            spec = spec_for(variant)
            for scenario in self.scenarios:
                for seed in seeds_for(spec, self.seeds, base=self.base_seed):
                    yield RunPlan(
                        scenario=scenario,
                        variant=variant,
                        seed=seed,
                        minutes=self.minutes,
                        speed=self.speed,
                        vehicles=self.vehicles,
                        llm=self.llm,
                    )

    def total(self) -> int:
        return sum(1 for _ in self.runs())


@dataclass(slots=True)
class RunResult:
    """What one run produced: its context, its log, and how it went."""

    plan: RunPlan
    context: RunContext
    wall_s: float = 0.0
    entries: int = 0
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


def plan_from(
    *,
    variants: Sequence[Variant],
    scenarios: Sequence[str],
    seeds: str | dict[str, int],
    minutes: float,
    speed: float = 0.0,
    vehicles: int = DEFAULT_VEHICLES,
    llm: bool = True,
    base_seed: int = BASE_SEED,
) -> SweepPlan:
    """A sweep plan from the CLI's arguments, with the ids resolved."""
    catalog = ScenarioCatalog()
    resolved = tuple(resolve_scenario(name, catalog) for name in scenarios)
    counts = parse_seeds(seeds) if isinstance(seeds, str) else dict(seeds)
    return SweepPlan(
        variants=tuple(variants),
        scenarios=resolved,
        seeds=counts,
        minutes=float(minutes),
        speed=float(speed),
        vehicles=int(vehicles),
        llm=bool(llm),
        base_seed=int(base_seed),
    )


#: `s1` as well as `s1_flood`, the same shortening `hu demo` takes.
ALIASES: dict[str, str] = {"s1": "s1_flood", "s2": "s2_blackout", "s3": "s3_incident"}


def resolve_scenario(name: str, catalog: ScenarioCatalog | None = None) -> str:
    """A demo name or a scenario id, as a scenario id that exists."""
    books = catalog or ScenarioCatalog()
    wanted = ALIASES.get(name.strip().lower(), name.strip())
    return books.card(wanted).id


async def sweep(
    plan: SweepPlan,
    *,
    audit_dir: str | None = None,
    on_run: Any = None,
) -> list[RunResult]:
    """Run every combination in the plan, one at a time, newest log last.

    `on_run` is called with each `RunResult` as it finishes, which is how the
    CLI prints a line per run rather than waiting for the sweep.
    """
    catalog = ScenarioCatalog()
    results: list[RunResult] = []
    for item in plan.runs():
        result = await run_one(item, catalog=catalog, audit_dir=audit_dir)
        results.append(result)
        if on_run is not None:
            on_run(result)
    return results


async def run_one(
    item: RunPlan,
    *,
    catalog: ScenarioCatalog | None = None,
    audit_dir: str | None = None,
) -> RunResult:
    """One run, from a cleared bus to a closed chain. Never raises.

    A run that cannot start -- no recording, no world built, a bad speed -- is
    a result that says so. One missing world must not end a sweep that has a
    hundred runs of another city still to do.
    """
    books = catalog or ScenarioCatalog()
    context = RunContext(
        run_id=item.run_id,
        scenario=item.scenario,
        variant=item.variant,
        seed=item.seed,
        vehicles=item.vehicles,
        speed=item.speed,
        minutes=item.minutes,
        directory=Path(audit_dir) if audit_dir else None,
    )
    result = RunResult(plan=item, context=context)
    started = time.monotonic()
    try:
        card = books.card(item.scenario)
    except CatalogError as exc:
        result.error = str(exc)
        return _with_error(result, exc)

    bus = Bus.from_env()
    approvals = InMemoryApprovals()
    pending = PendingCompiles()
    runner = ScenarioRunner(
        bus,
        ViewerHub(),
        RailIndex(),
        OperatorRegistry(),
        approvals=approvals,
        pending=pending,
        spec=spec_for(item.variant),
        audit_run_id=item.run_id,
        audit_dir=audit_dir,
    )
    try:
        await runner.start(card, _options(item, card))
    except (ScenarioError, ValueError, RuntimeError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        await _shut(runner, bus)
        return _with_error(result, exc)

    key = asyncio.create_task(_hold_the_second_key(runner, approvals), name="second-key")
    try:
        await _watch(runner, item)
    finally:
        key.cancel()
        await asyncio.gather(key, return_exceptions=True)
        status = runner.status()
        result.notes = [str(note) for note in status.get("errors") or ()]
        await _shut(runner, bus)
    result.wall_s = round(time.monotonic() - started, 2)
    result.entries = _entries(item, audit_dir)
    return result


def _with_error(result: RunResult, exc: BaseException) -> RunResult:
    """A failed run as a row, with the reason on the context the metrics read."""
    log.warning("eval: %s failed: %s", result.plan.label(), exc)
    result.context = RunContext(
        **{**_context_fields(result.context), "error": result.error or str(exc)}
    )
    return result


def _context_fields(context: RunContext) -> dict[str, Any]:
    return {name: getattr(context, name) for name in RunContext.__dataclass_fields__}


def _options(item: RunPlan, card: ScenarioCard) -> StartOptions:
    """The run's start options, with wall minutes turned into simulated ones.

    `--minutes` is the wall-clock length of a run, which is the figure anyone
    planning a sweep is actually budgeting. The simulator counts ticks, and a
    tick is a simulated second, so a ten-minute run at the scenario's 60x is
    600 simulated minutes of fleet and 600 minutes of feed -- which is what
    makes S1's first warning, twenty feed-minutes in, land inside the window.
    """
    speed = item.speed or card.default_speed or 1.0
    simulated = min(item.minutes * speed, MAX_SIM_MINUTES)
    if simulated < item.minutes * speed:
        log.warning(
            "eval: %s asked for %.0f simulated minutes; clamped to %.0f",
            item.label(),
            item.minutes * speed,
            simulated,
        )
    return StartOptions(
        speed=speed,
        vehicles=item.vehicles,
        seed=item.seed,
        minutes=simulated,
        llm=item.llm,
    )


async def _watch(runner: ScenarioRunner, item: RunPlan) -> None:
    """Wait for the fleets to finish, or for the wall budget to run out."""
    deadline = time.monotonic() + item.minutes * 60.0 + WATCH_GRACE_S
    while time.monotonic() < deadline:
        status = runner.status()
        if status.get("fleets_complete"):
            return
        await asyncio.sleep(WATCH_POLL_S)
    log.info("eval: %s reached its wall budget; stopping", item.label())


async def _hold_the_second_key(runner: ScenarioRunner, approvals: InMemoryApprovals) -> None:
    """Issue a token for whatever the gate is waiting on, until cancelled.

    In process rather than over HTTP, because a sweep runs no server. It has no
    privileges either way: it can only see a proposal the gate itself recorded
    as refused for want of a key, the token is checked like any other, and the
    second trip through the gate is a full one -- a proposal that fails any
    other rule is refused again with its reasons.
    """
    granted: set[str] = set()
    while True:
        try:
            for waiting in runner.pending.waiting():
                proposal_id = waiting.proposal_id
                if proposal_id in granted:
                    continue
                granted.add(proposal_id)
                approvals.grant(proposal_id, EVAL_APPROVER, note=EVAL_NOTE)
                outcome = await runner.recompile(proposal_id)
                log.info("eval: second key on %s -> %s", proposal_id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a lost race with a stopping scenario
            log.debug("eval: second key: %s", exc)
        await asyncio.sleep(APPROVAL_POLL_S)


async def _shut(runner: ScenarioRunner, bus: Bus) -> None:
    """Stop the run, clear the streams, drop the connection.

    The streams are cleared so the next run's entry counts mean something,
    which `CLAUDE.md` asks for between timed runs and `ScenarioRunner.stop`
    already does; this is the belt for the case where the stop itself failed.
    """
    try:
        await runner.stop()
    except Exception as exc:  # pragma: no cover - a stop that fails is logged
        log.warning("eval: stop failed: %s", exc)
    try:
        await bus.delete_streams(ALL_STREAMS)
    except Exception as exc:  # pragma: no cover
        log.debug("eval: clearing streams failed: %s", exc)
    finally:
        await bus.close()


def _entries(item: RunPlan, audit_dir: str | None) -> int:
    """Lines in the run's chain, for the progress line."""
    from headsup.audit.chain import log_path, read_lines

    path = log_path(item.run_id, audit_dir)
    if not path.is_file():
        return 0
    try:
        return sum(1 for _ in read_lines(path))
    except Exception:  # pragma: no cover - the metrics report this properly
        return 0


def measured(results: Sequence[RunResult]) -> list[RunMetrics]:
    """Every finished run as a metrics row, read back off its own log."""
    return [measure(result.context) for result in results]
