"""The query parameters `GET /advisories` and `GET /hazards` accept.

A bbox the server guessed at would answer a question nobody asked, so every
malformed one is a 400 with the reason in it. The filtering itself is pinned
here against geometry rather than against Redis; the stream read is covered by
the e2e run.
"""

from __future__ import annotations

import pytest

from headsup.api.store import QueryError, intersects, parse_bbox
from samples import POLYGON

#: A box around the S1 crossing the sample polygon sits on.
AROUND_CROSSING = "-98.50,29.41,-98.48,29.43"
#: Downtown Austin: nowhere near it.
ELSEWHERE = "-97.76,30.25,-97.72,30.29"


def test_a_bbox_is_four_degrees_west_south_east_north() -> None:
    assert parse_bbox(AROUND_CROSSING) == (-98.50, 29.41, -98.48, 29.43)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_an_absent_bbox_is_no_filter(raw: str | None) -> None:
    assert parse_bbox(raw) is None


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("-98.5,29.4,-98.4", "comma-separated"),
        ("-98.5,29.4,-98.4,29.5,30", "comma-separated"),
        ("west,south,east,north", "not four numbers"),
        ("-98.4,29.4,-98.5,29.5", "west<east"),
        ("-98.5,29.5,-98.4,29.4", "west<east"),
        ("-200,29.4,-98.4,29.5", "longitudes"),
        ("-98.5,-91,-98.4,29.5", "latitudes"),
    ],
)
def test_a_malformed_bbox_says_what_is_wrong(raw: str, why: str) -> None:
    with pytest.raises(QueryError, match=why):
        parse_bbox(raw)


def test_a_geometry_matches_a_box_it_reaches_into() -> None:
    assert intersects(POLYGON, parse_bbox(AROUND_CROSSING))
    assert not intersects(POLYGON, parse_bbox(ELSEWHERE))


def test_everything_matches_when_there_is_no_box() -> None:
    assert intersects(POLYGON, None)


def test_a_geometry_that_will_not_parse_matches_no_query() -> None:
    """A query answers about what it can read, and says nothing about the rest."""
    assert not intersects({"type": "Polygon", "coordinates": "nonsense"}, parse_bbox(ELSEWHERE))
