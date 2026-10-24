"""The two cities Heads Up ingests for, and which feeds cover each.

Gauge sites are the USGS monitoring locations that actually report gauge
height (parameter 00065) in each county; a site that only reports discharge
would make `hu ingest live` look broken for no reason.
"""

from __future__ import annotations

from dataclasses import dataclass

from headsup.ingest.austin_incidents import INCIDENT_DATASET
from headsup.ingest.austin_signals import SIGNAL_STATUS_DATASET

#: USGS parameter code for gauge height in feet.
GAUGE_HEIGHT = "00065"


@dataclass(frozen=True, slots=True)
class City:
    """One metro: its NWS alert area, its county, and its stream gauges."""

    key: str
    name: str
    #: NWS `area` query value, a state or marine code.
    nws_area: str
    county_fips: str
    county_name: str
    #: USGS monitoring location numbers, without the `USGS-` prefix.
    gauge_sites: tuple[str, ...]
    #: Socrata dataset id of this city's traffic signal status feed, if it
    #: publishes one. Austin does; San Antonio's signals are not on an open
    #: feed, so a city without one simply polls one feed fewer.
    signal_dataset: str | None = None
    #: Socrata dataset id of this city's traffic incident report feed, if it
    #: publishes one. Austin does; San Antonio's dispatch board is not open, so
    #: again a city without one simply polls one feed fewer.
    incident_dataset: str | None = None

    @property
    def gauge_ids(self) -> tuple[str, ...]:
        """Site numbers in the OGC API's `USGS-08178800` form."""
        return tuple(f"USGS-{site}" for site in self.gauge_sites)


SAN_ANTONIO = City(
    key="san_antonio",
    name="San Antonio",
    nws_area="TX",
    county_fips="48029",
    county_name="Bexar",
    # Salado Creek, north-east San Antonio: the S1 flood corridor, ordered
    # upstream to downstream so a crest is visible moving down the list.
    gauge_sites=(
        "08178585",  # Salado Ck at Wilderness Rd
        "08178593",  # Salado Ck at Blanco Rd
        "08178700",  # Salado Ck at Loop 410
        "08178740",  # Salado Ck at E Houston St
        "08178800",  # Salado Ck at Loop 13
    ),
)

AUSTIN = City(
    key="austin",
    name="Austin",
    nws_area="TX",
    county_fips="48453",
    county_name="Travis",
    gauge_sites=(
        "08154700",  # Bull Ck at Loop 360
        "08155300",  # Barton Ck at Loop 360
        "08155400",  # Barton Ck abv Barton Springs
        "08156800",  # Shoal Ck at W 12th St
        "08158000",  # Colorado Rv at Austin
        "08158030",  # Boggy Ck at Manor Rd
        "08159000",  # Onion Ck at US 183
    ),
    signal_dataset=SIGNAL_STATUS_DATASET,
    incident_dataset=INCIDENT_DATASET,
)

CITIES: dict[str, City] = {city.key: city for city in (AUSTIN, SAN_ANTONIO)}


def city(key: str) -> City:
    """Look up a city by key, with the available keys in the error."""
    try:
        return CITIES[key]
    except KeyError:
        known = ", ".join(sorted(CITIES))
        raise ValueError(f"unknown city {key!r}; known cities: {known}") from None
