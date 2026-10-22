"""Telemetry: the cadence, the counter keys, and the viewer's field names."""

from __future__ import annotations

from fixtures.sim import grid_net, no_hazards, scripted_hazards, tiny_config
from headsup.contracts import Telemetry, VehicleStatus
from headsup.sim import counters as C
from headsup.sim.config import SimConfig, SimMode
from headsup.sim.engine import Simulation
from samples import T0


def _sim(cfg: SimConfig | None = None, *, mode=SimMode.LIVE, hazards=None) -> Simulation:
    cfg = cfg or tiny_config()
    net = grid_net()
    return Simulation(net, cfg, mode=mode, hazards=hazards or no_hazards(), start=T0)


def test_two_hertz_wall_clock_at_ten_times_speed_is_every_fifth_tick() -> None:
    """A 1-second tick cannot emit 2 Hz of *sim* time.

    The viewer wants 2-5 Hz of real time, so the stride is derived from
    `--speed`. At 10x that is every fifth tick.
    """
    assert tiny_config(speed=10.0, telemetry_hz=2.0).telemetry_stride == 5
    assert tiny_config(speed=60.0, telemetry_hz=2.0).telemetry_stride == 30
    assert tiny_config(speed=1.0, telemetry_hz=2.0).telemetry_stride == 1
    assert tiny_config(speed=4.0, telemetry_hz=4.0).telemetry_stride == 1


def test_an_unpaced_run_still_publishes_on_a_sane_stride() -> None:
    """`--speed 0` has no wall-clock rate to track, so it falls back to 1x."""
    assert tiny_config(speed=0.0, telemetry_hz=2.0).telemetry_stride == 1


def test_frames_arrive_exactly_on_the_stride() -> None:
    cfg = tiny_config(speed=10.0, telemetry_hz=2.0)
    sim = _sim(cfg)
    ticks_with_frames = [
        result.tick for _ in range(40) if (result := sim.step()).telemetry is not None
    ]
    assert ticks_with_frames == [5, 10, 15, 20, 25, 30, 35, 40]


def test_a_frame_carries_every_vehicle_in_the_viewers_field_names() -> None:
    cfg = tiny_config()
    sim = _sim(cfg)
    frame = None
    while frame is None:
        frame = sim.step().telemetry
    assert isinstance(frame, Telemetry)
    assert frame.type == "telemetry"
    assert len(frame.vehicles) == cfg.vehicles

    wire = frame.to_wire()
    assert wire["type"] == "telemetry"
    first = wire["vehicles"][0]
    # The contract `viz/mock_stream.py` already serves.
    for field in ("id", "op", "lon", "lat", "heading"):
        assert field in first


def test_positions_stay_inside_the_city_and_headings_stay_a_bearing() -> None:
    sim = _sim()
    net = grid_net()
    minx, miny, maxx, maxy = net.world.graph.edges[0].geometry.bounds
    for edge in net.world.graph.edges:
        ex0, ey0, ex1, ey1 = edge.geometry.bounds
        minx, miny = min(minx, ex0), min(miny, ey0)
        maxx, maxy = max(maxx, ex1), max(maxy, ey1)

    for _ in range(60):
        frame = sim.step().telemetry
        if frame is None:
            continue
        for state in frame.vehicles:
            assert minx - 1e-6 <= state.lon <= maxx + 1e-6
            assert miny - 1e-6 <= state.lat <= maxy + 1e-6
            assert 0.0 <= state.heading < 360.0
            assert 0.0 <= state.speed_mph <= 120.0


def test_a_held_vehicle_reports_no_speed() -> None:
    net = grid_net()
    sim = _sim(tiny_config(stall_to_assist_s=3), hazards=scripted_hazards(net))
    seen_held = False
    for _ in range(200):
        frame = sim.step().telemetry
        if frame is None:
            continue
        for state in frame.vehicles:
            if state.status in (
                VehicleStatus.STOPPED,
                VehicleStatus.STALLED,
                VehicleStatus.AWAITING_ASSIST,
            ):
                assert state.speed_mph == 0.0
                seen_held = True
    assert seen_held, "the hazard should have held at least one vehicle"


def test_the_counters_a_frame_carries_are_the_documented_keys() -> None:
    sim = _sim()
    frame = None
    while frame is None:
        frame = sim.step().telemetry
    for key in C.expected_keys(("op_a", "op_b", "op_c")):
        assert key in frame.counters, key
    assert all(isinstance(value, int) for value in frame.counters.values())


def test_totals_equal_the_sum_of_their_parts() -> None:
    net = grid_net()
    sim = _sim(tiny_config(stall_to_assist_s=3), hazards=scripted_hazards(net))
    for _ in range(200):
        sim.step()
    counters = sim.counters()
    for metric in (C.HAZARD_ENTRIES, C.ASSIST_OPENED, C.RIDES_COMPLETED, C.BLOCKED_SECONDS):
        parts = sum(counters[C.key(metric, op)] for op in ("op_a", "op_b", "op_c"))
        assert counters[C.key(metric)] == parts


def test_a_ghost_frame_is_flagged_and_counts_as_models_built() -> None:
    sim = _sim(mode=SimMode.GHOST)
    result = None
    while result is None or result.telemetry is None:
        result = sim.step()
    assert result.telemetry.ghost is True
    assert result.models_built == len(result.telemetry.vehicles) + 1


def test_no_models_are_built_on_a_tick_without_a_frame() -> None:
    """Pydantic construction is the dominant telemetry cost; it must be rare."""
    cfg = tiny_config(speed=10.0, telemetry_hz=2.0)
    sim = _sim(cfg)
    for _ in range(20):
        result = sim.step()
        if result.telemetry is None:
            assert result.models_built == 0
