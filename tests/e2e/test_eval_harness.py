"""The harness end to end: two one-minute runs, two logs, every metric computed.

Needs the Docker services (`uv run hu infra up`) and a built Austin
(`uv run hu world build --city austin`). The model is mocked, so nothing is
spent: what is checked is the harness, not the judgment.

This is D4.1's "Done when". Two runs of S3 -- the baseline and the full system
-- on the same seed, each sealing its own hash-chained audit log, and then the
metrics computed by reading those logs back:

1. **every metric is computed** on a sixty-second mock run. Not plausible, not
   non-zero -- computed: a figure the brief asks for that comes back `None`
   from a real run means the harness cannot measure it, whatever the unit
   tests say about synthetic logs.
2. **V0 drives into more hazards than V4.** The claim the whole prototype
   rests on, measured off two logs rather than asserted.

S3 is the scenario because it is the one whose hazard physically blocks roads
and asks nothing of the second key: an emergency scene closes the junction at
6th and Guadalupe twenty simulated minutes in, so an unadvised fleet has
something to drive into and an advised one has something to be told about.
"""

from __future__ import annotations

import pytest
import redis.exceptions

from headsup.bus import Bus
from headsup.eval.metrics import RunMetrics, measure
from headsup.eval.runs import RunPlan, run_one
from headsup.eval.variants import Variant
from headsup.world.model import is_built

pytestmark = pytest.mark.e2e

SCENARIO = "s3_incident"
CITY = "austin"
REDIS_URL = "redis://localhost:6379/0"
INFRA_HINT = "run `uv run hu infra up` first"

#: The Done-when's own budget: one wall-clock minute per run. At S3's 60x that
#: is sixty simulated minutes, so the scene -- reported twenty minutes in and
#: archived at seventy-five -- is in force for most of the run.
MINUTES = 1.0

#: Enough vehicles that some of them meet one downtown junction. The same 300
#: `hu demo` runs, and for the same reason: sixty vehicles over the whole
#: Austin graph almost never reach one block.
VEHICLES = 300

SEED = 42

#: The figures the brief names. A run of a real scenario has to produce a
#: number for each of them, or the harness cannot measure what it claims to.
REQUIRED: tuple[str, ...] = (
    "hazard_entries",
    "time_to_advisory_s",
    "assist_peak",
    "assist_p95_wait_s",
    "blocked_corridor_minutes",
    "completed_ride_rate_per_vehicle_hour",
    "extra_minutes_per_rerouted_trip",
    "false_positive_area_hours",
    "llm_usd_per_sim_hour",
    "p95_agent_latency_ms",
)


async def _redis_or_fail() -> None:
    bus = Bus(REDIS_URL)
    try:
        await bus.ping()
    except (redis.exceptions.RedisError, OSError) as exc:
        pytest.fail(f"{INFRA_HINT}: {REDIS_URL} ({exc})")
    finally:
        await bus.close()


def _plan(variant: Variant) -> RunPlan:
    return RunPlan(
        scenario=SCENARIO,
        variant=variant,
        seed=SEED,
        minutes=MINUTES,
        vehicles=VEHICLES,
    )


@pytest.fixture(scope="module")
def audit_dir(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A log directory of this test's own, so a run here is not a demo's.

    The chains are the measurement, and writing them beside the real ones in
    `data/audit/` would leave a test's runs in `hu eval --table-only`.
    """
    return str(tmp_path_factory.mktemp("eval_audit"))


@pytest.fixture(scope="module")
def runs(audit_dir: str) -> dict[str, RunMetrics]:
    """One V0 run and one V4 run of S3, measured off their own logs.

    Module-scoped because the two runs are the expensive part: they load the
    Austin graph, build the drive network and replay at 60x, and every
    assertion below reads the same two rows. Sequential, as every eval run is
    -- two scenarios sharing one Redis would delete each other's streams.
    """
    if not is_built(CITY):
        pytest.fail(f"{CITY} is not built - run `uv run hu world build --city {CITY}`")
    import asyncio

    async def both() -> dict[str, RunMetrics]:
        await _redis_or_fail()
        out: dict[str, RunMetrics] = {}
        for variant in (Variant.V0, Variant.V4):
            result = await run_one(_plan(variant), audit_dir=audit_dir)
            assert result.ok, f"{variant.value} did not run: {result.error}"
            out[variant.value] = measure(result.context)
        return out

    return asyncio.run(both())


# --- the Done-when ----------------------------------------------------------


def test_every_metric_is_computed_on_a_sixty_second_mock_run(
    runs: dict[str, RunMetrics],
) -> None:
    full = runs[Variant.V4.value]
    missing = [field for field in REQUIRED if getattr(full, field) is None]
    assert not missing, f"not computed from the log: {', '.join(missing)}"


def test_the_baseline_drives_into_more_hazards_than_the_full_system(
    runs: dict[str, RunMetrics],
) -> None:
    baseline, full = runs[Variant.V0.value], runs[Variant.V4.value]
    assert baseline.hazard_entries > full.hazard_entries, (
        f"V0 {baseline.hazard_entries} vs V4 {full.hazard_entries}; "
        f"V4 published {full.advisories} advisory(ies)"
    )


# --- that the figures rest on something ------------------------------------


def test_each_run_sealed_its_own_verified_chain(runs: dict[str, RunMetrics]) -> None:
    for name, row in runs.items():
        assert row.chain_ok, f"{name}: {row.chain}"
        assert row.entries > 0, f"{name} recorded nothing"
        assert row.run_id.endswith(f"{name}_s{SEED}")


def test_the_baseline_read_no_feeds_and_published_nothing(
    runs: dict[str, RunMetrics],
) -> None:
    """V0 is the fleet alone: the replay fills the streams and nobody reads them."""
    baseline = runs[Variant.V0.value]
    assert baseline.fleet == "ghost"
    assert baseline.candidates == 0
    assert baseline.hazards == 0
    assert baseline.advisories == 0
    assert baseline.llm_calls == 0


def test_the_full_system_ran_the_pipeline_and_published(runs: dict[str, RunMetrics]) -> None:
    full = runs[Variant.V4.value]
    assert full.fleet == "live"
    assert full.candidates > 0
    assert full.hazards > 0
    assert full.proposals > 0
    assert full.advisories > 0
    assert full.compile_published > 0


def test_the_full_run_carries_its_own_baseline_fleet(runs: dict[str, RunMetrics]) -> None:
    """Both fleets on one clock, which is what the extra-minutes figure needs."""
    full = runs[Variant.V4.value]
    assert full.ghost_hazard_entries is not None
    assert full.ghost_blocked_corridor_minutes is not None
    assert full.ghost_hazard_entries >= full.hazard_entries


def test_the_advisory_was_published_inside_the_time_to_advisory_budget(
    runs: dict[str, RunMetrics],
) -> None:
    """The same sixty-second budget `tests/e2e/test_s3.py` holds the run to."""
    full = runs[Variant.V4.value]
    assert full.time_to_advisory_s is not None
    assert full.time_to_advisory_s <= 60.0


def test_the_queue_curve_is_sampled_off_the_frames(runs: dict[str, RunMetrics]) -> None:
    """What the S2 chart draws, here on the run that produced it."""
    for row in runs.values():
        assert row.queue_curve, f"{row.variant} carried no queue curve"
        minutes = [point[0] for point in row.queue_curve]
        assert minutes == sorted(minutes)
