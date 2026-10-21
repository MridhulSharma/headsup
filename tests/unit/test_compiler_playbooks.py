"""The playbooks on disk, and what the loader refuses.

The committed playbooks are policy, so these tests assert on them directly: a
commit that widens a cap or adds an action to a class has to change a test
too, which is the point.
"""

from __future__ import annotations

import pytest

from headsup.agents.compiler import (
    Playbook,
    PlaybookError,
    PlaybookSet,
    default_playbooks,
)
from headsup.agents.compiler.playbooks import clear_cache
from headsup.contracts import ActionType, HazardClass

MINIMAL = {
    "class": "FLOOD",
    "policy_rule": "flood.v1",
    "description": "A test playbook.",
    "allowed_actions": ["NO_ENTRY"],
    "max_area_km2": 10.0,
    "max_lifetime_minutes": 60,
    "two_key_area_km2": 5.0,
}


@pytest.fixture
def books() -> PlaybookSet:
    return default_playbooks()


# --- what ships in playbooks/ ----------------------------------------------


def test_every_hazard_class_has_a_playbook(books: PlaybookSet) -> None:
    """A class with no playbook can publish nothing, so none may be missing."""
    assert books.missing == ()
    assert len(books) == len(HazardClass)


def test_a_flood_may_close_a_crossing(books: PlaybookSet) -> None:
    flood = books.require(HazardClass.FLOOD)
    assert flood.allows(ActionType.NO_ENTRY)
    assert flood.rule_for(ActionType.NO_ENTRY) == "flood.no_entry.v1"


def test_a_signal_outage_may_not_close_an_intersection(books: PlaybookSet) -> None:
    """A dark signal becomes an all-way stop; closing it moves the problem."""
    outage = books.require(HazardClass.SIGNAL_OUTAGE)
    assert outage.allows(ActionType.ALL_WAY_STOP)
    assert not outage.allows(ActionType.NO_ENTRY)


def test_a_school_zone_may_not_close_a_street(books: PlaybookSet) -> None:
    school = books.require(HazardClass.SCHOOL_ZONE)
    assert school.allowed_actions == [ActionType.SPEED_CAP, ActionType.DENSITY_CAP]


def test_only_severe_weather_may_pause_service(books: PlaybookSet) -> None:
    """PAUSE_SERVICE is the most drastic action, so one class owns it."""
    allowed = {book.hazard_class for book in books if book.allows(ActionType.PAUSE_SERVICE)}
    assert allowed == {HazardClass.SEVERE_WEATHER}


def test_emergency_scenes_have_the_tightest_area_cap(books: PlaybookSet) -> None:
    caps = {book.hazard_class: book.max_area_km2 for book in books}
    assert caps[HazardClass.EMERGENCY_SCENE] == min(caps.values())
    # Severe weather really does cover a county, and pays for it with the
    # narrowest action list.
    assert caps[HazardClass.SEVERE_WEATHER] == max(caps.values())


def test_every_playbook_can_two_key_before_it_hits_its_area_cap(books: PlaybookSet) -> None:
    for book in books:
        assert book.two_key_area_km2 <= book.max_area_km2


def test_every_allowed_action_has_a_rule_id(books: PlaybookSet) -> None:
    """The advisory's policy_rule comes from here, never from the proposer."""
    for book in books:
        for action in book.allowed_actions:
            assert book.rule_for(action).startswith(book.hazard_class.value.lower())


# --- the loader --------------------------------------------------------------


def test_a_two_key_limit_above_the_area_cap_is_refused() -> None:
    """A two-key limit nothing can reach is a rule that never fires."""
    with pytest.raises(ValueError, match="could never fire"):
        Playbook.model_validate({**MINIMAL, "two_key_area_km2": 99.0})


def test_a_duplicate_action_is_refused() -> None:
    with pytest.raises(ValueError, match="same action twice"):
        Playbook.model_validate({**MINIMAL, "allowed_actions": ["NO_ENTRY", "NO_ENTRY"]})


def test_a_rule_for_an_action_the_class_forbids_is_refused() -> None:
    with pytest.raises(ValueError, match="does not allow"):
        Playbook.model_validate({**MINIMAL, "action_rules": {"PAUSE_SERVICE": "flood.pause.v1"}})


def test_an_unknown_key_is_refused() -> None:
    """Playbooks are policy: a typo must fail loudly, not be ignored."""
    with pytest.raises(ValueError):
        Playbook.model_validate({**MINIMAL, "max_area_km_2": 3.0})


def test_a_zero_cap_is_refused() -> None:
    with pytest.raises(ValueError):
        Playbook.model_validate({**MINIMAL, "max_area_km2": 0})


def test_a_directory_with_no_playbooks_is_an_error(tmp_path) -> None:
    with pytest.raises(PlaybookError, match=r"no .* playbooks"):
        PlaybookSet.load(tmp_path)


def test_a_missing_directory_is_an_error(tmp_path) -> None:
    with pytest.raises(PlaybookError, match="no playbook directory"):
        PlaybookSet.load(tmp_path / "nope")


def test_a_malformed_file_names_itself(tmp_path) -> None:
    (tmp_path / "flood.yaml").write_text("class: FLOOD\nmax_area_km2: wide\n", encoding="utf-8")
    with pytest.raises(PlaybookError, match=r"flood\.yaml"):
        PlaybookSet.load(tmp_path)


def test_two_files_for_one_class_are_refused(tmp_path) -> None:
    import yaml

    (tmp_path / "a.yaml").write_text(yaml.safe_dump(MINIMAL), encoding="utf-8")
    (tmp_path / "b.yaml").write_text(yaml.safe_dump(MINIMAL), encoding="utf-8")
    with pytest.raises(PlaybookError, match="already has a playbook"):
        PlaybookSet.load(tmp_path)


def test_the_playbook_directory_can_be_overridden(tmp_path, monkeypatch) -> None:
    """A scenario may ship its own caps; the override is explicit."""
    import yaml

    (tmp_path / "flood.yaml").write_text(yaml.safe_dump(MINIMAL), encoding="utf-8")
    monkeypatch.setenv("HU_PLAYBOOK_DIR", str(tmp_path))
    clear_cache()
    try:
        books = default_playbooks()
        assert books.classes == (HazardClass.FLOOD,)
        assert books.require(HazardClass.FLOOD).max_area_km2 == 10.0
    finally:
        clear_cache()


def test_an_unknown_class_is_asked_for_and_not_invented(books: PlaybookSet) -> None:
    empty = PlaybookSet({})
    assert empty.get(HazardClass.FLOOD) is None
    with pytest.raises(PlaybookError, match="no playbook for FLOOD"):
        empty.require(HazardClass.FLOOD)
