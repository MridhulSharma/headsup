"""Intersection lookup: a dispatcher's cross-street string to a graph node.

The Austin incident feed writes where an incident is the way a dispatcher
types it - `W 6TH ST / GUADALUPE ST`, `LAMAR BLVD & 5TH`, `RIVERSIDE DR AT
CONGRESS AVE` - and some rows carry no coordinates at all. Those rows are the
whole reason S3 exists, so something has to turn that text into a position.

This module is that something, and it is deliberately *not* a geocoder. It
knows one question: which intersection of this city's own road graph has both
of these street names on it? No gazetteer, no network call, no guessing at a
block number. The answer is a node of the graph the simulator drives on, or
nothing - and nothing is a usable answer, because a sentinel that cannot place
a report must not publish a hazard about it.

**Why matching needs normalising at all.** OSM writes `West 6th Street`; the
feed writes `W 6TH ST`. So both sides are reduced to a base name plus an
optional direction: `6th street` + `west`. Two names match when their bases
are equal and their directions agree or one of them is silent - the feed often
drops the direction, and `GUADALUPE ST` should still find `Guadalupe Street`.

**Why the base keeps its suffix.** Dropping `street` would make `6th` match
`6th Street` and `West 6th Street` alike, which is what we want, but it would
also let `Lamar` match both `Lamar Boulevard` and `Lamar Street` - two
different roads in Austin. The suffix is expanded rather than discarded, and a
feed string with no suffix at all matches any suffix, which is the one place
this is deliberately generous.

**An ambiguous answer is no answer.** If two nodes carry both street names -
a street that crosses the same avenue twice, a name OSM split oddly - the
lookup returns None rather than picking one. Guessing the wrong corner would
place a hazard a block from where it is, and a hazard in the wrong place is
worse than one that was never published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from headsup.world.graph import RoadGraph

#: What separates two street names in a dispatch location string. Checked as
#: whole words for the alphabetic ones, so `MANOR RD` does not split on `AND`
#: inside a name and `ATTERBURY` does not split on `AT`.
_SEPARATORS = re.compile(r"\s*(?:/|&|@|\+|\bAT\b|\bAND\b|\bNEAR\b)\s*", re.IGNORECASE)

#: Everything that is not a letter, a digit or a space. A dispatcher's string
#: carries commas, periods and the odd parenthesis.
_PUNCTUATION = re.compile(r"[^a-z0-9 ]+")

_SPACES = re.compile(r"\s+")

#: Street-type abbreviations, expanded to what OSM spells out. The values are
#: the OSM spellings, not a canonical form of our own: this table exists to
#: make the two sides of one comparison agree.
SUFFIXES: dict[str, str] = {
    "st": "street",
    "str": "street",
    "ave": "avenue",
    "av": "avenue",
    "blvd": "boulevard",
    "blv": "boulevard",
    "rd": "road",
    "dr": "drive",
    "ln": "lane",
    "ct": "court",
    "cir": "circle",
    "pl": "place",
    "ter": "terrace",
    "trl": "trail",
    "pkwy": "parkway",
    "pky": "parkway",
    "hwy": "highway",
    "expy": "expressway",
    "frontage": "frontage",
    "svrd": "service road",
    "cv": "cove",
    "bnd": "bend",
    "psge": "passage",
    "xing": "crossing",
}

#: Direction abbreviations. A direction is held apart from the base name
#: because the feed drops it far more often than OSM does.
DIRECTIONS: dict[str, str] = {
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
    "ne": "northeast",
    "nw": "northwest",
    "se": "southeast",
    "sw": "southwest",
    "north": "north",
    "south": "south",
    "east": "east",
    "west": "west",
    "northeast": "northeast",
    "northwest": "northwest",
    "southeast": "southeast",
    "southwest": "southwest",
}

#: Every street-type word, expanded form included, so a suffix is recognised
#: whichever side it came from.
_SUFFIX_WORDS = frozenset(SUFFIXES) | frozenset(SUFFIXES.values())

#: Words that carry no part of a street's identity: `BLOCK OF`, and the
#: direction-of-travel markers the feed appends to a highway name.
_NOISE_WORDS = frozenset({"block", "of", "the", "svc", "sb", "nb", "eb", "wb", "bound"})

#: A house number or a block range at the *start* of an address: `1500`,
#: `910-919`, `10900-11500`. Only ever stripped from the front, because a bare
#: number inside a name is part of the name - `N 1 Ave`, `Ih 35`, `FM 620` -
#: and a pure ordinal (`6th`) is a street name wherever it sits.
_BLOCK_NUMBER = re.compile(r"^\d+(?:-\d+)?$")

#: A parenthetical note, which is not part of the street name.
_PARENTHETICAL = re.compile(r"\([^)]*\)?")


@dataclass(frozen=True, slots=True)
class Street:
    """One street name reduced to what two spellings of it can agree on."""

    #: `6th street`, `guadalupe street`, `lamar boulevard`.
    base: str
    #: `west`, or empty when the name did not say.
    direction: str = ""
    #: True when the name carried no street-type word at all, which matches
    #: any suffix: the feed writes `GUADALUPE` for `Guadalupe Street`.
    bare: bool = False

    def matches(self, other: Street) -> bool:
        """Whether these two spellings can be the same street.

        Directions have to agree unless one side is silent. A bare name - one
        with no street type - matches any suffix on the same stem.
        """
        if self.direction and other.direction and self.direction != other.direction:
            return False
        if self.base == other.base:
            return True
        if self.bare and other.base.startswith(f"{self.base} "):
            return True
        return bool(other.bare and self.base.startswith(f"{other.base} "))


def _words(text: str) -> list[str]:
    """The identifying words of one street name, in order.

    Three things go before anything is compared, in this order, because each
    one changes what the next sees: a parenthetical note is not part of a
    name, a leading house number or block range is not part of a name, and
    `BLOCK OF` is the feed saying "somewhere along here" rather than naming a
    corner. What is left starts with the direction, if the name gave one.
    """
    cleaned = _PUNCTUATION.sub(" ", _PARENTHETICAL.sub(" ", text.lower()))
    words = [word for word in _SPACES.sub(" ", cleaned).strip().split(" ") if word]
    while words and _BLOCK_NUMBER.match(words[0]):
        words.pop(0)
    return [word for word in words if word not in _NOISE_WORDS]


def street(text: str) -> Street | None:
    """One street name as a `Street`, or None if nothing identifying is left.

    `"W 6TH ST"` -> base `6th street`, direction `west`.
    `"GUADALUPE"` -> base `guadalupe`, bare.
    `"910-919 W Cesar Chavez St"` -> base `cesar chavez street`, west.
    `"1100 BLOCK"` -> None: a block range names no street.
    """
    words = _words(text)
    direction = ""
    if words and words[0] in DIRECTIONS:
        direction = DIRECTIONS[words[0]]
        words = words[1:]
    kept = [SUFFIXES.get(word, word) for word in words]
    if not kept:
        return None
    base = " ".join(kept)
    return Street(base=base, direction=direction, bare=kept[-1] not in _SUFFIX_WORDS)


def cross_streets(text: str) -> tuple[Street, ...]:
    """The streets a dispatch location string names, in the order given.

    Two is the useful answer: an intersection. One means the feed named a
    stretch of road rather than a corner, and this module cannot place that.
    """
    parts = [street(part) for part in _SEPARATORS.split(text or "") if part.strip()]
    found: list[Street] = []
    for item in parts:
        if item is not None and item not in found:
            found.append(item)
    return tuple(found)


class IntersectionIndex:
    """Every junction of one road graph, by the street names on it.

    Built once per graph: the city graph is a few thousand edges, and the
    sentinel asks this question once per unlocated report.
    """

    def __init__(self, graph: RoadGraph) -> None:
        self.graph = graph
        self._by_node: dict[int, tuple[Street, ...]] = {}
        names: dict[int, set[Street]] = {}
        for edge in graph.edges:
            if not edge.name:
                continue
            parsed = street(edge.name)
            if parsed is None:
                continue
            names.setdefault(edge.u, set()).add(parsed)
            names.setdefault(edge.v, set()).add(parsed)
        self._by_node = {
            node: tuple(sorted(found, key=lambda item: (item.base, item.direction)))
            for node, found in names.items()
            # One name is a mid-block node, not an intersection anybody names.
            if len(found) >= 2
        }

    @property
    def junctions(self) -> int:
        """Nodes that carry at least two street names."""
        return len(self._by_node)

    def streets_at(self, node: int) -> tuple[Street, ...]:
        """The street names on one node, for a test or an error message."""
        return self._by_node.get(node, ())

    def node_for(self, text: str) -> int | None:
        """The one junction a dispatch location string names, or None.

        None for a string that names fewer than two streets, for a pair this
        graph has no junction for, and for a pair that matches more than one
        junction - an ambiguous corner is not a located one.
        """
        wanted = cross_streets(text)
        if len(wanted) < 2:
            return None
        first, second = wanted[0], wanted[1]
        hits = [
            node
            for node, present in self._by_node.items()
            if any(first.matches(item) for item in present)
            and any(second.matches(item) for item in present)
        ]
        return hits[0] if len(hits) == 1 else None

    def locate(self, text: str) -> tuple[float, float] | None:
        """Where the named junction is, as `(lon, lat)`, or None."""
        node = self.node_for(text)
        return None if node is None else self.graph.position_of(node)


@lru_cache(maxsize=4)
def index_for(graph: RoadGraph) -> IntersectionIndex:
    """The index for one graph, built once.

    Keyed on the graph object: `RoadGraph` is not hashable by value and a
    rebuild means a restart, so identity is the right key and four cities is
    more than this process will ever hold.
    """
    return IntersectionIndex(graph)
