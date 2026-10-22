"""The compiler service will not start with the critical-access rule unguarded.

`CompilerService` is the only thing in Heads Up that writes to the
`advisories` stream. A service that came up with the null POI checker would
publish closures without ever asking whether one of them seals off a
hospital - and it would say so only in an audit record nobody reads until
afterwards. So it refuses to start instead.

`hu compile` without a city stays runnable: it prints an advisory and writes
nothing, and its audit record carries the word "unguarded". The difference
between the two is the whole point - these tests pin it.

Nothing here needs Redis: the refusal happens before a connection is made.
"""

from __future__ import annotations

import shutil

import pytest

from fixtures.compiler import approve, clock, proposal
from fixtures.world import GRAPH_FILE, advisory_box, grid_world
from headsup.agents.compiler import Compiler, CompilerError, NullPoiChecker, StubPoiChecker
from headsup.agents.compiler.service import CompilerService, env_poi_checker, require_guarded
from headsup.world import WorldPoiChecker, graph_path, pois_path, save_pois
from headsup.world.model import clear_world_cache

#: Over the fixture hospital's whole block: every way in is closed.
SEALED = advisory_box(0.98, 0.98, 2.02, 2.02)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    """No HU_CITY, and a cache directory with nothing in it.

    Without the second half these tests would read whatever this machine has
    built into `data/cache/`, and "refuses an unbuilt city" would pass or fail
    depending on who ran `hu world build` last.
    """
    monkeypatch.delenv("HU_CITY", raising=False)
    monkeypatch.setenv("HU_WORLD_CACHE", str(tmp_path / "empty-cache"))
    clear_world_cache()
    yield
    clear_world_cache()


@pytest.fixture
def built(tmp_path, monkeypatch):
    """A built world for `austin`, in a cache directory of its own."""
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(GRAPH_FILE, graph_path("austin", cache))
    save_pois(pois_path("austin", cache), "austin", grid_world().pois)
    monkeypatch.setenv("HU_WORLD_CACHE", str(cache))
    return cache


# --- HU_CITY ---------------------------------------------------------------


def test_the_service_refuses_to_start_without_hu_city() -> None:
    with pytest.raises(CompilerError, match="HU_CITY is unset"):
        CompilerService()


def test_from_env_refuses_to_start_without_hu_city() -> None:
    """The environment-driven path an agent worker uses."""
    with pytest.raises(CompilerError, match="HU_CITY is unset"):
        CompilerService.from_env()


def test_the_refusal_says_what_to_set_and_what_to_run() -> None:
    with pytest.raises(CompilerError) as caught:
        CompilerService()
    message = str(caught.value)
    assert "HU_CITY=austin|san_antonio" in message
    assert "hu world build" in message


def test_an_empty_hu_city_is_as_good_as_unset(monkeypatch) -> None:
    monkeypatch.setenv("HU_CITY", "   ")
    with pytest.raises(CompilerError, match="HU_CITY is unset"):
        CompilerService()


# --- a city that is named but not built ------------------------------------


def test_the_service_refuses_a_city_with_no_road_graph(monkeypatch, tmp_path) -> None:
    """Named but unbuilt is still unguarded, and still a refusal."""
    monkeypatch.setenv("HU_CITY", "austin")
    monkeypatch.setenv("HU_WORLD_CACHE", str(tmp_path))
    with pytest.raises(CompilerError, match="will not start with no POI data for austin"):
        CompilerService()


def test_that_refusal_names_the_city_to_build(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HU_CITY", "san_antonio")
    monkeypatch.setenv("HU_WORLD_CACHE", str(tmp_path))
    with pytest.raises(CompilerError) as caught:
        CompilerService()
    assert "hu world build --city san_antonio" in str(caught.value)


def test_an_unknown_city_is_refused_too(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HU_CITY", "dallas")
    monkeypatch.setenv("HU_WORLD_CACHE", str(tmp_path))
    with pytest.raises(CompilerError, match="no POI data for dallas"):
        CompilerService()


# --- an injected compiler --------------------------------------------------


def test_an_injected_compiler_with_no_poi_data_is_refused() -> None:
    """`hu compile --publish` without a city comes through here."""
    with pytest.raises(CompilerError, match="critical-access rule would pass every closure"):
        CompilerService(compiler=Compiler(poi=NullPoiChecker()))


def test_an_injected_guarded_compiler_needs_no_hu_city() -> None:
    """A caller that already chose a checker has already chosen a city."""
    service = CompilerService(compiler=Compiler(poi=grid_world().checker()))
    assert isinstance(service.compiler.poi, WorldPoiChecker)


def test_a_stub_checker_counts_as_guarded() -> None:
    """The guard is about having POI data, not about where it came from."""
    service = CompilerService(compiler=Compiler(poi=StubPoiChecker()))
    assert isinstance(service.compiler.poi, StubPoiChecker)


def test_the_tests_that_exercise_the_bus_can_opt_out() -> None:
    """In writing, so an unguarded publisher is always a deliberate one."""
    service = CompilerService(allow_unguarded=True)
    assert isinstance(service.compiler.poi, NullPoiChecker)


# --- what starting gets you ------------------------------------------------


def test_a_built_city_starts_and_carries_its_graph(monkeypatch, built) -> None:
    monkeypatch.setenv("HU_CITY", "austin")
    service = CompilerService()
    assert isinstance(service.compiler.poi, WorldPoiChecker)
    assert "austin graph" in service.compiler.poi.describes_itself()


def test_the_service_that_starts_can_refuse_a_hospital_closure(monkeypatch, built) -> None:
    """The guard is worth having only if the rule then does something."""
    monkeypatch.setenv("HU_CITY", "austin")
    service = CompilerService()
    service.compiler = Compiler(clock=clock(), poi=service.compiler.poi)
    result = service.compiler.compile(proposal(geometry=SEALED), approve())
    assert not result.ok
    assert any("access edges closed" in reason for reason in result.reasons)


# --- the pieces on their own ------------------------------------------------


def test_env_poi_checker_raises_rather_than_returning_the_null_one() -> None:
    with pytest.raises(CompilerError, match="HU_CITY is unset"):
        env_poi_checker()


def test_env_poi_checker_returns_the_world_checker_for_a_built_city(monkeypatch, built) -> None:
    monkeypatch.setenv("HU_CITY", "austin")
    assert isinstance(env_poi_checker(), WorldPoiChecker)


def test_require_guarded_passes_a_real_checker_and_stops_the_null_one() -> None:
    require_guarded(Compiler(poi=grid_world().checker()))
    with pytest.raises(CompilerError):
        require_guarded(Compiler(poi=NullPoiChecker()))
