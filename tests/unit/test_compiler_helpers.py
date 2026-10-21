"""The parts the rules are built from: area, geometry soundness, POIs, approvals."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fixtures.compiler import CENTRE, NOW, box, hospital
from headsup.agents.compiler import (
    DEFAULT_TTL,
    Approval,
    InMemoryApprovals,
    NoApprovals,
    NullPoiChecker,
    Poi,
    PoiChecker,
    PoiKind,
    StubPoiChecker,
    area_km2,
    covers,
    new_token,
    sound,
)
from headsup.contracts import ActionType, Polygon
from samples import LINE, POINT, POLYGON

# --- area -------------------------------------------------------------------


def test_the_s1_crossing_is_about_five_hectares() -> None:
    assert area_km2(POLYGON) == pytest.approx(0.0484, abs=0.001)


def test_area_is_geodesic_not_square_degrees() -> None:
    """A degree of longitude is 97 km in Bexar County, not 111 km."""
    one_degree = Polygon(
        coordinates=[[[-99.0, 29.0], [-98.0, 29.0], [-98.0, 30.0], [-99.0, 30.0], [-99.0, 29.0]]]
    )
    assert area_km2(one_degree) == pytest.approx(10748, rel=0.01)


def test_the_same_box_measures_the_same_at_a_different_latitude() -> None:
    """A cap that drifted with latitude would not be a cap."""
    south = area_km2(box(2.0, centre=CENTRE))
    north = area_km2(box(2.0, centre=(-97.74, 45.0)))
    assert south == pytest.approx(north, rel=0.02)


def test_winding_order_does_not_change_the_area() -> None:
    ring = POLYGON.coordinates[0]
    reversed_ring = Polygon(coordinates=[list(reversed(ring))])
    assert area_km2(reversed_ring) == pytest.approx(area_km2(POLYGON))


@pytest.mark.parametrize("geometry", [POINT, LINE], ids=["point", "line"])
def test_points_and_lines_have_no_area(geometry) -> None:
    assert area_km2(geometry) == 0.0


def test_a_hole_is_subtracted() -> None:
    outer = box(4.0).coordinates[0]
    inner = box(1.0).coordinates[0]
    with_hole = Polygon(coordinates=[outer, list(reversed(inner))])
    assert area_km2(with_hole) == pytest.approx(3.0, rel=0.05)


# --- geometry soundness -----------------------------------------------------


def test_a_good_polygon_is_sound() -> None:
    assert sound(POLYGON) is None


def test_a_bow_tie_is_not_sound() -> None:
    bow_tie = Polygon.model_construct(
        type="Polygon",
        coordinates=[
            [[-98.50, 29.41], [-98.49, 29.42], [-98.50, 29.42], [-98.49, 29.41], [-98.50, 29.41]]
        ],
    )
    assert "invalid Polygon" in sound(bow_tie)


def test_a_polygon_outside_the_wgs84_bounds_is_not_sound() -> None:
    off_world = Polygon.model_construct(
        type="Polygon",
        coordinates=[
            [[-200.0, 29.0], [-199.0, 29.0], [-199.0, 30.0], [-200.0, 30.0], [-200.0, 29.0]]
        ],
    )
    assert "outside WGS84 bounds" in sound(off_world)


def test_covers_is_what_a_revision_is_measured_against() -> None:
    assert covers(box(4.0), box(1.0))
    assert not covers(box(1.0), box(4.0))
    assert covers(box(1.0), box(1.0))


# --- POI checkers -----------------------------------------------------------


def test_both_checkers_satisfy_the_protocol() -> None:
    assert isinstance(NullPoiChecker(), PoiChecker)
    assert isinstance(StubPoiChecker(), PoiChecker)


def test_the_null_checker_admits_it_knows_nothing() -> None:
    checker = NullPoiChecker()
    assert checker.blocked_access(POLYGON, ActionType.NO_ENTRY) == ()
    assert "unguarded" in checker.describes_itself()


def test_the_stub_reports_a_poi_whose_every_access_point_is_inside() -> None:
    checker = StubPoiChecker(pois=[hospital(inside=2)])
    blocked = checker.blocked_access(box(1.0), ActionType.NO_ENTRY)
    assert len(blocked) == 1
    assert blocked[0].access_blocked == blocked[0].access_total == 2
    assert "Riverside General" in str(blocked[0])


def test_the_stub_ignores_a_poi_with_a_way_out() -> None:
    checker = StubPoiChecker(pois=[hospital(inside=1, outside=1)])
    assert checker.blocked_access(box(1.0), ActionType.NO_ENTRY) == ()


@pytest.mark.parametrize(
    "action",
    [ActionType.SPEED_CAP, ActionType.DENSITY_CAP, ActionType.ALL_WAY_STOP, ActionType.KEEP_CLEAR],
)
def test_only_a_blocking_action_can_cut_a_poi_off(action: ActionType) -> None:
    checker = StubPoiChecker(pois=[hospital(inside=2)])
    assert checker.blocked_access(box(1.0), action) == ()


@pytest.mark.parametrize("kind", [PoiKind.HOSPITAL, PoiKind.FIRE_STATION, PoiKind.EMS_STATION])
def test_emergency_services_are_critical(kind: PoiKind) -> None:
    assert Poi(id="p", kind=kind, name="n").critical


def test_a_school_is_not_critical() -> None:
    assert not Poi(id="p", kind=PoiKind.SCHOOL, name="n").critical


def test_a_poi_with_no_known_access_points_is_not_reported() -> None:
    """Unknown is not the same as blocked; the real checker arrives in D2.1."""
    checker = StubPoiChecker(pois=[Poi(id="p", kind=PoiKind.HOSPITAL, name="Unmapped")])
    assert checker.blocked_access(box(1.0), ActionType.NO_ENTRY) == ()


# --- approvals --------------------------------------------------------------


def test_a_fresh_token_is_long_enough_not_to_be_guessed() -> None:
    assert len(new_token()) >= 16


def test_an_empty_store_approves_nothing() -> None:
    assert NoApprovals().approval_for("prop_test_1") is None


def test_a_granted_approval_is_found_and_usable() -> None:
    store = InMemoryApprovals()
    granted = store.grant("prop_test_1", "duty_officer_7", now=NOW)
    found = store.approval_for("prop_test_1")
    assert found == granted
    assert found.usable_at(NOW)
    assert found.usable_at(NOW + DEFAULT_TTL - timedelta(seconds=1))


def test_an_approval_stops_counting_after_its_ttl() -> None:
    store = InMemoryApprovals()
    store.grant("prop_test_1", "duty_officer_7", ttl=timedelta(minutes=5), now=NOW)
    approval = store.approval_for("prop_test_1")
    assert not approval.usable_at(NOW + timedelta(minutes=6))


def test_an_approval_does_not_count_before_it_was_given() -> None:
    store = InMemoryApprovals()
    store.grant("prop_test_1", "duty_officer_7", now=NOW)
    assert not store.approval_for("prop_test_1").usable_at(NOW - timedelta(minutes=1))


def test_an_approval_with_no_ttl_does_not_expire() -> None:
    store = InMemoryApprovals()
    store.grant("prop_test_1", "duty_officer_7", ttl=None, now=NOW)
    assert store.approval_for("prop_test_1").usable_at(NOW + timedelta(days=365))


def test_a_token_is_fingerprinted_not_echoed() -> None:
    approval = Approval(
        proposal_id="prop_test_1",
        token="abcdefghijklmnopqrstuvwx",
        approved_by="duty_officer_7",
        approved_at=NOW,
    )
    assert approval.fingerprint == "abcdef..."
    assert approval.token not in approval.fingerprint


def test_a_short_token_is_refused() -> None:
    with pytest.raises(ValueError):
        Approval(
            proposal_id="prop_test_1", token="short", approved_by="duty_officer_7", approved_at=NOW
        )


def test_an_expiry_before_the_approval_is_refused() -> None:
    with pytest.raises(ValueError, match="is not after"):
        Approval(
            proposal_id="prop_test_1",
            token=new_token(),
            approved_by="duty_officer_7",
            approved_at=NOW,
            expires_at=NOW - timedelta(minutes=1),
        )


def test_revoking_an_approval_removes_it() -> None:
    store = InMemoryApprovals()
    store.grant("prop_test_1", "duty_officer_7", now=NOW)
    assert store.revoke("prop_test_1")
    assert store.approval_for("prop_test_1") is None
    assert not store.revoke("prop_test_1")


def test_the_pending_list_holds_the_approvals_that_still_count() -> None:
    store = InMemoryApprovals()
    store.grant("prop_a", "duty_officer_7", now=NOW - timedelta(minutes=2))
    store.grant("prop_b", "duty_officer_7", now=NOW)
    store.grant(
        "prop_old", "duty_officer_7", ttl=timedelta(minutes=1), now=NOW - timedelta(hours=1)
    )
    pending = store.pending(NOW)
    assert [item.proposal_id for item in pending] == ["prop_a", "prop_b"]


def test_a_second_grant_replaces_the_first() -> None:
    store = InMemoryApprovals()
    store.grant("prop_test_1", "duty_officer_7", now=NOW)
    second = store.grant("prop_test_1", "duty_officer_9", now=NOW)
    assert len(store) == 1
    assert store.approval_for("prop_test_1") == second
